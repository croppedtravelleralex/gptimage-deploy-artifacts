from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

from fastapi import HTTPException, Request

from services.account_service import account_service
from services.auth_service import auth_service
from services.config import config

BASE_DIR = Path(__file__).resolve().parents[1]
WEB_DIST_DIR = BASE_DIR / "web_dist"


def extract_bearer_token(authorization: str | None) -> str:
    scheme, _, value = str(authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return ""
    return value.strip()


def _legacy_admin_identity(token: str) -> dict[str, object] | None:
    auth_key = str(config.auth_key or "").strip()
    if auth_key and token == auth_key:
        return {"id": "admin", "name": "管理员", "role": "admin"}
    return None


def require_identity(authorization: str | None) -> dict[str, object]:
    token = extract_bearer_token(authorization)
    identity = _legacy_admin_identity(token) or auth_service.authenticate(token)
    if identity is None:
        raise HTTPException(status_code=401, detail={"error": "密钥无效或已失效，请重新登录"})
    return identity


def require_auth_key(authorization: str | None) -> None:
    require_identity(authorization)


def require_admin(authorization: str | None) -> dict[str, object]:
    identity = require_identity(authorization)
    if identity.get("role") != "admin":
        raise HTTPException(status_code=403, detail={"error": "需要管理员权限才能执行这个操作"})
    return identity


def resolve_image_base_url(request: Request) -> str:
    return config.base_url or f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"


def raise_image_quota_error(exc: Exception) -> None:
    message = str(exc)
    if "no available image quota" in message.lower():
        detail: dict = {"error": "no available image quota"}
        breakdown = getattr(exc, "breakdown", None)
        if isinstance(breakdown, dict):
            detail["schedulable_breakdown"] = {
                "buckets": breakdown.get("buckets"),
                "primary_reason_counts": breakdown.get("primary_reason_counts"),
                "runtime": breakdown.get("runtime"),
            }
        raise HTTPException(status_code=429, detail=detail) from exc
    raise HTTPException(status_code=502, detail={"error": message}) from exc


def sanitize_cpa_pool(pool: dict | None) -> dict | None:
    if not isinstance(pool, dict):
        return None
    return {key: value for key, value in pool.items() if key != "secret_key"}


def sanitize_cpa_pools(pools: list[dict]) -> list[dict]:
    return [sanitized for pool in pools if (sanitized := sanitize_cpa_pool(pool)) is not None]


def sanitize_sub2api_server(server: dict | None) -> dict | None:
    if not isinstance(server, dict):
        return None
    sanitized = {key: value for key, value in server.items() if key not in {"password", "api_key"}}
    sanitized["has_api_key"] = bool(str(server.get("api_key") or "").strip())
    return sanitized


def sanitize_sub2api_servers(servers: list[dict]) -> list[dict]:
    return [sanitized for server in servers if (sanitized := sanitize_sub2api_server(server)) is not None]


def start_limited_account_watcher(stop_event: Event) -> Thread:
    interval_seconds = config.refresh_account_interval_minute * 60

    def _max_tokens_per_cycle() -> int:
        try:
            settings = config.get_account_maintenance_loop_settings()
            return max(20, min(200, int(settings.get("batch_limit") or 80)))
        except Exception:
            return 80

    def _refresh_and_sync_panda(tokens: list[str]) -> None:
        if not tokens:
            return
        account_service.refresh_accounts(tokens)
        try:
            from services.account_refresh_all_service import account_refresh_all_service

            summary = account_refresh_all_service.sync_last_refreshed_accounts_to_panda()
            if any(int(summary.get(key) or 0) for key in ("synced", "queued", "failed")):
                print(
                    "[account-watcher] panda sync after refresh "
                    f"synced={int(summary.get('synced') or 0)} "
                    f"queued={int(summary.get('queued') or 0)} "
                    f"failed={int(summary.get('failed') or 0)}"
                )
        except Exception as exc:
            print(f"[account-watcher] panda sync after refresh fail {exc}")

    def worker() -> None:
        # Avoid refreshing the whole account pool during container startup.
        if stop_event.wait(interval_seconds):
            return
        while not stop_event.is_set():
            try:
                from services.account_refresh_all_service import account_refresh_all_service
                if account_refresh_all_service.is_active():
                    print("[account-watcher] skip because refresh-all job is active")
                    stop_event.wait(interval_seconds)
                    continue
                limited_tokens = account_service.list_limited_tokens()
                normal_tokens = account_service.list_normal_tokens()
                expiring_tokens = account_service.list_expiring_access_tokens()
                keepalive_tokens = account_service.list_refresh_token_keepalive_tokens()
                max_tokens_per_cycle = _max_tokens_per_cycle()
                tokens = list(dict.fromkeys([*limited_tokens, *normal_tokens, *expiring_tokens]))
                expiring_token_set = set(expiring_tokens)
                keepalive_tokens = [token for token in keepalive_tokens if token not in expiring_token_set]
                if tokens:
                    refresh_tokens = tokens[:max_tokens_per_cycle]
                    print(
                        "[account-watcher] checking "
                        f"{len(refresh_tokens)}/{len(tokens)} selected accounts "
                        f"({len(limited_tokens)} limited, "
                        f"{len(normal_tokens)} normal, "
                        f"{len(expiring_tokens)} expiring access tokens)"
                    )
                    _refresh_and_sync_panda(refresh_tokens)
                if keepalive_tokens:
                    keepalive_batch = keepalive_tokens[:max_tokens_per_cycle]
                    print(f"[account-watcher] keepalive {len(keepalive_batch)}/{len(keepalive_tokens)} refresh tokens")
                    result = account_service.keepalive_refresh_tokens(keepalive_batch)
                    if result.get("errors"):
                        print(f"[account-watcher] keepalive errors: {result['errors']}")
            except Exception as exc:
                print(f"[account-watcher] fail {exc}")
            stop_event.wait(interval_seconds)

    thread = Thread(target=worker, name="account-watcher", daemon=True)
    thread.start()
    return thread


def web_asset_cache_headers(asset: Path) -> dict[str, str]:
    """Hashed Next chunks are immutable; HTML entrypoints must revalidate."""
    try:
        rel = asset.resolve().relative_to(WEB_DIST_DIR.resolve()).as_posix()
    except ValueError:
        rel = asset.name
    if rel.startswith("_next/static/"):
        return {"Cache-Control": "public, max-age=31536000, immutable"}
    if rel.endswith(".html") or rel in {"", "index.html"}:
        return {"Cache-Control": "no-cache"}
    return {"Cache-Control": "public, max-age=3600"}


def resolve_web_asset(requested_path: str) -> Path | None:
    if not WEB_DIST_DIR.exists():
        return None
    clean_path = requested_path.strip("/")
    base_dir = WEB_DIST_DIR.resolve()
    candidates = [base_dir / "index.html"] if not clean_path else [
        base_dir / Path(clean_path),
        base_dir / clean_path / "index.html",
        base_dir / f"{clean_path}.html",
    ]
    for candidate in candidates:
        try:
            candidate.resolve().relative_to(base_dir)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


def web_dist_health() -> dict[str, object]:
    """Return static frontend mount health (served from WEB_DIST_DIR inside container)."""
    required = (
        "index.html",
        "accounts/index.html",
        "chat/index.html",
        "login/index.html",
    )
    missing: list[str] = []
    sizes: dict[str, int] = {}
    for rel in required:
        path = WEB_DIST_DIR / rel
        if not path.is_file():
            missing.append(rel)
            continue
        try:
            sizes[rel] = int(path.stat().st_size)
        except OSError:
            missing.append(rel)
    manifest_path = WEB_DIST_DIR / "web_dist-manifest.json"
    build_id = ""
    if manifest_path.is_file():
        try:
            import json

            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            build_id = str(data.get("git_commit") or data.get("built_at") or "")
        except Exception:
            build_id = ""
    ok = not missing and int(sizes.get("index.html") or 0) > 0
    return {
        "ok": ok,
        "dir": str(WEB_DIST_DIR),
        "missing": missing,
        "index_bytes": int(sizes.get("index.html") or 0),
        "build_id": build_id,
    }
