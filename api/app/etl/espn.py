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

from ..config import espn_settings, league_cache_dir, resolve_league
from ..db import mark_synced

log = logging.getLogger(__name__)


def _cache_path(name: str, league_id: str | None = None) -> Path:
    return league_cache_dir(league_id) / f"{name}.json"


def _write_cache(name: str, payload, league_id: str | None = None) -> None:
    _cache_path(name, league_id).write_text(
        json.dumps({"fetched_at": datetime.now(timezone.utc).isoformat(), "data": payload}, indent=1),
        encoding="utf-8",
    )


def read_cache(name: str, league_id: str | None = None):
    path = _cache_path(name, league_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def espn_available(league_id: str | None = None) -> bool:
    s = espn_settings(league_id)
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


def sync_espn(league_id: str | None = None) -> dict:
    """Pull rosters, free agents, matchups, standings. Returns a summary dict."""
    lid = resolve_league(league_id)
    s = espn_settings(lid)
    if not s["league_id"]:
        raise RuntimeError(f"ESPN league_id not configured for '{lid}' — manual mode available.")

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

    _write_cache("teams", teams, lid)
    _write_cache("free_agents", free_agents, lid)
    _write_cache("matchups", {"week": week, "matchups": matchups}, lid)
    mark_synced(f"espn:{lid}", f"{len(teams)} teams, {len(free_agents)} FAs, week={week}")
    return {"league_id": lid, "teams": len(teams), "free_agents": len(free_agents), "week": week}


def fetch_draft_picks(league_id: str | None = None) -> list[dict]:
    """Live draft picks from ESPN's mDraftDetail view.

    Deliberately does NOT use `espn_api`'s `League.draft`: its `_fetch_draft`
    early-returns unless `draftDetail.drafted` is true, and ESPN only sets that
    flag once a draft has *finished* — which would make it useless for the case
    that matters, reading picks while the draft is running. We read
    `draftDetail.picks` directly, whatever the flag says.

    Returns raw ESPN picks: [{espn_team_id, espn_player_id, round, round_pick,
    overall, keeper}]. Player id resolution happens in the router, which owns
    the player table.
    """
    lid = resolve_league(league_id)
    s = espn_settings(lid)
    if not s["league_id"]:
        raise RuntimeError(f"ESPN league_id not configured for '{lid}'.")

    from espn_api.requests.espn_requests import EspnFantasyRequests

    cookies = None
    if s.get("espn_s2") and s.get("swid"):
        cookies = {"espn_s2": s["espn_s2"], "SWID": s["swid"]}

    client = EspnFantasyRequests(
        sport="nfl", year=int(s["year"]), league_id=int(s["league_id"]), cookies=cookies
    )
    data = client.league_get(params={"view": "mDraftDetail"})
    detail = (data or {}).get("draftDetail") or {}
    picks = detail.get("picks") or []

    out = []
    for p in picks:
        overall = p.get("overallPickNumber")
        if overall is None:
            # Older payloads omit it; derive from round + pick within round.
            rnd, rp = p.get("roundId"), p.get("roundPickNumber")
            overall = None if rnd is None or rp is None else p.get("overallPickNumber")
        out.append({
            "espn_team_id": p.get("teamId"),
            "espn_player_id": p.get("playerId"),
            "round": p.get("roundId"),
            "round_pick": p.get("roundPickNumber"),
            "overall": overall,
            "keeper": bool(p.get("keeper")),
        })
    out.sort(key=lambda r: (r["overall"] is None, r["overall"] or 0))
    log.info("espn draft %s: %d picks (drafted flag=%s)", lid, len(out), detail.get("drafted"))
    return out


def team_slot_map(league_id: str | None = None) -> dict[int, int]:
    """ESPN teamId -> draft slot (1..teams), derived from round 1's pick order.

    ESPN team ids are arbitrary and unrelated to draft position, so the only
    reliable mapping is the order teams actually picked in round one.
    """
    picks = fetch_draft_picks(league_id)
    mapping: dict[int, int] = {}
    for p in picks:
        if p["round"] == 1 and p["round_pick"] and p["espn_team_id"] is not None:
            mapping.setdefault(int(p["espn_team_id"]), int(p["round_pick"]))
    return mapping


# espn-api lineup slot names for bench/IR -- never "started" so never in `starters`.
_NON_STARTING_SLOTS = {"BE", "IR", "BN"}


def _starters_from_roster(roster: list[dict]) -> list[dict]:
    """Roster entries actually in a starting `lineup_slot` (not bench/IR).

    Kept as full entries (not resolved ids) -- the router resolves them with
    the same `resolve_roster_entry` it already uses for the rest of the
    roster, so identity resolution logic lives in exactly one place.
    """
    return [p for p in roster if p.get("lineup_slot") not in _NON_STARTING_SLOTS]


def get_my_roster(league_id: str | None = None) -> dict:
    """My roster: ESPN cache if synced (first team owned by user unattributable —
    use manual selection), else manual roster, else empty manual-mode shell."""
    lid = resolve_league(league_id)
    manual = read_cache("manual_roster", lid)
    my_team = read_cache("my_team", lid)  # {"team_id": N} chosen in UI
    teams = read_cache("teams", lid)
    if teams and my_team:
        tid = my_team["data"]["team_id"]
        for t in teams["data"]:
            if t["team_id"] == tid:
                team = {**t, "starters": _starters_from_roster(t.get("roster") or [])}
                return {"mode": "espn", "team": team}
    if manual:
        return {"mode": "manual", "team": {"name": "My Team (manual)", "roster": manual["data"]}}
    return {"mode": "none", "team": {"name": "My Team", "roster": []}}


def set_my_team(team_id: int, league_id: str | None = None) -> None:
    _write_cache("my_team", {"team_id": team_id}, league_id)


def set_manual_roster(players: list[dict], league_id: str | None = None) -> None:
    """players: [{name, position, team?}] — resolved against players table by routers."""
    lid = resolve_league(league_id)
    _write_cache("manual_roster", players, lid)
    mark_synced(f"espn_manual:{lid}", f"{len(players)} players")
