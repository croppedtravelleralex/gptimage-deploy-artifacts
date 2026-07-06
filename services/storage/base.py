from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class StorageBackend(ABC):
    """抽象存储后端基类"""

    @abstractmethod
    def load_accounts(self) -> list[dict[str, Any]]:
        """加载所有账号数据"""
        pass

    @abstractmethod
    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """保存所有账号数据"""
        pass

    def upsert_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """增量新增/更新账号。

        老的 JSON/Git 后端没有行级写入能力时走兼容实现；SQLite/Postgres 后端会覆盖为
        单事务 upsert，避免每次账号状态变化都全量 dump 整个 accounts.json。
        """
        if not accounts:
            return
        current: dict[str, dict[str, Any]] = {}
        for item in self.load_accounts():
            if not isinstance(item, dict):
                continue
            token = str(item.get("access_token") or item.get("accessToken") or "").strip()
            if token:
                current[token] = {**item, "access_token": token}
        for item in accounts:
            if not isinstance(item, dict):
                continue
            token = str(item.get("access_token") or item.get("accessToken") or "").strip()
            if token:
                current[token] = {**current.get(token, {}), **item, "access_token": token}
        self.save_accounts(list(current.values()))

    def delete_accounts(self, tokens: list[str]) -> None:
        """增量删除账号。

        默认兼容实现会重写完整列表；数据库后端会覆盖为行级 delete。
        """
        target = {str(token or "").strip() for token in tokens if str(token or "").strip()}
        if not target:
            return
        kept = [
            item
            for item in self.load_accounts()
            if str((item or {}).get("access_token") or (item or {}).get("accessToken") or "").strip() not in target
        ]
        self.save_accounts(kept)

    @abstractmethod
    def load_auth_keys(self) -> list[dict[str, Any]]:
        """加载所有鉴权密钥数据"""
        pass

    @abstractmethod
    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """保存所有鉴权密钥数据"""
        pass

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """健康检查，返回存储后端状态"""
        pass

    @abstractmethod
    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        pass
