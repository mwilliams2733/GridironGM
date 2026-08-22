"""ROS stops at the last week that counts toward making the playoffs."""
from __future__ import annotations

import copy

from app.config import league_config
from app.models.projections import REG_SEASON_WEEKS, last_scoring_week


def test_absent_playoff_week_start_keeps_the_full_regular_season():
    """The gate: ESPN leagues do not set the key and must not move."""
    assert last_scoring_week(league_config()) == REG_SEASON_WEEKS


def test_playoffs_starting_week_15_caps_at_week_14():
    cfg = copy.deepcopy(league_config())
    cfg["league"]["playoff_week_start"] = 15
    assert last_scoring_week(cfg) == 14


def test_a_late_playoff_start_never_exceeds_the_regular_season():
    cfg = copy.deepcopy(league_config())
    cfg["league"]["playoff_week_start"] = 25
    assert last_scoring_week(cfg) == REG_SEASON_WEEKS
