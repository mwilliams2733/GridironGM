"""Draft board + persisted draft state (data/draft_state.json)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import current_season, draft_state_path, league_config, resolve_league
from ..etl import espn as espn_etl
from ..models import projections as proj
from ..models import vorp
from ._common import all_players, records, resolve_espn_player

log = logging.getLogger(__name__)
router = APIRouter(prefix="/draft", tags=["draft"])

# Season projections are league-agnostic (scoring is shared), so this cache is
# keyed by season only and is reused across all leagues.
_season_proj_cache: dict[int, "object"] = {}


def _season_proj(season: int):
    if season not in _season_proj_cache:
        _season_proj_cache[season] = proj.project_season(season)
    return _season_proj_cache[season]


def _default_state() -> dict:
    return {"picks": [], "my_slot": None}


def _load_state(league_id: str | None = None) -> dict:
    path = draft_state_path(league_id)
    if not path.exists():
        return _default_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return _default_state()
    state.setdefault("picks", [])
    state.setdefault("my_slot", None)
    return state


def _save_state(state: dict, league_id: str | None = None) -> None:
    draft_state_path(league_id).write_text(json.dumps(state, indent=1), encoding="utf-8")


def _drafted_ids(state: dict) -> set[str]:
    return {p["player_id"] for p in state["picks"]}


def _my_roster(state: dict) -> list[str]:
    return [p["player_id"] for p in state["picks"] if p.get("by_me")]


def _my_slot(state: dict, cfg: dict) -> int | None:
    """Draft slot for my team: set during the draft, else from league config."""
    slot = state.get("my_slot")
    if slot is None:
        slot = (cfg.get("draft") or {}).get("my_slot")
    return int(slot) if slot else None


def _team_rosters(state: dict, cfg: dict) -> list[dict]:
    """Every team's picks so far, indexed by draft slot.

    This is what makes opponent needs visible — without attribution the app can
    only tell you a player is gone, not who has him.
    """
    teams = int(cfg["league"]["teams"])
    mine = _my_slot(state, cfg)
    by_slot: dict[int, list[dict]] = {s: [] for s in range(1, teams + 1)}
    for p in state["picks"]:
        slot = p.get("slot")
        if slot in by_slot:
            by_slot[slot].append(p)

    out = []
    for slot in range(1, teams + 1):
        picks = by_slot[slot]
        counts: dict[str, int] = {}
        for p in picks:
            pos = p.get("position")
            if pos:
                counts[pos] = counts.get(pos, 0) + 1
        out.append({
            "slot": slot,
            "name": f"Team {slot}" if slot != mine else "My Team",
            "is_me": slot == mine,
            "picks": picks,
            "position_counts": counts,
        })
    return out


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


def _round_and_slot(overall: int, teams: int) -> tuple[int, int]:
    """Inverse of `_snake_pick_numbers`: which round and draft slot owns pick `overall`.

    Odd rounds run 1..teams, even rounds reverse — so slot is mirrored on even
    rounds. `overall` is 1-indexed.
    """
    if teams < 1:
        raise ValueError("teams must be >= 1")
    rnd = (overall - 1) // teams + 1
    pos_in_round = (overall - 1) % teams + 1  # 1..teams, in draft order
    slot = pos_in_round if rnd % 2 == 1 else teams - pos_in_round + 1
    return rnd, slot


@router.get("/board")
def get_board(limit: int = 50, league_id: str | None = None) -> dict:
    lid = resolve_league(league_id)
    state = _load_state(lid)
    cfg = league_config(lid)
    season = current_season()
    season_proj = _season_proj(season)

    drafted = _drafted_ids(state)
    my_roster = _my_roster(state)
    current_pick = len(state["picks"]) + 1
    teams = int(cfg["league"]["teams"])

    board = vorp.vorp_board(
        drafted_ids=drafted, my_roster=my_roster, season=season,
        season_proj=season_proj, pick_number=current_pick, cfg=cfg,
    )

    my_next_pick = None
    my_slot = _my_slot(state, cfg)
    if my_slot:
        rounds = int(cfg["draft"]["rounds"])
        seq = _snake_pick_numbers(int(my_slot), teams, rounds)
        upcoming = [p for p in seq if p >= current_pick]
        my_next_pick = upcoming[0] if upcoming else None

    on_the_clock = _round_and_slot(current_pick, teams)[1]

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
        "current_round": _round_and_slot(current_pick, teams)[0],
        "on_the_clock": on_the_clock,
        "my_slot": my_slot,
        "my_next_pick": my_next_pick,
        "runs": runs,
        "tier_depth": tier_depth,
        "teams": _team_rosters(state, cfg),
        "league": {"id": cfg["league"]["id"], "name": cfg["league"]["name"], "teams": teams},
    }


class PickBody(BaseModel):
    player_id: str
    # Explicit team slot; omit to auto-assign from the snake order. Use it for
    # trades or when entering picks out of order.
    slot: int | None = None
    # Only consulted when the draft slot is unknown, so a league that never sets
    # my_slot still behaves as it did before attribution existed.
    by_me: bool = False


def _build_pick(state: dict, cfg: dict, player_id: str, slot: int | None,
                by_me_fallback: bool, source: str) -> dict:
    """Assemble one pick record, attributing it to a team."""
    teams = int(cfg["league"]["teams"])
    overall = len(state["picks"]) + 1
    rnd, auto_slot = _round_and_slot(overall, teams)
    if slot is not None:
        if not 1 <= slot <= teams:
            raise HTTPException(status_code=400, detail=f"slot must be 1..{teams}, got {slot}")
        assigned = slot
    else:
        assigned = auto_slot

    season_proj = _season_proj(current_season())
    meta = season_proj[season_proj.player_id == player_id]
    pos = meta.iloc[0].position if not meta.empty else None
    name = meta.iloc[0]["name"] if not meta.empty else player_id

    mine = _my_slot(state, cfg)
    by_me = (assigned == mine) if mine else by_me_fallback

    return {
        "overall": overall, "round": rnd, "slot": assigned,
        "player_id": player_id, "name": name, "position": pos,
        "by_me": by_me, "source": source,
    }


@router.post("/pick")
def post_pick(body: PickBody, league_id: str | None = None) -> dict:
    lid = resolve_league(league_id)
    state = _load_state(lid)
    if body.player_id in _drafted_ids(state):
        raise HTTPException(status_code=400, detail=f"{body.player_id} already drafted")
    pick = _build_pick(state, league_config(lid), body.player_id, body.slot, body.by_me, "manual")
    state["picks"].append(pick)
    _save_state(state, lid)
    return {"picks": len(state["picks"]), "pick": pick}


@router.post("/undo")
def post_undo(league_id: str | None = None) -> dict:
    lid = resolve_league(league_id)
    state = _load_state(lid)
    undone = state["picks"].pop() if state["picks"] else None
    _save_state(state, lid)
    return {"picks": len(state["picks"]), "undone": undone}


class ResetBody(BaseModel):
    my_slot: int | None = None


@router.post("/reset")
def post_reset(body: ResetBody | None = None, league_id: str | None = None) -> dict:
    lid = resolve_league(league_id)
    state = _default_state()
    if body and body.my_slot is not None:
        teams = int(league_config(lid)["league"]["teams"])
        if not 1 <= body.my_slot <= teams:
            raise HTTPException(status_code=400, detail=f"my_slot must be 1..{teams}")
        state["my_slot"] = body.my_slot
    _save_state(state, lid)
    return state


@router.post("/sync-espn")
def post_sync_espn(league_id: str | None = None) -> dict:
    """Pull picks from ESPN and merge them into this league's draft state.

    Idempotent: picks are keyed on their overall number, so re-syncing mid-draft
    only appends what's new. Manual entries are never overwritten — if you typed
    a pick ESPN also reports, yours stays and the ESPN copy is skipped.
    """
    lid = resolve_league(league_id)
    cfg = league_config(lid)
    teams = int(cfg["league"]["teams"])

    try:
        espn_picks = espn_etl.fetch_draft_picks(lid)
    except Exception as exc:
        log.warning("espn draft fetch failed for %s: %s", lid, exc)
        raise HTTPException(status_code=502, detail=f"ESPN draft fetch failed: {exc}") from exc

    state = _load_state(lid)
    existing_overall = {p.get("overall") for p in state["picks"]}
    existing_players = _drafted_ids(state)

    players = all_players()
    season_proj = _season_proj(current_season())
    slot_by_espn_team = {}
    for p in espn_picks:
        if p["round"] == 1 and p["round_pick"] and p["espn_team_id"] is not None:
            slot_by_espn_team.setdefault(int(p["espn_team_id"]), int(p["round_pick"]))

    mine = _my_slot(state, cfg)
    added, skipped, unresolved = 0, 0, []

    for p in espn_picks:
        overall = p["overall"]
        if overall is None or overall in existing_overall:
            skipped += 1
            continue
        pid = resolve_espn_player(p["espn_player_id"], "", None, players)
        if not pid:
            unresolved.append(p["espn_player_id"])
            continue
        if pid in existing_players:
            skipped += 1
            continue

        rnd, auto_slot = _round_and_slot(overall, teams)
        slot = slot_by_espn_team.get(int(p["espn_team_id"])) if p["espn_team_id"] is not None else None
        slot = slot or auto_slot

        meta = season_proj[season_proj.player_id == pid]
        state["picks"].append({
            "overall": overall,
            "round": p["round"] or rnd,
            "slot": slot,
            "player_id": pid,
            "name": meta.iloc[0]["name"] if not meta.empty else pid,
            "position": meta.iloc[0].position if not meta.empty else None,
            "by_me": slot == mine if mine else False,
            "source": "espn",
        })
        existing_overall.add(overall)
        existing_players.add(pid)
        added += 1

    state["picks"].sort(key=lambda r: r.get("overall") or 0)
    _save_state(state, lid)
    return {
        "league_id": lid, "added": added, "skipped": skipped,
        "unresolved": unresolved, "total_picks": len(state["picks"]),
    }


@router.get("/recommendation")
def get_recommendation(league_id: str | None = None) -> dict:
    lid = resolve_league(league_id)
    state = _load_state(lid)
    cfg = league_config(lid)
    season = current_season()
    season_proj = _season_proj(season)
    drafted = _drafted_ids(state)
    my_roster = _my_roster(state)
    current_pick = len(state["picks"]) + 1

    board = vorp.vorp_board(
        drafted_ids=drafted, my_roster=my_roster, season=season,
        season_proj=season_proj, pick_number=current_pick, cfg=cfg,
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
def get_my_roster_endpoint(league_id: str | None = None) -> dict:
    lid = resolve_league(league_id)
    state = _load_state(lid)
    season = current_season()
    season_proj = _season_proj(season)
    cfg = league_config(lid)
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

    return {"picks": rows, "slot_suggestions": slot_suggestions, "my_slot": _my_slot(state, cfg)}
