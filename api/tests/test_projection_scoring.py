"""Raw reads are league-agnostic; scoring is applied per league."""
from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest

from app.config import league_config
from app.models import projections as proj
from app.scoring import score_frame, score_offense

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


def test_recent_form_uses_the_season_being_played():
    """Regression guard: `_weekly_raw` deliberately excludes `season` (it's the
    feeder-season set for `project_season`'s history aggregates), so wiring
    `_recent_form` through `_weekly`/`_weekly_raw` silently made this always
    return an empty series -- `season` was never in the pulled rows. It must
    read `_weekly_rows((season,))` directly instead.
    """
    cur = proj._weekly_rows((2025,))
    if cur.empty:
        pytest.skip("weekly_stats has no 2025 rows on this checkout")
    form = proj._recent_form(2025, 10)
    assert not form.empty, (
        "recent form must not be empty when weekly_stats has 2025 rows before week 10"
    )


def test_dvp_factors_reaches_current_season_branch():
    """Regression guard: same bug as above, one level up -- `dvp_factors` must
    actually use `season`'s own data when enough weeks exist, not silently
    fall back to `season - 1` every time. Proven by recomputing the
    current-season-only DvP table independently (bypassing `dvp_factors`'
    internal fallback branch entirely) and asserting it matches what
    `dvp_factors` returns.
    """
    season, upto_week = 2025, 10
    cur = proj._weekly_rows((season,))
    if cur.empty:
        pytest.skip("weekly_stats has no 2025 rows on this checkout")
    early = cur[(cur.week < upto_week) & (cur.week <= proj.REG_SEASON_WEEKS)]
    if early.week.nunique() < 4:
        pytest.skip("fewer than 4 weeks of 2025 data before week 10 on this checkout")

    early = early.copy()
    early["fp"] = score_frame(early)
    per_game = early.groupby(["opponent", "position", "week"], as_index=False)["fp"].sum()
    team_pos = per_game.groupby(["opponent", "position"])["fp"].mean()
    league = per_game.groupby("position")["fp"].mean()
    expected = {}
    for (team, pos), val in team_pos.items():
        lg = league.get(pos, val)
        expected[(team, pos)] = float(np.clip(val / lg if lg else 1.0, *proj.DVP_CLIP))

    actual = proj.dvp_factors(season, upto_week)
    assert actual == expected, (
        "dvp_factors did not match the season-only computation -- it is not "
        "reaching the current-season branch"
    )
