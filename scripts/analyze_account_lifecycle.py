from __future__ import annotations

import argparse
import collections
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_ACCOUNTS = BASE_DIR / "data" / "accounts.json"
DEFAULT_DIAGNOSTICS = BASE_DIR / "data" / "register_post_verify_diagnostics.jsonl"


def _parse_time(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _load_accounts(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not path.exists():
        return items
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            items.append(item)
    return items


def _age_minutes(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round(max(0.0, (end - start).total_seconds() / 60.0), 2)


def _bucket_age(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 5:
        return "<5m"
    if value < 10:
        return "5-10m"
    if value < 20:
        return "10-20m"
    if value < 60:
        return "20-60m"
    return ">=60m"


def analyze(accounts_path: Path, diagnostics_path: Path) -> dict[str, Any]:
    accounts = _load_accounts(accounts_path)
    diagnostics = _load_jsonl(diagnostics_path)
    status_counts = collections.Counter(str(account.get("status") or "正常") for account in accounts)
    has_fp_count = sum(1 for account in accounts if isinstance(account.get("fp"), dict) and bool(account.get("fp")))
    has_account_proxy_count = sum(1 for account in accounts if str(account.get("proxy") or "").strip())
    new_account_cutoff = datetime(2026, 6, 29, 9, 0, tzinfo=timezone.utc)
    new_accounts = [
        account
        for account in accounts
        if (created_at := _parse_time(account.get("created_at"))) is not None
        and created_at >= new_account_cutoff
    ]
    new_has_fp_count = sum(1 for account in new_accounts if isinstance(account.get("fp"), dict) and bool(account.get("fp")))
    available = [
        account
        for account in accounts
        if str(account.get("status") or "正常") not in {"禁用", "异常", "限流"}
        and (bool(account.get("image_quota_unknown")) or int(account.get("quota") or 0) > 0)
    ]

    invalid_ages: list[float] = []
    invalid_age_buckets: collections.Counter[str] = collections.Counter()
    invalid_errors: collections.Counter[str] = collections.Counter()
    for account in accounts:
        status = str(account.get("status") or "正常")
        error = str(account.get("last_refresh_error") or account.get("last_quota_refresh_error") or "")
        lowered_error = error.lower()
        is_invalid_error = any(
            marker in lowered_error
            for marker in (
                "token invalidated",
                "invalid access token",
                "refresh_token_invalidated",
                "session has ended",
                "account_deactivated",
            )
        )
        if status not in {"异常", "禁用"} and not is_invalid_error:
            continue
        created_at = _parse_time(account.get("created_at"))
        failed_at = _parse_time(
            account.get("last_invalid_at")
            or account.get("last_refresh_error_at")
            or account.get("last_quota_refresh_at")
            or account.get("last_token_refresh_error_at")
        )
        age = _age_minutes(created_at, failed_at)
        if age is not None:
            invalid_ages.append(age)
        invalid_age_buckets[_bucket_age(age)] += 1
        if error:
            invalid_errors[error[:120]] += 1

    post_verify_counts = collections.Counter(str(item.get("verification_failed") or "unknown") for item in diagnostics)
    post_verify_errors: collections.Counter[str] = collections.Counter()
    post_verify_domains: collections.Counter[str] = collections.Counter()
    for item in diagnostics:
        account = item.get("account") if isinstance(item.get("account"), dict) else {}
        post_verify_domains[str(account.get("email_domain") or "unknown")] += 1
        summary = item.get("refresh_summary") if isinstance(item.get("refresh_summary"), dict) else {}
        errors = summary.get("errors") if isinstance(summary.get("errors"), list) else []
        for error in errors:
            text = str(error.get("error") if isinstance(error, dict) else error)
            if text:
                post_verify_errors[text[:120]] += 1

    return {
        "accounts_path": str(accounts_path),
        "diagnostics_path": str(diagnostics_path),
        "accounts_total": len(accounts),
        "status_counts": dict(status_counts),
        "has_fp_count": has_fp_count,
        "has_account_proxy_count": has_account_proxy_count,
        "new_accounts_since_2026_06_29_09_00_utc": len(new_accounts),
        "new_accounts_has_fp_count": new_has_fp_count,
        "available_count": len(available),
        "quota_total": sum(max(0, int(account.get("quota") or 0)) for account in available),
        "invalid_age_min": min(invalid_ages) if invalid_ages else None,
        "invalid_age_median": sorted(invalid_ages)[len(invalid_ages) // 2] if invalid_ages else None,
        "invalid_age_max": max(invalid_ages) if invalid_ages else None,
        "invalid_age_buckets": dict(invalid_age_buckets),
        "invalid_top_errors": invalid_errors.most_common(10),
        "post_register_verify_total": len(diagnostics),
        "post_register_verify_counts": dict(post_verify_counts),
        "post_register_verify_top_domains": post_verify_domains.most_common(10),
        "post_register_verify_top_errors": post_verify_errors.most_common(10),
        "decision_hint": _decision_hint(invalid_age_buckets, post_verify_counts),
    }


def _decision_hint(invalid_age_buckets: collections.Counter[str], post_verify_counts: collections.Counter[str]) -> str:
    immediate_invalid = int(post_verify_counts.get("invalid") or 0) + int(invalid_age_buckets.get("<5m") or 0)
    delayed_invalid = int(invalid_age_buckets.get("10-20m") or 0) + int(invalid_age_buckets.get("20-60m") or 0)
    transient = int(post_verify_counts.get("transient") or 0)
    if immediate_invalid:
        return "注册后立即验号失败占比存在：优先查注册/token/session 链路，不要把拿到 token 当成功。"
    if delayed_invalid:
        return "主要是注册后一段时间才 invalid：优先查 OpenAI 延迟风控、邮箱域、IP/代理指纹和批量行为。"
    if transient:
        return "验号失败以 transient 为主：优先查代理/WARP/Cloudflare/网络稳定性。"
    return "当前样本没有足够 invalid 证据；继续保留注册后验号诊断日志。"


def main() -> None:
    parser = argparse.ArgumentParser(description="分析账号注册/刷新生命周期，定位死号发生阶段。")
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    parser.add_argument("--diagnostics", type=Path, default=DEFAULT_DIAGNOSTICS)
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    result = analyze(args.accounts, args.diagnostics)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"accounts_total={result['accounts_total']} available_count={result['available_count']} quota_total={result['quota_total']}")
    print(f"status_counts={result['status_counts']}")
    print(
        "fp_counts="
        f"{result['has_fp_count']}/{result['accounts_total']} "
        f"new={result['new_accounts_has_fp_count']}/{result['new_accounts_since_2026_06_29_09_00_utc']}"
    )
    print(
        "invalid_age_min/median/max="
        f"{result['invalid_age_min']}/{result['invalid_age_median']}/{result['invalid_age_max']}"
    )
    print(f"invalid_age_buckets={result['invalid_age_buckets']}")
    print(f"post_register_verify_counts={result['post_register_verify_counts']}")
    print(f"decision_hint={result['decision_hint']}")


if __name__ == "__main__":
    main()
