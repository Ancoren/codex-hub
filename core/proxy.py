"""
Core API proxy logic.
Forwards requests to OpenAI backend with automatic account switching.
Supports both standard and streaming responses.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncGenerator

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse

from app.config import get_config
from models.database import Account, db
from services.account_pool import pool
from utils.logger import get_logger

logger = get_logger("proxy")

# Endpoints that should be forwarded
OPENAI_ENDPOINTS = {
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/completions",
    "/v1/embeddings",
    "/v1/models",
    "/v1/images/generations",
    "/v1/audio/transcriptions",
    "/v1/audio/translations",
}


class ProxyError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


async def forward_request(
    request: Request,
    endpoint: str,
    body: bytes | None = None,
) -> httpx.Response:
    """
    Forward a request to OpenAI using an account from the pool.
    Returns the raw upstream response.
    """
    cfg = get_config()
    strategy = cfg.strategy

    # Get account from pool
    account = pool.get(strategy)
    if not account:
        raise ProxyError("No active accounts available", 503)

    # Build headers
    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    headers["authorization"] = f"Bearer {account.access_token}"

    url = f"{cfg.openai_base_url}{endpoint}"
    method = request.method

    start = time.time()
    client: httpx.AsyncClient | None = None

    try:
        client = httpx.AsyncClient(timeout=cfg.openai_timeout)

        if method == "GET":
            resp = await client.get(url, headers=headers)
        elif method == "POST":
            content = body or await request.body()
            # Try to parse as JSON to set correct content-type
            try:
                json.loads(content)
                resp = await client.post(url, headers=headers, content=content)
            except (json.JSONDecodeError, UnicodeDecodeError):
                resp = await client.post(url, headers=headers, content=content)
        elif method == "DELETE":
            resp = await client.delete(url, headers=headers)
        else:
            resp = await client.request(method, url, headers=headers)

        latency = (time.time() - start) * 1000

        # Parse usage if available
        tokens_in = 0
        tokens_out = 0
        try:
            if resp.headers.get("content-type", "").startswith("application/json"):
                data = resp.json()
                usage = data.get("usage", {})
                tokens_in = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
                tokens_out = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)
        except Exception:
            pass

        # Log
        db.log_request(
            account_id=account.id,
            client_ip=request.client.host if request.client else "",
            endpoint=endpoint,
            model="",
            status_code=resp.status_code,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            latency_ms=latency,
            error="",
        )

        if resp.status_code == 200:
            pool.mark_success(account.id)
        elif resp.status_code in (401, 403):
            pool.mark_failure(account.id, f"auth_error_{resp.status_code}")
        elif resp.status_code == 429:
            pool.mark_failure(account.id, "rate_limited")
        else:
            pool.mark_failure(account.id, f"http_{resp.status_code}")

        return resp

    except httpx.TimeoutException:
        pool.mark_failure(account.id, "timeout")
        raise ProxyError("Upstream timeout", 504)
    except httpx.ConnectError as e:
        pool.mark_failure(account.id, f"connect_error: {e}")
        raise ProxyError("Upstream connection failed", 502)
    except Exception as e:
        pool.mark_failure(account.id, str(e))
        raise ProxyError(f"Proxy error: {e}", 502)
    finally:
        if client:
            await client.aclose()


async def forward_streaming(
    request: Request,
    endpoint: str,
    body: bytes,
) -> AsyncGenerator[bytes, None]:
    """
    Forward a streaming request and yield SSE chunks.
    """
    cfg = get_config()
    account = pool.get(cfg.strategy)
    if not account:
        yield b'data: {"error": "No active accounts"}\n\n'
        return

    headers = dict(request.headers)
    headers.pop("host", None)
    headers.pop("content-length", None)
    headers["authorization"] = f"Bearer {account.access_token}"

    url = f"{cfg.openai_base_url}{endpoint}"
    client: httpx.AsyncClient | None = None
    start = time.time()
    status_code = 200
    error_msg = ""

    try:
        client = httpx.AsyncClient(timeout=cfg.streaming_timeout)
        async with client.stream("POST", url, headers=headers, content=body) as resp:
            status_code = resp.status_code
            if status_code != 200:
                error_msg = f"upstream_{status_code}"
                body_bytes = await resp.aread()
                yield body_bytes
                return

            async for chunk in resp.aiter_bytes():
                yield chunk

        pool.mark_success(account.id)
    except Exception as e:
        error_msg = str(e)
        pool.mark_failure(account.id, error_msg)
        yield f'data: {{"error": "{error_msg}"}}\n\n'.encode()
    finally:
        latency = (time.time() - start) * 1000
        db.log_request(
            account_id=account.id,
            client_ip=request.client.host if request.client else "",
            endpoint=endpoint,
            model="",
            status_code=status_code,
            latency_ms=latency,
            error=error_msg,
        )
        if client:
            await client.aclose()
