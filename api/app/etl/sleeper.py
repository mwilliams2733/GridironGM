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

import httpx
import pandas as pd

from ..config import PARQUET_DIR, ensure_dirs, league_cache_dir, league_config, resolve_league
from ..db import mark_synced
from ..models.projections import norm_team

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
    except httpx.HTTPError as exc:
        # Only a genuine network/transport failure falls back to the cache. A
        # successfully-fetched-but-malformed payload must NOT land here: that
        # would silently mask a real Sleeper schema change as "offline".
        if path.exists():
            log.warning("sleeper player map fetch failed (%s); using cache", exc)
            df = pd.read_parquet(path)
            return {r.player_id: json.loads(r.blob) for r in df.itertuples()}
        raise
    pd.DataFrame([{"player_id": k, "blob": json.dumps(v)}
                  for k, v in data.items()]).to_parquet(path, index=False)
    return data


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
        # Sleeper spells the Rams "LAR"; the rest of the app canonicalizes to
        # "LA" (schedules/weekly_stats/odds convention). Route through the
        # same norm_team the rest of the app uses rather than adding a
        # second, Sleeper-specific team-code table that can drift from it.
        return f"DST_{norm_team(team)}" if team else None

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

    # resolve_player_name itself returns None for an empty/whitespace name --
    # e.g. Sleeper's "0" placeholder for an empty starter slot, which has no
    # entry in the player map and so no full_name to match on.
    return resolve_player_name(entry.get("full_name") or "", pos, players)


def _espn_lut(players: pd.DataFrame) -> dict:
    esp = players.dropna(subset=["espn_id"]).copy()
    if esp.empty:
        return {}
    return dict(zip(esp["espn_id"].astype(float), esp["player_id"]))


def parse_roster(raw: dict, player_map: dict, players: pd.DataFrame) -> dict:
    """One Sleeper roster -> {roster_id, players, starters, faab_used, unresolved}.

    `players` holds resolved app player_ids; `unresolved` holds the raw Sleeper
    ids that the cascade could not place. The full roster is the union of the
    two -- `len(players)` alone undercounts whenever anything failed to
    resolve.
    """
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

    A sync-time check rather than a test, because it needs the network. Covers
    every scoring key this app transcribes by hand from Sleeper's league
    settings into `league.yaml` -- the point is to catch a transcription
    error, not just a live rule change, so every authored key that Sleeper
    also reports must be checked, not just a handful.

    A points-allowed tier is checked at its lower bound (Sleeper's own key
    naming, e.g. `pts_allow_7_13` for our `[13, 4]` tier) against the fantasy
    points value; tiers are matched positionally against
    `dst.points_allowed_tiers` since Sleeper's granularity (7 buckets) matches
    ours exactly for leagues authored this way.
    """
    live = league.get("scoring_settings") or {}
    cfg = league_config(lid)["scoring"]
    checks = {
        # passing
        "pass_yd": 1 / cfg["passing"]["yards_per_point"],
        "pass_td": cfg["passing"]["touchdown"],
        "pass_int": cfg["passing"]["interception"],
        "pass_2pt": cfg["passing"]["two_point"],
        # rushing
        "rush_yd": 1 / cfg["rushing"]["yards_per_point"],
        "rush_td": cfg["rushing"]["touchdown"],
        "rush_2pt": cfg["rushing"]["two_point"],
        # receiving
        "rec": cfg["receiving"]["reception"],
        "rec_yd": 1 / cfg["receiving"]["yards_per_point"],
        "rec_td": cfg["receiving"]["touchdown"],
        "rec_2pt": cfg["receiving"]["two_point"],
        # misc
        "fum_lost": cfg["misc"]["fumble_lost"],
        # kicking (Sleeper splits 0-39 into three bands, all worth fg_0_39 here)
        "fgm_0_19": cfg["kicking"]["fg_0_39"],
        "fgm_20_29": cfg["kicking"]["fg_0_39"],
        "fgm_30_39": cfg["kicking"]["fg_0_39"],
        "fgm_40_49": cfg["kicking"]["fg_40_49"],
        "fgm_50p": cfg["kicking"]["fg_50_plus"],
        "fgmiss": cfg["kicking"]["fg_missed"],
        "xpm": cfg["kicking"]["xp_made"],
        "xpmiss": cfg["kicking"]["xp_missed"],
        # dst
        "sack": cfg["dst"]["sack"],
        "int": cfg["dst"]["interception"],
        "fum_rec": cfg["dst"]["fumble_recovery"],
        "ff": cfg["dst"].get("forced_fumble"),
        "def_td": cfg["dst"]["touchdown"],
        "safe": cfg["dst"]["safety"],
        "blk_kick": cfg["dst"]["block_kick"],
    }
    for key, ours in checks.items():
        theirs = live.get(key)
        if theirs is not None and ours is not None and float(theirs) != float(ours):
            log.warning(
                "sleeper %s: scoring drift on %s — league.yaml has %s, Sleeper has %s",
                lid, key, ours, theirs)

    # points-allowed ladder: match Sleeper's pts_allow_* keys against our
    # tiers by lower bound.
    tier_keys = {
        0: "pts_allow_0", 1: "pts_allow_1_6", 7: "pts_allow_7_13",
        14: "pts_allow_14_20", 21: "pts_allow_21_27", 28: "pts_allow_28_34",
        35: "pts_allow_35p",
    }
    tiers = (cfg.get("dst") or {}).get("points_allowed_tiers") or []
    # tiers are [upper_bound, points]; derive each tier's lower bound from the
    # previous tier's upper bound to match against Sleeper's *_N_M keys.
    lower = 0
    for upper, pts in tiers:
        key = tier_keys.get(lower)
        if key is not None:
            theirs = live.get(key)
            if theirs is not None and float(theirs) != float(pts):
                log.warning(
                    "sleeper %s: scoring drift on %s (points allowed tier "
                    "starting %s) — league.yaml has %s, Sleeper has %s",
                    lid, key, lower, pts, theirs)
        lower = upper + 1
