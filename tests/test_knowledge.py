"""Knowledge: content-addressed dedupe + provenance, FTS5 search, OR-Set retract/re-add."""

from __future__ import annotations

from atmcp.services import knowledge as k
from conftest import join


async def test_content_dedupe_and_provenance(team):
    a = await join(team, "alice")
    b = await join(team, "bob")

    r1 = await k.post_knowledge(a, "DB uses WAL", "SQLite is in WAL mode", tags=["db"])
    assert r1["deduped"] is False
    assert r1["contributor_count"] == 1

    # Bob posts the identical finding → same content_id, deduped, contributor_count grows.
    r2 = await k.post_knowledge(b, "DB uses WAL", "SQLite is in WAL mode", tags=["db"])
    assert r2["content_id"] == r1["content_id"]
    assert r2["deduped"] is True
    assert r2["contributor_count"] == 2

    items = await k.search_knowledge(a)
    assert len(items) == 1
    assert items[0]["contributor_count"] == 2


async def test_tag_normalization_collapses(team):
    a = await join(team, "alice")
    r1 = await k.post_knowledge(a, "T", "body", tags=["DB", "Perf"])
    r2 = await k.post_knowledge(a, "T", "body", tags=["perf", "db"])  # same set, different order/case
    assert r1["content_id"] == r2["content_id"]


async def test_fts_search(team):
    a = await join(team, "alice")
    await k.post_knowledge(a, "Redis leases", "We use SET NX for task leases", tags=["redis"])
    await k.post_knowledge(a, "Postgres notes", "unrelated content about indexes", tags=["pg"])

    hits = await k.search_knowledge(a, query="leases")
    assert len(hits) == 1
    assert "Redis" in hits[0]["title"]

    # tag filter
    tagged = await k.search_knowledge(a, tags=["redis"])
    assert len(tagged) == 1


async def test_retract_is_orset(team):
    a = await join(team, "alice")
    posted = await k.post_knowledge(a, "ephemeral", "to be retracted", tags=["x"])
    cid = posted["content_id"]

    assert len(await k.search_knowledge(a)) == 1
    rr = await k.retract_knowledge(a, cid)
    assert rr["ok"]
    assert len(await k.search_knowledge(a)) == 0  # tombstoned

    # Re-post the same content → add-bias revives it.
    again = await k.post_knowledge(a, "ephemeral", "to be retracted", tags=["x"])
    assert again["content_id"] == cid
    assert len(await k.search_knowledge(a)) == 1


async def test_retract_unknown(team):
    a = await join(team, "alice")
    rr = await k.retract_knowledge(a, "deadbeef")
    assert rr["ok"] is False
    assert rr["error"] == "unknown_content_id"
