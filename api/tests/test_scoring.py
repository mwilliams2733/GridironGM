"""Scoring engine tests: score_offense vs hand-computed lines, points_allowed tiers,
half-PPR reception value from config."""
from __future__ import annotations

from app.config import league_config
from app.scoring import points_allowed_score, score_offense


def test_score_offense_qb_line():
    # 300 passing yds, 3 TD, 1 INT, 20 rushing yds, 0 fumbles
    stats = {
        "passing_yards": 300, "passing_tds": 3, "interceptions": 1,
        "rushing_yards": 20, "rushing_tds": 0,
    }
    # 300/25=12.0 + 3*4=12 + 1*-2=-2 + 20/10=2.0 => 24.0
    expected = 300 / 25 + 3 * 4 - 2 + 20 / 10
    assert score_offense(stats) == round(expected, 2)


def test_score_offense_wr_line_half_ppr():
    stats = {
        "receptions": 7, "receiving_yards": 95, "receiving_tds": 1,
    }
    cfg = league_config()["scoring"]["receiving"]
    expected = 7 * cfg["reception"] + 95 / cfg["yards_per_point"] + 1 * cfg["touchdown"]
    assert score_offense(stats) == round(expected, 2)


def test_score_offense_fumbles_and_two_point():
    stats = {
        "rushing_yards": 50, "rushing_2pt_conversions": 1, "rushing_fumbles_lost": 1,
        "receiving_fumbles_lost": 1,
    }
    cfg = league_config()["scoring"]
    r, misc = cfg["rushing"], cfg["misc"]
    expected = 50 / r["yards_per_point"] + 1 * r["two_point"] + 2 * misc["fumble_lost"]
    assert score_offense(stats) == round(expected, 2)


def test_score_offense_empty_stats_is_zero():
    assert score_offense({}) == 0.0


def test_score_offense_nan_safe():
    assert score_offense({"passing_yards": float("nan"), "passing_tds": 2}) == 8.0


def test_points_allowed_tier_edges():
    tiers = league_config()["scoring"]["dst"]["points_allowed_tiers"]
    # Exact boundary values must map to the tier they fall within (<=).
    for max_allowed, fantasy_pts in tiers:
        assert points_allowed_score(max_allowed) == float(fantasy_pts)


def test_points_allowed_just_above_boundary_moves_to_next_tier():
    tiers = league_config()["scoring"]["dst"]["points_allowed_tiers"]
    for (max_allowed, _pts), (_next_max, next_pts) in zip(tiers, tiers[1:]):
        assert points_allowed_score(max_allowed + 1) == float(next_pts)


def test_points_allowed_shutout():
    tiers = dict((m, p) for m, p in league_config()["scoring"]["dst"]["points_allowed_tiers"])
    assert points_allowed_score(0) == float(tiers[0])


def test_points_allowed_beyond_last_tier_uses_last():
    tiers = league_config()["scoring"]["dst"]["points_allowed_tiers"]
    assert points_allowed_score(1000) == float(tiers[-1][1])
