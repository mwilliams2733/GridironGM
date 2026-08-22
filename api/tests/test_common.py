"""Player-resolution helpers in app.routers._common.

Empty-key regression: `resolve_player_name` used to crash with
`KeyError: False` on an empty/whitespace-only name, because its fuzzy-match
branch indexed the players DataFrame with the bare boolean `False` instead of
a boolean Series (`players[... if key else False]`). The blast radius wasn't
just Sleeper's empty-slot placeholder -- `routers/draft.py` calls
`resolve_espn_player(espn_id, "", None, players)` with a hardcoded empty name
whenever an ESPN pick has no matching player, and four more call sites pass
`p.get("name", "")`. Any of those crashes the whole sync on an unmatched pick.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.routers._common import norm_name, resolve_espn_player, resolve_player_name


def _players():
    df = pd.DataFrame([
        {"player_id": "00-0033077", "name": "Dak Prescott", "position": "QB",
         "team": "DAL", "espn_id": "4429202", "status": "ACT"},
        {"player_id": "00-0036389", "name": "Jalen Hurts", "position": "QB",
         "team": "PHI", "espn_id": "4040715", "status": "ACT"},
    ])
    df["norm"] = df["name"].map(norm_name)
    return df


def test_resolve_player_name_empty_string_returns_none():
    assert resolve_player_name("", None, _players()) is None


def test_resolve_player_name_whitespace_only_returns_none():
    assert resolve_player_name("   ", None, _players()) is None


def test_resolve_espn_player_unmatchable_id_with_empty_name_returns_none():
    """This is the exact call shape at routers/draft.py:403 -- an ESPN pick
    whose espn_player_id has no match in the players table, resolved with a
    hardcoded empty name. Wiring, not the bare helper."""
    assert resolve_espn_player(999999999, "", None, _players()) is None


def test_resolve_player_name_still_resolves_a_real_name():
    """Non-empty inputs must be unaffected by the empty-key guard."""
    assert resolve_player_name("Jalen Hurts", "QB", _players()) == "00-0036389"
