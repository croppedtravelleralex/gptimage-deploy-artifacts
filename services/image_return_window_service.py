from __future__ import annotations

import time
from contextlib import contextmanager
from threading import BoundedSemaphore, Lock
from typing import Iterator

from services.config import config


class ImageReturnWindowService:
    """限制同时“下载图片 + b64/url 组装”的回传窗口。

    生成阶段可以继续并发跑；当大量图片几乎同时完成时，只有窗口内的图片进入
    下载、base64 编码、落本地图片索引和构造响应阶段，减少尾部带宽/CPU/内存拥塞。
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._size = -1
        self._semaphore: BoundedSemaphore | None = None

    def _current(self) -> tuple[int, BoundedSemaphore | None]:
        size = int(config.image_return_window_size or 0)
        with self._lock:
            if size <= 0:
                self._size = size
                self._semaphore = None
                return size, None
            if self._semaphore is None or self._size != size:
                self._size = size
                self._semaphore = BoundedSemaphore(size)
            return size, self._semaphore

    @contextmanager
    def acquire(self, slots: int = 1) -> Iterator[None]:
        size, semaphore = self._current()
        if size <= 0 or semaphore is None:
            yield
            return
        needed = max(1, min(int(slots or 1), size))
        timeout = float(config.image_return_window_timeout_secs or 180.0)
        deadline = time.monotonic() + timeout
        acquired = 0
        try:
            for _ in range(needed):
                remaining = max(0.0, deadline - time.monotonic())
                if not semaphore.acquire(timeout=remaining):
                    raise TimeoutError(
                        f"image return window timeout: waited {timeout:.0f}s for {needed} slot(s)"
                    )
                acquired += 1
            yield
        finally:
            for _ in range(acquired):
                try:
                    semaphore.release()
                except ValueError:
                    break


image_return_window_service = ImageReturnWindowService()
