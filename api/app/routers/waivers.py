"""Waiver-wire rankings: ESPN free-agent cache, or top undrafted-by-ADP fallback."""
from __future__ import annotations

import logging

from fastapi import APIRouter

from ..config import current_season, league_config
from ..etl import espn as espn_etl
from ..etl import platform
from ..models import projections as proj
from ..models import vorp
from ..models import waivers as waivers_model
from ._common import all_players, records, resolve_roster_entry

log = logging.getLogger(__name__)
router = APIRouter(prefix="/waivers", tags=["waivers"])


def _my_roster_ids(league_id: str | None = None) -> list[str]:
    try:
        result = platform.get_my_roster(league_id)
    except Exception:
        return []
    roster = (result.get("team") or {}).get("roster") or []
    if not roster:
        return []
    players = all_players()
    ids = []
    for p in roster:
        pid = resolve_roster_entry(p, players)
        if pid:
            ids.append(pid)
    return ids


def _free_agent_ids(my_roster: list[str], season: int,
                    league_id: str | None = None) -> tuple[list[str], str | None]:
    platform_ids, platform_warning = platform.free_agents(league_id, season)
    if platform_ids is not None:
        return [pid for pid in platform_ids if pid not in my_roster], platform_warning

    cache = espn_etl.read_cache("free_agents", league_id)
    if cache and cache.get("data"):
        players = all_players()
        ids = []
        for p in cache["data"]:
            pid = resolve_espn_player(p.get("espn_id"), p.get("name", ""), p.get("position"), players)
            if pid and pid not in my_roster:
                ids.append(pid)
        if ids:
            return ids, None

    # fallback: top undrafted-by-ADP players not on my roster
    season_proj = proj.project_season(season)
    if season_proj.empty:
        return [], "no projections available"
    adp = vorp.resolve_adp(season)
    adp_ids = adp.dropna(subset=["player_id"]).sort_values("adp")["player_id"].tolist()
    ids = [pid for pid in adp_ids if pid not in my_roster][:150]
    if not ids:
        ids = [pid for pid in season_proj.player_id.tolist() if pid not in my_roster][:150]
    return ids, "ESPN free-agent cache unavailable — using ADP fallback"


@router.get("/rankings")
def get_rankings(week: int = 1, league_id: str | None = None) -> dict:
    season = current_season()
    my_roster = _my_roster_ids(league_id)
    fa_ids, warning = _free_agent_ids(my_roster, season, league_id)
    if not fa_ids:
        return {"rankings": [], "warning": warning or "no free agents available"}

    try:
        df = waivers_model.rank_free_agents(fa_ids, my_roster, season, week)
    except Exception as exc:
        log.warning("rank_free_agents failed: %s", exc)
        return {"rankings": [], "warning": str(exc)}

    out = {"rankings": records(df)}
    if warning:
        out["warning"] = warning
    return out
