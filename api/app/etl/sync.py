"""Sync orchestrator. CLI: `python -m app.etl.sync [stats|odds|adp|espn|all]`.

Each lane fails independently — a dead Odds API never blocks a stats refresh.
"""
from __future__ import annotations

import logging
import sys

from ..db import init_db, read_df

log = logging.getLogger(__name__)

SCOPES = ("stats", "odds", "adp", "espn")

# What actually moves between now and your next pick. A full "stats" pull walks
# three seasons of weekly data and takes minutes — unusable mid-draft — while
# these three are the signals that change during camp and on draft night.
DRAFT_DAY_SCOPES = ("depth", "injuries", "adp")


def run_sync(scope: str = "all") -> dict:
    init_db()
    results: dict[str, object] = {}
    if scope == "draft-day":
        scopes = DRAFT_DAY_SCOPES
    elif scope == "all":
        scopes = SCOPES
    else:
        scopes = (scope,)
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
            elif name == "depth":
                from .nfl_data import sync_depth_charts
                results["depth"] = sync_depth_charts()
            elif name == "injuries":
                from .nfl_data import sync_injuries
                results["injuries"] = sync_injuries()
            elif name == "espn":
                # Every configured league syncs, ESPN or Sleeper alike; one
                # unconfigured/failing league must not stop the others, so each
                # is reported independently.
                from ..config import league_ids
                from .platform import platform_of
                per_league = {}
                for lid in league_ids():
                    try:
                        if platform_of(lid) == "sleeper":
                            from .sleeper import sleeper_available, sync_sleeper
                            per_league[lid] = (sync_sleeper(lid) if sleeper_available(lid)
                                               else "skipped (no sleeper_league_id)")
                        else:
                            from .espn import espn_available, sync_espn
                            per_league[lid] = (sync_espn(lid) if espn_available(lid)
                                               else "skipped (no league_id — manual mode)")
                    except Exception as exc:
                        log.exception("league sync failed for %s", lid)
                        per_league[lid] = f"error: {exc}"
                results["espn"] = per_league
        except Exception as exc:
            log.exception("sync %s failed", name)
            results[name] = f"error: {exc}"

    # A sync updates the database; without this the process keeps serving
    # pre-sync projections until restart.
    from ..models import projections as proj
    proj._weekly_rows.cache_clear()
    proj._players.cache_clear()
    proj._snaps.cache_clear()
    proj._completed_seasons.cache_clear()
    from ..routers import draft as draft_router
    draft_router._season_proj_cache.clear()

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
