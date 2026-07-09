"""Scoring engine: convert a raw stat line into fantasy points per config/league.yaml.

This is the single place scoring rules are applied. Projections, VORP, waivers,
and the lineup optimizer all consume points produced here (or nflverse's
half-PPR column, which `score_offense` reproduces — verified by tests).
"""
from __future__ import annotations

from typing import Mapping

from .config import league_config


def _num(stats: Mapping, key: str) -> float:
    v = stats.get(key)
    return float(v) if v is not None and v == v else 0.0  # NaN-safe


def score_offense(stats: Mapping) -> float:
    """Score a QB/RB/WR/TE stat line. Keys follow nflverse weekly column names."""
    s = league_config()["scoring"]
    p, r, rec, misc = s["passing"], s["rushing"], s["receiving"], s["misc"]
    pts = 0.0
    # passing
    pts += _num(stats, "passing_yards") / p["yards_per_point"]
    pts += _num(stats, "passing_tds") * p["touchdown"]
    pts += _num(stats, "interceptions") * p["interception"]
    pts += _num(stats, "passing_2pt_conversions") * p["two_point"]
    # rushing
    pts += _num(stats, "rushing_yards") / r["yards_per_point"]
    pts += _num(stats, "rushing_tds") * r["touchdown"]
    pts += _num(stats, "rushing_2pt_conversions") * r["two_point"]
    # receiving
    pts += _num(stats, "receptions") * rec["reception"]
    pts += _num(stats, "receiving_yards") / rec["yards_per_point"]
    pts += _num(stats, "receiving_tds") * rec["touchdown"]
    pts += _num(stats, "receiving_2pt_conversions") * rec["two_point"]
    # misc
    fumbles = (
        _num(stats, "rushing_fumbles_lost")
        + _num(stats, "receiving_fumbles_lost")
        + _num(stats, "sack_fumbles_lost")
    )
    pts += fumbles * misc["fumble_lost"]
    pts += _num(stats, "special_teams_tds") * misc["return_touchdown"]
    return round(pts, 2)


def score_kicker(stats: Mapping) -> float:
    k = league_config()["scoring"]["kicking"]
    pts = 0.0
    pts += _num(stats, "fg_made_0_19") * k["fg_0_39"]
    pts += _num(stats, "fg_made_20_29") * k["fg_0_39"]
    pts += _num(stats, "fg_made_30_39") * k["fg_0_39"]
    pts += _num(stats, "fg_made_40_49") * k["fg_40_49"]
    pts += _num(stats, "fg_made_50_59") * k["fg_50_plus"]
    pts += _num(stats, "fg_made_60_") * k["fg_50_plus"]
    pts += _num(stats, "fg_missed") * k["fg_missed"]
    pts += _num(stats, "pat_made") * k["xp_made"]
    pts += _num(stats, "pat_missed") * k["xp_missed"]
    return round(pts, 2)


def score_dst(stats: Mapping) -> float:
    d = league_config()["scoring"]["dst"]
    pts = 0.0
    pts += _num(stats, "sacks") * d["sack"]
    pts += _num(stats, "interceptions") * d["interception"]
    pts += _num(stats, "fumble_recoveries") * d["fumble_recovery"]
    pts += _num(stats, "touchdowns") * d["touchdown"]
    pts += _num(stats, "safeties") * d["safety"]
    pts += _num(stats, "blocked_kicks") * d["block_kick"]
    pts += points_allowed_score(_num(stats, "points_allowed"))
    return round(pts, 2)


def points_allowed_score(points_allowed: float) -> float:
    tiers = league_config()["scoring"]["dst"]["points_allowed_tiers"]
    for max_allowed, fantasy_pts in tiers:
        if points_allowed <= max_allowed:
            return float(fantasy_pts)
    return float(tiers[-1][1])
