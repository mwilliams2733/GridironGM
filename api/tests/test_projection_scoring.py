"""Raw reads are league-agnostic; scoring is applied per league."""
from __future__ import annotations

import copy

import pandas as pd
import pytest

from app.config import league_config
from app.models import projections as proj
from app.scoring import score_offense

RAW = pd.DataFrame([
    {"season": 2025, "week": 1, "player_id": "p1", "team": "BUF", "opponent": "MIA",
     "position": "WR", "carries": 0, "targets": 8, "receptions": 6, "attempts": 0,
     "passing_yards": 0, "passing_tds": 0, "interceptions": 0,
     "passing_2pt_conversions": 0, "rushing_yards": 0, "rushing_tds": 0,
     "rushing_2pt_conversions": 0, "receiving_yards": 84, "receiving_tds": 1,
     "receiving_2pt_conversions": 0, "rushing_fumbles_lost": 0,
     "receiving_fumbles_lost": 0, "sack_fumbles_lost": 0, "special_teams_tds": 0},
])


def test_scored_frame_uses_the_supplied_league_scoring():
    half = league_config()
    full = copy.deepcopy(half)
    full["scoring"]["receiving"]["reception"] = 1.0

    from app.scoring import score_frame
    half_pts = score_frame(RAW, half).iloc[0]
    full_pts = score_frame(RAW, full).iloc[0]

    # 6 receptions x 0.5 extra per reception
    assert round(full_pts - half_pts, 2) == 3.0


def test_scored_frame_matches_row_wise_scorer_on_real_shape():
    from app.scoring import score_frame
    assert score_frame(RAW).iloc[0] == score_offense(RAW.iloc[0].to_dict())


def test_weekly_raw_has_no_points_column():
    """Raw reads must be league-agnostic; a points column would bake in one league."""
    df = proj._weekly_raw(2025)
    if df.empty:
        pytest.skip("weekly_stats has no rows on this checkout")
    assert "fp" not in df.columns
    assert "fantasy_points_half_ppr" not in df.columns


def test_weekly_adds_fp_for_the_given_config():
    df = proj._weekly(2025, league_config())
    if df.empty:
        pytest.skip("weekly_stats has no rows on this checkout")
    assert "fp" in df.columns
