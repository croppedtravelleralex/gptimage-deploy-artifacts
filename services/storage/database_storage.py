from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import Column, String, Text, create_engine, Integer, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from services.storage.base import StorageBackend

Base = declarative_base()


class AccountModel(Base):
    """账号数据模型"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    access_token = Column(String(2048), unique=True, nullable=False, index=True)
    data = Column(Text, nullable=False)  # JSON 格式存储完整账号数据


class AuthKeyModel(Base):
    """鉴权密钥数据模型"""
    __tablename__ = "auth_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key_id = Column(String(255), unique=True, nullable=False, index=True)
    data = Column(Text, nullable=False)


class DatabaseStorageBackend(StorageBackend):
    """数据库存储后端（支持 SQLite、PostgreSQL、MySQL 等）"""

    _CHUNK_SIZE = 500

    def __init__(
        self,
        database_url: str,
        *,
        import_accounts_path: Path | None = None,
        import_auth_keys_path: Path | None = None,
    ):
        self.database_url = database_url
        connect_args: dict[str, Any] = {}
        if database_url.startswith("sqlite"):
            # AccountService 会在多个 worker/maintenance 线程里做行级更新。
            # SQLite 需要关闭同线程限制，并使用 WAL 降低读写互斥。
            connect_args["check_same_thread"] = False
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,  # 自动检测连接是否有效
            pool_recycle=3600,   # 1小时回收连接
            connect_args=connect_args,
        )
        Base.metadata.create_all(self.engine)
        self._configure_sqlite()
        self.Session = sessionmaker(bind=self.engine)
        self._bootstrap_from_json(import_accounts_path, import_auth_keys_path)

    @staticmethod
    def _token_from_account(item: dict[str, Any]) -> str:
        return str((item or {}).get("access_token") or (item or {}).get("accessToken") or "").strip()

    @staticmethod
    def _auth_key_from_item(item: dict[str, Any]) -> str:
        return str((item or {}).get("id") or (item or {}).get("key_id") or "").strip()

    @staticmethod
    def _json_dumps(item: Any) -> str:
        return json.dumps(item, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _load_json_list(path: Path | None, *, dict_items_key: str | None = None) -> list[dict[str, Any]]:
        if path is None or not path.exists() or not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(data, dict) and dict_items_key:
            data = data.get(dict_items_key)
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def _configure_sqlite(self) -> None:
        if not self.database_url.startswith("sqlite"):
            return
        try:
            with self.engine.begin() as conn:
                conn.execute(text("PRAGMA journal_mode=WAL"))
                conn.execute(text("PRAGMA synchronous=NORMAL"))
                conn.execute(text("PRAGMA temp_store=MEMORY"))
                conn.execute(text("PRAGMA busy_timeout=5000"))
        except Exception:
            # PRAGMA 失败不影响数据库功能，最多退回 SQLite 默认模式。
            pass

    def _bootstrap_from_json(self, accounts_path: Path | None, auth_keys_path: Path | None) -> None:
        """首次切换到 SQLite 时从旧 JSON 快照导入。

        只有对应表为空才导入，避免生产已有 SQLite 数据时被旧 accounts.json 覆盖。
        """
        session = self.Session()
        try:
            account_count = session.query(AccountModel).count()
            auth_key_count = session.query(AuthKeyModel).count()
        finally:
            session.close()

        if account_count <= 0:
            accounts = self._load_json_list(accounts_path)
            if accounts:
                self.upsert_accounts(accounts)
        if auth_key_count <= 0:
            auth_keys = self._load_json_list(auth_keys_path, dict_items_key="items")
            if auth_keys:
                self.save_auth_keys(auth_keys)

    def load_accounts(self) -> list[dict[str, Any]]:
        """从数据库加载账号数据"""
        session = self.Session()
        try:
            accounts = []
            for row in session.query(AccountModel).order_by(AccountModel.id.asc()).all():
                try:
                    account_data = json.loads(row.data)
                    if isinstance(account_data, dict):
                        accounts.append(account_data)
                except json.JSONDecodeError:
                    continue
            return accounts
        finally:
            session.close()

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """保存账号数据到数据库。

        这是“全量替换”语义，仅用于初始化/显式迁移；运行时账号变化由
        upsert_accounts/delete_accounts 负责，避免整池重写。
        """
        deduped: dict[str, dict[str, Any]] = {}
        for item in accounts:
            if not isinstance(item, dict):
                continue
            token = self._token_from_account(item)
            if token:
                payload = dict(item)
                payload.pop("accessToken", None)
                payload["access_token"] = token
                deduped[token] = payload

        session = self.Session()
        try:
            existing = {row.access_token: row for row in session.query(AccountModel).all()}
            incoming_tokens = set(deduped)
            for token, row in existing.items():
                if token not in incoming_tokens:
                    session.delete(row)
            for token, item in deduped.items():
                row = existing.get(token)
                payload = self._json_dumps(item)
                if row is None:
                    session.add(AccountModel(access_token=token, data=payload))
                else:
                    row.data = payload
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def upsert_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """行级 upsert，多账号单事务提交。"""
        deduped: dict[str, dict[str, Any]] = {}
        for item in accounts:
            if not isinstance(item, dict):
                continue
            token = self._token_from_account(item)
            if not token:
                continue
            payload = dict(item)
            payload.pop("accessToken", None)
            payload["access_token"] = token
            deduped[token] = {**deduped.get(token, {}), **payload}
        if not deduped:
            return

        session = self.Session()
        try:
            tokens = list(deduped)
            existing: dict[str, AccountModel] = {}
            for offset in range(0, len(tokens), self._CHUNK_SIZE):
                chunk = tokens[offset: offset + self._CHUNK_SIZE]
                for row in session.query(AccountModel).filter(AccountModel.access_token.in_(chunk)).all():
                    existing[row.access_token] = row
            for token, item in deduped.items():
                payload = self._json_dumps(item)
                row = existing.get(token)
                if row is None:
                    session.add(AccountModel(access_token=token, data=payload))
                else:
                    row.data = payload
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_accounts(self, tokens: list[str]) -> None:
        """行级删除账号。"""
        target = list(dict.fromkeys(str(token or "").strip() for token in tokens if str(token or "").strip()))
        if not target:
            return
        session = self.Session()
        try:
            for offset in range(0, len(target), self._CHUNK_SIZE):
                chunk = target[offset: offset + self._CHUNK_SIZE]
                session.query(AccountModel).filter(AccountModel.access_token.in_(chunk)).delete(
                    synchronize_session=False
                )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def load_auth_keys(self) -> list[dict[str, Any]]:
        """从数据库加载鉴权密钥数据"""
        return self._load_rows(AuthKeyModel)

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """保存鉴权密钥数据到数据库"""
        self._save_rows(AuthKeyModel, auth_keys, "id", "key_id")

    def _load_rows(self, model: type[AccountModel] | type[AuthKeyModel]) -> list[dict[str, Any]]:
        session = self.Session()
        try:
            items = []
            for row in session.query(model).order_by(model.id.asc()).all():
                try:
                    item_data = json.loads(row.data)
                    if isinstance(item_data, dict):
                        items.append(item_data)
                except json.JSONDecodeError:
                    continue
            return items
        finally:
            session.close()

    def _save_rows(
        self,
        model: type[AccountModel] | type[AuthKeyModel],
        items: list[dict[str, Any]],
        source_key: str,
        target_key: str | None = None,
    ) -> None:
        session = self.Session()
        try:
            session.query(model).delete()
            for item in items:
                if not isinstance(item, dict):
                    continue
                key_value = str(item.get(source_key) or "").strip()
                if not key_value:
                    continue
                session.add(
                    model(
                        **{target_key or source_key: key_value},
                        data=self._json_dumps(item),
                    )
                )
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def health_check(self) -> dict[str, Any]:
        """健康检查"""
        try:
            session = self.Session()
            try:
                # 尝试执行简单查询
                session.execute(text("SELECT 1"))
                count = session.query(AccountModel).count()
                auth_key_count = session.query(AuthKeyModel).count()
                return {
                    "status": "healthy",
                    "backend": "database",
                    "database_url": self._mask_password(self.database_url),
                    "account_count": count,
                    "auth_key_count": auth_key_count,
                }
            finally:
                session.close()
        except Exception as e:
            return {
                "status": "unhealthy",
                "backend": "database",
                "error": str(e),
            }

    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        db_type = "unknown"
        if "sqlite" in self.database_url:
            db_type = "sqlite"
        elif "postgresql" in self.database_url or "postgres" in self.database_url:
            db_type = "postgresql"
        elif "mysql" in self.database_url:
            db_type = "mysql"
        
        return {
            "type": "database",
            "db_type": db_type,
            "description": f"数据库存储 ({db_type})",
            "database_url": self._mask_password(self.database_url),
        }

    @staticmethod
    def _mask_password(url: str) -> str:
        """隐藏数据库连接字符串中的密码"""
        if "://" not in url:
            return url
        try:
            protocol, rest = url.split("://", 1)
            if "@" in rest:
                credentials, host = rest.split("@", 1)
                if ":" in credentials:
                    username, _ = credentials.split(":", 1)
                    return f"{protocol}://{username}:****@{host}"
            return url
        except Exception:
            return url
