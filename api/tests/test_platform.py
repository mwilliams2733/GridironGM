"""Platform dispatch: leagues route to their own ETL."""
from __future__ import annotations

import copy

import pytest

from app.config import league_config, league_ids, validate_league_config
from app.etl.platform import platform_of


def test_leagues_without_a_platform_default_to_espn():
    assert platform_of("league1") == "espn"


def test_the_sundt_league_is_registered_as_sleeper():
    assert "sundt" in league_ids()
    assert platform_of("sundt") == "sleeper"


def test_sundt_carries_full_ppr_scoring():
    assert league_config("sundt")["scoring"]["receiving"]["reception"] == 1.0


def test_sundt_carries_minus_one_interceptions():
    assert league_config("sundt")["scoring"]["passing"]["interception"] == -1


def test_sundt_declares_a_super_flex_slot():
    roster = league_config("sundt")["roster"]
    assert roster["starters"]["SUPER_FLEX"] == 1
    assert "QB" in roster["flex_slots"]["SUPER_FLEX"]["eligible"]


def test_sundt_playoffs_start_week_15():
    assert league_config("sundt")["league"]["playoff_week_start"] == 15


def test_espn_leagues_do_not_declare_playoff_week_start():
    """The gate: adding the key to ESPN leagues would change their ROS."""
    assert league_config("league1")["league"].get("playoff_week_start") is None


# ---------------------------------------------------------------------------
# Carry-forward 1: flex_slots share dicts must sum to 1.0.
# A hand-authored config summing to 0.8 or 1.2 silently under/over-allocates
# replacement demand with no error anywhere -- validate_league_config must
# catch it at load time.
# ---------------------------------------------------------------------------
def test_sundt_flex_slot_shares_sum_to_one():
    cfg = league_config("sundt")
    for label, d in cfg["roster"]["flex_slots"].items():
        assert sum(d["share"].values()) == pytest.approx(1.0)


def test_flex_slot_share_not_summing_to_one_is_rejected():
    cfg = copy.deepcopy(league_config("sundt"))
    cfg["roster"]["flex_slots"]["FLEX"]["share"] = {"RB": 0.5, "WR": 0.2, "TE": 0.1}
    with pytest.raises(ValueError, match="sum to 1.0"):
        validate_league_config(cfg, "sundt")


# ---------------------------------------------------------------------------
# Carry-forward 2: playoff_week_start must be a positive integer in a sane
# range. `last_scoring_week` computes min(REG_SEASON_WEEKS, start - 1), so a
# configured 0 yields -1, silently zeroing every ROS projection.
# ---------------------------------------------------------------------------
def test_playoff_week_start_zero_is_rejected():
    cfg = copy.deepcopy(league_config("sundt"))
    cfg["league"]["playoff_week_start"] = 0
    with pytest.raises(ValueError, match="playoff_week_start"):
        validate_league_config(cfg, "sundt")


def test_playoff_week_start_out_of_range_is_rejected():
    cfg = copy.deepcopy(league_config("sundt"))
    cfg["league"]["playoff_week_start"] = 30
    with pytest.raises(ValueError, match="playoff_week_start"):
        validate_league_config(cfg, "sundt")


def test_playoff_week_start_non_integer_is_rejected():
    cfg = copy.deepcopy(league_config("sundt"))
    cfg["league"]["playoff_week_start"] = "15"
    with pytest.raises(ValueError, match="playoff_week_start"):
        validate_league_config(cfg, "sundt")


def test_leagues_that_omit_the_key_pass_validation():
    """The four ESPN leagues set neither key -- validation must not start
    rejecting configs that were always legal."""
    validate_league_config(league_config("league1"), "league1")
