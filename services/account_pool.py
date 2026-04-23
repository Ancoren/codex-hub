"""
Account pool management with thread-safe selection.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Optional

from models.database import Account, AccountStatus, db
from utils.logger import get_logger

logger = get_logger("account_pool")


class AccountPool:
    """
    Thread-safe account pool with pluggable selection strategies.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._index = 0
        self._accounts: dict[int, Account] = {}
        self._load_accounts()

    def _load_accounts(self) -> None:
        """Load active accounts from DB into memory."""
        accounts = db.list_accounts(status=AccountStatus.ACTIVE)
        with self._lock:
            self._accounts = {a.id: a for a in accounts}
        logger.info(f"Loaded {len(self._accounts)} active accounts into pool")

    def refresh(self) -> None:
        """Reload accounts from DB."""
        self._load_accounts()

    def get_active_accounts(self) -> list[Account]:
        with self._lock:
            return list(self._accounts.values())

    def get(self, strategy: str = "least_used") -> Optional[Account]:
        """Select an account using the given strategy."""
        with self._lock:
            candidates = list(self._accounts.values())
            if not candidates:
                return None

            if strategy == "round_robin":
                acc = candidates[self._index % len(candidates)]
                self._index += 1
                return acc

            if strategy == "random":
                return random.choice(candidates)

            if strategy == "least_used":
                return min(candidates, key=lambda a: a.total_requests)

            if strategy == "priority":
                # Could be extended with priority field
                return candidates[0]

            # Default fallback
            return candidates[0]

    def mark_success(self, account_id: int) -> None:
        """Mark account as successfully used."""
        db.record_usage(account_id)
        with self._lock:
            if account_id in self._accounts:
                self._accounts[account_id].total_requests += 1

    def mark_failure(self, account_id: int, error: str = "") -> None:
        """Mark account as failed. May disable it if threshold reached."""
        db.increment_failure(account_id, error)
        with self._lock:
            if account_id in self._accounts:
                acc = self._accounts[account_id]
                acc.failure_count += 1
                acc.last_error = error
                if acc.failure_count >= 3:
                    logger.warning(f"Account {acc.email} disabled after {acc.failure_count} failures")
                    acc.status = AccountStatus.ERROR
                    del self._accounts[account_id]

    def update_account_token(self, account_id: int, access_token: str, refresh_token: str = "") -> None:
        db.update_account_token(account_id, access_token, refresh_token)
        with self._lock:
            if account_id in self._accounts:
                self._accounts[account_id].access_token = access_token
                if refresh_token:
                    self._accounts[account_id].refresh_token = refresh_token

    def add_account(self, account: Account) -> None:
        with self._lock:
            self._accounts[account.id] = account

    def remove_account(self, account_id: int) -> None:
        with self._lock:
            self._accounts.pop(account_id, None)


# Singleton
pool = AccountPool()
