"""League/roster endpoints: ESPN or manual roster mode, teams, players lookup."""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import current_season, league_config
from ..db import read_df
from ..etl import espn as espn_etl
from ..etl import platform
from ..models import projections as proj
from ._common import all_players, records, resolve_player_name, resolve_roster_entry

log = logging.getLogger(__name__)
router = APIRouter(prefix="/league", tags=["league"])


def _resolve_roster_players(roster: list[dict]) -> list[dict]:
    """Attach player_id to each roster entry, regardless of platform shape."""
    if not roster:
        return []
    players = all_players()
    out = []
    for p in roster:
        pid = resolve_roster_entry(p, players)
        out.append({**p, "player_id": pid})
    return out


@router.get("/roster")
def get_roster(league_id: str | None = None) -> dict:
    try:
        result = platform.get_my_roster(league_id)
    except Exception as exc:
        log.warning("get_my_roster failed: %s", exc)
        return {"mode": "none", "team": {"name": "My Team", "roster": []}, "warning": str(exc)}

    team = dict(result.get("team") or {})
    roster = team.get("roster") or []
    team["roster"] = _resolve_roster_players(roster)
    return {"mode": result.get("mode", "none"), "team": team}


class ManualPlayer(BaseModel):
    name: str
    position: str | None = None
    team: str | None = None


class ManualRosterBody(BaseModel):
    players: list[ManualPlayer]


@router.post("/roster")
def post_roster(body: ManualRosterBody, league_id: str | None = None) -> dict:
    players = all_players()
    resolved, unresolved = [], []
    for p in body.players:
        pid = resolve_player_name(p.name, p.position, players)
        if pid:
            resolved.append({"name": p.name, "position": p.position, "team": p.team, "player_id": pid})
        else:
            unresolved.append(p.name)
    espn_etl.set_manual_roster([p.dict() for p in body.players], league_id)
    return {"resolved": resolved, "unresolved": unresolved}


@router.get("/teams")
def get_teams(league_id: str | None = None) -> dict:
    cache = espn_etl.read_cache("teams", league_id)
    if not cache:
        return {"teams": [], "warning": "no ESPN sync yet"}
    return {"teams": cache.get("data", [])}


class MyTeamBody(BaseModel):
    team_id: int


@router.post("/my-team")
def post_my_team(body: MyTeamBody, league_id: str | None = None) -> dict:
    espn_etl.set_my_team(body.team_id, league_id)
    return {"team_id": body.team_id}


@router.get("/players")
def get_players(search: str = "", position: str = "", limit: int = 50) -> dict:
    season = current_season()
    try:
        season_proj = proj.project_season(season)
    except Exception as exc:
        log.warning("project_season failed: %s", exc)
        season_proj = None

    df = read_df("SELECT player_id, name, position, team, status FROM players")
    if position:
        df = df[df.position == position.upper()]
    if search:
        s = search.lower()
        df = df[df.name.str.lower().str.contains(s, na=False)]

    adp_df = read_df("SELECT player_name, position, adp FROM adp")
    if season_proj is not None and not season_proj.empty:
        proj_by_id = season_proj.set_index("player_id")[["proj_points"]]
        df = df.join(proj_by_id, on="player_id")
    else:
        df["proj_points"] = None

    if not adp_df.empty:
        df = df.merge(
            adp_df.rename(columns={"player_name": "name", "adp": "adp"}),
            on=["name", "position"], how="left",
        )
    else:
        df["adp"] = None

    df = df.sort_values(
        by=["proj_points"], ascending=False, na_position="last"
    ).head(limit)
    return {"players": records(df)}
