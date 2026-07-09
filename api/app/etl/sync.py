"""Sync orchestrator. CLI: `python -m app.etl.sync [stats|odds|adp|espn|all]`.

Each lane fails independently — a dead Odds API never blocks a stats refresh.
"""
from __future__ import annotations

import logging
import sys

from ..db import init_db, read_df

log = logging.getLogger(__name__)

SCOPES = ("stats", "odds", "adp", "espn")


def run_sync(scope: str = "all") -> dict:
    init_db()
    results: dict[str, object] = {}
    scopes = SCOPES if scope == "all" else (scope,)
    for name in scopes:
        try:
            if name == "stats":
                from .nfl_data import sync_all_stats
                results["stats"] = sync_all_stats()
            elif name == "odds":
                from .odds import sync_odds
                results["odds"] = sync_odds()
            elif name == "adp":
                from .adp import sync_adp
                results["adp"] = sync_adp()
            elif name == "espn":
                from .espn import espn_available, sync_espn
                results["espn"] = sync_espn() if espn_available() else "skipped (no league_id — manual mode)"
        except Exception as exc:
            log.exception("sync %s failed", name)
            results[name] = f"error: {exc}"
    return results


def sync_status() -> list[dict]:
    init_db()
    df = read_df("SELECT scope, last_synced, detail FROM sync_log ORDER BY scope")
    return df.to_dict(orient="records")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    scope = sys.argv[1] if len(sys.argv) > 1 else "all"
    out = run_sync(scope)
    for k, v in out.items():
        print(f"{k}: {v}")
