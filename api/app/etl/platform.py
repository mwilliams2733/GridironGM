"""Dispatch roster and free-agent reads to the league's platform.

Routers consume one shape regardless of platform; the per-platform modules
(`etl/espn.py`, `etl/sleeper.py`) keep their own cache layouts.
"""
from __future__ import annotations

import logging

from ..config import league_config, resolve_league

log = logging.getLogger(__name__)

DEFAULT_PLATFORM = "espn"


def platform_of(league_id: str | None = None) -> str:
    return league_config(league_id)["league"].get("platform") or DEFAULT_PLATFORM


def get_my_roster(league_id: str | None = None) -> dict:
    lid = resolve_league(league_id)
    if platform_of(lid) == "sleeper":
        from . import sleeper
        cache = sleeper.read_cache("teams", lid)
        if not cache:
            return {"mode": "none", "team": {"name": "My Team", "roster": []}}
        rosters = cache["data"]
        # Match on owner_id, NOT roster_id and emphatically not `draft.my_slot`
        # (a snake DRAFT slot, unrelated to roster ids -- both are 1..teams, so
        # confusing them returns another manager's roster with no error at all).
        # owner_id is also stable if a commissioner rebuilds the league.
        user_id = league_config(lid)["league"].get("sleeper_user_id")
        if not user_id:
            log.warning(
                "sleeper %s: no sleeper_user_id configured — cannot identify my "
                "roster. Set leagues[%s].sleeper_user_id in league.yaml.", lid, lid)
            return {"mode": "none", "team": {"name": "My Team", "roster": []}}
        mine = next(
            (r for r in rosters if str(r.get("owner_id")) == str(user_id)), None)
        if mine is None:
            # Never fall through to another roster: a silently wrong team is far
            # worse than a loud failure on every Sundt surface.
            raise LookupError(
                f"sleeper {lid}: sleeper_user_id={user_id!r} owns none of the "
                f"{len(rosters)} synced rosters "
                f"(owners: {[r.get('owner_id') for r in rosters]})")
        return {"mode": "sleeper", "team": {
            "name": mine["name"],
            "roster": [{"player_id": p} for p in mine["players"]],
            "starters": mine["starters"],
            "faab_used": mine["faab_used"],
        }}
    from . import espn
    return espn.get_my_roster(lid)


def free_agents(league_id: str | None = None, season: int | None = None):
    """(player_ids, warning). Sleeper has no free-agent endpoint, so the pool is
    derived: every rostered player across the league is subtracted from the
    scored player universe, then capped -- with the cap reported, never silent.
    """
    lid = resolve_league(league_id)
    if platform_of(lid) != "sleeper":
        return None, None          # caller falls through to its ESPN path

    from . import sleeper
    cache = sleeper.read_cache("teams", lid)
    if not cache:
        return [], "no Sleeper sync yet"
    taken = {p for r in cache["data"] for p in r["players"]}

    from ..models import projections as proj
    s = proj.project_season(season or league_config(lid)["league"]["season"],
                            cfg=league_config(lid))
    pool = [pid for pid in s.sort_values("proj_points", ascending=False).player_id
            if pid not in taken]
    capped, limit = pool[:200], 200
    warning = (f"showing top {limit} of {len(pool)} available"
               if len(pool) > limit else None)
    return capped, warning
