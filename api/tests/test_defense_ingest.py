"""Team-defense ingest: nflverse team stats plus points allowed from schedules."""
from __future__ import annotations

import pandas as pd
import pytest

from app.etl.nfl_data import DEFENSE_INPUT_COLUMNS, normalize_team_defense

TEAM_STATS = pd.DataFrame([
    {"season": 2025, "week": 1, "team": "HOU", "opponent_team": "IND",
     "def_sacks": 4, "def_interceptions": 2, "def_fumbles_forced": 1,
     "fumble_recovery_opp": 1, "def_tds": 1, "def_safeties": 0,
     "def_punt_blocks": 0, "def_pat_blocks": 0, "def_fg_blocks": 1,
     "special_teams_tds": 0},
])
SCORES = pd.DataFrame([
    {"season": 2025, "week": 1, "home_team": "HOU", "away_team": "IND",
     "home_score": 27, "away_score": 13},
])


def test_points_allowed_is_the_opponents_score():
    out = normalize_team_defense(TEAM_STATS, SCORES)
    assert out.set_index("team").loc["HOU", "points_allowed"] == 13


def test_every_defense_scoring_input_survives():
    out = normalize_team_defense(TEAM_STATS, SCORES)
    missing = [c for c in DEFENSE_INPUT_COLUMNS if c not in out.columns]
    assert not missing, f"defense scoring inputs dropped: {missing}"
    nulls = [c for c in DEFENSE_INPUT_COLUMNS if out[c].isna().any()]
    assert not nulls, f"defense scoring inputs arrived null: {nulls}"


def test_opponent_is_carried_through():
    out = normalize_team_defense(TEAM_STATS, SCORES)
    assert out.iloc[0]["opponent"] == "IND"


def test_a_row_with_no_matching_schedule_is_dropped_not_nulled():
    """A silent NaN points_allowed would score as a shutout. Drop instead."""
    orphan = pd.concat([TEAM_STATS, TEAM_STATS.assign(week=99)], ignore_index=True)
    out = normalize_team_defense(orphan, SCORES)
    assert len(out) == 1


def test_a_matched_row_with_a_null_score_is_dropped_not_nulled():
    """The join key can match (e.g. an unplayed future game) while the score is
    still NULL. The inner join alone does not protect against this — only a
    dropna on points_allowed after the merge does. A fabricated 0 here is the
    same fabricated-shutout bug the orphan-row guard exists to prevent.
    """
    unplayed_stats = TEAM_STATS.assign(week=2)
    unplayed_schedule = pd.DataFrame([
        {"season": 2025, "week": 2, "home_team": "HOU", "away_team": "IND",
         "home_score": None, "away_score": None},
    ])
    stats = pd.concat([TEAM_STATS, unplayed_stats], ignore_index=True)
    scores = pd.concat([SCORES, unplayed_schedule], ignore_index=True)
    out = normalize_team_defense(stats, scores)
    assert len(out) == 1
    assert out.iloc[0]["week"] == 1


def test_a_missing_raw_defensive_column_raises_instead_of_zero_filling():
    """A raw defensive stat column absent from the source frame must be a loud
    failure, not a silent zero — the same class of bug that left
    weekly_stats.interceptions NULL for three seasons undetected.
    """
    incomplete = TEAM_STATS.drop(columns=["def_sacks"])
    with pytest.raises(ValueError, match="def_sacks"):
        normalize_team_defense(incomplete, SCORES)
