"""Identifiers, timestamps, and token helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from ulid import ULID


def new_id() -> str:
    """A lexicographically-sortable, time-prefixed unique id (ULID)."""
    return str(ULID())


def now_ms() -> int:
    """Server wall-clock in epoch milliseconds (used for audit/age, not ordering)."""
    return int(time.time() * 1000)


def gen_token(nbytes: int = 32) -> str:
    """A URL-safe secret (join token / dashboard token)."""
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Stable sha256 hex of a token; only the hash is persisted."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(token: str, token_hash: str) -> bool:
    """Constant-time comparison of a presented token against a stored hash."""
    return hmac.compare_digest(hash_token(token), token_hash)
