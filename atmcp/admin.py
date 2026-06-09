"""Tiny admin CLI for local ops (works directly against SQLite — no server needed):

    python -m atmcp.admin create-team my-team
    python -m atmcp.admin create-team my-team --join-token secret123
"""

from __future__ import annotations

import argparse
import asyncio
import json

from atmcp import db
from atmcp.services import identity as identity_svc


async def _create(name: str, join_token: str | None, dashboard_token: str | None) -> dict:
    await db.init()
    try:
        return await identity_svc.create_team(name, join_token, dashboard_token)
    finally:
        await db.close()


def main() -> None:
    p = argparse.ArgumentParser(prog="atmcp.admin", description="ATMcp admin CLI")
    sub = p.add_subparsers(dest="cmd", required=True)
    ct = sub.add_parser("create-team", help="create a team and print its tokens")
    ct.add_argument("name")
    ct.add_argument("--join-token", default=None)
    ct.add_argument("--dashboard-token", default=None)

    args = p.parse_args()
    if args.cmd == "create-team":
        try:
            res = asyncio.run(_create(args.name, args.join_token, args.dashboard_token))
        except identity_svc.TeamExistsError:
            raise SystemExit(f"team already exists: {args.name}")
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
