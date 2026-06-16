"""Token/cost usage accounting — the per-agent "how much have we spent" meter.

A worker reports one row per model run (the `usage` + `total_cost_usd` block that
`claude -p --output-format json` already returns, so it costs nothing extra to
capture). Rows are append-only in `usage_events`; reads aggregate per agent and
per team, plus rolling windows that mirror Claude's rate-limit windows (5h / 7d)
so the dashboard can show "how close are we to the limit". Old rows are pruned by
the reaper. This is monitoring only — the hard budget brake lives in the poller,
which is the thing that can actually stop claiming work.
"""

from __future__ import annotations

from typing import Any

from atmcp import db, events
from atmcp.ids import now_ms

# Rolling windows that mirror the Claude subscription rate-limit windows.
WINDOW_5H_MS = 5 * 3600 * 1000
WINDOW_7D_MS = 7 * 24 * 3600 * 1000


def _i(v: Any) -> int:
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _f(v: Any) -> float:
    try:
        return max(0.0, float(v or 0.0))
    except (TypeError, ValueError):
        return 0.0


async def record_usage(
    team_id: str,
    agent_id: str,
    directive_id: str | None,
    model: str | None,
    input_tokens: Any,
    output_tokens: Any,
    cache_read: Any,
    cache_creation: Any,
    cost_usd: Any,
    num_turns: Any,
    duration_ms: Any,
) -> dict[str, Any]:
    """Append one execution's usage. Drops a stale/foreign directive tag to NULL
    (never loses the row over a bad id) and emits a usage_reported event so the
    dashboard refreshes its meters live."""
    inp, out = _i(input_tokens), _i(output_tokens)
    cr, cc = _i(cache_read), _i(cache_creation)
    cost = _f(cost_usd)
    turns, dur = _i(num_turns), _i(duration_ms)
    now = now_ms()
    async with db.transaction() as tx:
        if directive_id is not None:
            ref = await tx.fetchval(
                "SELECT 1 FROM directives WHERE team_id=? AND directive_id=?",
                (team_id, directive_id),
            )
            if not ref:
                directive_id = None
        await tx.execute(
            "INSERT INTO usage_events(team_id,agent_id,directive_id,model,input_tokens,"
            "output_tokens,cache_read,cache_creation,cost_usd,num_turns,duration_ms,ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (team_id, agent_id, directive_id, model, inp, out, cr, cc, cost, turns, dur, now),
        )
        await events.append(
            tx, team_id, events.USAGE_REPORTED, "agent", agent_id, agent_id,
            {"model": model, "input_tokens": inp, "output_tokens": out,
             "cost_usd": round(cost, 6), "directive_id": directive_id},
        )
    return {"ok": True, "agent_id": agent_id, "cost_usd": round(cost, 6),
            "tokens": inp + out}


def _zero() -> dict[str, Any]:
    return {"input_tokens": 0, "output_tokens": 0, "cache_read": 0, "cache_creation": 0,
            "cost_usd": 0.0, "runs": 0, "last_ts": 0}


async def team_usage(team_id: str) -> dict[str, Any]:
    """Per-agent + team totals, plus 5h/7d rolling-window cost/tokens/runs."""
    now = now_ms()
    rows = await db.fetchall(
        "SELECT agent_id, "
        "SUM(input_tokens) AS inp, SUM(output_tokens) AS out, "
        "SUM(cache_read) AS cr, SUM(cache_creation) AS cc, "
        "SUM(cost_usd) AS cost, COUNT(*) AS runs, MAX(ts) AS last_ts "
        "FROM usage_events WHERE team_id=? GROUP BY agent_id",
        (team_id,),
    )

    async def _window(ms: int) -> dict[str, dict[str, Any]]:
        wr = await db.fetchall(
            "SELECT agent_id, SUM(cost_usd) AS cost, "
            "SUM(input_tokens)+SUM(output_tokens) AS toks, COUNT(*) AS runs "
            "FROM usage_events WHERE team_id=? AND ts>=? GROUP BY agent_id",
            (team_id, now - ms),
        )
        return {r["agent_id"]: {"cost_usd": round(_f(r["cost"]), 6),
                                "tokens": _i(r["toks"]), "runs": _i(r["runs"])} for r in wr}

    w5, w7 = await _window(WINDOW_5H_MS), await _window(WINDOW_7D_MS)

    agents: dict[str, Any] = {}
    team = _zero()
    team["window_5h"] = {"cost_usd": 0.0, "tokens": 0, "runs": 0}
    team["window_7d"] = {"cost_usd": 0.0, "tokens": 0, "runs": 0}
    for r in rows:
        aid = r["agent_id"]
        a = {
            "input_tokens": _i(r["inp"]), "output_tokens": _i(r["out"]),
            "cache_read": _i(r["cr"]), "cache_creation": _i(r["cc"]),
            "cost_usd": round(_f(r["cost"]), 6), "runs": _i(r["runs"]),
            "last_ts": _i(r["last_ts"]),
            "window_5h": w5.get(aid, {"cost_usd": 0.0, "tokens": 0, "runs": 0}),
            "window_7d": w7.get(aid, {"cost_usd": 0.0, "tokens": 0, "runs": 0}),
        }
        agents[aid] = a
        for k in ("input_tokens", "output_tokens", "cache_read", "cache_creation", "runs"):
            team[k] += a[k]
        team["cost_usd"] += a["cost_usd"]
        team["last_ts"] = max(team["last_ts"], a["last_ts"])
        for w in ("window_5h", "window_7d"):
            team[w]["cost_usd"] += a[w]["cost_usd"]
            team[w]["tokens"] += a[w]["tokens"]
            team[w]["runs"] += a[w]["runs"]
    team["cost_usd"] = round(team["cost_usd"], 6)
    for w in ("window_5h", "window_7d"):
        team[w]["cost_usd"] = round(team[w]["cost_usd"], 6)
    return {"agents": agents, "team": team}


async def prune_expired(retention_ms: int) -> int:
    cutoff = now_ms() - retention_ms
    async with db.transaction() as tx:
        cur = await tx.execute("DELETE FROM usage_events WHERE ts < ?", (cutoff,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
