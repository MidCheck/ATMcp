"""Content-addressing for knowledge: canonical JSON + sha256.

Two agents posting the *same* finding (same title/body/tags) produce the same
content_id, which gives automatic dedupe and provenance with no special logic.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no insignificant whitespace, UTF-8."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalize_tags(tags: list[str] | None) -> list[str]:
    """Lower-cased, de-duplicated, sorted tags for stable addressing."""
    if not tags:
        return []
    seen = {t.strip().lower() for t in tags if t and t.strip()}
    return sorted(seen)


def content_id(title: str, body: str, tags: list[str] | None) -> str:
    """sha256 over canonicalized {title, body, sorted(tags)} → hex digest."""
    payload = canonical_json(
        {"title": title, "body": body, "tags": normalize_tags(tags)}
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
