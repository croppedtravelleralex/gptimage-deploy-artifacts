"""周期性刷新可调度账号的配额（quota）信息。

对每个可调度的图片生图账号调用 `account_service.fetch_remote_info`，
将最新的 quota / status 写入账号记录，使调度侧获得准确的配额水位。

- 后台线程按 `image_quota_refresh_interval_sec`（默认 60s）循环执行。
- `schedule_refresh(token)` 支持外部排队立即刷新指定号（去重）。
- `snapshot()` 返回当前状态用于诊断和 UI。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from services.account_service import account_service
from services.config import config
from utils.log import logger


def _interval_sec() -> float:
    """从 config 读取刷新间隔，默认 60 秒。"""
    try:
        return max(10.0, float(config.data.get("image_quota_refresh_interval_sec", 60.0)))
    except (TypeError, ValueError):
        return 60.0


class ImageQuotaRefreshService:
    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._clock: Callable[[], float] = clock or time.time
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # token -> queued_at timestamp
        self._pending: dict[str, float] = {}
        # 累计指标
        self._totals: dict[str, int] = {
            "ticks": 0,
            "refreshed": 0,
            "errors": 0,
            "scheduled": 0,
            "skipped": 0,
        }
        self._last_error = ""
        self._last_ok_at = 0.0

    def _now(self) -> float:
        return float(self._clock())

    # ---- 生命周期 ----

    def start(self) -> None:
        """启动后台刷新线程（幂等）。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="image-quota-refresh", daemon=True
        )
        self._thread.start()
        logger.info({"event": "image_quota_refresh_started"})

    def stop(self) -> None:
        """停止后台线程并等待退出。"""
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        logger.info({"event": "image_quota_refresh_stopped"})

    # ---- 外部接口 ----

    def schedule_refresh(self, token: str) -> None:
        """排队一个立即刷新请求（按 token 去重，已排队的不再重复加入）。"""
        key = str(token or "").strip()
        if not key:
            return
        now = self._now()
        with self._lock:
            # 已在队列中则只更新时间戳
            if key in self._pending:
                self._pending[key] = now
                return
            self._pending[key] = now
            self._totals["scheduled"] += 1

    def snapshot(self) -> dict[str, Any]:
        """返回服务当前状态快照。"""
        with self._lock:
            pending_count = len(self._pending)
            return {
                "worker_alive": bool(self._thread and self._thread.is_alive()),
                "pending_count": pending_count,
                "totals": dict(self._totals),
                "last_error": self._last_error,
                "last_ok_at": self._last_ok_at or None,
                "interval_sec": _interval_sec(),
            }

    # ---- 内部循环 ----

    def _loop(self) -> None:
        while not self._stop.is_set():
            interval = _interval_sec()
            try:
                self._tick()
            except Exception as exc:
                with self._lock:
                    self._last_error = f"{type(exc).__name__}: {exc}"[:240]
                    self._totals["errors"] += 1
                logger.warning(
                    {
                        "event": "image_quota_refresh_tick_error",
                        "error": str(exc)[:240],
                    }
                )
            self._stop.wait(interval)

    def _tick(self) -> None:
        """一次刷新周期：处理已排队请求 + 遍历全部可调度账号兜底刷新。"""
        now = self._now()

        # 1. 取出所有排队的 token
        with self._lock:
            pending = dict(self._pending)
            self._pending.clear()
            self._totals["ticks"] += 1

        # 2. 处理排队请求（立即刷新）
        for token in pending:
            try:
                result = account_service.fetch_remote_info(
                    token, event="image_quota_refresh:queued"
                )
                if result is not None:
                    with self._lock:
                        self._totals["refreshed"] += 1
                        self._last_ok_at = self._now()
                        self._last_error = ""
            except Exception as exc:
                with self._lock:
                    self._totals["errors"] += 1
                    self._last_error = f"{token[:16]}...: {type(exc).__name__}: {exc}"[:240]
                logger.warning(
                    {
                        "event": "image_quota_refresh_queued_fail",
                        "token_prefix": token[:16],
                        "error": str(exc)[:240],
                    }
                )

        # 3. 遍历所有可调度账号兜底刷新（按最新列表）
        accounts = account_service.list_accounts()
        for account in accounts:
            if self._stop.is_set():
                return
            if not isinstance(account, dict):
                continue
            if not account_service._is_image_account_schedulable(account):
                continue
            token = str(account.get("access_token") or "").strip()
            if not token:
                continue
            try:
                result = account_service.fetch_remote_info(
                    token, event="image_quota_refresh:tick"
                )
                if result is not None:
                    with self._lock:
                        self._totals["refreshed"] += 1
                        self._last_ok_at = self._now()
                        self._last_error = ""
            except Exception as exc:
                with self._lock:
                    self._totals["errors"] += 1
                    self._last_error = f"{token[:16]}...: {type(exc).__name__}: {exc}"[:240]
                logger.warning(
                    {
                        "event": "image_quota_refresh_tick_fail",
                        "token_prefix": token[:16],
                        "error": str(exc)[:240],
                    }
                )


image_quota_refresh_service = ImageQuotaRefreshService()
