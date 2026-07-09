"""ESPN league sync via espn-api, with graceful manual-entry fallback.

Private-league auth uses espn_s2 + SWID from the local secrets file. If any
credential is missing or the API fails, the app degrades to manual mode: the
user pastes a roster which is stored in data/espn_cache/manual_roster.json.
All ESPN state is cached as JSON so the app works offline after a sync.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..config import ESPN_CACHE_DIR, ensure_dirs, espn_settings
from ..db import mark_synced

log = logging.getLogger(__name__)


def _cache_path(name: str) -> Path:
    ensure_dirs()
    return ESPN_CACHE_DIR / f"{name}.json"


def _write_cache(name: str, payload) -> None:
    _cache_path(name).write_text(
        json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "data": payload}, indent=1),
        encoding="utf-8",
    )


def read_cache(name: str):
    path = _cache_path(name)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def espn_available() -> bool:
    s = espn_settings()
    return bool(s["league_id"])


def _player_dict(p) -> dict:
    return {
        "espn_id": getattr(p, "playerId", None),
        "name": getattr(p, "name", None),
        "position": getattr(p, "position", None),
        "team": getattr(p, "proTeam", None),
        "injury_status": getattr(p, "injuryStatus", None),
        "lineup_slot": getattr(p, "lineupSlot", None),
        "percent_owned": getattr(p, "percent_owned", None),
        "projected_total": getattr(p, "projected_total_points", None),
    }


def sync_espn() -> dict:
    """Pull rosters, free agents, matchups, standings. Returns a summary dict."""
    s = espn_settings()
    if not s["league_id"]:
        raise RuntimeError("ESPN league_id not configured — manual mode available.")

    from espn_api.football import League

    league = League(
        league_id=int(s["league_id"]), year=int(s["year"]),
        espn_s2=s["espn_s2"], swid=s["swid"],
    )

    teams = [
        {
            "team_id": t.team_id, "name": t.team_name, "abbrev": getattr(t, "team_abbrev", ""),
            "wins": t.wins, "losses": t.losses, "points_for": t.points_for,
            "points_against": t.points_against, "standing": t.standing,
            "roster": [_player_dict(p) for p in t.roster],
        }
        for t in league.teams
    ]
    free_agents = [_player_dict(p) for p in league.free_agents(size=200)]
    week = getattr(league, "current_week", None)
    matchups = []
    try:
        for m in league.box_scores(week):
            matchups.append({
                "home_team": m.home_team.team_name if m.home_team else None,
                "away_team": m.away_team.team_name if m.away_team else None,
                "home_score": m.home_score, "away_score": m.away_score,
            })
    except Exception as exc:
        log.warning("box scores unavailable: %s", exc)

    _write_cache("teams", teams)
    _write_cache("free_agents", free_agents)
    _write_cache("matchups", {"week": week, "matchups": matchups})
    mark_synced("espn", f"{len(teams)} teams, {len(free_agents)} FAs, week={week}")
    return {"teams": len(teams), "free_agents": len(free_agents), "week": week}


def get_my_roster() -> dict:
    """My roster: ESPN cache if synced (first team owned by user unattributable —
    use manual selection), else manual roster, else empty manual-mode shell."""
    manual = read_cache("manual_roster")
    my_team = read_cache("my_team")  # {"team_id": N} chosen in UI
    teams = read_cache("teams")
    if teams and my_team:
        tid = my_team["data"]["team_id"]
        for t in teams["data"]:
            if t["team_id"] == tid:
                return {"mode": "espn", "team": t}
    if manual:
        return {"mode": "manual", "team": {"name": "My Team (manual)", "roster": manual["data"]}}
    return {"mode": "none", "team": {"name": "My Team", "roster": []}}


def set_my_team(team_id: int) -> None:
    _write_cache("my_team", {"team_id": team_id})


def set_manual_roster(players: list[dict]) -> None:
    """players: [{name, position, team?}] — resolved against players table by routers."""
    _write_cache("manual_roster", players)
    mark_synced("espn_manual", f"{len(players)} players")
