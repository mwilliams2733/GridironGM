"""Dashboard aggregate endpoint — one call renders the home page."""
from __future__ import annotations

import logging

from fastapi import APIRouter

from ..config import current_season, league_config, resolve_league
from ..etl import espn as espn_etl
from ..etl.odds import game_lines
from ..etl.sync import sync_status
from ._common import records
from . import league as league_router
from . import lineup as lineup_router
from . import waivers as waivers_router

log = logging.getLogger(__name__)
router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
def get_dashboard(league_id: str | None = None) -> dict:
    season = current_season()
    lid = resolve_league(league_id)
    cfg = league_config(lid)

    try:
        my_team = league_router.get_roster(lid)
    except Exception as exc:
        log.warning("dashboard: roster failed: %s", exc)
        my_team = {"mode": "none", "team": {"name": "My Team", "roster": []}, "warning": str(exc)}

    try:
        lineup = lineup_router.get_optimal(season=season, week=None, league_id=lid)
        optimal_lineup = {
            "week": lineup["week"], "season": lineup["season"],
            "optimal_total": lineup["optimal_total"], "current_total": lineup["current_total"],
            "delta": lineup["delta"], "starters": lineup["starters"],
        }
        if lineup.get("warning"):
            optimal_lineup["warning"] = lineup["warning"]
    except Exception as exc:
        log.warning("dashboard: lineup failed: %s", exc)
        optimal_lineup = {"warning": str(exc)}

    try:
        waivers = waivers_router.get_rankings(week=1, league_id=lid)
        top_waivers = waivers.get("rankings", [])[:5]
    except Exception as exc:
        log.warning("dashboard: waivers failed: %s", exc)
        top_waivers = []

    try:
        teams_cache = espn_etl.read_cache("teams", lid)
        standings = teams_cache.get("data", []) if teams_cache else []
    except Exception as exc:
        log.warning("dashboard: standings failed: %s", exc)
        standings = []

    try:
        # Scope to the same week the lineup is built for, so the board shows one
        # slate rather than every remaining game of the season.
        odds_week = optimal_lineup.get("week") or lineup_router._default_week(season)
        odds_board = records(game_lines(season=season, week=odds_week))
    except Exception as exc:
        log.warning("dashboard: odds failed: %s", exc)
        odds_board = []

    try:
        status = sync_status()
    except Exception as exc:
        log.warning("dashboard: sync_status failed: %s", exc)
        status = []

    return {
        "league": {"id": cfg["league"]["id"], "name": cfg["league"]["name"],
                   "teams": cfg["league"]["teams"]},
        "my_team": my_team,
        "optimal_lineup": optimal_lineup,
        "top_waivers": top_waivers,
        "standings": standings,
        "odds_board": odds_board,
        "sync_status": status,
    }
