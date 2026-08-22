"""Start/Sit optimizer endpoint."""
from __future__ import annotations

import logging

from fastapi import APIRouter

from ..config import current_season, league_config
from ..db import read_df
from ..etl import platform
from ..models import lineup as lineup_model
from ._common import all_players, records, resolve_roster_entry

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


def _roster_ids(league_id: str | None = None, players=None) -> list[str]:
    try:
        result = platform.get_my_roster(league_id)
    except Exception:
        return []
    roster = (result.get("team") or {}).get("roster") or []
    if not roster:
        return []
    if players is None:
        players = all_players()
    ids = []
    for p in roster:
        pid = resolve_roster_entry(p, players)
        if pid:
            ids.append(pid)
    return ids


def _current_lineup_ids(team: dict, players=None) -> list[str]:
    """Resolve `team["starters"]` to player_ids, regardless of platform shape.

    Sleeper's `starters` are already resolved player_ids, or `None` for an
    empty slot -- `None` must be skipped before the `isinstance` check below,
    not handed to `resolve_roster_entry`, which indexes into the entry with
    `"player_id" in entry` and raises `TypeError` on a non-dict. ESPN's
    starters are raw roster entries (`espn_id`/`name`/`position`) that need
    `resolve_roster_entry`, same as `_roster_ids` uses for the rest of the
    roster.
    """
    starters = team.get("starters") or []
    if players is None:
        players = all_players()
    ids = []
    for s in starters:
        if not s:
            continue
        pid = s if isinstance(s, str) else resolve_roster_entry(s, players)
        if pid:
            ids.append(pid)
    return ids


@router.get("/optimal")
def get_optimal(season: int | None = None, week: int | None = None,
                league_id: str | None = None) -> dict:
    season = season or current_season()
    week = week if week is not None else _default_week(season)

    players = all_players()
    roster_ids = _roster_ids(league_id, players=players)
    if not roster_ids:
        return {
            "starters": [], "bench": [], "current_total": None, "optimal_total": None,
            "delta": None, "close_calls": [], "week": week, "season": season,
            "warning": "no roster available — set up ESPN sync or manual roster",
        }

    try:
        result_roster = platform.get_my_roster(league_id)
        current = _current_lineup_ids(result_roster.get("team") or {}, players=players)
        result = lineup_model.optimize(roster_ids, season, week,
                                       current_lineup=current or None,
                                       cfg=league_config(league_id))
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
