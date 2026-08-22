"""Weekly-stats ingest tests: nflverse column normalization.

These guard the boundary where nflverse's schema meets ours. The keep-filter in
``normalize_weekly`` is deliberately permissive so an unpublished season or a
dropped optional column doesn't sink a sync — which is exactly how
``passing_interceptions`` was silently lost for three seasons, leaving
``weekly_stats.interceptions`` 100% NULL and ``score_offense`` blind to picks
when fed DB rows.

So these tests assert on real nflverse column names, not on our own.
"""
from __future__ import annotations

import pandas as pd

from app.etl.nfl_data import SCORING_INPUT_COLUMNS, normalize_weekly
from app.scoring import score_offense

# One QB week, spelled the way nflverse currently spells it.
NFLVERSE_ROW = {
    "season": 2025,
    "week": 3,
    "player_id": "00-0000001",
    "team": "BUF",
    "opponent_team": "MIA",
    "position": "QB",
    "completions": 24,
    "attempts": 35,
    "passing_yards": 310,
    "passing_tds": 3,
    "passing_interceptions": 2,          # <- NOT "interceptions"
    "sacks_suffered": 1,
    "carries": 6,
    "rushing_yards": 41,
    "rushing_tds": 1,
    "receptions": 0,
    "targets": 0,
    "receiving_yards": 0,
    "receiving_tds": 0,
    "target_share": 0.0,
    "air_yards_share": 0.0,
    "rushing_fumbles_lost": 0,
    "receiving_fumbles_lost": 0,
    "sack_fumbles_lost": 1,
    "passing_2pt_conversions": 1,
    "rushing_2pt_conversions": 0,
    "receiving_2pt_conversions": 0,
    "special_teams_tds": 0,
}


def _nflverse_points(s: dict) -> float:
    """nflverse standard scoring, written out independently of `scoring.py`.

    Deliberately hardcoded rather than read from league.yaml: this encodes the
    EXTERNAL contract our stored points column is built on, so comparing it to
    our config-driven engine is a real check and not the engine agreeing with
    itself. Standard = half-PPR minus the per-reception value.
    """
    return (
        s["passing_yards"] * 0.04
        + s["passing_tds"] * 4
        + s["passing_interceptions"] * -2
        + s["passing_2pt_conversions"] * 2
        + s["rushing_yards"] * 0.1
        + s["rushing_tds"] * 6
        + s["rushing_2pt_conversions"] * 2
        + s["receiving_yards"] * 0.1
        + s["receiving_tds"] * 6
        + s["receiving_2pt_conversions"] * 2
        + (s["rushing_fumbles_lost"] + s["receiving_fumbles_lost"]
           + s["sack_fumbles_lost"]) * -2
        + s["special_teams_tds"] * 6
    )


def _frame(**overrides) -> pd.DataFrame:
    """One nflverse-shaped row; `fantasy_points` always matches its own stat line."""
    row = {**NFLVERSE_ROW, **overrides}
    row["fantasy_points"] = _nflverse_points(row)
    return pd.DataFrame([row])


def test_passing_interceptions_is_mapped_onto_interceptions():
    """nflverse renamed this column; we store it as `interceptions`."""
    out = normalize_weekly(_frame())
    assert "interceptions" in out.columns
    assert out.iloc[0]["interceptions"] == 2


def test_opponent_team_is_mapped_onto_opponent():
    out = normalize_weekly(_frame())
    assert out.iloc[0]["opponent"] == "MIA"


def test_sacks_suffered_is_mapped_onto_sacks():
    out = normalize_weekly(_frame())
    assert out.iloc[0]["sacks"] == 1


def test_every_scoring_input_survives_normalization():
    """The guard the original bug needed.

    `score_offense` reads these columns off stored rows. If normalization drops
    one, scoring silently under- or over-counts instead of failing, so assert
    presence and non-nullness rather than trusting the permissive keep-filter.
    """
    out = normalize_weekly(_frame())
    missing = [c for c in SCORING_INPUT_COLUMNS if c not in out.columns]
    assert not missing, f"scoring inputs dropped during normalization: {missing}"
    nulls = [c for c in SCORING_INPUT_COLUMNS if out[c].isna().any()]
    assert not nulls, f"scoring inputs arrived null: {nulls}"


def test_a_normalized_row_scores_the_same_as_nflverse_standard_plus_ppr():
    """The invariant per-league scoring depends on.

    There is no stored points column any more, so score the normalized row
    directly and compare against independently-computed nflverse standard
    scoring plus the configured per-reception value.
    """
    from app.config import league_config
    row_in = {**NFLVERSE_ROW, "receptions": 4, "receiving_yards": 52}
    out = normalize_weekly(_frame(receptions=4, receiving_yards=52))
    rec_val = league_config()["scoring"]["receiving"]["reception"]
    expected = round(_nflverse_points(row_in) + 4 * rec_val, 2)
    assert score_offense(out.iloc[0].to_dict()) == expected


def test_normalized_frame_has_no_baked_points_column():
    out = normalize_weekly(_frame())
    assert "fantasy_points_half_ppr" not in out.columns


def test_a_missing_optional_column_still_syncs():
    """Permissiveness is intentional for columns we don't score on."""
    df = _frame().drop(columns=["air_yards_share"])
    out = normalize_weekly(df)
    assert "air_yards_share" not in out.columns
    assert out.iloc[0]["interceptions"] == 2
