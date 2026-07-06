from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from services.account_service import account_service
from services.config import DATA_DIR
from services.register import mail_provider, openai_register


REGISTER_FILE = DATA_DIR / "register.json"
STOP_DETACH_AFTER_SECONDS = 90.0
REGISTER_TRANSIENT_COOLDOWN_MAX_SECONDS = 5
REGISTER_TRANSIENT_DEGRADE_STREAK = 5
REGISTER_TRANSIENT_DEGRADED_MIN_WORKERS = 5


def _serialize_outlook_pool(credentials: list[dict]) -> str:
    return "\n".join(
        f'{c["email"]}----{c.get("password", "")}----{c["client_id"]}----{c["refresh_token"]}' for c in credentials
    )


def _merge_outlook_pool(old_text: str, new_text: str) -> str:
    """合并已存邮箱池与新导入文本，按邮箱去重，新导入的同名邮箱覆盖旧凭据。"""
    merged: dict[str, dict] = {}
    for credential in mail_provider.parse_outlook_credentials(old_text or ""):
        merged[credential["email"].strip().lower()] = credential
    for credential in mail_provider.parse_outlook_credentials(new_text or ""):
        merged[credential["email"].strip().lower()] = credential
    return _serialize_outlook_pool(list(merged.values()))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_config() -> dict:
    return {**openai_register.config, "mode": "total", "target_quota": 100, "target_available": 10, "check_interval": 5, "enabled": False, "stats": {"success": 0, "fail": 0, "transient": 0, "done": 0, "running": 0, "threads": openai_register.config["threads"], "elapsed_seconds": 0, "avg_seconds": 0, "success_rate": 0, "current_quota": 0, "current_available": 0}}


def _normalize(raw: dict) -> dict:
    cfg = _default_config()
    cfg.update({k: v for k, v in raw.items() if k not in {"stats", "logs"}})
    cfg["total"] = max(1, int(cfg.get("total") or 1))
    cfg["threads"] = max(1, int(cfg.get("threads") or 1))
    cfg["mode"] = str(cfg.get("mode") or "total").strip() if str(cfg.get("mode") or "total").strip() in {"total", "quota", "available"} else "total"
    cfg["target_quota"] = max(1, int(cfg.get("target_quota") or 1))
    cfg["target_available"] = max(1, int(cfg.get("target_available") or 1))
    cfg["check_interval"] = max(1, int(cfg.get("check_interval") or 5))
    cfg["proxy"] = str(cfg.get("proxy") or "").strip()
    if isinstance(cfg.get("mail"), dict):
        cfg["mail"].pop("proxy", None)
    cfg["enabled"] = bool(cfg.get("enabled"))
    stats = {**_default_config()["stats"], **(raw.get("stats") if isinstance(raw.get("stats"), dict) else {}),
             "threads": cfg["threads"]}
    cfg["stats"] = stats
    return cfg


def _local_proxy_endpoint(proxy_url: str) -> tuple[str, int] | None:
    raw = str(proxy_url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    host = str(parsed.hostname or "").strip().lower()
    port = parsed.port
    if host not in {"127.0.0.1", "localhost", "::1"} or not port:
        return None
    return host, int(port)


def _local_proxy_available(proxy_url: str, *, timeout: float = 1.0) -> bool:
    endpoint = _local_proxy_endpoint(proxy_url)
    if endpoint is None:
        return True
    try:
        with socket.create_connection(endpoint, timeout=timeout):
            return True
    except OSError:
        return False


class RegisterService:
    def __init__(self, store_file: Path, *, auto_start: bool = True):
        self._store_file = store_file
        self._lock = threading.RLock()
        self._runner: threading.Thread | None = None
        self._logs: list[dict] = []
        self._panda_buffer: list[dict] = []
        openai_register.register_log_sink = self._append_log
        self._config = self._load()
        if auto_start and self._config["enabled"]:
            self.start()

    def _load(self) -> dict:
        try:
            return _normalize(json.loads(self._store_file.read_text(encoding="utf-8")))
        except Exception:
            return _normalize({})

    def _save(self) -> None:
        self._store_file.parent.mkdir(parents=True, exist_ok=True)
        self._store_file.write_text(json.dumps(self._config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def get(self) -> dict:
        with self._lock:
            snapshot = json.loads(json.dumps({**self._config, "logs": self._logs[-300:]}, ensure_ascii=False))
        self._redact_outlook_pools(snapshot)
        return snapshot

    @staticmethod
    def _mask_email(email: str) -> str:
        local, sep, domain = str(email or "").partition("@")
        if not sep:
            return "***"
        masked = (local[:2] + "***" + local[-1:]) if len(local) > 2 else (local[:1] + "***")
        return f"{masked}@{domain}"

    def _redact_outlook_pools(self, snapshot: dict) -> None:
        """把 outlook_token 邮箱池里的密码/refresh_token 从对外输出中抹掉，仅保留脱敏预览与统计。

        mailboxes 改为只写导入框（输出为空），避免把密码与 refresh_token 通过 GET/SSE 反复广播。
        """
        mail = snapshot.get("mail")
        if not isinstance(mail, dict):
            return
        providers = mail.get("providers")
        if not isinstance(providers, list):
            return
        for provider in providers:
            if not isinstance(provider, dict) or provider.get("type") != "outlook_token":
                continue
            credentials = mail_provider.parse_outlook_credentials(str(provider.get("mailboxes") or ""))
            provider["mailboxes"] = ""
            provider["mailboxes_count"] = len(credentials)
            provider["mailboxes_preview"] = [self._mask_email(c["email"]) for c in credentials]
            provider["mailboxes_stats"] = mail_provider.outlook_token_pool_stats(credentials)

    def _drop_mail_proxy(self) -> None:
        if isinstance(self._config.get("mail"), dict):
            self._config["mail"].pop("proxy", None)

    def _merge_outlook_pools(self, updates: dict) -> None:
        """对 outlook_token provider：把前端新导入的 mailboxes 与已存池按邮箱合并去重。

        前端 mailboxes 是只写导入框，留空表示不改动；填入的新行追加/覆盖已存凭据。
        按数组下标与已存的同类型 provider 对齐。
        """
        mail = updates.get("mail")
        if not isinstance(mail, dict) or not isinstance(mail.get("providers"), list):
            return
        old_mail = self._config.get("mail") if isinstance(self._config.get("mail"), dict) else {}
        old_providers = old_mail.get("providers") if isinstance(old_mail.get("providers"), list) else []
        for index, provider in enumerate(mail["providers"]):
            if not isinstance(provider, dict) or provider.get("type") != "outlook_token":
                continue
            old = old_providers[index] if index < len(old_providers) and isinstance(old_providers[index], dict) else {}
            old_text = str(old.get("mailboxes") or "") if old.get("type") == "outlook_token" else ""
            new_text = str(provider.get("mailboxes") or "")
            provider["mailboxes"] = _merge_outlook_pool(old_text, new_text) if (old_text or new_text) else ""
            for key in ("mailboxes_count", "mailboxes_preview", "mailboxes_stats"):
                provider.pop(key, None)

    def _prune_unused_outlook_pools(self) -> int:
        mail = self._config.get("mail")
        if not isinstance(mail, dict):
            return 0
        providers = mail.get("providers")
        if not isinstance(providers, list):
            return 0
        total_removed = 0
        for provider in providers:
            if not isinstance(provider, dict) or provider.get("type") != "outlook_token":
                continue
            credentials = mail_provider.parse_outlook_credentials(str(provider.get("mailboxes") or ""))
            kept, removed = mail_provider.prune_outlook_unused_credentials(credentials)
            if removed:
                provider["mailboxes"] = _serialize_outlook_pool(kept)
                total_removed += removed
            for key in ("mailboxes_count", "mailboxes_preview", "mailboxes_stats"):
                provider.pop(key, None)
        return total_removed

    def update(self, updates: dict) -> dict:
        with self._lock:
            self._merge_outlook_pools(updates)
            self._config = _normalize({**self._config, **updates})
            self._drop_mail_proxy()
            openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads")})
            self._save()
            return self.get()

    def start(self) -> dict:
        with self._lock:
            if self._runner and self._runner.is_alive():
                if not bool(self._config.get("enabled")):
                    self._append_log(
                        "拒绝启动注册任务：上一轮注册任务仍在收尾，等待运行线程归零后再启动",
                        "yellow",
                    )
                    return self.get()
                self._save()
                return self.get()
            if not _local_proxy_available(str(self._config.get("proxy") or "")):
                self._config["enabled"] = False
                self._config["stats"]["updated_at"] = _now()
                self._save()
                endpoint = _local_proxy_endpoint(str(self._config.get("proxy") or ""))
                target = f"{endpoint[0]}:{endpoint[1]}" if endpoint else "configured proxy"
                self._append_log(f"拒绝启动注册任务：本地代理不可用 {target}", "red")
                return self.get()
            self._config["enabled"] = True
            self._drop_mail_proxy()
            self._logs = []
            metrics = self._pool_metrics()
            self._config["stats"] = {"job_id": uuid.uuid4().hex, "success": 0, "fail": 0, "transient": 0, "done": 0, "running": 0, "threads": self._config["threads"], **metrics, "started_at": _now(), "updated_at": _now()}
            openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads")})
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": time.time()})
            self._save()
            self._runner = threading.Thread(target=self._run, daemon=True, name="openai-register")
            self._runner.start()
            self._append_log(f"注册任务启动，模式={self._config['mode']}，线程数={self._config['threads']}", "yellow")
            return self.get()

    def start_if_enabled(self) -> dict:
        with self._lock:
            enabled = bool(self._config.get("enabled"))
        if enabled:
            return self.start()
        return self.get()

    def stop(self) -> dict:
        with self._lock:
            self._config["enabled"] = False
            self._config["stats"]["updated_at"] = _now()
            self._save()
            self._append_log("已请求停止注册任务，正在等待当前运行任务结束", "yellow")
            return self.get()

    def reset(self) -> dict:
        with self._lock:
            self._logs = []
            self._config["stats"] = {"success": 0, "fail": 0, "transient": 0, "done": 0, "running": 0, "threads": self._config["threads"], "elapsed_seconds": 0, "avg_seconds": 0, "success_rate": 0, **self._pool_metrics(), "updated_at": _now()}
            with openai_register.stats_lock:
                openai_register.stats.update({"done": 0, "success": 0, "fail": 0, "start_time": 0.0})
            self._save()
            return self.get()

    def reset_outlook_pool(self, scope: str = "all") -> dict:
        scope = str(scope or "all").strip().lower()
        if scope == "unused":
            with self._lock:
                removed = self._prune_unused_outlook_pools()
                openai_register.config.update({k: self._config[k] for k in ("mail", "proxy", "total", "threads")})
                self._save()
                self._append_log(f"已清空 Outlook 邮箱池未使用邮箱，移除 {removed} 个", "yellow")
            return self.get()
        scope = "failed" if str(scope) == "failed" else "all"
        cleared = mail_provider.reset_outlook_token_pool_state(scope)
        with self._lock:
            self._append_log(
                f"已重置 Outlook 邮箱池状态（范围={'仅失败/占用' if scope == 'failed' else '全部'}），清除 {cleared} 条记录",
                "yellow",
            )
        return self.get()

    def _append_log(self, text: str, color: str = "") -> None:
        with self._lock:
            self._logs.append({"time": _now(), "text": str(text), "level": str(color or "info")})
            self._logs = self._logs[-300:]

    def _pool_metrics(self) -> dict:
        items = account_service.list_accounts()
        normal = [item for item in items if item.get("status") == "正常"]
        return {
            "current_quota": sum(int(item.get("quota") or 0) for item in normal if not item.get("image_quota_unknown")),
            "current_available": len(normal),
        }

    def _target_reached(self, cfg: dict, submitted: int) -> bool:
        mode = str(cfg.get("mode") or "total")
        metrics = self._pool_metrics()
        self._bump(**metrics)
        if mode == "quota":
            reached = metrics["current_quota"] >= int(cfg.get("target_quota") or 1)
            self._append_log(f"检查号池：当前正常账号={metrics['current_available']}，当前剩余额度={metrics['current_quota']}，目标额度={cfg.get('target_quota')}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        if mode == "available":
            reached = metrics["current_available"] >= int(cfg.get("target_available") or 1)
            self._append_log(f"检查号池：当前正常账号={metrics['current_available']}，目标账号={cfg.get('target_available')}，当前剩余额度={metrics['current_quota']}，{'跳过注册' if reached else '继续注册'}", "yellow")
            return reached
        return submitted >= int(cfg.get("total") or 1)

    def _registered_account_from_result(self, result: dict) -> dict | None:
        if not result.get("ok"):
            return None
        payload = result.get("result") if isinstance(result.get("result"), dict) else {}
        token = str(payload.get("access_token") or "").strip()
        if not token:
            return None
        resolved = account_service.resolve_access_token(token)
        account = account_service.get_account(resolved)
        if account:
            return account
        return payload

    def _buffer_registered_account_for_panda(self, result: dict) -> None:
        account = self._registered_account_from_result(result)
        if not account:
            return
        try:
            from services.panda_staging_service import panda_staging_service

            staged = panda_staging_service.stage_account(account, source="register_service")
        except Exception as exc:
            with self._lock:
                self._panda_buffer.append(account)
            self._append_log(f"注册账号进入 Panda 成熟度探活队列失败，已暂存本地缓冲：{exc}", "yellow")
            return
        if staged:
            self._append_log("注册账号已进入本地成熟度探活队列，完成 1h/3h/6h 三次探活后再按水位上传 Panda", "green")

    def _flush_registered_panda_buffer(self, *, final: bool = False) -> None:
        with self._lock:
            if not self._panda_buffer:
                return
            accounts = list(self._panda_buffer)
            self._panda_buffer = []

        staged = 0
        try:
            from services.panda_staging_service import panda_staging_service

            for account in accounts:
                if panda_staging_service.stage_account(account, source="register_buffer"):
                    staged += 1
        except Exception as exc:
            with self._lock:
                self._panda_buffer = [*accounts, *self._panda_buffer]
            self._append_log(f"注册账号转入 Panda 成熟度探活队列失败：{exc}", "yellow")
            return
        if staged:
            self._append_log(f"已将 {staged} 个注册账号转入本地成熟度探活队列，暂不直接上传 Panda", "green")

    def _bump(self, **updates) -> None:
        with self._lock:
            self._config["stats"].update(updates)
            stats = self._config["stats"]
            started_at = str(stats.get("started_at") or "")
            if started_at:
                try:
                    elapsed = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds())
                except Exception:
                    elapsed = 0.0
                done = int(stats.get("done") or 0)
                success = int(stats.get("success") or 0)
                fail = int(stats.get("fail") or 0)
                stats["elapsed_seconds"] = round(elapsed, 1)
                stats["avg_seconds"] = round(elapsed / success, 1) if success else 0
                stats["success_rate"] = round(success * 100 / max(1, success + fail), 1)
            self._config["stats"]["updated_at"] = _now()
            self._save()

    def _run(self) -> None:
        threads = int(self.get()["threads"])
        submitted, done, success, fail, transient = 0, 0, 0, 0, 0
        transient_streak = 0
        executor = ThreadPoolExecutor(max_workers=threads)
        futures = set()
        stop_seen_at: float | None = None
        detached = False
        last_effective_threads = threads
        try:
            while True:
                cfg = self.get()
                effective_threads = threads
                if transient_streak >= REGISTER_TRANSIENT_DEGRADE_STREAK:
                    effective_threads = max(REGISTER_TRANSIENT_DEGRADED_MIN_WORKERS, threads // 2)
                if effective_threads != last_effective_threads:
                    if effective_threads < threads:
                        self._append_log(
                            f"检测到连续网络/代理瞬断，临时降并发 {threads}->{effective_threads}，避免把 40080/WARP 打穿",
                            "yellow",
                        )
                    else:
                        self._append_log(f"网络/代理瞬断缓解，恢复注册并发 {threads}", "green")
                    last_effective_threads = effective_threads
                while (
                    self.get()["enabled"]
                    and not self._target_reached(cfg, submitted)
                    and len(futures) < effective_threads
                ):
                    submitted += 1
                    futures.add(executor.submit(openai_register.worker, submitted))
                self._bump(running=len(futures), done=done, success=success, fail=fail, transient=transient)
                if not futures and (not self.get()["enabled"] or str(cfg.get("mode") or "total") == "total"):
                    break
                if not futures:
                    time.sleep(max(1, int(cfg.get("check_interval") or 5)))
                    continue
                if not self.get()["enabled"]:
                    if stop_seen_at is None:
                        stop_seen_at = time.monotonic()
                    elif time.monotonic() - stop_seen_at >= STOP_DETACH_AFTER_SECONDS:
                        for future in list(futures):
                            future.cancel()
                        self._append_log(
                            f"注册停止等待超过 {int(STOP_DETACH_AFTER_SECONDS)}s，已停止接收新任务并释放启动锁；少量已进入网络请求的 worker 会自行超时清理",
                            "yellow",
                        )
                        detached = True
                        break
                finished, futures = wait(futures, timeout=1.0, return_when=FIRST_COMPLETED)
                if not finished:
                    continue
                cooldown_seconds = 0
                for future in finished:
                    try:
                        result = future.result()
                        if result.get("ok"):
                            done += 1
                            success += 1
                            transient_streak = 0
                        elif result.get("transient"):
                            done += 1
                            fail += 1
                            transient += 1
                            transient_streak += 1
                            cooldown_seconds = max(cooldown_seconds, min(REGISTER_TRANSIENT_COOLDOWN_MAX_SECONDS, 5 * transient_streak))
                        else:
                            done += 1
                            fail += 1
                            transient_streak = 0
                        self._buffer_registered_account_for_panda(result)
                    except Exception:
                        done += 1
                        fail += 1
                        transient_streak = 0
                if cooldown_seconds and self.get()["enabled"]:
                    self._append_log(f"检测到连续网络/代理瞬断，短冷却 {cooldown_seconds}s 后继续，避免全局长冷却打掉吞吐", "yellow")
                    time.sleep(cooldown_seconds)
        finally:
            executor.shutdown(wait=not detached, cancel_futures=True)
        self._flush_registered_panda_buffer(final=True)
        self._bump(running=0, done=done, success=success, fail=fail, transient=transient, finished_at=_now())
        with self._lock:
            self._config["enabled"] = False
            self._save()
        self._append_log(f"注册任务结束，成功{success}，失败{fail}", "yellow")


def _allow_register_autostart() -> bool:
    disabled = str(os.getenv("CHATGPT2API_DISABLE_REGISTER_AUTOSTART") or "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        return False
    # 测试导入模块时不能因为真实 data/register.json.enabled=true 而拉起注册任务。
    argv0 = Path(sys.argv[0]).name.lower()
    argv_text = " ".join(str(item).lower() for item in sys.argv)
    return "pytest" not in argv0 and "pytest" not in argv_text


register_service = RegisterService(REGISTER_FILE, auto_start=False)
