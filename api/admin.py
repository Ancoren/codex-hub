"""
Admin API for managing accounts and viewing stats.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import get_config
from models.database import Account, AccountStatus, db
from services.account_pool import pool
from utils.logger import get_logger

logger = get_logger("admin")
router = APIRouter(prefix="/admin")

_authorized_tokens: set[str] = set()


class LoginReq(BaseModel):
    password: str


class AddAccountReq(BaseModel):
    email: str = Field(..., min_length=3)
    access_token: str = Field(..., min_length=10)
    refresh_token: str = ""
    id_token: str = ""
    account_id: str = ""
    password: str = ""


class AccountResp(BaseModel):
    id: int
    email: str
    status: str
    total_requests: int
    total_tokens_input: int
    total_tokens_output: int
    last_used_at: str | None
    created_at: str | None


class StatsResp(BaseModel):
    total_accounts: int
    active_accounts: int
    total_requests: int
    total_tokens_input: int
    total_tokens_output: int


def verify_admin(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    token = auth.split(" ", 1)[1]
    if token not in _authorized_tokens:
        raise HTTPException(status_code=401, detail="Invalid token")
    return token


@router.post("/login")
async def login(req: LoginReq) -> dict[str, Any]:
    if req.password == get_config().admin_password:
        import secrets
        token = secrets.token_hex(16)
        _authorized_tokens.add(token)
        return {"status": "success", "token": token}
    raise HTTPException(status_code=401, detail="Invalid password")


@router.get("/stats", response_model=StatsResp)
async def get_stats(_: str = Depends(verify_admin)) -> StatsResp:
    accounts = db.list_accounts()
    active = [a for a in accounts if a.status == AccountStatus.ACTIVE]
    return StatsResp(
        total_accounts=len(accounts),
        active_accounts=len(active),
        total_requests=sum(a.total_requests for a in accounts),
        total_tokens_input=sum(a.total_tokens_input for a in accounts),
        total_tokens_output=sum(a.total_tokens_output for a in accounts),
    )


@router.get("/accounts")
async def list_accounts(
    status: str | None = None,
    _: str = Depends(verify_admin),
) -> list[dict[str, Any]]:
    accounts = db.list_accounts(status=status)
    return [a.to_dict() for a in accounts]


@router.post("/accounts")
async def add_account(
    req: AddAccountReq,
    _: str = Depends(verify_admin),
) -> dict[str, Any]:
    existing = db.get_account_by_email(req.email)
    if existing:
        raise HTTPException(status_code=409, detail="Account already exists")
    acc = db.add_account(
        email=req.email,
        access_token=req.access_token,
        refresh_token=req.refresh_token,
        id_token=req.id_token,
        account_id=req.account_id,
        password=req.password,
    )
    pool.add_account(acc)
    return {"status": "success", "account": acc.to_dict()}


@router.delete("/accounts/{account_id}")
async def delete_account(
    account_id: int,
    _: str = Depends(verify_admin),
) -> dict[str, Any]:
    ok = db.delete_account(account_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Account not found")
    pool.remove_account(account_id)
    return {"status": "success"}


@router.post("/accounts/{account_id}/refresh")
async def refresh_account(
    account_id: int,
    _: str = Depends(verify_admin),
) -> dict[str, Any]:
    from services.health_checker import checker
    acc = db.get_account(account_id)
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    checker._check_one(acc)
    return {"status": "success", "account": acc.to_dict()}


@router.get("/logs")
async def get_logs(
    account_id: int | None = None,
    limit: int = 100,
    _: str = Depends(verify_admin),
) -> list[dict[str, Any]]:
    return db.get_logs(account_id=account_id, limit=limit)


@router.post("/pool/refresh")
async def refresh_pool(_: str = Depends(verify_admin)) -> dict[str, Any]:
    pool.refresh()
    return {"status": "success", "active_count": len(pool.get_active_accounts())}
