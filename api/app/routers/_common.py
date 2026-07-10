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


def resolve_player_name(name: str, position: str | None, players: pd.DataFrame) -> str | None:
    """Best-effort resolve a free-text name (+optional position) to a player_id."""
    key = norm_name(name)
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
