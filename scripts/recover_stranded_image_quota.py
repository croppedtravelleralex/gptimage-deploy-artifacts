#!/usr/bin/env python3
"""A2-5：找回被 `status=限流` 单向门永久沉底的生图账号（默认 dry-run）。

背景（见 docs/28-scheduling-queue-slot-audit-20260726.md §A3）：
额度归零时 `mark_image_result` 曾把账号打成 `status=限流` 并落库，而
`_quota_window_due_for_lazy_refresh()` 第一条就要求 `status==正常`，
于是专门为救这类账号写的懒刷新逃生口被自己关死 —— 账号一生只需耗尽一次
即永久退池，重启也不恢复。A2-4 已修住新增，本脚本负责把**已经沉底的存量**捞回来。

修复动作与运行期完全同源：直接调用
`AccountService._heal_hard_quota_limited_status()`，
把 `限流` 翻译成 `image_soft_capped` flag（仍由 `restore_at` 决定何时放行），
因此脚本与调度器不可能出现判定漂移，且天然幂等（第二次跑找不到 `限流` 行）。

安全约定：
- **默认 dry-run**，写库必须显式 `--apply`。
- 必须显式指定 `--sqlite` 或 `--json`，没有隐式默认库，不会误碰生产 `data/accounts.db`。
- `--apply` 前先整库备份，并写出可逐行回滚的 `rollback.json`。
- 幂等：重复执行不产生二次写入。

用法：
    # 只看不改（推荐先跑这个，肉眼核对 before/after 表）
    py -3 scripts/recover_stranded_image_quota.py --sqlite /path/to/accounts.db

    # 核对无误后再写
    py -3 scripts/recover_stranded_image_quota.py --sqlite /path/to/accounts.db --apply

    # 回滚
    py -3 scripts/recover_stranded_image_quota.py --sqlite /path/to/accounts.db \
        --rollback data/backups/stranded-image-quota-<UTC>/rollback.json --apply
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# services.account_service 在模块尾部构造 account_service 单例，会打开
# config.get_storage_backend()。这里强制单例落到无副作用的 JSON backend
# （JSONStorageBackend.__init__ 不做任何读写），确保"import 本脚本"绝不
# 打开/初始化生产 accounts.db —— 本脚本只操作 --sqlite/--json 显式传入的库。
os.environ.setdefault("STORAGE_BACKEND", "json")

from services.account_service import AccountService  # noqa: E402
from services.storage.base import StorageBackend  # noqa: E402
from services.storage.database_storage import DatabaseStorageBackend  # noqa: E402
from services.storage.json_storage import JSONStorageBackend  # noqa: E402

STATUS_RATE_LIMITED = "限流"
STATUS_NORMAL = "正常"

# 本脚本唯一允许改动的字段；rollback.json 也只记录这些键。
REPAIR_KEYS = ("status", "image_soft_capped")

# 判定结论 → 是否属于"已沉底、应当修复"
STRANDED_REASONS = frozenset({"quota_positive", "window_expired", "no_restore_at"})


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _token_hash(token: object) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:16]


def _token_of(account: dict[str, Any]) -> str:
    return str(account.get("access_token") or account.get("accessToken") or "").strip()


def _quota_of(account: dict[str, Any]) -> int:
    try:
        return int(account.get("quota") or 0)
    except (TypeError, ValueError):
        return 0


def _restore_at_of(account: dict[str, Any]):
    return AccountService._parse_time(  # noqa: SLF001 - 复用运行期同一解析口径
        account.get("restore_at") or account.get("image_gen_window_reset_at")
    )


def _short(value: object, width: int) -> str:
    text = "-" if value is None or value == "" else str(value)
    if text == "True":
        text = "yes"
    elif text == "False":
        text = "no"
    return text if len(text) <= width else text[: width - 1] + "…"


# --------------------------------------------------------------------------- #
# 分类 / 计划
# --------------------------------------------------------------------------- #

def classify(account: dict[str, Any], *, now: datetime | None = None) -> str:
    """给单个账号定性；只有 STRANDED_REASONS 里的结论才会被修复。"""
    moment = now or datetime.now(timezone.utc)
    if str(account.get("status") or "") != STATUS_RATE_LIMITED:
        return "not_rate_limited"
    if AccountService._is_true_unlimited_image_account(account):  # noqa: SLF001
        # Pro/ProLite 不参与额度扣减，不属于 A2-4 的漂移链路（见报告"相邻发现"）。
        return "true_unlimited"
    if bool(account.get("image_quota_unknown")):
        # 额度未知 ≠ 已耗尽，无法确证可恢复，保持原状。
        return "quota_unknown"
    if _quota_of(account) > 0:
        # 账面还有额度却被 限流 卡住：image_quota_state() 报 "blocked"。
        return "quota_positive"
    restore_at = _restore_at_of(account)
    if restore_at is None:
        # 最坏情况：既没额度也没重置时间，懒刷新永远不会触发。
        return "no_restore_at"
    if restore_at.tzinfo is None:
        restore_at = restore_at.replace(tzinfo=timezone.utc)
    if restore_at <= moment:
        return "window_expired"
    # 额度窗口还没到：当下是**合法**耗尽，不是沉底。不动它，避免造成上游 429。
    return "window_pending"


def _snapshot(account: dict[str, Any]) -> dict[str, Any]:
    """给运维肉眼核对用的状态切片（全部走运行期同一批谓词）。"""
    restore_at = _restore_at_of(account)
    eligible_at = AccountService._lazy_refresh_eligible_at(account)  # noqa: SLF001
    return {
        "status": account.get("status"),
        "quota": _quota_of(account),
        "restore_at": restore_at.isoformat() if restore_at else None,
        "image_soft_capped": bool(account.get("image_soft_capped")),
        "lazy_eligible_at": eligible_at.isoformat() if eligible_at else None,
        "lazy_refresh_due": AccountService._quota_window_due_for_lazy_refresh(account),  # noqa: SLF001
        "image_available": AccountService._is_image_account_available(account),  # noqa: SLF001
    }


def plan_repair(account: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """算出单行的 before/after，不写任何东西。"""
    reason = classify(account, now=now)
    before = _snapshot(account)
    if reason not in STRANDED_REASONS:
        return {
            "token_hash": _token_hash(_token_of(account)),
            "email": account.get("email"),
            "reason": reason,
            "stranded": False,
            "repairable": False,
            "before": before,
            "after": before,
            "changed_keys": [],
        }

    # 修复动作 == 运行期 _normalize_account 里那一条，保证零漂移 + 幂等。
    healed = AccountService._heal_hard_quota_limited_status(dict(account))  # noqa: SLF001
    changed = [key for key in REPAIR_KEYS if healed.get(key) != account.get(key)]
    return {
        "token_hash": _token_hash(_token_of(account)),
        "email": account.get("email"),
        "reason": reason,
        "stranded": True,
        # heal 在 auto_remove_rate_limited_accounts=True 时按设计不动手
        # （该模式下账号被删除而非回收）→ 这里会是 False，并在下方告警。
        "repairable": bool(changed),
        "before": before,
        "after": _snapshot(healed),
        "changed_keys": changed,
        "_healed": healed,
    }


# --------------------------------------------------------------------------- #
# 存储
# --------------------------------------------------------------------------- #

def load_accounts(*, sqlite_path: Path | None, json_path: Path | None) -> list[dict[str, Any]]:
    """**只读**加载账号。

    dry-run 全程只走这里：SQLite 用 `mode=ro` URI（与
    scripts/repair_panda_account_identity.py 同一约定），绝不构造写后端。
    否则 SQLAlchemy 的 create_all + `PRAGMA journal_mode=WAL` 会改写库头，
    "dry-run 零写入"就不再成立。
    """
    if sqlite_path is not None:
        if not sqlite_path.exists():
            raise SystemExit(f"sqlite not found: {sqlite_path}")
        conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
        try:
            rows = conn.execute("select data from accounts").fetchall()
        finally:
            conn.close()
        out: list[dict[str, Any]] = []
        for (raw,) in rows:
            try:
                item = json.loads(raw or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                out.append(item)
        return out
    assert json_path is not None
    if not json_path.exists():
        raise SystemExit(f"json store not found: {json_path}")
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read json store {json_path}: {exc}") from exc
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def open_writer(*, sqlite_path: Path | None, json_path: Path | None) -> StorageBackend:
    """只在真正要写（--apply）时才构造写后端，走服务层 storage 接口。"""
    if sqlite_path is not None:
        return DatabaseStorageBackend(f"sqlite:///{sqlite_path}")
    assert json_path is not None
    return JSONStorageBackend(json_path)


def backup_store(
    *,
    sqlite_path: Path | None,
    json_path: Path | None,
    out_dir: Path,
) -> list[str]:
    """写库前整库备份。SQLite 连 -wal/-shm 一起拷，避免只拷到半个快照。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    source = sqlite_path or json_path
    assert source is not None
    candidates = [source]
    if sqlite_path is not None:
        candidates += [
            sqlite_path.with_name(sqlite_path.name + "-wal"),
            sqlite_path.with_name(sqlite_path.name + "-shm"),
        ]
    for path in candidates:
        if not path.exists():
            continue
        target = out_dir / path.name
        shutil.copy2(path, target)
        copied.append(str(target))
    return copied


def write_repairs(backend: StorageBackend, healed: list[dict[str, Any]]) -> int:
    """行级 upsert（服务层 storage 接口，非裸 SQL）。"""
    if not healed:
        return 0
    upsert = getattr(backend, "upsert_accounts", None)
    if callable(upsert):
        upsert(healed)
        return len(healed)
    # JSON backend 只有全量 save 语义：读回整表，逐 token 覆盖后回写。
    by_token = {_token_of(item): dict(item) for item in backend.load_accounts()}
    for item in healed:
        by_token[_token_of(item)] = dict(item)
    backend.save_accounts(list(by_token.values()))
    return len(healed)


def rollback_targets(
    accounts: list[dict[str, Any]], rollback: dict[str, Any]
) -> list[dict[str, Any]]:
    """按 rollback.json 把 REPAIR_KEYS 还原回原值（其余字段原样保留）。"""
    by_hash = {_token_hash(_token_of(a)): dict(a) for a in accounts}
    restored: list[dict[str, Any]] = []
    for entry in rollback.get("entries") or []:
        account = by_hash.get(str(entry.get("token_hash") or ""))
        if account is None:
            continue
        for key, value in (entry.get("before") or {}).items():
            if key not in REPAIR_KEYS:
                continue
            if value is None:
                account.pop(key, None)
            else:
                account[key] = value
        restored.append(account)
    return restored


# --------------------------------------------------------------------------- #
# 报表
# --------------------------------------------------------------------------- #

_COLUMNS = (
    ("token", 16),
    ("email", 30),
    ("reason", 15),
    ("status", 8),
    ("quota", 6),
    ("soft_cap", 8),
    ("restore_at", 20),
    ("lazy_due", 8),
    ("available", 9),
)


def _row_cells(kind: str, plan: dict[str, Any]) -> list[str]:
    side = plan[kind]
    return [
        plan["token_hash"] if kind == "before" else "",
        (plan.get("email") or "-") if kind == "before" else "",
        plan["reason"] if kind == "before" else "",
        str(side.get("status") or "-"),
        str(side.get("quota")),
        "yes" if side.get("image_soft_capped") else "no",
        (side.get("restore_at") or "-")[:20],
        "yes" if side.get("lazy_refresh_due") else "no",
        "yes" if side.get("image_available") else "no",
    ]


def print_table(plans: list[dict[str, Any]]) -> None:
    header = "  ".join(name.ljust(width) for name, width in _COLUMNS)
    print(header)
    print("-" * len(header))
    for plan in plans:
        for kind, marker in (("before", "before"), ("after", "after ")):
            cells = _row_cells(kind, plan)
            line = "  ".join(
                _short(cell, width).ljust(width)
                for cell, (_name, width) in zip(cells, _COLUMNS)
            )
            print(f"{line}  <{marker}>")
        if plan["changed_keys"]:
            print(f"{'':>18}changed: {', '.join(plan['changed_keys'])}")
        print("-" * len(header))


def summarize(plans: list[dict[str, Any]]) -> dict[str, Any]:
    reasons: dict[str, int] = {}
    for plan in plans:
        reasons[plan["reason"]] = reasons.get(plan["reason"], 0) + 1
    return {
        "scanned": len(plans),
        "stranded": sum(1 for p in plans if p["stranded"]),
        "repairable": sum(1 for p in plans if p["repairable"]),
        "reasons": reasons,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------------------------------------------------------- #
# 回滚
# --------------------------------------------------------------------------- #

def build_rollback(plans: list[dict[str, Any]], accounts_by_token: dict[str, dict]) -> dict[str, Any]:
    entries = []
    for plan in plans:
        if not plan["repairable"]:
            continue
        token = next(
            (t for t in accounts_by_token if _token_hash(t) == plan["token_hash"]),
            "",
        )
        if not token:
            continue
        original = accounts_by_token[token]
        entries.append(
            {
                "token_hash": plan["token_hash"],
                "email": original.get("email"),
                "before": {key: original.get(key) for key in REPAIR_KEYS},
            }
        )
    return {"kind": "stranded_image_quota_rollback", "generated_at": _utc_stamp(), "entries": entries}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--sqlite", type=Path, help="accounts.db 路径（必须显式给出）")
    source.add_argument("--json", dest="json_store", type=Path, help="accounts.json 路径")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库；不给此参数时**只读**，什么都不改",
    )
    parser.add_argument("--backup-dir", type=Path, default=None, help="备份目录（默认 <库同级>/backups/...）")
    parser.add_argument("--json-out", type=Path, default=None, help="把 plans/summary 另存为 JSON")
    parser.add_argument("--rollback", type=Path, default=None, help="按 rollback.json 还原（同样需要 --apply）")
    parser.add_argument("--quiet", action="store_true", help="不打印 before/after 表，只输出 summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = args.sqlite or args.json_store
    assert source is not None
    # 只读加载；写后端只在 --apply 分支里才构造。
    accounts = load_accounts(sqlite_path=args.sqlite, json_path=args.json_store)

    if args.rollback is not None:
        rollback = json.loads(args.rollback.read_text(encoding="utf-8"))
        targets = rollback_targets(accounts, rollback)
        if not args.apply:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "dry_run": True,
                        "mode": "rollback",
                        "entries": len(rollback.get("entries") or []),
                        "would_restore": len(targets),
                        "hint": "add --apply to restore",
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        writer = open_writer(sqlite_path=args.sqlite, json_path=args.json_store)
        restored = write_repairs(writer, targets)
        print(json.dumps({"ok": True, "mode": "rollback", "restored": restored}, ensure_ascii=False))
        return 0

    accounts_by_token = {_token_of(a): dict(a) for a in accounts if _token_of(a)}
    now = datetime.now(timezone.utc)
    plans = [plan_repair(account, now=now) for account in accounts]
    interesting = [p for p in plans if p["reason"] != "not_rate_limited"]
    summary = summarize(plans)

    if not args.quiet and interesting:
        print(f"# stranded image-quota recovery — source={source}")
        print(f"# mode={'APPLY' if args.apply else 'DRY-RUN (no writes)'}")
        print()
        print_table(interesting)

    blocked = [p for p in plans if p["stranded"] and not p["repairable"]]
    if blocked:
        print(
            f"! {len(blocked)} stranded row(s) not repairable — "
            "auto_remove_rate_limited_accounts is enabled, so the runtime deletes "
            "these accounts instead of recycling them. Disable that config first "
            "if you want them recovered."
        )

    repairable = [p for p in plans if p["repairable"]]
    result: dict[str, Any] = {
        "ok": True,
        "mode": "apply" if args.apply else "dry_run",
        "source": str(source),
        "summary": summary,
    }

    if not args.apply:
        result["dry_run"] = True
        result["would_repair"] = len(repairable)
        result["hint"] = "re-run with --apply to write (a full backup is taken first)"
    elif not repairable:
        result["repaired"] = 0
        result["note"] = "nothing stranded — already idempotent no-op"
    else:
        out_dir = args.backup_dir or (source.parent / "backups" / f"stranded-image-quota-{_utc_stamp()}")
        out_dir.mkdir(parents=True, exist_ok=True)
        copied = backup_store(sqlite_path=args.sqlite, json_path=args.json_store, out_dir=out_dir)
        rollback = build_rollback(repairable, accounts_by_token)
        rollback_path = out_dir / "rollback.json"
        rollback_path.write_text(
            json.dumps(rollback, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        writer = open_writer(sqlite_path=args.sqlite, json_path=args.json_store)
        repaired = write_repairs(writer, [p["_healed"] for p in repairable])
        result["repaired"] = repaired
        result["backup"] = copied
        result["rollback"] = str(rollback_path)

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "summary": summary,
            "plans": [{k: v for k, v in p.items() if k != "_healed"} for p in plans],
            "result": {k: v for k, v in result.items() if k != "summary"},
        }
        args.json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        result["json_out"] = str(args.json_out)

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
