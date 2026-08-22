"""Flex slots are declarative, so SUPER_FLEX is expressible without code changes."""
from __future__ import annotations

import copy

from app.config import league_config
from app.models.vorp import flex_slot_defs, replacement_levels

SUNDT_ROSTER = {
    "starters": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1,
                 "SUPER_FLEX": 1, "K": 1, "DST": 1},
    "flex_eligible": ["RB", "WR", "TE"],
    "flex_slots": {
        "FLEX": {"eligible": ["RB", "WR", "TE"],
                 "share": {"RB": .45, "WR": .45, "TE": .10}},
        "SUPER_FLEX": {"eligible": ["QB", "RB", "WR", "TE"],
                       "share": {"QB": .90, "RB": .04, "WR": .04, "TE": .02}},
    },
    "bench": 7, "ir": 2,
}


def _sundt_cfg():
    cfg = copy.deepcopy(league_config())
    cfg["league"]["teams"] = 12
    cfg["roster"] = copy.deepcopy(SUNDT_ROSTER)
    return cfg


def test_espn_replacement_levels_are_unchanged():
    """The gate: four existing leagues must not move."""
    assert replacement_levels(league_config())["QB"] == 12


def test_absent_flex_slots_synthesises_the_legacy_flex():
    cfg = league_config()
    defs = flex_slot_defs(cfg)
    assert set(defs) == {"FLEX"}
    assert defs["FLEX"]["eligible"] == cfg["roster"]["flex_eligible"]
    assert defs["FLEX"]["share"]["RB"] == 0.45


def test_superflex_moves_qb_replacement_from_12_to_23():
    levels = replacement_levels(_sundt_cfg())
    assert levels["QB"] == 23


def test_superflex_replacement_levels_match_the_spec():
    levels = replacement_levels(_sundt_cfg())
    assert levels["RB"] == 30
    assert levels["WR"] == 30
    assert levels["TE"] == 13
    assert levels["K"] == 12
    assert levels["DST"] == 12


def test_a_ten_team_league_ranks_lower_than_a_twelve_team_league():
    cfg10 = _sundt_cfg()
    cfg10["league"]["teams"] = 10
    assert replacement_levels(cfg10)["QB"] < replacement_levels(_sundt_cfg())["QB"]
