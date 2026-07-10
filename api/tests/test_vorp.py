"""VORP tests: replacement levels derived from config, vorp = proj - replacement,
drafted players excluded from board. Uses the real read-only SQLite DB (no network)."""
from __future__ import annotations

import copy

import pytest

from app.config import league_config
from app.models import projections as proj
from app.models import vorp


@pytest.fixture(scope="module")
def season():
    return int(league_config()["league"]["season"])


@pytest.fixture(scope="module")
def season_proj(season):
    return proj.project_season(season)


def test_replacement_levels_from_config():
    cfg = league_config()
    levels = vorp.replacement_levels(cfg)
    teams = cfg["league"]["teams"]
    starters = cfg["roster"]["starters"]
    # QB has no FLEX eligibility -> exactly starters*teams
    assert levels["QB"] == starters["QB"] * teams
    # RB/WR/TE get starters*teams plus a share of FLEX*teams
    assert levels["RB"] > starters["RB"] * teams
    assert levels["WR"] > starters["WR"] * teams


def test_replacement_levels_move_with_config_change():
    cfg = copy.deepcopy(league_config())
    base_levels = vorp.replacement_levels(cfg)

    cfg["league"]["teams"] = cfg["league"]["teams"] + 4  # bigger league
    bigger_levels = vorp.replacement_levels(cfg)

    for pos in ("QB", "RB", "WR", "TE"):
        assert bigger_levels[pos] > base_levels[pos]


def test_replacement_levels_move_with_starter_count():
    cfg = copy.deepcopy(league_config())
    base_levels = vorp.replacement_levels(cfg)

    cfg["roster"]["starters"] = dict(cfg["roster"]["starters"])
    cfg["roster"]["starters"]["WR"] = cfg["roster"]["starters"]["WR"] + 1
    more_wr_levels = vorp.replacement_levels(cfg)

    assert more_wr_levels["WR"] > base_levels["WR"]


def test_vorp_equals_proj_minus_replacement(season, season_proj):
    if season_proj.empty:
        pytest.skip("no season projection data available")
    cfg = league_config()
    repl = vorp.replacement_points(season_proj, cfg)
    board = vorp.vorp_board(season=season, season_proj=season_proj)
    assert not board.empty
    for _, row in board.head(30).iterrows():
        expected = round(row["proj"] - repl.get(row["pos"], 0.0), 1)
        assert row["vorp"] == expected


def test_drafted_players_excluded_from_board(season, season_proj):
    if season_proj.empty:
        pytest.skip("no season projection data available")
    top_ids = set(season_proj.sort_values("proj_points", ascending=False).head(5).player_id)
    board = vorp.vorp_board(drafted_ids=top_ids, season=season, season_proj=season_proj)
    assert not any(pid in top_ids for pid in board.player_id)


def test_my_roster_excluded_from_board(season, season_proj):
    if season_proj.empty:
        pytest.skip("no season projection data available")
    mine = list(season_proj.sort_values("proj_points", ascending=False).head(3).player_id)
    board = vorp.vorp_board(my_roster=mine, season=season, season_proj=season_proj)
    assert not any(pid in mine for pid in board.player_id)


def test_board_has_expected_columns(season, season_proj):
    if season_proj.empty:
        pytest.skip("no season projection data available")
    board = vorp.vorp_board(season=season, season_proj=season_proj)
    expected_cols = {"player_id", "name", "pos", "team", "proj", "vorp", "tier",
                      "bye", "adp", "adp_delta", "need_score", "rationale"}
    assert expected_cols.issubset(set(board.columns))
