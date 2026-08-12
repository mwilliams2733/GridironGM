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


# ---------------------------------------------------------------------------
# ADP delta orientation. web/src/components/TierBadge.tsx renders delta > 0 as
# "falling" (a value) and delta < 0 as "reach", so the sign is user-facing.
# ---------------------------------------------------------------------------
def _row_for(board, pid):
    m = board[board.player_id == pid]
    assert not m.empty, f"{pid} missing from board"
    return m.iloc[0]


def test_pre_adp_player_is_a_reach_not_falling(season, season_proj):
    """At pick 1, a player whose ADP is 45 has not fallen — drafting him is a reach."""
    if season_proj.empty:
        pytest.skip("no season projection data available")
    board = vorp.vorp_board(season=season, season_proj=season_proj, pick_number=1)
    priced = board[board.adp.notna() & (board.adp > 20)]
    if priced.empty:
        pytest.skip("no ADP-priced players beyond pick 20")
    row = priced.iloc[0]
    assert row.adp_delta < 0, "a player taken well before his ADP must read negative"
    assert "falling" not in row.rationale


def test_player_available_past_his_adp_is_falling(season, season_proj):
    """The same top-ADP player still on the board at pick 60 HAS fallen."""
    if season_proj.empty:
        pytest.skip("no season projection data available")
    board = vorp.vorp_board(season=season, season_proj=season_proj, pick_number=60)
    early = board[board.adp.notna() & (board.adp < 10)]
    if early.empty:
        pytest.skip("no early-ADP players available")
    row = early.iloc[0]
    assert row.adp_delta > 0
    assert "falling" in row.rationale


def test_adp_delta_is_picks_past_adp(season, season_proj):
    if season_proj.empty:
        pytest.skip("no season projection data available")
    pick = 30
    board = vorp.vorp_board(season=season, season_proj=season_proj, pick_number=pick)
    priced = board[board.adp.notna()].head(20)
    for _, r in priced.iterrows():
        assert r.adp_delta == round(pick - r.adp, 1)


def test_fills_starter_need_only_when_a_slot_is_open(season, season_proj):
    """A high-VORP player at an already-filled position must not claim to fill a need."""
    if season_proj.empty:
        pytest.skip("no season projection data available")
    cfg = league_config()
    starters = cfg["roster"]["starters"]
    # RB is the discriminating case: fill every RB slot (RB starters + FLEX) and the
    # best remaining RB still carries a large VORP, so a need clause keyed off the
    # composite need_score would keep claiming a starter need.
    need_rb = starters["RB"] + starters.get("FLEX", 0)
    rbs = list(season_proj[season_proj.position == "RB"]
               .sort_values("proj_points", ascending=False)
               .head(need_rb).player_id)
    board = vorp.vorp_board(my_roster=rbs, season=season, season_proj=season_proj)
    remaining_rbs = board[board.pos == "RB"]
    assert not remaining_rbs.empty
    assert remaining_rbs.iloc[0].vorp > 10 * vorp.NEED_UNFILLED_W, \
        "test is only meaningful while a high-VORP RB remains"
    assert not any("fills starter need" in r for r in remaining_rbs.rationale), \
        "RB slots are full — no RB should advertise a starter need"
    # ...while an untouched position still does.
    assert any("fills starter need" in r for r in board[board.pos == "WR"].head(5).rationale)
