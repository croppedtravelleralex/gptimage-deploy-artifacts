from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from services.storage.base import StorageBackend


class JSONStorageBackend(StorageBackend):
    """本地 JSON 文件存储后端"""

    _SAVE_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8, 1.2)

    def __init__(self, file_path: Path, auth_keys_path: Path | None = None):
        self.file_path = file_path
        self.auth_keys_path = auth_keys_path or file_path.with_name("auth_keys.json")
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self.auth_keys_path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _load_json_list(file_path: Path) -> list[dict[str, Any]]:
        if not file_path.exists():
            return []
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, Exception):
            return []

    @staticmethod
    def _atomic_write_text(file_path: Path, payload: str) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        last_error: OSError | None = None
        for index, delay in enumerate(JSONStorageBackend._SAVE_RETRY_DELAYS):
            tmp_path = file_path.with_name(f".{file_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                if file_path.exists():
                    try:
                        shutil.copy2(file_path, file_path.with_name(f"{file_path.name}.bak"))
                    except OSError:
                        pass
                os.replace(tmp_path, file_path)
                return
            except OSError as exc:
                last_error = exc
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                if index == len(JSONStorageBackend._SAVE_RETRY_DELAYS) - 1:
                    raise
                time.sleep(delay)
        if last_error is not None:
            raise last_error

    @staticmethod
    def _save_json_value(file_path: Path, value: Any) -> None:
        JSONStorageBackend._atomic_write_text(
            file_path,
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        )

    @staticmethod
    def _save_json_list(file_path: Path, items: list[dict[str, Any]]) -> None:
        JSONStorageBackend._save_json_value(file_path, items)

    def load_accounts(self) -> list[dict[str, Any]]:
        """从 JSON 文件加载账号数据"""
        return self._load_json_list(self.file_path)

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """保存账号数据到 JSON 文件"""
        self._save_json_list(self.file_path, accounts)

    def load_auth_keys(self) -> list[dict[str, Any]]:
        """从 JSON 文件加载鉴权密钥数据"""
        if not self.auth_keys_path.exists():
            return []
        try:
            data = json.loads(self.auth_keys_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            return []
        if isinstance(data, dict):
            data = data.get("items")
        return data if isinstance(data, list) else []

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """保存鉴权密钥数据到 JSON 文件"""
        self._save_json_value(self.auth_keys_path, {"items": auth_keys})

    def health_check(self) -> dict[str, Any]:
        """健康检查"""
        try:
            # 检查文件是否可读写
            if self.file_path.exists():
                self.file_path.read_text(encoding="utf-8")
            return {
                "status": "healthy",
                "backend": "json",
                "file_exists": self.file_path.exists(),
                "file_path": str(self.file_path),
                "auth_keys_file_exists": self.auth_keys_path.exists(),
                "auth_keys_file_path": str(self.auth_keys_path),
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "backend": "json",
                "error": str(e),
            }

    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        return {
            "type": "json",
            "description": "本地 JSON 文件存储",
            "file_path": str(self.file_path),
            "file_exists": self.file_path.exists(),
            "auth_keys_file_path": str(self.auth_keys_path),
            "auth_keys_file_exists": self.auth_keys_path.exists(),
        }
