"""
Background health checker and token refresher.
Runs in a daemon thread, periodically checks all accounts.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from app.config import get_config
from models.database import Account, AccountStatus, db
from services.account_pool import pool
from utils.logger import get_logger

logger = get_logger("health_checker")

TOKEN_URL = "https://auth.openai.com/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


class HealthChecker:
    def __init__(self, interval: int = 300) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(f"Health checker started (interval={self.interval}s)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._check_all()
            except Exception:
                logger.exception("Health check cycle error")
            self._stop.wait(self.interval)

    def _check_all(self) -> None:
        accounts = db.list_accounts()
        logger.debug(f"Checking {len(accounts)} accounts...")
        for acc in accounts:
            if self._stop.is_set():
                break
            try:
                self._check_one(acc)
            except Exception as e:
                logger.warning(f"Health check failed for {acc.email}: {e}")

    def _check_one(self, acc: Account) -> None:
        """Check account health: token validity + quota."""
        # Skip if disabled and no auto-refresh
        if acc.status in (AccountStatus.DISABLED, AccountStatus.EXPIRED):
            return

        # 1. Try to refresh token if needed
        if get_config().auto_refresh_token and acc.refresh_token:
            ok, new_token = self._refresh_token(acc)
            if ok and new_token:
                pool.update_account_token(acc.id, new_token, acc.refresh_token)
                logger.info(f"Token refreshed for {acc.email}")

        # 2. Test token by calling /v1/models
        ok, error = self._test_token(acc)
        if ok:
            db.update_account_status(acc.id, AccountStatus.ACTIVE)
            pool.add_account(acc)
        else:
            db.increment_failure(acc.id, error)
            if acc.failure_count >= get_config().max_failures_before_disable:
                db.update_account_status(acc.id, AccountStatus.ERROR, error)
                pool.remove_account(acc.id)

    def _refresh_token(self, acc: Account) -> tuple[bool, str]:
        """Refresh access_token using refresh_token."""
        if not acc.refresh_token:
            return False, ""
        try:
            resp = httpx.post(
                TOKEN_URL,
                data={
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": acc.refresh_token,
                    "redirect_uri": "http://localhost:1455/auth/callback",
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                new_access = data.get("access_token", "")
                new_refresh = data.get("refresh_token", acc.refresh_token)
                db.update_account_token(acc.id, new_access, new_refresh)
                return True, new_access
            else:
                logger.warning(f"Token refresh failed for {acc.email}: HTTP {resp.status_code}")
                return False, ""
        except Exception as e:
            logger.warning(f"Token refresh exception for {acc.email}: {e}")
            return False, ""

    def _test_token(self, acc: Account) -> tuple[bool, str]:
        """Quick API test."""
        try:
            resp = httpx.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {acc.access_token}"},
                timeout=15,
            )
            if resp.status_code == 200:
                return True, ""
            if resp.status_code == 401:
                return False, "invalid_token"
            if resp.status_code == 429:
                db.update_account_status(acc.id, AccountStatus.RATE_LIMITED)
                return False, "rate_limited"
            return False, f"http_{resp.status_code}"
        except Exception as e:
            return False, str(e)


# Singleton
checker = HealthChecker(interval=get_config().health_check_interval)
