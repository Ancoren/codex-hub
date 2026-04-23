"""
Database layer for Codex Hub.
"""

from __future__ import annotations

import enum
import json
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Generator, Optional

from sqlalchemy import (
    JSON, Column, DateTime, Float, Integer, String, Text, create_engine, select
)
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import get_config

Base = declarative_base()


class AccountStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    EXPIRED = "expired"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(191), unique=True, nullable=False, index=True)
    password = Column(Text)
    access_token = Column(Text)
    refresh_token = Column(Text)
    id_token = Column(Text)
    account_id = Column(String(191))

    # Usage tracking
    total_requests = Column(Integer, default=0)
    total_tokens_input = Column(Integer, default=0)
    total_tokens_output = Column(Integer, default=0)
    last_used_at = Column(DateTime)

    # Health
    status = Column(String(20), default=AccountStatus.ACTIVE, index=True)
    failure_count = Column(Integer, default=0)
    last_error = Column(Text)
    last_check_at = Column(DateTime)

    # Quota info (from /dashboard/billing/usage or similar)
    quota_info = Column(JSON, default=dict)
    expires_at = Column(DateTime)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self, include_token: bool = False) -> dict:
        d = {
            "id": self.id,
            "email": self.email,
            "account_id": self.account_id,
            "status": self.status,
            "total_requests": self.total_requests,
            "total_tokens_input": self.total_tokens_input,
            "total_tokens_output": self.total_tokens_output,
            "last_used_at": self.last_used_at.isoformat() if self.last_used_at else None,
            "last_check_at": self.last_check_at.isoformat() if self.last_check_at else None,
            "failure_count": self.failure_count,
            "last_error": self.last_error,
            "quota_info": self.quota_info or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_token:
            d["access_token"] = self.access_token
            d["refresh_token"] = self.refresh_token
        return d


class RequestLog(Base):
    __tablename__ = "request_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, index=True)
    client_ip = Column(String(64))
    endpoint = Column(String(255))
    model = Column(String(64))
    status_code = Column(Integer)
    tokens_input = Column(Integer, default=0)
    tokens_output = Column(Integer, default=0)
    latency_ms = Column(Float, default=0.0)
    error = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class DatabaseManager:
    def __init__(self) -> None:
        self._engine = None
        self._session_factory = None

    def init(self) -> None:
        cfg = get_config()
        self._engine = create_engine(
            cfg.get_db_url(),
            connect_args={"check_same_thread": False} if cfg.db_url.startswith("sqlite") else {},
            pool_pre_ping=True,
        )
        self._session_factory = sessionmaker(bind=self._engine, expire_on_commit=False)
        Base.metadata.create_all(self._engine)

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        if self._session_factory is None:
            self.init()
        s = self._session_factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # --- Account CRUD ---

    def add_account(self, email: str, access_token: str, refresh_token: str = "",
                    id_token: str = "", account_id: str = "", password: str = "") -> Account:
        with self.session() as s:
            acc = Account(
                email=email,
                access_token=access_token,
                refresh_token=refresh_token,
                id_token=id_token,
                account_id=account_id,
                password=password,
                status=AccountStatus.ACTIVE,
            )
            s.add(acc)
            s.flush()
            return acc

    def get_account(self, account_id: int) -> Optional[Account]:
        with self.session() as s:
            return s.query(Account).filter_by(id=account_id).first()

    def get_account_by_email(self, email: str) -> Optional[Account]:
        with self.session() as s:
            return s.query(Account).filter_by(email=email).first()

    def list_accounts(self, status: str | None = None) -> list[Account]:
        with self.session() as s:
            q = s.query(Account)
            if status:
                q = q.filter_by(status=status)
            return q.order_by(Account.id.desc()).all()

    def update_account_token(self, account_id: int, access_token: str, refresh_token: str = "") -> bool:
        with self.session() as s:
            acc = s.query(Account).filter_by(id=account_id).first()
            if not acc:
                return False
            acc.access_token = access_token
            if refresh_token:
                acc.refresh_token = refresh_token
            acc.updated_at = datetime.utcnow()
            return True

    def update_account_status(self, account_id: int, status: str, error: str = "") -> bool:
        with self.session() as s:
            acc = s.query(Account).filter_by(id=account_id).first()
            if not acc:
                return False
            acc.status = status
            if error:
                acc.last_error = error
            if status == AccountStatus.ACTIVE:
                acc.failure_count = 0
            acc.last_check_at = datetime.utcnow()
            return True

    def increment_failure(self, account_id: int, error: str = "") -> bool:
        with self.session() as s:
            acc = s.query(Account).filter_by(id=account_id).first()
            if not acc:
                return False
            acc.failure_count += 1
            acc.last_error = error
            acc.last_check_at = datetime.utcnow()
            if acc.failure_count >= get_config().max_failures_before_disable:
                acc.status = AccountStatus.ERROR
            return True

    def record_usage(self, account_id: int, tokens_input: int = 0, tokens_output: int = 0) -> bool:
        with self.session() as s:
            acc = s.query(Account).filter_by(id=account_id).first()
            if not acc:
                return False
            acc.total_requests += 1
            acc.total_tokens_input += tokens_input
            acc.total_tokens_output += tokens_output
            acc.last_used_at = datetime.utcnow()
            acc.failure_count = 0
            return True

    def delete_account(self, account_id: int) -> bool:
        with self.session() as s:
            acc = s.query(Account).filter_by(id=account_id).first()
            if acc:
                s.delete(acc)
                return True
            return False

    # --- Request Logs ---

    def log_request(self, **kwargs: Any) -> None:
        with self.session() as s:
            s.add(RequestLog(**kwargs))

    def get_logs(self, account_id: int | None = None, limit: int = 100) -> list[dict]:
        with self.session() as s:
            q = s.query(RequestLog)
            if account_id:
                q = q.filter_by(account_id=account_id)
            rows = q.order_by(RequestLog.id.desc()).limit(limit).all()
            return [{
                "id": r.id,
                "account_id": r.account_id,
                "endpoint": r.endpoint,
                "model": r.model,
                "status_code": r.status_code,
                "tokens_input": r.tokens_input,
                "tokens_output": r.tokens_output,
                "latency_ms": round(r.latency_ms, 2),
                "error": r.error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            } for r in rows]


db = DatabaseManager()
