"""Draft board + persisted draft state (data/draft_state.json)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import DATA_DIR, current_season, ensure_dirs, league_config
from ..models import projections as proj
from ..models import vorp
from ._common import records

log = logging.getLogger(__name__)
router = APIRouter(prefix="/draft", tags=["draft"])

STATE_PATH = DATA_DIR / "draft_state.json"

_season_proj_cache: dict[int, "object"] = {}


def _season_proj(season: int):
    if season not in _season_proj_cache:
        _season_proj_cache[season] = proj.project_season(season)
    return _season_proj_cache[season]


def _default_state() -> dict:
    return {"picks": [], "my_slot": None}


def _load_state() -> dict:
    ensure_dirs()
    if not STATE_PATH.exists():
        return _default_state()
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _default_state()


def _save_state(state: dict) -> None:
    ensure_dirs()
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")


def _drafted_ids(state: dict) -> set[str]:
    return {p["player_id"] for p in state["picks"]}


def _my_roster(state: dict) -> list[str]:
    return [p["player_id"] for p in state["picks"] if p.get("by_me")]


def _snake_pick_numbers(my_slot: int, teams: int, rounds: int) -> list[int]:
    """1-indexed overall pick numbers belonging to my_slot in a snake draft."""
    picks = []
    for rnd in range(1, rounds + 1):
        if rnd % 2 == 1:
            slot_pick = my_slot
        else:
            slot_pick = teams - my_slot + 1
        picks.append((rnd - 1) * teams + slot_pick)
    return picks


@router.get("/board")
def get_board(limit: int = 50) -> dict:
    state = _load_state()
    cfg = league_config()
    season = current_season()
    season_proj = _season_proj(season)

    drafted = _drafted_ids(state)
    my_roster = _my_roster(state)
    current_pick = len(state["picks"]) + 1

    board = vorp.vorp_board(
        drafted_ids=drafted, my_roster=my_roster, season=season,
        season_proj=season_proj, pick_number=current_pick,
    )

    my_next_pick = None
    my_slot = state.get("my_slot")
    if my_slot:
        teams = int(cfg["league"]["teams"])
        rounds = int(cfg["draft"]["rounds"])
        seq = _snake_pick_numbers(int(my_slot), teams, rounds)
        upcoming = [p for p in seq if p >= current_pick]
        my_next_pick = upcoming[0] if upcoming else None

    # positional run detector: last 8 picks
    last8 = state["picks"][-8:]
    runs: dict[str, int] = {}
    for p in last8:
        pos = p.get("position")
        if pos:
            runs[pos] = runs.get(pos, 0) + 1

    # tier depth per position (count of players remaining in tier 1/2)
    tier_depth: dict[str, dict] = {}
    if not board.empty:
        for pos, sub in board.groupby("pos"):
            tier_depth[pos] = sub.groupby("tier").size().to_dict()

    return {
        "board": records(board.head(limit)),
        "drafted_count": len(drafted),
        "current_pick": current_pick,
        "my_next_pick": my_next_pick,
        "runs": runs,
        "tier_depth": tier_depth,
    }


class PickBody(BaseModel):
    player_id: str
    by_me: bool = False


@router.post("/pick")
def post_pick(body: PickBody) -> dict:
    state = _load_state()
    if body.player_id in _drafted_ids(state):
        raise HTTPException(status_code=400, detail=f"{body.player_id} already drafted")
    season = current_season()
    season_proj = _season_proj(season)
    meta = season_proj[season_proj.player_id == body.player_id]
    pos = meta.iloc[0].position if not meta.empty else None
    name = meta.iloc[0]["name"] if not meta.empty else body.player_id
    state["picks"].append({
        "player_id": body.player_id, "by_me": body.by_me,
        "position": pos, "name": name,
    })
    _save_state(state)
    return {"picks": len(state["picks"])}


@router.post("/undo")
def post_undo() -> dict:
    state = _load_state()
    if state["picks"]:
        state["picks"].pop()
    _save_state(state)
    return {"picks": len(state["picks"])}


class ResetBody(BaseModel):
    my_slot: int | None = None


@router.post("/reset")
def post_reset(body: ResetBody | None = None) -> dict:
    state = _default_state()
    if body and body.my_slot is not None:
        state["my_slot"] = body.my_slot
    _save_state(state)
    return state


@router.get("/recommendation")
def get_recommendation() -> dict:
    state = _load_state()
    season = current_season()
    season_proj = _season_proj(season)
    drafted = _drafted_ids(state)
    my_roster = _my_roster(state)
    current_pick = len(state["picks"]) + 1

    board = vorp.vorp_board(
        drafted_ids=drafted, my_roster=my_roster, season=season,
        season_proj=season_proj, pick_number=current_pick,
    )
    if board.empty:
        return {"recommended": None, "alternatives": [], "warning": "no board available"}

    ranked = board.sort_values(["need_score", "vorp"], ascending=False).reset_index(drop=True)
    recommended = ranked.iloc[0]
    alternatives = ranked.iloc[1:5]
    return {
        "recommended": records(recommended.to_frame().T)[0],
        "alternatives": records(alternatives),
    }


@router.get("/my-roster")
def get_my_roster_endpoint() -> dict:
    state = _load_state()
    season = current_season()
    season_proj = _season_proj(season)
    cfg = league_config()
    starters = cfg["roster"]["starters"]
    flex_elig = cfg["roster"]["flex_eligible"]

    mine = [p for p in state["picks"] if p.get("by_me")]
    ids = [p["player_id"] for p in mine]
    meta = season_proj[season_proj.player_id.isin(ids)] if not season_proj.empty else season_proj

    filled: dict[str, int] = {}
    rows = []
    for p in mine:
        m = meta[meta.player_id == p["player_id"]] if meta is not None and not meta.empty else None
        pos = m.iloc[0].position if m is not None and not m.empty else p.get("position")
        proj_pts = float(m.iloc[0].proj_points) if m is not None and not m.empty else None
        filled[pos] = filled.get(pos, 0) + 1
        rows.append({
            "player_id": p["player_id"], "name": p.get("name"), "position": pos,
            "proj_points": proj_pts,
        })

    slot_suggestions = {}
    for pos, need in starters.items():
        if pos == "FLEX":
            flex_have = sum(filled.get(fp, 0) for fp in flex_elig) - sum(
                min(filled.get(fp, 0), starters.get(fp, 0)) for fp in flex_elig
            )
            slot_suggestions["FLEX"] = max(0, need - max(flex_have, 0))
        else:
            slot_suggestions[pos] = max(0, need - filled.get(pos, 0))

    return {"roster": rows, "slot_suggestions": slot_suggestions, "my_slot": state.get("my_slot")}
