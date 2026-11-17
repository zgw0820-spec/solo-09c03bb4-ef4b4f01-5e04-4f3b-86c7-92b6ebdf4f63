"""Runtime configuration loaded from environment variables.

The same image runs both roles; behaviour is selected with ``DNS_ROLE``
(``primary`` or ``backup``). Each container gets its own SQLite file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(key: str, default: str) -> str:
    value = os.environ.get(key)
    return value if value not in (None, "") else default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    role: str = _env("DNS_ROLE", "primary")
    # Fixed intranet zone served by this deployment.
    dns_zone: str = _env("DNS_ZONE", "internal.local")
    dns_host: str = _env("DNS_HOST", "0.0.0.0")
    dns_port: int = _env_int("DNS_PORT", 5053)
    api_host: str = _env("API_HOST", "0.0.0.0")
    api_port: int = _env_int("API_PORT", 8000)
    db_path: str = _env("DB_PATH", "/data/dns.db")
    # Backup only: base URL of the primary HTTP API.
    primary_api_url: str = _env("PRIMARY_API_URL", "http://primary:8000")
    # Backup background synchronisation interval (seconds).
    sync_interval: float = float(_env_int("SYNC_INTERVAL", 5))
    http_timeout: float = float(_env_int("HTTP_TIMEOUT", 10))
    max_records: int = _env_int("MAX_RECORDS", 8)

    @property
    def is_primary(self) -> bool:
        return self.role == "primary"


settings = Settings()
