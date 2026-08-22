"""Term-table scoring: one rule set, two scorers."""
from __future__ import annotations

import pandas as pd

from app.config import league_config
from app.scoring import profile_key, score_frame, score_offense, scoring_terms

STATS = {
    "passing_yards": 310, "passing_tds": 3, "interceptions": 2,
    "passing_2pt_conversions": 1, "rushing_yards": 41, "rushing_tds": 1,
    "rushing_2pt_conversions": 0, "receptions": 4, "receiving_yards": 52,
    "receiving_tds": 0, "receiving_2pt_conversions": 0,
    "rushing_fumbles_lost": 0, "receiving_fumbles_lost": 0,
    "sack_fumbles_lost": 1, "special_teams_tds": 0,
}


def test_yardage_divisors_become_multipliers():
    t = scoring_terms()
    assert t["passing_yards"] == 1 / 25
    assert t["rushing_yards"] == 1 / 10
    assert t["receiving_yards"] == 1 / 10


def test_reception_value_comes_from_config():
    t = scoring_terms()
    assert t["receptions"] == league_config()["scoring"]["receiving"]["reception"]


def test_all_three_fumble_columns_share_the_fumble_value():
    t = scoring_terms()
    lost = league_config()["scoring"]["misc"]["fumble_lost"]
    for c in ("rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost"):
        assert t[c] == lost


def test_score_frame_agrees_with_score_offense_row_for_row():
    """The invariant the bulk path depends on. Derive, don't duplicate."""
    df = pd.DataFrame([
        STATS,
        {**STATS, "receptions": 9, "passing_tds": 0},
        # passing_yards=311 -> 311/25=12.44, a genuine 2nd decimal digit.
        # Every other row here is a round number (310/25=12.40, 41/10=4.10,
        # 52/10=5.20, ...), so a regression that rounds score_frame to only
        # 1 decimal place (e.g. .round(1) instead of .round(2)) is invisible
        # on those rows: 37.7 == 37.7 either way. This row is what actually
        # exercises the 2-decimal-place equivalence — don't "simplify" it
        # back to a round number.
        {**STATS, "passing_yards": 311},
    ])
    bulk = score_frame(df)
    for i in range(len(df)):
        assert bulk.iloc[i] == score_offense(df.iloc[i].to_dict())


def test_score_frame_treats_missing_columns_as_zero():
    df = pd.DataFrame([{"receptions": 4, "receiving_yards": 52}])
    expected = score_offense({"receptions": 4, "receiving_yards": 52})
    assert score_frame(df).iloc[0] == expected


def test_score_frame_treats_nan_as_zero():
    df = pd.DataFrame([{**STATS, "receiving_yards": float("nan")}])
    expected = score_offense({**STATS, "receiving_yards": 0})
    assert score_frame(df).iloc[0] == expected


def test_a_different_scoring_block_gives_a_different_profile_key():
    cfg = league_config()
    import copy
    full_ppr = copy.deepcopy(cfg)
    full_ppr["scoring"]["receiving"]["reception"] = 1.0
    assert profile_key(cfg) != profile_key(full_ppr)


def test_profile_key_is_stable_across_calls():
    assert profile_key() == profile_key()
