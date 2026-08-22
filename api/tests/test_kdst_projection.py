"""K and DST projections come from history, scored per league."""
from __future__ import annotations

import copy

import pandas as pd

from app.config import league_config
from app.models.projections import _dst_per_season, _kicker_per_season


DEF_ROWS = pd.DataFrame([
    {"season": 2025, "week": w, "team": "HOU", "opponent": "IND",
     "def_sacks": 3, "def_interceptions": 1, "def_fumbles_forced": 1,
     "fumble_recovery_opp": 1, "def_tds": 0, "def_safeties": 0,
     "def_punt_blocks": 0, "def_pat_blocks": 0, "def_fg_blocks": 0,
     "special_teams_tds": 0, "points_allowed": 17}
    for w in range(1, 6)
])


def test_dst_scores_through_the_leagues_own_points_allowed_ladder():
    """17 allowed is worth 1 point in the ESPN ladder and 1 in Sleeper's, but a
    shutout is worth 5 vs 10 — so the ladders must not be shared."""
    espn = league_config("league1")  # explicit: this test's numbers are ESPN's ladder
    sleeper = copy.deepcopy(espn)
    sleeper["scoring"]["dst"]["points_allowed_tiers"] = [
        [0, 10], [6, 7], [13, 4], [20, 1], [27, 0], [34, -1], [999, -4]]

    shutout = DEF_ROWS.assign(points_allowed=0)
    e = _dst_per_season(shutout, espn)
    s = _dst_per_season(shutout, sleeper)
    assert s.iloc[0]["ppg"] - e.iloc[0]["ppg"] == 5.0


def test_dst_per_season_aggregates_games_and_ppg():
    # ESPN dst scoring: sack=1, interception=2, fumble_recovery=2, touchdown=6,
    # safety=2, block_kick=2; points_allowed_tiers has [17, 1] (<=17 allowed -> 1).
    # DEF_ROWS: 3 sacks + 1 INT + 1 fumble_recovery_opp + 17 allowed, per game:
    #   3*1 + 1*2 + 1*2 + 0*6 + 0*2 + 0*2 + 1 (PA tier) = 3 + 2 + 2 + 1 = 8.0
    out = _dst_per_season(DEF_ROWS, league_config("league1"))  # explicit: 8.0 is ESPN's ladder
    row = out.iloc[0]
    assert row["games"] == 5
    assert row["season"] == 2025
    assert row["ppg"] == 8.0


def test_kicker_per_season_aggregates_games_and_ppg():
    k = pd.DataFrame([
        {"season": 2025, "week": w, "player_id": "k1", "team": "SEA",
         "fg_made_0_19": 0, "fg_made_20_29": 1, "fg_made_30_39": 0,
         "fg_made_40_49": 1, "fg_made_50_59": 0, "fg_made_60_": 0,
         "fg_missed": 0, "pat_made": 2, "pat_missed": 0}
        for w in range(1, 5)
    ])
    out = _kicker_per_season(k, league_config())
    assert out.iloc[0]["games"] == 4
    assert out.iloc[0]["ppg"] == 9.0  # 3 + 4 + 2
