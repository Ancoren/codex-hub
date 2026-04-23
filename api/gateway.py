"""
OpenAI-compatible API gateway.
Routes incoming requests to the account pool.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import get_config
from core.proxy import OPENAI_ENDPOINTS, ProxyError, forward_request, forward_streaming
from utils.logger import get_logger

logger = get_logger("gateway")
router = APIRouter()


def verify_api_key(authorization: str = Header(None)) -> str:
    cfg = get_config()
    if not cfg.api_key:
        return ""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing API key")
    key = authorization.split(" ", 1)[1]
    if key != cfg.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_handler(
    request: Request,
    path: str,
    api_key: str = Depends(verify_api_key),
) -> Any:
    endpoint = f"/{path}"

    # Only proxy known OpenAI endpoints
    if endpoint not in OPENAI_ENDPOINTS:
        # Allow /v1/models and some others even if not explicitly listed
        if not any(endpoint.startswith(p) for p in OPENAI_ENDPOINTS):
            raise HTTPException(status_code=404, detail="Endpoint not supported")

    body = await request.body()

    # Detect streaming
    is_streaming = False
    if endpoint == "/v1/chat/completions" or endpoint == "/v1/responses":
        try:
            data = json.loads(body)
            is_streaming = data.get("stream", False)
        except Exception:
            pass

    try:
        if is_streaming:
            return StreamingResponse(
                forward_streaming(request, endpoint, body),
                media_type="text/event-stream",
            )
        else:
            resp = await forward_request(request, endpoint, body)
            content = await resp.aread() if hasattr(resp, "aread") else resp.content
            return JSONResponse(
                content=json.loads(content) if content else {},
                status_code=resp.status_code,
                headers={k: v for k, v in resp.headers.items()
                         if k.lower() in ("content-type", "x-request-id")},
            )
    except ProxyError as e:
        logger.error(f"Proxy error: {e.message}")
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        logger.exception("Gateway error")
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/health")
async def health() -> dict[str, Any]:
    from services.account_pool import pool
    active = pool.get_active_accounts()
    return {
        "status": "ok",
        "active_accounts": len(active),
        "version": "1.0.0",
    }
