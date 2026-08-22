"""Kicker ingest. score_kicker has always read these names; nothing supplied them."""
from __future__ import annotations

import pandas as pd
import pytest

from app.etl.nfl_data import KICKING_INPUT_COLUMNS, normalize_kicking
from app.scoring import score_kicker

NFLVERSE_K = pd.DataFrame([
    {"season": 2025, "week": 1, "player_id": "00-0000009", "team": "SEA",
     "opponent_team": "SF", "position": "K",
     "fg_made_0_19": 0, "fg_made_20_29": 1, "fg_made_30_39": 1,
     "fg_made_40_49": 1, "fg_made_50_59": 1, "fg_made_60_": 0,
     "fg_missed": 1, "pat_made": 2, "pat_missed": 0},
    {"season": 2025, "week": 1, "player_id": "00-0000010", "team": "BUF",
     "opponent_team": "MIA", "position": "QB",
     "fg_made_0_19": 0, "fg_made_20_29": 0, "fg_made_30_39": 0,
     "fg_made_40_49": 0, "fg_made_50_59": 0, "fg_made_60_": 0,
     "fg_missed": 0, "pat_made": 0, "pat_missed": 0},
])


def test_only_kickers_are_kept():
    out = normalize_kicking(NFLVERSE_K)
    assert list(out["player_id"]) == ["00-0000009"]


def test_every_kicking_scoring_input_survives():
    out = normalize_kicking(NFLVERSE_K)
    missing = [c for c in KICKING_INPUT_COLUMNS if c not in out.columns]
    assert not missing, f"kicking scoring inputs dropped: {missing}"


def test_a_normalized_kicker_row_scores_correctly():
    out = normalize_kicking(NFLVERSE_K)
    # 20-29, 30-39 -> 3 each; 40-49 -> 4; 50-59 -> 5; one miss -> -1; 2 XP -> 2
    assert score_kicker(out.iloc[0].to_dict()) == 3 + 3 + 4 + 5 - 1 + 2


def test_opponent_is_carried_through():
    out = normalize_kicking(NFLVERSE_K)
    assert out.iloc[0]["opponent"] == "SF"


def test_missing_kicking_column_raises_loudly():
    """A genuinely absent scoring column must raise, not silently score as zero.

    weekly_stats.interceptions was silently NULL for three seasons because a
    permissive zero-fill masked a renamed/missing nflverse column. normalize_kicking
    must not repeat that: dropping a real input column here should raise ValueError
    naming the missing column, not fabricate a zero.
    """
    broken = NFLVERSE_K.drop(columns=["fg_made_40_49"])
    with pytest.raises(ValueError, match="fg_made_40_49"):
        normalize_kicking(broken)
