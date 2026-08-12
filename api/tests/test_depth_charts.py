"""Depth chart integration into the draft board.

Depth rank is the preseason signal the projections engine does NOT capture — it
reads only completed seasons, and nflverse publishes no preseason box scores. So
these guard the two things that make it useful: it reaches the board, and a
missing rank is never silently treated as "buried".
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.config import league_config
from app.db import read_df
from app.models import vorp


@pytest.fixture(scope="module")
def board():
    return vorp.vorp_board(cfg=league_config())


def test_depth_charts_hold_one_row_per_player():
    df = read_df("SELECT player_id, COUNT(*) n FROM depth_charts GROUP BY player_id HAVING n > 1")
    assert df.empty, "depth_charts must hold only the latest snapshot per player"


def test_depth_rank_is_a_positive_ordinal():
    df = read_df("SELECT depth_rank FROM depth_charts")
    if df.empty:
        pytest.skip("depth charts not synced locally")
    assert (df["depth_rank"] >= 1).all(), "rank 1 is the starter; 0 or negative is meaningless"


def test_board_exposes_depth_rank(board):
    assert "depth_rank" in board.columns
    if board.empty:
        pytest.skip("no board available")
    known = board["depth_rank"].notna().sum()
    assert known > 0, "no player on the board carries a depth chart rank"


def test_missing_depth_rank_is_null_not_zero(board):
    """A player absent from the depth chart must read as unknown. Coercing to 0
    would rank him ahead of every starter anywhere the column is sorted."""
    if board.empty:
        pytest.skip("no board available")
    assert not (board["depth_rank"].fillna(-1) == 0).any()


def test_rationale_flags_backups_but_not_starters(board):
    if board.empty:
        pytest.skip("no board available")
    starters = board[board["depth_rank"] == 1]
    backups = board[board["depth_rank"] > 1]
    if starters.empty or backups.empty:
        pytest.skip("need both starters and backups on the board")

    assert not starters["rationale"].str.contains("depth chart").any(), (
        "being the starter is the expected case and should not clutter every line"
    )
    assert backups["rationale"].str.contains("depth chart").any(), (
        "a backup's depth position is exactly what the rationale should surface"
    )


def test_depth_map_survives_a_missing_table(monkeypatch):
    """Depth charts are optional — a fresh DB must not break the draft board."""
    def boom(*_a, **_kw):
        raise RuntimeError("no such table: depth_charts")

    monkeypatch.setattr(vorp, "read_df", boom)
    assert vorp.depth_map() == {}


def test_depth_map_handles_empty_table(monkeypatch):
    monkeypatch.setattr(vorp, "read_df", lambda *_a, **_kw: pd.DataFrame())
    assert vorp.depth_map() == {}
