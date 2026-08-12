"""Start/Sit optimizer endpoint."""
from __future__ import annotations

import logging

from fastapi import APIRouter

from ..config import current_season
from ..db import read_df
from ..etl import espn as espn_etl
from ..models import lineup as lineup_model
from ._common import all_players, records, resolve_espn_player

log = logging.getLogger(__name__)
router = APIRouter(prefix="/lineup", tags=["lineup"])

REG_SEASON_WEEKS = 18


def _default_week(season: int) -> int:
    """Next unplayed week for `season` (first week with no weekly_stats rows), else 1."""
    played = read_df(
        "SELECT DISTINCT week FROM weekly_stats WHERE season=?", (season,)
    )
    if played.empty:
        return 1
    played_weeks = set(int(w) for w in played["week"])
    for w in range(1, REG_SEASON_WEEKS + 1):
        if w not in played_weeks:
            return w
    return 1


def _roster_ids(league_id: str | None = None) -> list[str]:
    try:
        result = espn_etl.get_my_roster(league_id)
    except Exception:
        return []
    roster = (result.get("team") or {}).get("roster") or []
    if not roster:
        return []
    players = all_players()
    ids = []
    for p in roster:
        pid = resolve_espn_player(p.get("espn_id"), p.get("name", ""), p.get("position"), players)
        if pid:
            ids.append(pid)
    return ids


@router.get("/optimal")
def get_optimal(season: int | None = None, week: int | None = None,
                league_id: str | None = None) -> dict:
    season = season or current_season()
    week = week if week is not None else _default_week(season)

    roster_ids = _roster_ids(league_id)
    if not roster_ids:
        return {
            "starters": [], "bench": [], "current_total": None, "optimal_total": None,
            "delta": None, "close_calls": [], "week": week, "season": season,
            "warning": "no roster available — set up ESPN sync or manual roster",
        }

    try:
        result = lineup_model.optimize(roster_ids, season, week)
    except Exception as exc:
        log.warning("lineup optimize failed: %s", exc)
        return {
            "starters": [], "bench": [], "current_total": None, "optimal_total": None,
            "delta": None, "close_calls": [], "week": week, "season": season,
            "warning": str(exc),
        }

    starters = [{"slot": slot, "player": player} for slot, player in result.slots.items()]
    return {
        "starters": starters,
        "bench": result.bench,
        "current_total": result.current_total,
        "optimal_total": result.total,
        "delta": result.delta,
        "close_calls": result.close_calls,
        "week": week,
        "season": season,
    }
