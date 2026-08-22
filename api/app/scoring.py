"""Scoring engine: convert a raw stat line into fantasy points per config/league.yaml.

This is the single place scoring rules are applied. Projections, VORP, waivers,
and the lineup optimizer all consume points produced here (or nflverse's
half-PPR column, which `score_offense` reproduces — verified by tests).
"""
from __future__ import annotations

import hashlib
import json
from typing import Mapping

import pandas as pd

from .config import league_config


def _num(stats: Mapping, key: str) -> float:
    v = stats.get(key)
    return float(v) if v is not None and v == v else 0.0  # NaN-safe


def scoring_terms(cfg: dict | None = None) -> dict[str, float]:
    """Canonical stat column -> points per unit.

    Yardage divisors in league.yaml (`yards_per_point: 25`) become multipliers
    (1/25) so every rule is a single multiply and the row-wise and vectorized
    scorers can share one table.

    LINEAR TERMS ONLY. Tiered defensive points-allowed is not expressible as a
    multiplier and stays in `points_allowed_score`.
    """
    s = (cfg or league_config())["scoring"]
    p, r, rec, misc = s["passing"], s["rushing"], s["receiving"], s["misc"]
    fum = misc["fumble_lost"]
    return {
        "passing_yards": 1.0 / p["yards_per_point"],
        "passing_tds": p["touchdown"],
        "interceptions": p["interception"],
        "passing_2pt_conversions": p["two_point"],
        "rushing_yards": 1.0 / r["yards_per_point"],
        "rushing_tds": r["touchdown"],
        "rushing_2pt_conversions": r["two_point"],
        "receptions": rec["reception"],
        "receiving_yards": 1.0 / rec["yards_per_point"],
        "receiving_tds": rec["touchdown"],
        "receiving_2pt_conversions": rec["two_point"],
        "rushing_fumbles_lost": fum,
        "receiving_fumbles_lost": fum,
        "sack_fumbles_lost": fum,
        "special_teams_tds": misc["return_touchdown"],
    }


def score_offense(stats: Mapping, cfg: dict | None = None) -> float:
    """Score a QB/RB/WR/TE stat line. Keys follow nflverse weekly column names."""
    return round(sum(_num(stats, k) * v for k, v in scoring_terms(cfg).items()), 2)


def score_frame(df: pd.DataFrame, cfg: dict | None = None) -> pd.Series:
    """Vectorized `score_offense` over a whole frame.

    Same term table as the row-wise scorer, so the two cannot drift. Columns the
    frame lacks contribute zero, matching `_num`'s treatment of missing keys.
    """
    terms = scoring_terms(cfg)
    cols = [c for c in terms if c in df.columns]
    if not cols:
        return pd.Series(0.0, index=df.index)
    mat = df[cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    weights = pd.Series({c: terms[c] for c in cols}, dtype=float)
    return (mat * weights).sum(axis=1).round(2)


def profile_key(cfg: dict | None = None) -> str:
    """Stable short hash of a scoring block. Identical scoring -> identical key."""
    s = (cfg or league_config())["scoring"]
    blob = json.dumps(s, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:12]


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
