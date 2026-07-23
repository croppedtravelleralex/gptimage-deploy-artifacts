from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from services.account_service import account_service
from services.config import BASE_DIR, DATA_DIR
from services import yumail_otp
from utils.log import logger


_STAGE_MESSAGES = {
    "queued": "等待执行 Outlook 恢复",
    "proxy_preflight": "正在验证 Webshare 代理",
    "mailbox_preflight": "正在验证 Outlook 邮箱可读",
    "login": "正在重新登录",
    "chatgpt_email_otp_login": "正在通过 Outlook OTP 登录 ChatGPT",
    "chatgpt_email_otp_token_received": "已取得新登录 token",
    "staging_new_token": "正在隔离写入新 token",
    "panda_webshare_verify": "正在通过 Panda Webshare 验证",
    "commit": "正在提交新 token 并移除旧 token",
    "terminal": "OpenAI 账号已停用，已停止自动恢复",
    "done": "恢复完成",
    "failed": "恢复失败",
}
_TOKEN_RE = re.compile(r"\beyJ[A-Za-z0-9._-]{32,}\b")
_TERMINAL_RECOVERY_REASONS = {"account_deactivated"}


def _mask_email(value: object) -> str:
    email = str(value or "").strip().lower()
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    if len(local) <= 3:
        masked = "***"
    else:
        masked = f"{local[:2]}***{local[-1]}"
    return f"{masked}@{domain}"


def _is_outlook_email(value: object) -> bool:
    email = str(value or "").strip().lower()
    return email.endswith(("@outlook.com", "@hotmail.com", "@live.com"))


def _has_recovery_evidence(account: dict[str, Any]) -> bool:
    if str(account.get("status") or "").strip() == "异常":
        return True
    if str(account.get("panda_receive_state") or "").strip().lower() == "rejected":
        return True
    if int(account.get("invalid_count") or 0) > 0:
        return True
    error_text = " ".join(
        str(account.get(key) or "")
        for key in (
            "last_refresh_error",
            "last_token_refresh_error",
            "last_quota_refresh_error",
            "panda_probe_last_error",
            "panda_verify_last_error",
        )
    ).lower()
    return any(marker in error_text for marker in ("token invalidated", "token_revoked", "invalidated oauth token"))


def _terminal_recovery_reason(account: dict[str, Any]) -> str:
    state = str(account.get("outlook_recovery_state") or "").strip().lower()
    explicit = str(account.get("outlook_recovery_terminal_reason") or "").strip().lower()
    if explicit in _TERMINAL_RECOVERY_REASONS:
        return explicit
    if state != "terminal":
        return ""
    error_text = " ".join(
        str(account.get(key) or "")
        for key in (
            "outlook_recovery_last_error",
            "last_refresh_error",
            "panda_verify_last_error",
        )
    ).lower()
    if "account_deactivated" in error_text or "deleted or deactivated" in error_text:
        return "account_deactivated"
    return "terminal"


def _is_terminal_outlook_recovery(account: dict[str, Any]) -> bool:
    return bool(_terminal_recovery_reason(account))


def _sanitize_error(value: object, *, email: str = "", limit: int = 500) -> str:
    text = str(value or "").strip()
    if email:
        text = re.sub(re.escape(email), _mask_email(email), text, flags=re.IGNORECASE)
    text = _TOKEN_RE.sub("<redacted-token>", text)
    return text[:limit] or "Outlook 账号恢复失败"


class OutlookAccountRecoveryService:
    """从账号页触发单个异常 Outlook 账号的受控恢复。"""

    def __init__(
        self,
        *,
        account_service: Any,
        base_dir: Path = BASE_DIR,
        data_dir: Path = DATA_DIR,
        credentials_file: Path | None = None,
        proxy_file: Path | None = None,
        timeout_secs: float = 900.0,
        worker_launcher: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self.account_service = account_service
        self.base_dir = Path(base_dir).resolve()
        self.data_dir = Path(data_dir).resolve()
        configured_credentials = os.getenv("PANDA_OUTLOOK_RECOVERY_CREDENTIALS_FILE", "").strip()
        configured_proxy = os.getenv("PANDA_OUTLOOK_RECOVERY_PROXY_FILE", "").strip()
        self.credentials_file = Path(
            credentials_file
            or configured_credentials
            or (self.data_dir / "runlogs" / "panda-outlook-recovery.credentials.secret.txt")
        ).resolve()
        self.proxy_file = Path(
            proxy_file
            or configured_proxy
            or (self.data_dir / "runlogs" / "webshare_good_csrf_200.secret.txt")
        ).resolve()
        self.recovery_script = (self.base_dir / "scripts" / "recover_panda_outlook_accounts.py").resolve()
        self.camoufox_script = (self.base_dir / "scripts" / "outlook_camoufox_password_relogin.py").resolve()
        self.timeout_secs = max(60.0, float(timeout_secs))
        self._worker_launcher = worker_launcher or self._launch_thread
        self._lock = threading.Lock()
        self._progress: dict[str, dict[str, Any]] = {}
        self._active_progress_id = ""

    @property
    def recovery_backend(self) -> str:
        value = str(os.getenv("OUTLOOK_RECOVERY_BACKEND") or "http").strip().lower()
        return value if value in {"http", "camoufox"} else "http"

    @staticmethod
    def _launch_thread(worker: Callable[[], None]) -> None:
        threading.Thread(target=worker, name="outlook-account-recovery", daemon=True).start()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _assert_protected_secret(path: Path, label: str) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"{label}未配置或文件不存在")
        if os.name != "nt" and path.stat().st_mode & 0o077:
            raise PermissionError(f"{label}权限必须为 600")

    def _validate_target(self, access_token: str) -> tuple[str, dict[str, Any]]:
        token = str(access_token or "").strip()
        if not token:
            raise ValueError("access_token is required")
        account = self.account_service.get_account(token)
        if not isinstance(account, dict):
            raise ValueError("账号不存在或 token 已变化，请刷新页面后重试")
        email = str(account.get("email") or "").strip().lower()
        if not _is_outlook_email(email):
            raise ValueError("当前一键恢复仅支持 Outlook/Hotmail/Live 账号")
        if _is_terminal_outlook_recovery(account):
            raise ValueError("OpenAI 账号已删除或停用，已停止自动恢复；官方恢复后请重新导入账号")
        if not _has_recovery_evidence(account):
            raise ValueError("仅允许恢复异常、rejected 或明确 token invalidated 的账号")
        has_password = bool(str(account.get("password") or "").strip())
        has_credentials = self.credentials_file.is_file()
        has_yumail = yumail_otp.is_configured()
        if not has_password:
            raise ValueError("need_openai_password: 账号缺少 OpenAI 密码，无法密码重登")
        if not has_credentials and not has_yumail:
            raise ValueError(
                "yumail_not_configured: 未配置 YuMail（YUMAIL_API_KEY）且无 Outlook 邮箱凭据文件，无法收 OTP"
            )
        # 仅当必须走 YuMail（无邮箱凭据）时才探针环回；有凭据文件时可独立恢复。
        if not has_credentials:
            probe = yumail_otp.probe_reachable()
            if not probe.get("ok"):
                raise RuntimeError(f"yumail_unreachable: {probe.get('error') or 'probe failed'}")
        if self.recovery_backend == "camoufox":
            if not self.camoufox_script.is_file():
                raise FileNotFoundError("Outlook Camoufox 恢复引擎不存在")
        elif not self.recovery_script.is_file():
            raise FileNotFoundError("Outlook 恢复引擎不存在")
        if self.proxy_file.exists():
            self._assert_protected_secret(self.proxy_file, "Webshare 代理文件")
        return email, account

    def start(self, access_token: str) -> str:
        email, _account = self._validate_target(access_token)
        with self._lock:
            if self._active_progress_id:
                active = self._progress.get(self._active_progress_id) or {}
                if not bool(active.get("done")):
                    raise RuntimeError("已有 Outlook 账号正在恢复，请等待当前任务完成")
            progress_id = str(uuid.uuid4())
            now = self._now()
            self._progress[progress_id] = {
                "progress_id": progress_id,
                "done": False,
                "ok": False,
                "stage": "queued",
                "message": _STAGE_MESSAGES["queued"],
                "email": _mask_email(email),
                "created_at": now,
                "updated_at": now,
                "error": "",
                "result": None,
            }
            self._active_progress_id = progress_id

        try:
            self._worker_launcher(lambda: self._run(progress_id, email, access_token))
        except Exception as exc:
            self._finish(progress_id, ok=False, error=_sanitize_error(exc, email=email))
            raise
        return progress_id

    def get_progress(self, progress_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._progress.get(str(progress_id or "").strip())
            return copy.deepcopy(value) if value is not None else None

    def is_busy(self) -> bool:
        with self._lock:
            progress_id = str(self._active_progress_id or "").strip()
            if not progress_id:
                return False
            active = self._progress.get(progress_id) or {}
            return not bool(active.get("done"))

    def check_prerequisites(self) -> tuple[bool, str]:
        try:
            if self.recovery_backend == "camoufox":
                if not self.camoufox_script.is_file():
                    return False, "Outlook Camoufox 恢复引擎不存在"
            elif not self.recovery_script.is_file():
                return False, "Outlook 恢复引擎不存在"
            has_credentials = self.credentials_file.is_file()
            if not yumail_otp.is_configured() and not has_credentials:
                return False, "yumail_not_configured: 未配置 YuMail 且无 Outlook 邮箱凭据"
            # 与 _validate_target 对齐：仅当无邮箱凭据、必须走 YuMail 时才强制探针。
            if not has_credentials and yumail_otp.is_configured():
                probe = yumail_otp.probe_reachable()
                if not probe.get("ok"):
                    return False, f"yumail_unreachable: {probe.get('error') or 'probe failed'}"
            if self.proxy_file.exists():
                self._assert_protected_secret(self.proxy_file, "Webshare 代理文件")
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def _update(self, progress_id: str, **updates: Any) -> None:
        with self._lock:
            progress = self._progress.get(progress_id)
            if progress is None:
                return
            progress.update(updates)
            progress["updated_at"] = self._now()

    def _finish(
        self,
        progress_id: str,
        *,
        ok: bool,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        self._update(
            progress_id,
            done=True,
            ok=bool(ok),
            stage="done" if ok else "failed",
            message=_STAGE_MESSAGES["done" if ok else "failed"],
            result=result,
            error=error,
        )
        with self._lock:
            if self._active_progress_id == progress_id:
                self._active_progress_id = ""

    def _run_paths(self, progress_id: str) -> tuple[Path, Path]:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = progress_id.split("-", 1)[0]
        report_dir = self.data_dir / "runlogs" / f"panda-outlook-ui-recovery-{stamp}-{suffix}"
        backup_dir = self.data_dir / "backups" / f"panda-outlook-ui-recovery-before-{stamp}-{suffix}"
        for path in (report_dir, backup_dir):
            path.mkdir(parents=True, exist_ok=False)
            if os.name != "nt":
                path.chmod(0o700)
        return report_dir, backup_dir

    def _build_command(self, email: str, report_dir: Path, backup_dir: Path) -> list[str]:
        if self.recovery_backend == "camoufox":
            command = [
                sys.executable,
                str(self.camoufox_script),
                "--root",
                str(self.base_dir),
                "--email",
                email,
                "--report-dir",
                str(report_dir),
            ]
            if self.proxy_file.is_file():
                command.extend(["--proxy-file", str(self.proxy_file)])
            return command

        command = [
            sys.executable,
            str(self.recovery_script),
            "--root",
            str(self.base_dir),
            "--target-email",
            email,
            "--limit",
            "1",
            "--report-dir",
            str(report_dir),
            "--backup-dir",
            str(backup_dir),
            "--allow-account-password",
            "--prefer-yumail-otp",
        ]
        if self.credentials_file.is_file():
            command.extend(["--credentials-file", str(self.credentials_file)])
        else:
            command.append("--skip-mailbox-if-missing")
        if self.proxy_file.is_file():
            command.extend(["--proxy-file", str(self.proxy_file)])
        return command

    def _mark_terminal_account(self, access_token: str, email: str, reason: str) -> None:
        normalized_reason = str(reason or "").strip().lower()
        if normalized_reason not in _TERMINAL_RECOVERY_REASONS:
            return
        now = self._now()
        message = "OpenAI 账号已删除或停用，无法自动恢复"
        updated = self.account_service.update_account(
            access_token,
            {
                "status": "禁用",
                "quota": 0,
                "image_quota_unknown": False,
                "panda_receive_state": "rejected",
                "panda_rejected_at": now,
                "panda_verify_last_error": normalized_reason,
                "last_refresh_error": normalized_reason,
                "last_refresh_error_at": now,
                "quota_refresh_failure_kind": "invalid",
                "outlook_recovery_state": "terminal",
                "outlook_recovery_terminal_reason": normalized_reason,
                "outlook_recovery_terminal_at": now,
                "outlook_recovery_last_attempt_at": now,
                "outlook_recovery_last_error": message,
                "updated_at": now,
            },
            quiet=True,
        )
        if updated is None:
            raise RuntimeError("终态账号标记失败：账号 token 已变化")
        logger.warning(
            {
                "event": "outlook_account_recovery_terminal",
                "email": _mask_email(email),
                "reason": normalized_reason,
            }
        )

    def _run(self, progress_id: str, email: str, access_token: str) -> None:
        process: subprocess.Popen[str] | None = None
        output_tail: deque[str] = deque(maxlen=30)
        try:
            report_dir, backup_dir = self._run_paths(progress_id)
            command = self._build_command(email, report_dir, backup_dir)
            env = os.environ.copy()
            current_pythonpath = env.get("PYTHONPATH", "").strip()
            env["PYTHONPATH"] = str(self.base_dir) + (os.pathsep + current_pythonpath if current_pythonpath else "")
            self._update(progress_id, stage="starting", message="正在启动 Outlook 恢复引擎")
            process = subprocess.Popen(
                command,
                cwd=str(self.base_dir),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )

            def read_output() -> None:
                assert process is not None
                assert process.stdout is not None
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    output_tail.append(_sanitize_error(line, email=email, limit=1000))
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("event") != "recovery_progress":
                        continue
                    stage = str(event.get("stage") or "").strip()
                    if stage:
                        self._update(
                            progress_id,
                            stage=stage,
                            message=_STAGE_MESSAGES.get(stage, "正在恢复 Outlook 账号"),
                        )

            reader = threading.Thread(target=read_output, name="outlook-recovery-output", daemon=True)
            reader.start()
            try:
                return_code = process.wait(timeout=self.timeout_secs)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait(timeout=10)
                raise TimeoutError(f"Outlook 恢复超过 {self.timeout_secs:.0f} 秒，已终止") from exc
            finally:
                reader.join(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()

            summary_path = report_dir / "summary.json"
            rows_path = report_dir / "rows.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
            rows = json.loads(rows_path.read_text(encoding="utf-8")) if rows_path.is_file() else []
            row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
            restored = int(summary.get("restored") or 0)
            failed = int(summary.get("failed") or 0)
            terminal_reason = str(row.get("terminal_reason") or "").strip().lower()
            if terminal_reason in _TERMINAL_RECOVERY_REASONS:
                self._mark_terminal_account(access_token, email, terminal_reason)
            if return_code != 0 or restored != 1 or failed != 0 or not bool(row.get("ok")):
                detail = row.get("error") or (output_tail[-1] if output_tail else f"recovery exit code {return_code}")
                raise RuntimeError(_sanitize_error(detail, email=email))

            self.account_service.reload_from_storage()
            result = {
                "email": _mask_email(email),
                "quota": int(row.get("quota") or 0),
                "status": str(row.get("status") or ""),
                "schedulable": bool(row.get("schedulable")),
                "old_removed": bool(row.get("old_removed")),
                "old_fp_inherited": bool(row.get("old_fp_inherited")),
                "login_via_chatgpt_email_otp": bool(row.get("login_via_chatgpt_email_otp")),
                "report_dir": str(report_dir),
                "backup_dir": str(backup_dir),
            }
            self._finish(progress_id, ok=True, result=result)
        except Exception as exc:
            error = _sanitize_error(exc, email=email)
            logger.warning({
                "event": "outlook_account_ui_recovery_failed",
                "email": _mask_email(email),
                "error": error,
            })
            self._finish(progress_id, ok=False, error=error)
        finally:
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                except Exception:
                    pass


outlook_account_recovery_service = OutlookAccountRecoveryService(account_service=account_service)
