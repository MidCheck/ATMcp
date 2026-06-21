"""Runtime configuration, read once from the environment.

Kept dependency-free (no pydantic-settings) so it is trivial to reason about.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    admin_token: str = os.environ.get("ATMCP_ADMIN_TOKEN", "change-me-admin-token")
    sqlite_path: str = os.environ.get("ATMCP_SQLITE_PATH", "./data/atmcp.db")
    redis_url: str = os.environ.get("ATMCP_REDIS_URL", "redis://localhost:6379/0")
    public_url: str = os.environ.get("ATMCP_PUBLIC_URL", "http://localhost:8000")

    heartbeat_ttl_s: int = _int("ATMCP_HEARTBEAT_TTL_S", 30)
    heartbeat_interval_s: int = _int("ATMCP_HEARTBEAT_INTERVAL_S", 10)
    presence_healthy_s: int = _int("ATMCP_PRESENCE_HEALTHY_S", 15)
    presence_degraded_s: int = _int("ATMCP_PRESENCE_DEGRADED_S", 30)

    lease_ttl_s: int = _int("ATMCP_LEASE_TTL_S", 90)
    reaper_interval_s: int = _int("ATMCP_REAPER_INTERVAL_S", 5)
    task_max_attempts: int = _int("ATMCP_TASK_MAX_ATTEMPTS", 5)

    idem_ttl_s: int = _int("ATMCP_IDEM_TTL_S", 600)
    stream_maxlen: int = _int("ATMCP_STREAM_MAXLEN", 1000)
    dashboard_auth: bool = _bool("ATMCP_DASHBOARD_AUTH", False)

    # Inter-agent output stream (for "view another agent's output")
    output_retention_s: int = _int("ATMCP_OUTPUT_RETENTION_S", 7200)  # prune older chunks
    output_max_chunk: int = _int("ATMCP_OUTPUT_MAX_CHUNK", 8192)      # truncate huge chunks

    # Token/cost usage accounting (dashboard meters + rolling windows). Retain long
    # enough to back the widest rolling window; default 30 days.
    usage_retention_s: int = _int("ATMCP_USAGE_RETENTION_S", 2592000)

    # Command Guard audit retention (default 30 days).
    guard_retention_s: int = _int("ATMCP_GUARD_RETENTION_S", 2592000)


settings = Settings()
