"""
Codex Hub — OpenAI API proxy with account pool management.
"""

from __future__ import annotations

import signal
import sys
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.admin import router as admin_router
from api.gateway import router as gateway_router
from app.config import get_config
from models.database import db
from services.health_checker import checker
from utils.logger import configure_logging, get_logger

logger = get_logger("main")


def _signal_handler(signum: int, _) -> None:
    logger.info(f"Signal {signum} received, shutting down...")
    checker.stop()
    sys.exit(0)


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    configure_logging(level=cfg.log_level)
    logger.info("=" * 50)
    logger.info("Codex Hub starting...")
    logger.info("=" * 50)

    db.init()
    checker.start()
    logger.info("Database and health checker initialized")

    yield

    logger.info("Shutting down...")
    checker.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Codex Hub",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if get_config().log_level == "DEBUG" else None,
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(admin_router)
    app.include_router(gateway_router)

    return app


app = create_app()

if __name__ == "__main__":
    cfg = get_config()
    uvicorn.run(
        "main:app",
        host=cfg.host,
        port=cfg.port,
        workers=cfg.workers,
        log_config=None,
        access_log=False,
    )
