"""Shared helpers for routers: JSON-safe DataFrame records, player resolution."""
from __future__ import annotations

import re
import unicodedata

import numpy as np
import pandas as pd

from ..db import read_df


def records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> list[dict], NaN/NaT -> None, JSON-safe."""
    if df is None or df.empty:
        return []
    safe = df.replace({np.nan: None})
    safe = safe.where(pd.notnull(safe), None)
    return safe.to_dict(orient="records")


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm_name(name: str) -> str:
    n = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    n = re.sub(r"[.'`]", "", n.lower())
    n = re.sub(r"[^a-z\s]", " ", n)
    toks = [t for t in n.split() if t not in _SUFFIXES]
    return " ".join(toks)


def all_players() -> pd.DataFrame:
    p = read_df("SELECT player_id, name, position, team, espn_id, status FROM players")
    p["norm"] = p["name"].map(norm_name)
    return p


def _dst_lookup() -> dict[str, str]:
    """'kansas city chiefs' / 'chiefs' / 'kc' -> 'DST_KC' (models use synthetic DST ids)."""
    from ..etl.odds import TEAM_ABBR

    lut: dict[str, str] = {}
    for full, abbr in TEAM_ABBR.items():
        dst = f"DST_{abbr}"
        lut[norm_name(full)] = dst
        lut[norm_name(full.split()[-1])] = dst  # nickname ("chiefs")
        lut[abbr.lower()] = dst
    return lut


def resolve_dst(name: str) -> str | None:
    key = norm_name(re.sub(r"\b(d/?st|defense|def)\b", "", name, flags=re.I))
    return _dst_lookup().get(key.strip())


def resolve_player_name(name: str, position: str | None, players: pd.DataFrame) -> str | None:
    """Best-effort resolve a free-text name (+optional position) to a player_id."""
    if position == "DST" or re.search(r"\b(d/?st|defense)\b", str(name), flags=re.I):
        dst = resolve_dst(name)
        if dst:
            return dst
    key = norm_name(name)
    if not key:
        # An empty/whitespace-only name is not a query that failed to match
        # anything -- it is no query at all. Return early: the exact-match
        # branch below would spuriously match any player whose norm is also
        # empty, and the fuzzy branch's `players[... if key else False]`
        # indexes the DataFrame with the bare boolean False and raises
        # KeyError rather than returning "no match".
        return None
    cand = players[players.norm == key]
    if position:
        pos_cand = cand[cand.position == position]
        if not pos_cand.empty:
            cand = pos_cand
    if not cand.empty:
        return cand.iloc[0].player_id
    # fuzzy: substring / startswith match
    starts = players[players.norm.str.startswith(key.split(" ")[0]) if key else False]
    if position:
        starts = starts[starts.position == position]
    if not starts.empty:
        # prefer close length match
        starts = starts.assign(_d=(starts.norm.str.len() - len(key)).abs())
        return starts.sort_values("_d").iloc[0].player_id
    return None


def resolve_espn_player(espn_id, name: str, position: str | None, players: pd.DataFrame) -> str | None:
    if espn_id is not None:
        try:
            eid = float(espn_id)
            m = players[players.espn_id.astype(float) == eid]
            if not m.empty:
                return m.iloc[0].player_id
        except (TypeError, ValueError):
            pass
    return resolve_player_name(name, position, players)


def resolve_roster_entry(entry: dict, players: pd.DataFrame) -> str | None:
    """player_id for one roster row, regardless of platform shape.

    Sleeper rosters (via `etl.platform.get_my_roster`) already carry a resolved
    `player_id` -- possibly `None` when the identity cascade could not place a
    player. ESPN rosters carry `espn_id`/`name`/`position` instead and need
    the ESPN resolver. Checking for the `player_id` key first is required: an
    ESPN entry has no such key, so `.get` falls through correctly, but a
    Sleeper entry's `espn_id`/`name` keys are simply absent, and blindly
    calling `resolve_espn_player` on it would resolve against an empty name
    and silently overwrite an already-correct id with None.
    """
    if "player_id" in entry:
        return entry["player_id"]
    return resolve_espn_player(entry.get("espn_id"), entry.get("name", ""),
                               entry.get("position"), players)
