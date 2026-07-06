from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from services.config import DATA_DIR, config

ImageInput = tuple[bytes, str, str]
MAX_IMAGE_REFERENCE_BYTES = 50 * 1024 * 1024


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _iso_from_ts(value: float | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _clean(value: object, default: str = "") -> str:
    text = str(value if value is not None else default).strip()
    return text or default


def _owner_id(identity: dict[str, object]) -> str:
    return _clean(identity.get("id")) or "anonymous"


def _safe_filename(value: str, fallback: str = "image.png") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._")
    return cleaned or fallback


class ImageAssetNotFoundError(ValueError):
    pass


class ImageAssetUploadWindowFullError(RuntimeError):
    def __init__(self, message: str, *, retry_after_secs: int = 5) -> None:
        super().__init__(message)
        self.retry_after_secs = max(1, int(retry_after_secs))


class ImageAssetService:
    """两阶段参考图资产存储。

    目标是让大参考图上传和 async task 入队解耦：上传阶段落盘得到 asset_id；
    任务提交只携带 asset_id，worker 真正执行时再读 bytes。
    """

    def __init__(
        self,
        db_path: Path | None = None,
        root_dir: Path | None = None,
        *,
        ttl_seconds: int | None = None,
        ttl_seconds_getter: Callable[[], int] | None = None,
        upload_global_concurrency_getter: Callable[[], int] | None = None,
        upload_per_user_concurrency_getter: Callable[[], int] | None = None,
        upload_max_bytes_inflight_getter: Callable[[], int] | None = None,
        upload_retry_after_seconds_getter: Callable[[], int] | None = None,
    ):
        self.db_path = db_path or (DATA_DIR / "image_reference_assets.db")
        self.root_dir = root_dir or (DATA_DIR / "image_assets" / "references")
        self._ttl_seconds = max(1, int(ttl_seconds if ttl_seconds is not None else 6 * 3600))
        self.ttl_seconds_getter = ttl_seconds_getter
        self.upload_global_concurrency_getter = upload_global_concurrency_getter
        self.upload_per_user_concurrency_getter = upload_per_user_concurrency_getter
        self.upload_max_bytes_inflight_getter = upload_max_bytes_inflight_getter
        self.upload_retry_after_seconds_getter = upload_retry_after_seconds_getter
        self._upload_condition = threading.Condition(threading.RLock())
        self._upload_active_global = 0
        self._upload_active_by_owner: dict[str, int] = {}
        self._upload_bytes_inflight = 0
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _settings(self) -> dict[str, object]:
        getter = getattr(config, "get_image_reference_assets_settings", None)
        if callable(getter):
            return getter()
        return {}

    def _ttl(self) -> int:
        if self.ttl_seconds_getter:
            try:
                return max(1, int(self.ttl_seconds_getter()))
            except Exception:
                pass
        try:
            return max(1, int(self._settings().get("asset_ttl_seconds") or self._ttl_seconds))
        except Exception:
            return self._ttl_seconds

    def _upload_global_limit(self) -> int:
        if self.upload_global_concurrency_getter:
            return max(1, int(self.upload_global_concurrency_getter()))
        return max(1, int(self._settings().get("upload_global_concurrency") or 6))

    def _upload_owner_limit(self) -> int:
        if self.upload_per_user_concurrency_getter:
            return max(1, int(self.upload_per_user_concurrency_getter()))
        return max(1, int(self._settings().get("upload_per_user_concurrency") or 3))

    def _upload_bytes_limit(self) -> int:
        if self.upload_max_bytes_inflight_getter:
            return max(1, int(self.upload_max_bytes_inflight_getter()))
        return max(1, int(self._settings().get("upload_max_bytes_inflight") or 96 * 1024 * 1024))

    def _upload_retry_after_secs(self) -> int:
        if self.upload_retry_after_seconds_getter:
            return max(1, int(self.upload_retry_after_seconds_getter()))
        return max(1, int(self._settings().get("upload_retry_after_seconds") or 5))

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS image_reference_assets (
                    asset_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    mime TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    bytes INTEGER NOT NULL,
                    storage_path TEXT NOT NULL,
                    created_ts REAL NOT NULL,
                    expires_ts REAL NOT NULL,
                    status TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_image_assets_owner ON image_reference_assets(owner_id, created_ts)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_image_assets_expires ON image_reference_assets(expires_ts)")
            conn.commit()

    def create_assets(self, identity: dict[str, object], images: list[ImageInput]) -> list[dict[str, Any]]:
        owner = _owner_id(identity)
        total_bytes = sum(len(data or b"") for data, _filename, _mime in images)
        self.cleanup_expired()
        with self.reserve_upload_window(identity, total_bytes):
            return self._create_assets_locked_window(owner, images)

    @contextmanager
    def reserve_upload_window(self, identity: dict[str, object], bytes_total: int):
        owner = _owner_id(identity)
        bytes_total = max(0, int(bytes_total or 0))
        retry_after = self._upload_retry_after_secs()
        with self._upload_condition:
            global_limit = self._upload_global_limit()
            owner_limit = self._upload_owner_limit()
            bytes_limit = self._upload_bytes_limit()
            owner_active = int(self._upload_active_by_owner.get(owner, 0))
            if self._upload_active_global >= global_limit:
                raise ImageAssetUploadWindowFullError(
                    f"image reference upload window is full ({self._upload_active_global}/{global_limit})",
                    retry_after_secs=retry_after,
                )
            if owner_active >= owner_limit:
                raise ImageAssetUploadWindowFullError(
                    f"image reference upload window is full for current user ({owner_active}/{owner_limit})",
                    retry_after_secs=retry_after,
                )
            if self._upload_bytes_inflight + bytes_total > bytes_limit:
                raise ImageAssetUploadWindowFullError(
                    f"image reference upload bytes window is full ({self._upload_bytes_inflight + bytes_total}/{bytes_limit})",
                    retry_after_secs=retry_after,
                )
            self._upload_active_global += 1
            self._upload_active_by_owner[owner] = owner_active + 1
            self._upload_bytes_inflight += bytes_total
        try:
            yield
        finally:
            with self._upload_condition:
                self._upload_active_global = max(0, self._upload_active_global - 1)
                current_owner_active = max(0, int(self._upload_active_by_owner.get(owner, 0)) - 1)
                if current_owner_active:
                    self._upload_active_by_owner[owner] = current_owner_active
                else:
                    self._upload_active_by_owner.pop(owner, None)
                self._upload_bytes_inflight = max(0, self._upload_bytes_inflight - bytes_total)
                self._upload_condition.notify_all()

    def _create_assets_locked_window(self, owner: str, images: list[ImageInput]) -> list[dict[str, Any]]:
        now = time.time()
        expires = now + self._ttl()
        items: list[dict[str, Any]] = []
        with self._connect() as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            for data, filename, mime in images:
                if not data:
                    raise ValueError("image file is empty")
                if len(data) > MAX_IMAGE_REFERENCE_BYTES:
                    raise ValueError("image exceeds 50MB limit")
                asset_id = uuid.uuid4().hex
                digest = hashlib.sha256(data).hexdigest()
                safe_name = _safe_filename(filename)
                storage_path = self.root_dir / f"{asset_id}-{safe_name}"
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                with os.fdopen(os.open(storage_path, flags, 0o600), "wb") as fh:
                    fh.write(data)
                conn.execute(
                    """
                    INSERT INTO image_reference_assets(
                        asset_id, owner_id, sha256, mime, filename, bytes,
                        storage_path, created_ts, expires_ts, status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset_id,
                        owner,
                        digest,
                        _clean(mime, "image/png"),
                        safe_name,
                        len(data),
                        str(storage_path),
                        now,
                        expires,
                        "ready",
                    ),
                )
                items.append(
                    {
                        "asset_id": asset_id,
                        "status": "ready",
                        "filename": safe_name,
                        "mime": _clean(mime, "image/png"),
                        "bytes": len(data),
                        "sha256": digest,
                        "created_at": _iso_from_ts(now),
                        "expires_at": _iso_from_ts(expires),
                    }
                )
            conn.commit()
        return items

    def get_asset(self, identity: dict[str, object], asset_id: str) -> dict[str, Any]:
        owner = _owner_id(identity)
        row = self._get_row(owner, asset_id)
        if row is None:
            raise ImageAssetNotFoundError("image asset not found")
        return self._public_row(row)

    def delete_asset(self, identity: dict[str, object], asset_id: str) -> bool:
        owner = _owner_id(identity)
        row = self._get_row(owner, asset_id)
        if row is None:
            return False
        with self._connect() as conn:
            conn.execute("DELETE FROM image_reference_assets WHERE owner_id=? AND asset_id=?", (owner, asset_id))
            conn.commit()
        try:
            Path(str(row["storage_path"])).unlink(missing_ok=True)
        except Exception:
            pass
        return True

    def read_assets(self, identity: dict[str, object], asset_ids: list[str]) -> list[ImageInput]:
        owner = _owner_id(identity)
        cleaned = [_clean(asset_id) for asset_id in asset_ids if _clean(asset_id)]
        if not cleaned:
            return []
        images: list[ImageInput] = []
        for asset_id in cleaned:
            row = self._get_row(owner, asset_id)
            if row is None:
                raise ImageAssetNotFoundError(f"image asset not found: {asset_id}")
            if str(row["status"]) != "ready":
                raise ImageAssetNotFoundError(f"image asset is not ready: {asset_id}")
            if float(row["expires_ts"] or 0.0) < time.time():
                raise ImageAssetNotFoundError(f"image asset expired: {asset_id}")
            path = Path(str(row["storage_path"]))
            if not path.exists():
                raise ImageAssetNotFoundError(f"image asset file missing: {asset_id}")
            images.append((path.read_bytes(), str(row["filename"]), str(row["mime"])))
        return images

    def cleanup_expired(self) -> int:
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT asset_id, storage_path FROM image_reference_assets WHERE expires_ts < ?",
                (now,),
            ).fetchall()
            conn.execute("DELETE FROM image_reference_assets WHERE expires_ts < ?", (now,))
            conn.commit()
        removed = 0
        for row in rows:
            try:
                Path(str(row["storage_path"])).unlink(missing_ok=True)
                removed += 1
            except Exception:
                pass
        return removed

    def _get_row(self, owner_id: str, asset_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM image_reference_assets WHERE owner_id=? AND asset_id=?",
                (owner_id, _clean(asset_id)),
            ).fetchone()

    def _public_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "asset_id": row["asset_id"],
            "status": row["status"],
            "filename": row["filename"],
            "mime": row["mime"],
            "bytes": row["bytes"],
            "sha256": row["sha256"],
            "created_at": _iso_from_ts(float(row["created_ts"])),
            "expires_at": _iso_from_ts(float(row["expires_ts"])),
        }


image_asset_service = ImageAssetService()
