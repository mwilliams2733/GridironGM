"""ROS stops at the last week that counts toward making the playoffs."""
from __future__ import annotations

import copy

import pytest

from app.config import league_config
from app.models.projections import (
    REG_SEASON_WEEKS,
    last_scoring_week,
    project_ros,
    project_season,
)


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


def test_project_ros_respects_the_playoff_cap():
    """Integration test: verify the horizon cap actually stops project_ros early.

    The wiring (line 754 in projections.py) passes the capped horizon to the
    weeks range that sums matchup factors. This test guards against reverting
    that line to REG_SEASON_WEEKS + 1, which would make all tests pass but
    break the feature.
    """
    # Build ROS projections with the default (uncapped) config
    s = project_season(2025)
    if s.empty:
        pytest.skip("season projections empty on this checkout")

    ros_uncapped = project_ros(2025, 10, season_proj=s)
    if ros_uncapped.empty:
        pytest.skip("ROS projections empty on this checkout")

    # Build ROS projections with playoffs starting week 15 (cap at week 14)
    cfg_capped = copy.deepcopy(league_config())
    cfg_capped["league"]["playoff_week_start"] = 15
    ros_capped = project_ros(2025, 10, season_proj=s, cfg=cfg_capped)

    # The capped league's max games_left must be strictly smaller.
    # Weeks 10-14 is 5 games; weeks 10-18 is 9 games (if no bye).
    assert ros_capped["games_left"].max() < ros_uncapped["games_left"].max()

    # No row in the capped frame should have more games than 10..14 (5 weeks max)
    assert ros_capped["games_left"].max() <= 5
