"""Sleeper league sync. Read-only REST, no authentication required.

Sleeper's public API needs no key, no cookies and no OAuth, which makes it the
simplest source in the app. Rate limit is 1000 calls/minute; a full sync is
under ten calls plus the player map.

Player identity is the one hard part. Sleeper carries a `gsis_id` cross-
reference, but populates it mostly for pre-2020 entrants -- measured at 17% of
players rostered in this league, missing Hurts, St. Brown, Jonathan Taylor and
Trevor Lawrence. Normalized name + position resolves 194/194 with zero
collisions and agrees with `gsis_id` on every player where both exist, so name
matching is the primary path and gsis_id is a confirmation, not the reverse.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

from ..config import PARQUET_DIR, ensure_dirs, league_cache_dir, league_config, resolve_league
from ..db import mark_synced

log = logging.getLogger(__name__)

BASE = "https://api.sleeper.app/v1"
TIMEOUT = 30

# Sleeper spells team defense DEF; the app uses DST everywhere else. Normalize
# here, at the boundary, never in the models.
SLEEPER_POS = {"DEF": "DST"}


def _get(path: str):
    resp = httpx.get(f"{BASE}/{path}", timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _settings(league_id: str | None = None) -> dict:
    lg = league_config(league_id).get("league", {})
    return {"league_id": lg.get("sleeper_league_id"),
            "draft_id": lg.get("sleeper_draft_id")}


def sleeper_available(league_id: str | None = None) -> bool:
    return bool(_settings(league_id)["league_id"])


def sleeper_player_map() -> dict:
    """All Sleeper players, cached to parquet. ~14MB; refresh daily at most."""
    ensure_dirs()
    path = PARQUET_DIR / "sleeper_players.parquet"
    try:
        data = _get("players/nfl")
        pd.DataFrame([{"player_id": k, "blob": json.dumps(v)}
                      for k, v in data.items()]).to_parquet(path, index=False)
        return data
    except Exception as exc:
        if path.exists():
            log.warning("sleeper player map fetch failed (%s); using cache", exc)
            df = pd.read_parquet(path)
            return {r.player_id: json.loads(r.blob) for r in df.itertuples()}
        raise


def resolve_sleeper_player(entry: dict, players: pd.DataFrame,
                           espn_lut: dict) -> str | None:
    """Resolve one Sleeper player entry to our player_id.

    Cascade: gsis_id -> espn_id -> normalized name + position. Returns None
    rather than guessing when all three miss, so callers can report it.
    """
    from ..routers._common import resolve_player_name

    pos = SLEEPER_POS.get(entry.get("position"), entry.get("position"))
    if pos == "DST":
        team = entry.get("player_id") or entry.get("team")
        return f"DST_{team}" if team else None

    gsis = entry.get("gsis_id")
    if gsis and gsis in set(players.player_id):
        return gsis

    espn = entry.get("espn_id")
    if espn is not None:
        try:
            hit = espn_lut.get(float(espn))
            if hit:
                return hit
        except (TypeError, ValueError):
            pass

    full_name = entry.get("full_name")
    if not full_name:
        # Sleeper uses "0" as a placeholder for an empty starter slot; the
        # player map has no entry for it, so there is no name to match on.
        # An empty search key isn't "no match", it's "no query" -- return
        # None rather than handing resolve_player_name a blank string.
        return None
    return resolve_player_name(full_name, pos, players)


def _espn_lut(players: pd.DataFrame) -> dict:
    esp = players.dropna(subset=["espn_id"]).copy()
    if esp.empty:
        return {}
    return dict(zip(esp["espn_id"].astype(float), esp["player_id"]))


def parse_roster(raw: dict, player_map: dict, players: pd.DataFrame) -> dict:
    """One Sleeper roster -> {roster_id, players, starters, faab_used, unresolved}."""
    lut = _espn_lut(players)

    def resolve(pid):
        return resolve_sleeper_player(
            {**player_map.get(pid, {}), "player_id": pid}, players, lut)

    ids, unresolved = [], []
    for pid in (raw.get("players") or []):
        got = resolve(pid)
        (ids if got else unresolved).append(got or pid)
    starters = [resolve(pid) for pid in (raw.get("starters") or [])]
    return {
        "roster_id": raw.get("roster_id"),
        "owner_id": raw.get("owner_id"),
        "players": ids,
        "starters": starters,
        "faab_used": (raw.get("settings") or {}).get("waiver_budget_used", 0),
        "unresolved": unresolved,
    }


def _write_cache(name: str, payload, league_id: str | None = None) -> None:
    path = league_cache_dir(league_id) / f"{name}.json"
    path.write_text(json.dumps(
        {"fetched_at": datetime.now(timezone.utc).isoformat(), "data": payload},
        indent=1), encoding="utf-8")


def read_cache(name: str, league_id: str | None = None):
    path = league_cache_dir(league_id) / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def sync_sleeper(league_id: str | None = None) -> dict:
    """Pull rosters, users and league settings. Returns a summary dict."""
    from ..routers._common import all_players

    lid = resolve_league(league_id)
    s = _settings(lid)
    if not s["league_id"]:
        raise RuntimeError(f"sleeper_league_id not configured for '{lid}'.")

    league = _get(f"league/{s['league_id']}")
    raw_rosters = _get(f"league/{s['league_id']}/rosters")
    users = _get(f"league/{s['league_id']}/users")
    player_map = sleeper_player_map()
    players = all_players()

    by_user = {u["user_id"]: u for u in users}
    rosters = []
    for r in raw_rosters:
        parsed = parse_roster(r, player_map, players)
        owner = by_user.get(r.get("owner_id")) or {}
        parsed["name"] = (owner.get("metadata") or {}).get("team_name") \
            or owner.get("display_name") or f"Roster {parsed['roster_id']}"
        rosters.append(parsed)

    _check_scoring_drift(league, lid)

    _write_cache("teams", rosters, lid)
    _write_cache("league", league, lid)
    unresolved = sum(len(r["unresolved"]) for r in rosters)
    mark_synced(f"sleeper:{lid}", f"{len(rosters)} rosters, {unresolved} unresolved")
    return {"league_id": lid, "rosters": len(rosters), "unresolved": unresolved}


def _check_scoring_drift(league: dict, lid: str) -> None:
    """Warn if the commissioner changed scoring since league.yaml was authored.

    A sync-time check rather than a test, because it needs the network.
    """
    live = league.get("scoring_settings") or {}
    cfg = league_config(lid)["scoring"]
    checks = {
        "rec": cfg["receiving"]["reception"],
        "pass_td": cfg["passing"]["touchdown"],
        "pass_int": cfg["passing"]["interception"],
        "rush_td": cfg["rushing"]["touchdown"],
    }
    for key, ours in checks.items():
        theirs = live.get(key)
        if theirs is not None and float(theirs) != float(ours):
            log.warning(
                "sleeper %s: scoring drift on %s — league.yaml has %s, Sleeper has %s",
                lid, key, ours, theirs)
