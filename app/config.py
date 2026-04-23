"""
Pydantic Settings for Codex Hub.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings


class AppConfig(BaseSettings):
    model_config = {"env_prefix": "HUB_", "extra": "ignore"}

    # Server
    host: str = "0.0.0.0"
    port: int = 8080
    workers: int = 1

    # Security
    admin_password: str = Field(default="admin", min_length=1)
    api_key: str = Field(default="", description="Optional global API key for gateway access")

    # Database
    db_url: str = "sqlite:///data/hub.db"

    # Balancer
    strategy: Literal["round_robin", "least_used", "random", "priority"] = "least_used"
    health_check_interval: int = Field(default=300, ge=30, description="Seconds between health checks")
    auto_refresh_token: bool = True
    max_failures_before_disable: int = 3

    # OpenAI upstream
    openai_base_url: str = "https://api.openai.com"
    openai_timeout: int = 120
    streaming_timeout: int = 300

    # Rate limiting (per client IP)
    rate_limit_rpm: int = 60

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    def get_db_url(self) -> str:
        return self.db_url


_config_instance: AppConfig | None = None


def get_config() -> AppConfig:
    global _config_instance
    if _config_instance is None:
        _config_instance = AppConfig()
    return _config_instance


def reload_config() -> AppConfig:
    global _config_instance
    _config_instance = AppConfig()
    return _config_instance
