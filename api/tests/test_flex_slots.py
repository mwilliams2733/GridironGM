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


# ---------------------------------------------------------------------------
# SUPER_FLEX must be visible to every consumer of the starter counts, not just
# `replacement_levels` and `slot_plan`. A hardcoded `starters.get("FLEX", 0)`
# makes a superflex league's second QB a non-starter, so the app can recommend
# DROPPING a starting QB.
# ---------------------------------------------------------------------------
import pandas as pd

from app.models import vorp
from app.models.waivers import _my_lineup_split


def _ros_frame():
    rows = [("QB1", "QB", 300), ("QB2", "QB", 280),
            ("RB1", "RB", 250), ("RB2", "RB", 240), ("RB3", "RB", 100),
            ("WR1", "WR", 250), ("WR2", "WR", 240), ("WR3", "WR", 150),
            ("TE1", "TE", 200), ("K1", "K", 130), ("D1", "DST", 120)]
    return pd.DataFrame([{"player_id": p, "position": pos, "proj_points": v}
                         for p, pos, v in rows])


def test_the_second_qb_is_a_starter_in_a_superflex_league():
    ros = _ros_frame()
    roster = list(ros.player_id)
    _, used, bench = _my_lineup_split(roster, ros, _sundt_cfg())
    assert "QB2" in used, "a superflex QB must fill SUPER_FLEX, not sit on the bench"
    assert "QB2" not in set(bench.player_id)


def test_the_second_qb_is_a_bench_player_in_a_single_flex_league():
    """The gate: an ESPN league must be unchanged — QB2 really is droppable there."""
    ros = _ros_frame()
    roster = list(ros.player_id)
    _, used, bench = _my_lineup_split(roster, ros, league_config("league1"))
    assert "QB2" not in used
    assert "QB2" in set(bench.player_id)


def _mini_season_proj():
    rows = [("QB1", "QB", "KC", 300), ("QB2", "QB", "BUF", 280),
            ("RB1", "RB", "SF", 250), ("WR1", "WR", "MIN", 250),
            ("TE1", "TE", "LV", 200), ("K1", "K", "NYJ", 130)]
    return pd.DataFrame([{"player_id": p, "name": p, "position": pos,
                          "team": t, "proj_points": v} for p, pos, t, v in rows])


def _fills_need(cfg, player_id, my_roster):
    """`unfilled` is not a returned column, but it drives the rationale bit
    "fills starter need" (and need_score) -- the user-visible consequence."""
    board = vorp.vorp_board(my_roster=my_roster, season=2026,
                            season_proj=_mini_season_proj(), cfg=cfg)
    row = board[board.player_id == player_id].iloc[0]
    return "fills starter need" in row["rationale"]


def test_a_superflex_league_still_needs_a_qb_after_drafting_one():
    """1 fixed QB + 1 SUPER_FLEX = 2 QB starters. With one drafted, one is
    still unfilled; the hardcoded FLEX form reports 0 and the board stops
    treating a second QB as a starter need at all."""
    assert _fills_need(_sundt_cfg(), "QB2", ["QB1"]) is True


def test_a_single_flex_league_needs_no_second_qb():
    """The gate: ESPN leagues must not start demanding a second QB."""
    assert _fills_need(league_config("league1"), "QB2", ["QB1"]) is False
