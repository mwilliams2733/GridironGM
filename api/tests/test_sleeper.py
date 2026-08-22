"""Sleeper payload parsing and the player-identity cascade.

No network: everything here runs off the captured fixture.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.etl.sleeper import parse_roster, resolve_sleeper_player

FIXTURE = Path(__file__).parent / "fixtures" / "sleeper_roster.json"


def _rosters():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_has_twelve_rosters():
    assert len(_rosters()) == 12


def test_starters_are_ten_ids_in_slot_order():
    """This is the current_lineup data the optimizer has never had."""
    mine = [r for r in _rosters() if r["roster_id"] == 12][0]
    assert len(mine["starters"]) == 10


def test_remaining_faab_comes_from_the_roster_settings():
    mine = [r for r in _rosters() if r["roster_id"] == 12][0]
    assert "waiver_budget_used" in mine["settings"]


def test_gsis_id_is_used_when_present():
    players = _fake_players()
    entry = {"gsis_id": "00-0033077", "full_name": "Dak Prescott", "position": "QB"}
    assert resolve_sleeper_player(entry, players, {}) == "00-0033077"


def test_name_matching_is_the_fallback_when_gsis_is_missing():
    """Sleeper omits gsis_id for most post-2020 entrants, so this is the
    PRIMARY path in practice: 194/194 rostered players resolve this way."""
    players = _fake_players()
    entry = {"gsis_id": None, "full_name": "Jalen Hurts", "position": "QB"}
    assert resolve_sleeper_player(entry, players, {}) == "00-0036389"


def test_team_defenses_map_to_synthetic_dst_ids():
    assert resolve_sleeper_player(
        {"player_id": "HOU", "position": "DEF"}, _fake_players(), {}) == "DST_HOU"


def test_an_unresolvable_entry_returns_none_rather_than_guessing():
    entry = {"gsis_id": None, "full_name": "Nobody Atallhere", "position": "WR"}
    assert resolve_sleeper_player(entry, _fake_players(), {}) is None


def test_empty_starter_slot_placeholder_does_not_crash():
    """Sleeper fills an empty starter slot with the literal id "0", which has
    no entry in the player map -- full_name is missing entirely, not just
    unmatched. This must return None, not raise, when handed to the cascade
    with an empty/missing name."""
    entry = {"gsis_id": None, "full_name": None, "position": None}
    assert resolve_sleeper_player(entry, _fake_players(), {}) is None


def test_parse_roster_resolves_via_name_when_gsis_and_espn_both_miss():
    """Guards the wiring, not just the helper: a roster whose players carry no
    gsis_id and no espn_id must still come back with real player_ids, proving
    parse_roster actually calls the full cascade rather than only checking the
    cheap ids and giving up."""
    players = _fake_players()
    player_map = {
        "111": {"gsis_id": None, "espn_id": None, "full_name": "Dak Prescott",
                 "position": "QB"},
        "222": {"gsis_id": None, "espn_id": None, "full_name": "Jalen Hurts",
                 "position": "QB"},
    }
    raw = {
        "roster_id": 1,
        "owner_id": "u1",
        "players": ["111", "222"],
        "starters": ["111"],
        "settings": {"waiver_budget_used": 12},
    }
    parsed = parse_roster(raw, player_map, players)
    assert parsed["players"] == ["00-0033077", "00-0036389"]
    assert parsed["starters"] == ["00-0033077"]
    assert parsed["unresolved"] == []
    assert parsed["faab_used"] == 12


def _fake_players():
    import pandas as pd
    from app.routers._common import norm_name
    df = pd.DataFrame([
        {"player_id": "00-0033077", "name": "Dak Prescott", "position": "QB",
         "team": "DAL", "espn_id": None, "status": "ACT"},
        {"player_id": "00-0036389", "name": "Jalen Hurts", "position": "QB",
         "team": "PHI", "espn_id": None, "status": "ACT"},
    ])
    df["norm"] = df["name"].map(norm_name)
    return df
