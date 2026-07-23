#!/usr/bin/env python3
"""账号身份证据 audit / canary / apply（本地或脱敏快照）。

模式：
  audit   只生成 inventory / 分类 / 签名对比
  canary  只处理一个 token hash（需 --token-hash 与可选 --expected-row-hash）
  apply   按已审核 planned manifest 逐条 update_account_identity

默认不连 Panda；对本地 accounts 存储或 --snapshot JSON 操作。
密码/token/Cookie 不写入报告，只保留 hash 前缀。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.account_fingerprint import ensure_complete_fp, normalize_fp
from services.account_identity import (
    missing_panda_identity_fields,
    normalize_account_identity,
    proxy_binding_hash,
)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _sha(value: object, n: int = 16) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:n]


def _token_hash(token: object) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:16]


def _email_hash(email: object) -> str:
    return hashlib.sha256(str(email or "").strip().lower().encode("utf-8")).hexdigest()[:12]


def _legacy_signature(account: dict[str, Any]) -> str:
    provider = str(account.get("proxy_provider") or "").strip().lower()
    scope = str(account.get("proxy_scope") or "").strip().lower()
    return _sha({"provider": provider, "scope": scope}, 12)


def _row_hash(account: dict[str, Any]) -> str:
    safe = {
        k: account.get(k)
        for k in sorted(account.keys())
        if k
        not in {
            "access_token",
            "accessToken",
            "refresh_token",
            "password",
            "proxy",
        }
    }
    safe["proxy_binding_hash"] = proxy_binding_hash(account.get("proxy"))
    safe["token_hash"] = _token_hash(account.get("access_token") or account.get("accessToken"))
    return _sha(safe, 24)


def _classify(account: dict[str, Any]) -> str:
    status = str(account.get("status") or "")
    receive = str(account.get("panda_receive_state") or "").strip().lower()
    recovery = str(account.get("outlook_recovery_state") or "").strip().lower()
    if status == "禁用" or recovery == "terminal" or receive == "rejected":
        if recovery == "terminal" or status == "禁用":
            return "E"
        if receive == "rejected" and int(account.get("invalid_count") or 0) > 0:
            return "E"
    proxy = str(account.get("proxy") or "").strip()
    has_reg = bool(str(account.get("registration_proxy_hash") or "").strip())
    has_egress = bool(str(account.get("proxy_egress_hash") or "").strip())
    fp = normalize_fp(account.get("fp"))
    has_fp = len(fp) >= 8
    if proxy and has_reg and has_egress and has_fp:
        return "A"
    if proxy and (has_egress or has_fp or has_reg):
        return "B"
    if not proxy and (has_reg or account.get("registration_proxy_endpoint")):
        return "C"
    if int(account.get("success") or 0) + int(account.get("fail") or 0) > 0 and (not proxy or not has_fp):
        return "D"
    if not proxy:
        return "C"
    return "B"


def _inventory_row(account: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_account_identity(dict(account))
    fp = normalize_fp(normalized.get("fp"))
    proxy = str(normalized.get("proxy") or "").strip()
    grade = _classify(normalized)
    return {
        "token_hash": _token_hash(normalized.get("access_token")),
        "email_hash": _email_hash(normalized.get("email")),
        "status": normalized.get("status"),
        "panda_receive_state": normalized.get("panda_receive_state"),
        "outlook_recovery_state": normalized.get("outlook_recovery_state"),
        "grade": grade,
        "proxy_present": bool(proxy),
        "proxy_provider": normalized.get("proxy_provider"),
        "proxy_scope": normalized.get("proxy_scope"),
        "legacy_signature": _legacy_signature(normalized),
        "node_signature_v2": proxy_binding_hash(proxy) if proxy else "",
        "registration_proxy_hash": normalized.get("registration_proxy_hash"),
        "proxy_egress_hash": normalized.get("proxy_egress_hash"),
        "fp_present": bool(fp),
        "fp_hash": _sha(fp, 16) if fp else "",
        "fp_origin": normalized.get("fp_origin"),
        "missing_panda_fields": missing_panda_identity_fields(normalized),
        "row_hash": _row_hash(normalized),
        "success": int(normalized.get("success") or 0),
        "fail": int(normalized.get("fail") or 0),
        "invalid_count": int(normalized.get("invalid_count") or 0),
        "lifecycle_ip_mode": normalized.get("lifecycle_ip_mode"),
        "registration_proxy_scope": normalized.get("registration_proxy_scope"),
    }


def load_accounts_from_sqlite(path: Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = con.execute("select data from accounts").fetchall()
    out: list[dict[str, Any]] = []
    for (raw,) in rows:
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def load_accounts_from_snapshot(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict) and isinstance(data.get("accounts"), list):
        return [x for x in data["accounts"] if isinstance(x, dict)]
    raise ValueError("snapshot must be list or {accounts:[...]}")


def load_accounts_from_local_service() -> list[dict[str, Any]]:
    from services.account_service import account_service

    return account_service.list_accounts()


def plan_repair(account: dict[str, Any]) -> dict[str, Any]:
    before = dict(account)
    planned = normalize_account_identity(dict(account))
    fp, filled = ensure_complete_fp(planned)
    planned["fp"] = fp
    if filled and not str(planned.get("fp_origin") or "").strip():
        planned["fp_origin"] = "repair_generated"
        planned["fp_persisted_at"] = datetime.now(timezone.utc).isoformat()
    if planned.get("proxy") and not planned.get("proxy_provider"):
        planned["proxy_provider"] = "webshare"
    grade = _classify(planned)
    changed = sorted(
        key
        for key in set(planned) | set(before)
        if planned.get(key) != before.get(key)
        and key not in {"access_token", "accessToken"}
    )
    return {
        "token_hash": _token_hash(before.get("access_token")),
        "grade": grade,
        "changed_keys": changed,
        "before_row_hash": _row_hash(before),
        "planned_row_hash": _row_hash(planned),
        "missing_after": missing_panda_identity_fields(planned),
        "planned_identity": {
            k: planned.get(k)
            for k in (
                "proxy_provider",
                "proxy_scope",
                "proxy_binding_hash",
                "proxy_egress_hash",
                "registration_proxy_hash",
                "lifecycle_ip_mode",
                "fp_origin",
            )
        },
        # 不输出完整 proxy / fp 正文
        "fp_hash": _sha(normalize_fp(planned.get("fp")), 16),
    }


def run_audit(accounts: list[dict[str, Any]], out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    inventory = [_inventory_row(a) for a in accounts]
    grades = Counter(row["grade"] for row in inventory)
    legacy = Counter(row["legacy_signature"] for row in inventory if row["legacy_signature"])
    v2 = Counter(row["node_signature_v2"] for row in inventory if row["node_signature_v2"])
    signature = {
        "legacy_groups": dict(legacy.most_common()),
        "v2_groups": dict(v2.most_common()),
        "legacy_unique": len(legacy),
        "v2_unique": len(v2),
        "hypothesis": (
            "H1_true_shared_endpoint"
            if v2 and max(v2.values()) >= 2
            else "H2_or_sparse"
        ),
    }
    planned = [plan_repair(a) for a in accounts]
    summary = {
        "total": len(inventory),
        "grades": dict(grades),
        "grade_sum": sum(grades.values()),
        "closed": sum(grades.values()) == len(inventory),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (out_dir / "inventory.json").write_text(
        json.dumps({"summary": summary, "rows": inventory}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "signature-comparison.json").write_text(
        json.dumps(signature, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "planned.json").write_text(
        json.dumps(planned, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    lines = [
        "# repair-classification",
        "",
        f"- total: {summary['total']}",
        f"- grades: {summary['grades']}",
        f"- closed: {summary['closed']}",
        f"- v2_unique: {signature['v2_unique']}",
        f"- hypothesis: {signature['hypothesis']}",
        "",
    ]
    (out_dir / "repair-classification.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def run_canary(
    accounts: list[dict[str, Any]],
    *,
    token_hash: str,
    expected_row_hash: str,
    out_dir: Path,
    apply: bool,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = None
    for account in accounts:
        if _token_hash(account.get("access_token")) == token_hash:
            target = account
            break
    if target is None:
        raise SystemExit(f"token hash not found: {token_hash}")
    before_hash = _row_hash(target)
    if expected_row_hash and expected_row_hash != before_hash:
        raise SystemExit(
            f"expected_row_hash mismatch: got={before_hash} expected={expected_row_hash}"
        )
    plan = plan_repair(target)
    (out_dir / "before.json").write_text(
        json.dumps({"token_hash": token_hash, "row_hash": before_hash, "grade": _classify(target)}, indent=2),
        encoding="utf-8",
    )
    (out_dir / "planned.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    result = {"applied": False, "plan": plan}
    if apply:
        from services.account_service import account_service
        from services.proxy_health import measure_proxy_egress_ip

        token = str(target.get("access_token") or "")
        updates = {
            "proxy_provider": plan["planned_identity"].get("proxy_provider") or target.get("proxy_provider"),
            "proxy_scope": plan["planned_identity"].get("proxy_scope") or target.get("proxy_scope"),
            "lifecycle_ip_mode": plan["planned_identity"].get("lifecycle_ip_mode")
            or target.get("lifecycle_ip_mode")
            or "sticky_one_ip_full",
            "fp_origin": plan["planned_identity"].get("fp_origin") or "repair_generated",
        }
        # 仅补缺失派生字段；不在 canary 中改绑 proxy 明文
        proxy_url = str(target.get("proxy") or "").strip()
        egress_probe: dict[str, Any] = {"samples": []}
        if not target.get("proxy_egress_hash") and proxy_url:
            hashes: list[str] = []
            ips: list[str] = []
            locs: list[str] = []
            for _ in range(3):
                sample = measure_proxy_egress_ip(proxy_url, timeout=20.0)
                egress_probe["samples"].append(
                    {
                        "ok": sample.get("ok"),
                        "egress_hash": sample.get("egress_hash"),
                        "loc": sample.get("loc"),
                        "elapsed_sec": sample.get("elapsed_sec"),
                        "error": sample.get("error"),
                    }
                )
                if sample.get("ok") and sample.get("egress_hash"):
                    hashes.append(str(sample["egress_hash"]))
                    ips.append(str(sample.get("ip") or ""))
                    locs.append(str(sample.get("loc") or ""))
            unique = sorted(set(hashes))
            egress_probe["unique_hashes"] = unique
            egress_probe["stable"] = len(unique) == 1
            if len(unique) == 1:
                updates["proxy_egress_hash"] = unique[0]
                if ips and ips[0]:
                    updates["proxy_egress_ip"] = ips[0]
            else:
                egress_probe["error"] = "egress_unstable_or_failed"
        if not target.get("registration_proxy_hash") and proxy_url:
            updates["registration_proxy_hash"] = proxy_binding_hash(proxy_url)
        (out_dir / "egress-probe.json").write_text(
            json.dumps(egress_probe, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if egress_probe.get("error"):
            raise SystemExit(f"canary egress probe failed: {egress_probe['error']}")
        normalized = normalize_account_identity({**target, **updates})
        updated = account_service.update_account_identity(
            token,
            {
                "proxy": normalized.get("proxy"),
                "proxy_provider": normalized.get("proxy_provider"),
                "proxy_scope": normalized.get("proxy_scope"),
                "proxy_egress_hash": normalized.get("proxy_egress_hash") or updates.get("proxy_egress_hash"),
                "proxy_egress_ip": updates.get("proxy_egress_ip") or normalized.get("proxy_egress_ip"),
                "registration_proxy_hash": normalized.get("registration_proxy_hash"),
                "lifecycle_ip_mode": normalized.get("lifecycle_ip_mode"),
                "fp": normalized.get("fp"),
                "fp_origin": normalized.get("fp_origin"),
            },
            reason="identity_canary_repair",
        )
        after = updated or account_service.get_account(token) or {}
        after_doc = {
            "token_hash": token_hash,
            "row_hash": _row_hash(after),
            "grade": _classify(after),
            "missing": missing_panda_identity_fields(after),
        }
        (out_dir / "after.json").write_text(json.dumps(after_doc, indent=2), encoding="utf-8")
        rollback = {
            "token_hash": token_hash,
            "restore_keys": [
                "proxy",
                "proxy_provider",
                "proxy_scope",
                "proxy_egress_hash",
                "registration_proxy_hash",
                "lifecycle_ip_mode",
                "fp",
                "fp_origin",
            ],
            "before_row_hash": before_hash,
        }
        (out_dir / "rollback.json").write_text(json.dumps(rollback, indent=2), encoding="utf-8")
        result["applied"] = True
        result["after"] = after_doc
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("audit", "canary", "apply"))
    parser.add_argument("--snapshot", type=Path, help="脱敏账号 JSON 快照")
    parser.add_argument("--sqlite", type=Path, help="只读 accounts.db")
    parser.add_argument("--local-service", action="store_true", help="读取本地 AccountService")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--token-hash", default="")
    parser.add_argument("--expected-row-hash", default="")
    parser.add_argument("--apply", action="store_true", help="canary/apply 时真正写库")
    parser.add_argument("--planned", type=Path, help="apply 模式已审核 planned.json")
    args = parser.parse_args()

    if args.snapshot:
        accounts = load_accounts_from_snapshot(args.snapshot)
    elif args.sqlite:
        accounts = load_accounts_from_sqlite(args.sqlite)
    elif args.local_service:
        accounts = load_accounts_from_local_service()
    else:
        parser.error("require --snapshot or --sqlite or --local-service")

    out_dir = args.out or (ROOT / "data" / "runlogs" / f"account-identity-remediation-{_utc()}")
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "audit":
        summary = run_audit(accounts, out_dir)
        print(json.dumps({"ok": True, "out": str(out_dir), "summary": summary}, ensure_ascii=False))
        return 0

    if args.mode == "canary":
        if not args.token_hash:
            parser.error("canary requires --token-hash")
        result = run_canary(
            accounts,
            token_hash=args.token_hash,
            expected_row_hash=args.expected_row_hash,
            out_dir=out_dir,
            apply=bool(args.apply),
        )
        print(json.dumps({"ok": True, "out": str(out_dir), "result": result}, ensure_ascii=False))
        return 0

    # apply: 仅应用 planned 中 grade B/C 且 missing 可本地补齐的项（仍需 --apply）
    planned_path = args.planned or (out_dir / "planned.json")
    if not planned_path.exists():
        run_audit(accounts, out_dir)
        planned_path = out_dir / "planned.json"
    planned_rows = json.loads(planned_path.read_text(encoding="utf-8"))
    if not args.apply:
        print(json.dumps({"ok": True, "dry_run": True, "planned": len(planned_rows), "out": str(out_dir)}))
        return 0
    from services.account_service import account_service

    applied = 0
    skipped = 0
    for plan in planned_rows:
        if plan.get("grade") not in {"B", "C"}:
            skipped += 1
            continue
        th = plan.get("token_hash")
        target = next((a for a in accounts if _token_hash(a.get("access_token")) == th), None)
        if not target:
            skipped += 1
            continue
        run_canary(
            [target],
            token_hash=th,
            expected_row_hash=str(plan.get("before_row_hash") or ""),
            out_dir=out_dir / f"apply-{th}",
            apply=True,
        )
        applied += 1
    print(json.dumps({"ok": True, "applied": applied, "skipped": skipped, "out": str(out_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
