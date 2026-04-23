"""
Import accounts from external sources into Codex Hub.
Supports:
- Direct JSON payload (batch import)
- SQLite database path (read from openai-cpa-optimized data.db)
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from utils.logger import get_logger

logger = get_logger("importer")


@dataclass
class ImportResult:
    total: int = 0
    success: int = 0
    skipped: int = 0
    failed: int = 0
    errors: List[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def import_from_sqlite(
    db_path: str,
    add_fn,
    skip_reg_only: bool = True,
) -> ImportResult:
    """
    Read accounts from an openai-cpa-optimized SQLite database
    and import them into Codex Hub.

    add_fn: callable(email, access_token, refresh_token, id_token, account_id, password) -> bool
    """
    result = ImportResult()
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT email, password, token_data FROM accounts ORDER BY id DESC")
        rows = cur.fetchall()
        conn.close()
    except Exception as exc:
        result.failed = 1
        result.errors.append(f"Failed to read SQLite: {exc}")
        return result

    for row in rows:
        result.total += 1
        email = row["email"]
        password = row["password"] or ""
        token_data_raw = row["token_data"] or "{}"

        try:
            token_data = json.loads(token_data_raw)
        except json.JSONDecodeError:
            result.failed += 1
            result.errors.append(f"{email}: invalid token_data JSON")
            continue

        if skip_reg_only and "仅注册成功" in token_data_raw:
            result.skipped += 1
            continue

        access_token = token_data.get("access_token", "")
        if not access_token:
            result.skipped += 1
            continue

        try:
            ok = add_fn(
                email=email,
                access_token=access_token,
                refresh_token=token_data.get("refresh_token", ""),
                id_token=token_data.get("id_token", ""),
                account_id=token_data.get("account_id", ""),
                password=password,
            )
            if ok:
                result.success += 1
            else:
                result.skipped += 1
        except Exception as exc:
            result.failed += 1
            result.errors.append(f"{email}: {exc}")

    return result


def import_from_json(
    items: List[Dict[str, Any]],
    add_fn,
    skip_existing: bool = True,
) -> ImportResult:
    """
    Import accounts from a JSON payload (used by /admin/import endpoint).
    """
    result = ImportResult()
    for item in items:
        result.total += 1
        email = item.get("email", "").strip()
        if not email:
            result.failed += 1
            result.errors.append("missing email")
            continue

        access_token = item.get("access_token", "")
        if not access_token:
            result.failed += 1
            result.errors.append(f"{email}: missing access_token")
            continue

        try:
            ok = add_fn(
                email=email,
                access_token=access_token,
                refresh_token=item.get("refresh_token", ""),
                id_token=item.get("id_token", ""),
                account_id=item.get("account_id", ""),
                password=item.get("password", ""),
            )
            if ok:
                result.success += 1
            else:
                result.skipped += 1
        except Exception as exc:
            result.failed += 1
            result.errors.append(f"{email}: {exc}")

    return result
