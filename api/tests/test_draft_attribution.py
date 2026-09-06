"""Snake-order pick attribution.

Getting this wrong misassigns every pick after the first turn, which silently
corrupts both `by_me` (so recommendations use the wrong roster) and every
opponent's roster. The round-trip against `_snake_pick_numbers` is the guard:
attribution must be the exact inverse of the sequence the app already uses to
tell you when you're next up.
"""
from __future__ import annotations

import pytest

import copy

from app.config import league_config
from app.routers.draft import _round_and_slot, _snake_pick_numbers, _team_rosters

SIZES = (10, 11, 12)
ROUNDS = 16


@pytest.mark.parametrize("teams", SIZES)
def test_inversion_round_trips_against_pick_sequence(teams):
    for slot in range(1, teams + 1):
        for overall in _snake_pick_numbers(slot, teams, ROUNDS):
            _, got = _round_and_slot(overall, teams)
            assert got == slot, f"pick {overall} in a {teams}-team league"


@pytest.mark.parametrize("teams", SIZES)
def test_every_round_covers_every_slot_exactly_once(teams):
    for rnd in range(1, ROUNDS + 1):
        slots = [_round_and_slot((rnd - 1) * teams + i, teams)[1] for i in range(1, teams + 1)]
        assert sorted(slots) == list(range(1, teams + 1))


@pytest.mark.parametrize("teams", SIZES)
def test_round_number_is_correct(teams):
    assert _round_and_slot(1, teams)[0] == 1
    assert _round_and_slot(teams, teams)[0] == 1
    assert _round_and_slot(teams + 1, teams)[0] == 2


def test_snake_turns_at_the_round_boundary():
    """The defining property: the team picking last in round 1 picks first in round 2."""
    teams = 12
    assert _round_and_slot(12, teams) == (1, 12)
    assert _round_and_slot(13, teams) == (2, 12)  # same team, back-to-back
    assert _round_and_slot(14, teams) == (2, 11)


def test_odd_rounds_run_forward_and_even_rounds_reverse():
    teams = 10
    first_round = [_round_and_slot(o, teams)[1] for o in range(1, 11)]
    second_round = [_round_and_slot(o, teams)[1] for o in range(11, 21)]
    assert first_round == list(range(1, 11))
    assert second_round == list(range(10, 0, -1))


def test_team_rosters_uses_configured_names_by_draft_slot():
    """league1 has real manager names now (config/league.yaml team_names),
    keyed by draft slot -- not ESPN team_id, since ESPN sync isn't wired up
    for this league (espn_league_id is still null).
    """
    cfg = league_config("league1")
    state = {"picks": [], "my_slot": None}
    rosters = _team_rosters(state, cfg)
    names = cfg["league"]["team_names"]
    assert [t["name"] for t in rosters] == names
    assert rosters[7]["name"] == "Taiyou"  # slot 8


def test_team_rosters_falls_back_to_generic_label_without_configured_names():
    cfg = copy.deepcopy(league_config("league1"))
    cfg["league"].pop("team_names", None)
    state = {"picks": [], "my_slot": 3}
    rosters = _team_rosters(state, cfg)
    assert rosters[0]["name"] == "Team 1"
    assert rosters[2]["name"] == "My Team"
