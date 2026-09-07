"""Projections engine — the single source of truth for GridironGM player value.

Everything downstream (VORP/draft board, waiver ranking, lineup optimizer) consumes
the DataFrames produced here. Three scopes:

    project_season(season) -> full-season half-PPR point totals (draft prep)
    project_week(season, week) -> matchup-adjusted single-week points
    project_ros(season, week) -> remaining-schedule aggregate

Design principles
-----------------
* League settings ALWAYS come from ``league_config()`` — nothing hard-coded.
* Data-fetch is separated from math so the math is unit-testable without a DB
  (see ``_weight_seasons``, ``age_multiplier``, ``dvp_factors`` on plain frames).
* Odds/injuries may be missing for any given week — every adjustment degrades to a
  neutral (1.0 / no-op) factor rather than raising.
* Every constant here is documented in ``docs/MODELING.md`` with rationale.

All formulas, weights and constants live in the ``CONST`` block below so MODELING.md
and the code cannot drift.
"""
from __future__ import annotations

import json
from datetime import date
from functools import lru_cache

import numpy as np
import pandas as pd

from ..config import league_config
from ..db import connect, read_df, upsert_rows

# ---------------------------------------------------------------------------
# Tunable constants (mirrored in docs/MODELING.md — keep the two in sync)
# ---------------------------------------------------------------------------
OFF_POS = ("QB", "RB", "WR", "TE")
# Canonical team abbreviations follow the schedules/weekly_stats/odds convention.
# The players table uses "AZ" and FFC ADP uses "LAR"; normalize both.
TEAM_NORM = {"AZ": "ARI", "LAR": "LA"}


def norm_team(team) -> str | None:
    if team is None or team != team:
        return team
    return TEAM_NORM.get(team, team)

REG_SEASON_WEEKS = 18            # NFL regular season; weeks 19-22 are playoffs (excluded)
FULL_SLATE = 17                  # games a fully-available player plays in an 18-week season


def last_scoring_week(cfg: dict | None = None) -> int:
    """Final week that counts toward making the fantasy playoffs.

    A league whose playoffs start week 15 stops accruing regular-season value at
    week 14; weeks 15-17 pay out only if you qualify and week 18 never does.
    Since waiver rankings are ROS-driven, an uncapped horizon systematically
    overvalues players with strong late schedules.

    Absent `playoff_week_start`, returns REG_SEASON_WEEKS so behaviour is
    unchanged -- which is what keeps the three ESPN leagues identical.
    """
    cfg = cfg or league_config()
    start = cfg["league"].get("playoff_week_start")
    if start is None:
        start = REG_SEASON_WEEKS + 1
    return min(REG_SEASON_WEEKS, int(start) - 1)


# Recency weights applied to the 3 most-recent completed seasons (newest first).
RECENCY_WEIGHTS = (0.50, 0.30, 0.20)

# Games-played regression: proj_games = blend * weighted_games + (1-blend) * league mean.
GAMES_REGRESS_BLEND = 0.65
LEAGUE_MEAN_GAMES = 15.5         # ~ mean games for a player who appears in a season

# Usage-trend adjustment: recent-vs-prior opportunity ratio maps to +/- this fraction.
USAGE_TREND_MAX = 0.10           # cap at +/-10%
USAGE_TREND_GAIN = 0.50          # sensitivity: factor = 1 + GAIN*(ratio-1), then clipped

# Team-change dampener + extra variance when a player's 2026 team != last-played team.
TEAM_CHANGE_DAMPEN = 0.96
TEAM_CHANGE_VAR_MULT = 1.15

# Floor/ceiling percentile (25th/75th ~ +/-0.6745 sigma of a normal).
PCTL_Z = 0.6745
DEFAULT_GAMES_SIGMA = 2.5        # fallback availability std when only one season of data

# Weekly model ---------------------------------------------------------------
WEEK_FORM_BLEND = 0.40           # weight on current-season recent form vs season baseline
WEEK_FORM_LOOKBACK = 4           # games of "recent form"
DVP_CLIP = (0.80, 1.20)          # opponent defense-vs-position multiplier bounds
IMPLIED_TOTAL_GAIN = 0.50        # share of a player's output tied to team total
GAME_ENV_CLIP = (0.85, 1.20)
LEAGUE_AVG_IMPLIED = 22.9        # mean implied team total (measured on 2026 odds); refreshed if odds present

# Spread / game-script: per-position response to being favored (neg spread) vs underdog.
# factor = 1 + coeff * (-spread/10), clipped to +/-SCRIPT_MAX.
SCRIPT_COEFF = {"QB": -0.04, "RB": 0.05, "WR": -0.03, "TE": -0.01}
SCRIPT_MAX = 0.08
HOME_FIELD = 0.02                # +2% home, -2% road

# Injury status dampeners (report_status from injuries table).
INJURY_MULT = {"Out": 0.0, "Doubtful": 0.25, "Questionable": 0.92}

# Age curves — position-specific multiplier vs peak (1.00). Linear-interpolated
# between anchors; flat outside the range. Age = season year - birth year.
AGE_CURVES = {
    "RB": {21: 0.95, 23: 1.00, 26: 1.00, 27: 0.96, 28: 0.91, 29: 0.85, 30: 0.78, 32: 0.65},
    "WR": {21: 0.90, 23: 0.98, 24: 1.00, 28: 1.00, 29: 0.98, 30: 0.95, 31: 0.90, 33: 0.80, 35: 0.68},
    "TE": {22: 0.85, 24: 0.96, 25: 1.00, 29: 1.00, 30: 0.97, 31: 0.92, 32: 0.86, 34: 0.75},
    "QB": {23: 0.95, 25: 1.00, 34: 1.00, 35: 0.98, 36: 0.95, 37: 0.90, 39: 0.80},
}


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------
@lru_cache(maxsize=8)
def _completed_seasons(season: int) -> tuple[int, ...]:
    """The (up to 3) completed seasons feeding a projection for ``season``."""
    n = int(league_config()["league"].get("history_seasons", 3))
    return tuple(season - i for i in range(1, n + 1))


# Raw stat columns pulled for scoring. League-agnostic — every league scores the
# same underlying stat line through its own term table.
_WEEKLY_RAW_COLUMNS = (
    "season", "week", "player_id", "team", "opponent", "position",
    "carries", "targets", "receptions", "attempts",
    "passing_yards", "passing_tds", "interceptions", "passing_2pt_conversions",
    "rushing_yards", "rushing_tds", "rushing_2pt_conversions",
    "receiving_yards", "receiving_tds", "receiving_2pt_conversions",
    "rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost",
    "special_teams_tds",
)


@lru_cache(maxsize=8)
def _weekly_rows(seasons: tuple[int, ...]) -> pd.DataFrame:
    """Raw regular-season stat lines for exactly ``seasons``.

    Cached on the season tuple alone, NOT on league: the stat line is the same
    for everyone, only the scoring differs. Scoring is applied by `_weekly`,
    which is cheap enough (a vectorized pass over ~18k rows) not to need its
    own cache. Two different callers need two different season sets -- see
    `_weekly_raw` (feeder seasons for a season projection) vs. `dvp_factors`/
    `_recent_form` (the season actually being played) -- so this takes the
    tuple explicitly rather than deriving it itself.
    """
    if not seasons:
        return read_df(f"SELECT {', '.join(_WEEKLY_RAW_COLUMNS)} FROM weekly_stats WHERE 0")
    q = (
        f"SELECT {', '.join(_WEEKLY_RAW_COLUMNS)} "
        f"FROM weekly_stats WHERE season IN ({','.join('?' for _ in seasons)}) "
        f"AND week <= {REG_SEASON_WEEKS}"
    )
    return read_df(q, tuple(seasons))


def _weekly_raw(season: int) -> pd.DataFrame:
    """Raw stat lines for the completed seasons FEEDING a projection for ``season``.

    Deliberately excludes ``season`` itself (see `_completed_seasons`) -- this
    is what `project_season`'s per-player history aggregates consume. Callers
    that need the season actually being played (`dvp_factors`, `_recent_form`)
    must call `_weekly_rows` directly with their own season tuple instead.
    """
    return _weekly_rows(_completed_seasons(season))


def _score_weekly(df: pd.DataFrame, cfg: dict | None) -> pd.DataFrame:
    """Attach an `fp` column scored for ``cfg``'s league to a raw stat frame."""
    from ..scoring import score_frame

    # The .copy() is LOAD-BEARING, not defensive habit: `_weekly_raw` returns the
    # `@lru_cache`d frame itself, unwrapped. Without the copy, scoring one league
    # writes `fp` into the shared cached frame and the next league to ask for the
    # same seasons reads the previous league's points. Do not "clean this up".
    df = df.copy()
    if df.empty:
        return df
    df["fp"] = score_frame(df, cfg)
    return df


def _weekly(season: int, cfg: dict | None = None) -> pd.DataFrame:
    """Weekly stat lines (feeder seasons for ``season``) with an `fp` column
    scored for ``cfg``'s league. Used by `project_season`'s history aggregates."""
    return _score_weekly(_weekly_raw(season), cfg)


@lru_cache(maxsize=1)
def _players() -> pd.DataFrame:
    return read_df("SELECT player_id, name, position, team, birthdate FROM players")


@lru_cache(maxsize=4)
def _snaps(season: int) -> pd.DataFrame:
    seasons = _completed_seasons(season)
    q = (
        "SELECT season, week, player_id, offense_pct FROM snap_counts "
        f"WHERE season IN ({','.join('?' for _ in seasons)}) AND week <= {REG_SEASON_WEEKS}"
    )
    return read_df(q, tuple(seasons))


def _schedule_opponents(season: int, week: int) -> dict[str, tuple[str, bool]]:
    """team -> (opponent, is_home) for one week, from the schedules table."""
    sch = read_df(
        "SELECT home_team, away_team FROM schedules WHERE season=? AND week=?",
        (season, week),
    )
    out: dict[str, tuple[str, bool]] = {}
    for _, r in sch.iterrows():
        out[r.home_team] = (r.away_team, True)
        out[r.away_team] = (r.home_team, False)
    return out


def _bye_weeks(season: int) -> dict[str, int]:
    """team -> bye week for a season (the 1..18 week with no scheduled game)."""
    sch = read_df(
        "SELECT week, home_team, away_team FROM schedules WHERE season=? AND week<=?",
        (season, REG_SEASON_WEEKS),
    )
    teams = pd.unique(sch[["home_team", "away_team"]].values.ravel())
    played: dict[str, set[int]] = {t: set() for t in teams}
    for _, r in sch.iterrows():
        played[r.home_team].add(int(r.week))
        played[r.away_team].add(int(r.week))
    byes: dict[str, int] = {}
    for t, wks in played.items():
        missing = [w for w in range(1, REG_SEASON_WEEKS + 1) if w not in wks]
        if missing:
            byes[t] = missing[0]
    return byes


# ---------------------------------------------------------------------------
# Pure math helpers (DB-free — unit-testable)
# ---------------------------------------------------------------------------
def _interp_curve(curve: dict[int, float], age: float) -> float:
    xs = sorted(curve)
    if age <= xs[0]:
        return curve[xs[0]]
    if age >= xs[-1]:
        return curve[xs[-1]]
    for lo, hi in zip(xs, xs[1:]):
        if lo <= age <= hi:
            f = (age - lo) / (hi - lo)
            return curve[lo] + f * (curve[hi] - curve[lo])
    return 1.0


def age_multiplier(position: str, age: float | None) -> float:
    """Age-curve multiplier vs peak. Neutral (1.0) if age unknown or pos uncurved."""
    if age is None or age != age or position not in AGE_CURVES:
        return 1.0
    return _interp_curve(AGE_CURVES[position], age)


def _weight_seasons(per_season: pd.DataFrame, newest: int, trend: bool = True) -> dict:
    """Recency- and reliability-weighted aggregation of a player's seasons.

    ``per_season`` columns: season, games, ppg, opp_pg (opportunities/game).
    Returns base_ppg, proj_games, usage_trend, and pooled dispersion inputs.

    ``trend=False`` skips the usage-trend computation and pins it at 1.0.
    Kickers and DSTs have no meaningful "opportunity" concept -- setting
    opp_pg equal to ppg does NOT make the ratio inert (a team/kicker whose
    ppg genuinely varies year to year still yields a non-1.0 ratio), so
    those callers pass trend=False rather than relying on a degenerate input.
    """
    if per_season.empty:
        return {"base_ppg": 0.0, "proj_games": 0.0, "usage_trend": 1.0, "n_seasons": 0}

    per_season = per_season.set_index("season")
    rec = {}
    for i, w in enumerate(RECENCY_WEIGHTS):
        rec[newest - i] = w

    # Reliability-weighted base ppg: recency * (games/full_slate, capped 1).
    num = den = 0.0
    gnum = gden = 0.0
    for s, row in per_season.iterrows():
        r = rec.get(s, 0.0)
        if r == 0.0:
            continue
        reliab = min(row.games, FULL_SLATE) / FULL_SLATE
        eff = r * reliab
        num += eff * row.ppg
        den += eff
        gnum += r * row.games
        gden += r
    base_ppg = num / den if den else float(per_season.ppg.mean())
    weighted_games = gnum / gden if gden else float(per_season.games.mean())
    proj_games = GAMES_REGRESS_BLEND * weighted_games + (1 - GAMES_REGRESS_BLEND) * LEAGUE_MEAN_GAMES
    proj_games = float(np.clip(proj_games, 6.0, FULL_SLATE))

    # Usage trend: most-recent season opp/g vs weighted mean of earlier seasons.
    usage_trend = 1.0
    if trend and newest in per_season.index and len(per_season) >= 2:
        recent = per_season.loc[newest, "opp_pg"]
        prior = per_season.drop(index=newest)["opp_pg"]
        prior_mean = float(prior.mean())
        if prior_mean > 0 and recent == recent:
            ratio = recent / prior_mean
            usage_trend = 1 + USAGE_TREND_GAIN * (ratio - 1)
            usage_trend = float(np.clip(usage_trend, 1 - USAGE_TREND_MAX, 1 + USAGE_TREND_MAX))

    return {
        "base_ppg": float(base_ppg),
        "proj_games": proj_games,
        "weighted_games": float(weighted_games),
        "usage_trend": usage_trend,
        "n_seasons": int((per_season.index.isin(rec)).sum()),
    }


def _opportunities(df: pd.DataFrame) -> pd.Series:
    """Per-row opportunity count used for the usage trend (position-aware)."""
    carries = df.carries.fillna(0)
    targets = df.targets.fillna(0)
    attempts = df.attempts.fillna(0)
    is_qb = df.position == "QB"
    return np.where(is_qb, attempts + carries, carries + targets)


# ---------------------------------------------------------------------------
# Season projection
# ---------------------------------------------------------------------------
def _birth_year(birthdate) -> int | None:
    if not birthdate or birthdate != birthdate:
        return None
    try:
        return int(str(birthdate)[:4])
    except ValueError:
        return None


def _inject_adp_rookies(df: pd.DataFrame, season: int, cfg: dict) -> pd.DataFrame:
    """A rookie has zero `weekly_stats` rows, so the history-driven loop above
    skips him entirely -- the "No rookies" gap (see project memory
    gridiron-gm-modeling-gaps.md item 4; Travis Hunter was the flagged case).
    Give him a projection derived from ADP alone, since draft-position market
    consensus is the only signal that exists for a player with no box scores.

    Scope is deliberately narrow: only players who are (a) on a CURRENT NFL
    roster -- `depth_charts` is season-scoped (unlike `players`, which is
    nflreadpy's all-time roster history and would otherwise resurrect retired
    players with no history, e.g. Troy Aikman, as "rookies") and (b) resolve to
    an ADP row, so a deep-roster player the market has no opinion on doesn't
    get an invented value.

    Projection = ``np.interp`` of the rookie's ADP against the (adp,
    proj_points) curve already implied by this league's OWN history-based
    rows AT THE SAME POSITION -- the market-to-points relationship this
    projection engine has already established, not a new assumption about
    rookie talent. Positions do NOT share a curve: proj_points scale is
    position-specific (a QB1 outscores an RB1 by ~80 points at similar ADP),
    so pooling positions makes the interpolation pick up whichever position
    happens to sit at a nearby ADP rather than the rookie's own market tier --
    verified against real data: a pooled curve put a 67-ADP rookie RB (208.7)
    above Saquon Barkley and Breece Hall because a 66-ADP QB spike was the
    nearest neighbor.
    """
    from .vorp import resolve_adp  # deferred: vorp imports this module at load time

    if df.empty:
        return df

    on_roster = set(read_df(
        f"SELECT DISTINCT player_id FROM depth_charts WHERE pos IN "
        f"({','.join('?' for _ in OFF_POS)})", OFF_POS)["player_id"])
    history = set(read_df("SELECT DISTINCT player_id FROM weekly_stats")["player_id"])
    rookie_ids = on_roster - history - set(df["player_id"])
    if not rookie_ids:
        return df

    adp = resolve_adp(season, cfg).dropna(subset=["player_id"])
    rookie_adp = adp[adp.player_id.isin(rookie_ids)]
    if rookie_adp.empty:
        return df

    curve_all = adp.merge(df[["player_id", "proj_points"]], on="player_id", how="inner")
    players = _players().set_index("player_id")

    rows = []
    for _, r in rookie_adp.iterrows():
        pid, pos = r["player_id"], r["position"]
        curve = curve_all[curve_all.position == pos].sort_values("adp")
        if len(curve) < 2:
            continue  # not enough same-position, ADP-resolved players to interpolate against
        xp, fp = curve["adp"].to_numpy(), curve["proj_points"].to_numpy()
        proj_points = float(np.interp(r["adp"], xp, fp))
        meta = players.loc[pid] if pid in players.index else None
        name = meta["name"] if meta is not None else pid
        team = norm_team(meta["team"]) if meta is not None else None
        rows.append({
            "player_id": pid, "name": name, "position": pos, "team": team,
            "proj_points": round(proj_points, 1),
            "proj_ppg": round(proj_points / FULL_SLATE, 2),
            # Wide, fixed band -- there is no game-log variance to measure for a
            # rookie, so this cannot use the season_sigma calc above.
            "floor": round(proj_points * 0.6, 1),
            "ceiling": round(proj_points * 1.4, 1),
            "components": {"rookie_adp_based": True, "adp": float(r["adp"])},
        })
    if not rows:
        return df
    return pd.concat([df, pd.DataFrame(rows)], ignore_index=True)


def project_season(season: int, store: bool = False, cfg: dict | None = None) -> pd.DataFrame:
    """Full-season projections (scored per ``cfg``'s league) for every offensive
    player with history, plus anchored K/DST models. Columns: player_id, name,
    position, team, proj_points, floor, ceiling, components (dict)."""
    if cfg is None:
        cfg = league_config()
    wk = _weekly(season, cfg)
    if wk.empty:
        return pd.DataFrame()
    newest = max(_completed_seasons(season))
    wk["opp"] = _opportunities(wk)

    # Per player-season aggregates.
    grp = wk.groupby(["player_id", "season"])
    per = grp.agg(
        games=("fp", "size"),
        ppg=("fp", "mean"),
        opp_pg=("opp", "mean"),
        std=("fp", "std"),
        position=("position", "last"),
        team=("team", "last"),
    ).reset_index()

    players = _players().set_index("player_id")
    rows = []
    for pid, pdf in per.groupby("player_id"):
        agg = _weight_seasons(
            pdf[["season", "games", "ppg", "opp_pg"]], newest
        )
        if agg["n_seasons"] == 0 or agg["base_ppg"] <= 0:
            continue
        pos = pdf.sort_values("season").iloc[-1].position
        last_team = pdf.sort_values("season").iloc[-1].team

        meta = players.loc[pid] if pid in players.index else None
        name = meta["name"] if meta is not None else pid
        cur_team = norm_team(meta["team"] if meta is not None and meta["team"] else last_team)
        last_team = norm_team(last_team)
        birth = _birth_year(meta["birthdate"]) if meta is not None else None
        age = (season - birth) if birth else None

        age_mult = age_multiplier(pos, age)
        team_changed = bool(cur_team and last_team and cur_team != last_team)
        team_mult = TEAM_CHANGE_DAMPEN if team_changed else 1.0

        adj_ppg = agg["base_ppg"] * agg["usage_trend"] * age_mult * team_mult
        proj_games = agg["proj_games"]
        proj_points = adj_ppg * proj_games

        # Dispersion: per-game variance across games + availability variance.
        player_games = wk[wk.player_id == pid]
        pergame_std = float(player_games.fp.std(ddof=0)) if len(player_games) > 1 else adj_ppg * 0.5
        games_sigma = float(pdf.games.std(ddof=0)) if len(pdf) > 1 else DEFAULT_GAMES_SIGMA
        season_sigma = np.sqrt(
            (pergame_std ** 2) * proj_games + (adj_ppg * games_sigma) ** 2
        )
        if team_changed:
            season_sigma *= TEAM_CHANGE_VAR_MULT
        floor = max(0.0, proj_points - PCTL_Z * season_sigma)
        ceiling = proj_points + PCTL_Z * season_sigma

        rows.append({
            "player_id": pid, "name": name, "position": pos, "team": cur_team,
            "proj_points": round(proj_points, 1),
            "proj_ppg": round(adj_ppg, 2),
            "floor": round(floor, 1), "ceiling": round(ceiling, 1),
            "components": {
                "base_ppg": round(agg["base_ppg"], 2),
                "proj_games": round(proj_games, 1),
                "usage_trend": round(agg["usage_trend"], 3),
                "age": age, "age_mult": round(age_mult, 3),
                "team_changed": team_changed, "team_mult": team_mult,
                "pergame_std": round(pergame_std, 2),
                "n_seasons": agg["n_seasons"],
            },
        })

    df = pd.DataFrame(rows)
    kdst = _project_k_dst_season(season, cfg)
    if not kdst.empty:
        df = pd.concat([df, kdst], ignore_index=True)
    df = _inject_adp_rookies(df, season, cfg)
    df = df.sort_values("proj_points", ascending=False).reset_index(drop=True)
    if store:
        _store(df, scope="season", season=season, week=0)
    return df


# ---------------------------------------------------------------------------
# Kicker / DST history-driven season models
# ---------------------------------------------------------------------------
def _dst_per_season(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """Per (team, season) games and points-per-game for a team defense.

    Points-allowed is tiered, not linear, so it cannot ride the term table and
    is scored per row by `score_dst`.
    """
    from ..scoring import score_dst

    if df.empty:
        return pd.DataFrame(columns=["team", "season", "games", "ppg", "opp_pg"])
    scored = df.copy()
    scored["pts"] = [score_dst(r, cfg) for r in df.to_dict("records")]
    out = scored.groupby(["team", "season"], as_index=False).agg(
        games=("pts", "size"), ppg=("pts", "mean"))
    out["opp_pg"] = out["ppg"]      # no usage concept for a defense
    return out


def _kicker_per_season(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """Per (player, season) games and points-per-game for a kicker."""
    from ..scoring import score_kicker

    if df.empty:
        return pd.DataFrame(columns=["player_id", "season", "games", "ppg", "opp_pg"])
    scored = df.copy()
    scored["pts"] = [score_kicker(r, cfg) for r in df.to_dict("records")]
    out = scored.groupby(["player_id", "season"], as_index=False).agg(
        games=("pts", "size"), ppg=("pts", "mean"))
    out["opp_pg"] = out["ppg"]
    return out


def _project_k_dst_season(season: int, cfg: dict | None = None) -> pd.DataFrame:
    """K and DST season projections from ingested history.

    Replaces the previous one-variable Vegas anchors (`K_BASE_PPG`,
    `DST_BASE_PPG`). Baselines come from the same recency-weighted aggregation
    offence uses -- `_weight_seasons` -- so K/DST cannot drift from how everyone
    else is aggregated. The Vegas signal is NOT discarded: it remains the
    matchup factor applied by `project_week` and `project_ros`.

    `usage_trend` is meaningless for these positions, so both callers pass
    `trend=False`, which pins the ratio at 1.0. `opp_pg` is set equal to `ppg`
    only to satisfy the column contract -- it does NOT by itself make the ratio
    inert (see `_weight_seasons`), which is precisely why `trend=False` exists.
    """
    cfg = cfg or league_config()
    seasons = _completed_seasons(season)
    placeholders = ",".join("?" for _ in seasons)
    newest = max(seasons)

    # `week <= REG_SEASON_WEEKS` is load-bearing: both tables hold weeks up to 22
    # (postseason), and the offensive path filters them out. Without it a team
    # that made a deep playoff run gets extra games folded into its per-season
    # `games`/`ppg`, inflating the projection (HOU DST measured 137.8 vs 121.9).
    kdf = read_df(
        f"SELECT * FROM kicking_stats WHERE season IN ({placeholders}) "
        f"AND week <= {REG_SEASON_WEEKS}", tuple(seasons))
    ddf = read_df(
        f"SELECT * FROM team_defense WHERE season IN ({placeholders}) "
        f"AND week <= {REG_SEASON_WEEKS}", tuple(seasons))

    players = _players().set_index("player_id")
    rows = []

    for pid, per in _kicker_per_season(kdf, cfg).groupby("player_id"):
        agg = _weight_seasons(per[["season", "games", "ppg", "opp_pg"]], newest, trend=False)
        if agg["n_seasons"] == 0 or agg["base_ppg"] <= 0:
            continue
        name = players.loc[pid, "name"] if pid in players.index else pid
        team = norm_team(players.loc[pid, "team"]) if pid in players.index else None
        proj = agg["base_ppg"] * agg["proj_games"]
        rows.append({
            "player_id": pid, "name": name, "position": "K", "team": team,
            "proj_points": round(proj, 1), "proj_ppg": round(agg["base_ppg"], 2),
            "floor": round(proj * 0.80, 1), "ceiling": round(proj * 1.20, 1),
            "components": {"model": "history", "n_seasons": agg["n_seasons"],
                           "proj_games": round(agg["proj_games"], 1)},
        })

    for team, per in _dst_per_season(ddf, cfg).groupby("team"):
        agg = _weight_seasons(per[["season", "games", "ppg", "opp_pg"]], newest, trend=False)
        # Deliberately no `base_ppg <= 0` guard here (unlike the kicker loop above):
        # a defense that gives up a lot of points allowed can legitimately average
        # a negative score under a tiered ladder -- that's real signal a fantasy
        # manager needs to see, not a data artifact to hide. A kicker cannot score
        # negative under any of these leagues' scoring, so `base_ppg <= 0` there
        # only ever catches missing/garbage data, never a real bad kicker.
        if agg["n_seasons"] == 0:
            continue
        t = norm_team(team)
        proj = agg["base_ppg"] * agg["proj_games"]
        rows.append({
            "player_id": f"DST_{t}", "name": f"{t} DST", "position": "DST", "team": t,
            "proj_points": round(proj, 1), "proj_ppg": round(agg["base_ppg"], 2),
            "floor": round(proj * 0.80, 1), "ceiling": round(proj * 1.20, 1),
            "components": {"model": "history", "n_seasons": agg["n_seasons"],
                           "proj_games": round(agg["proj_games"], 1)},
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Weekly projection
# ---------------------------------------------------------------------------
def dvp_factors(season: int, upto_week: int, cfg: dict | None = None) -> dict:
    """Opponent defense-vs-position multipliers.

    For each (defense_team, position): mean fantasy points allowed to that
    position per game, divided by the league average, clipped to DVP_CLIP. Uses
    the current season up to ``upto_week`` if >=4 weeks of data exist, else the
    prior season. Returns {(team, pos): factor}. Neutral (missing key -> 1.0).

    Computed from the scored frame rather than SQL so it reflects the league's
    own scoring. Needs the season actually being played PLUS the prior season
    (for the low-sample-size fallback below) -- neither is `_weekly_raw`'s
    feeder-season set, so this reads `_weekly_rows` directly rather than going
    through `_weekly`/`_weekly_raw`, which deliberately excludes ``season``.
    """
    wk = _score_weekly(_weekly_rows((season, season - 1)), cfg)
    if wk.empty:
        return {}

    def compute(sea: int, wk_max: int) -> pd.DataFrame:
        sub = wk[(wk.season == sea) & (wk.week < wk_max) & (wk.week <= REG_SEASON_WEEKS)]
        if sub.empty:
            return sub
        return sub.groupby(["opponent", "position", "week"], as_index=False)["fp"].sum()

    per_game = compute(season, upto_week)
    if per_game.empty or per_game.week.nunique() < 4:
        per_game = compute(season - 1, REG_SEASON_WEEKS + 1)
    if per_game.empty:
        return {}
    team_pos = per_game.groupby(["opponent", "position"])["fp"].mean()
    league = per_game.groupby("position")["fp"].mean()
    out = {}
    for (team, pos), val in team_pos.items():
        lg = league.get(pos, val)
        f = float(np.clip(val / lg if lg else 1.0, *DVP_CLIP))
        out[(team, pos)] = f
    return out


def _injury_status(season: int, week: int) -> dict[str, str]:
    inj = read_df(
        "SELECT player_id, report_status FROM injuries WHERE season=? AND week=?",
        (season, week),
    )
    return {r.player_id: r.report_status for _, r in inj.iterrows()
            if r.report_status in INJURY_MULT}


def _recent_form(season: int, week: int, cfg: dict | None = None) -> pd.Series:
    """Mean fp over the prior WEEK_FORM_LOOKBACK games this season, per player.

    Needs the season actually being played, not `_weekly_raw`'s feeder-season
    set (which deliberately excludes ``season``), so this reads `_weekly_rows`
    directly rather than going through `_weekly`.
    """
    lo = max(1, week - WEEK_FORM_LOOKBACK)
    wk = _score_weekly(_weekly_rows((season,)), cfg)
    if wk.empty:
        return pd.Series(dtype=float)
    sub = wk[(wk.season == season) & (wk.week >= lo) & (wk.week < week)]
    if sub.empty:
        return pd.Series(dtype=float)
    return sub.groupby("player_id")["fp"].mean()


def _odds_by_team(season: int, week: int) -> dict[str, dict]:
    """team -> {spread, implied_total, is_home} from cached odds, matched to this
    week's schedule by opponent AND venue. Empty if odds are stale/missing.

    Venue matters: books price the whole season, so divisional home-and-homes
    produce two rows with the same (team, opponent). Matching on opponent alone
    lets the wrong leg win (spread sign flipped, wrong implied total) for ~96
    team-weeks. (team, opponent, is_home) is collision-free across the slate.
    """
    from ..etl.odds import game_lines
    gl = game_lines()
    if gl.empty:
        return {}
    sched = _schedule_opponents(season, week)
    out: dict[str, dict] = {}
    for _, r in gl.iterrows():
        # only trust an odds row if it matches the scheduled matchup for this team
        sch = sched.get(r.team)
        if sch and sch[0] == r.opponent and bool(r.is_home) == sch[1]:
            out[r.team] = {"spread": r.spread, "implied_total": r.implied_total,
                           "is_home": bool(r.is_home)}
    return out


def _matchup_factor(pos: str, opp: str, is_home: bool,
                    team_odds: dict | None, opp_odds: dict | None,
                    dvp: dict, avg_imp: float) -> tuple[float, dict]:
    """The multiplicative matchup adjustment for one player in one game.

    Single source of truth for the weekly factor chain: ``project_week`` applies it
    to one week, ``project_ros`` sums it over the remaining schedule. Keeping both on
    this function is what makes ROS equal to the sum of the weekly projections
    (pinned by ``test_ros_equals_sum_of_weekly_projections``) instead of a parallel
    reimplementation that can drift.

    Deliberately EXCLUDED, because neither generalises past a single week:
      * recent form  -- a baseline choice, not a matchup factor, and ROS has no
        per-week form to blend.
      * injury status -- a report_status is only valid for the week it was filed.
        Applying a "Questionable" to all 12 remaining games would be nonsense.
    ``project_week`` applies the injury multiplier itself, on top of this.

    Returns ``(factor, components)``.
    """
    dvp_f = dvp.get((opp, pos), 1.0)

    # Game environment. For offence, output scales with your OWN implied total. For a
    # DST it scales INVERSELY with the opponent's -- weaker offence faced => more
    # sacks/turnovers/low points-allowed. Same direction as the season DST model (4).
    game_env = 1.0
    priced = False
    if pos == "DST":
        if opp_odds is not None and avg_imp:
            game_env = float(np.clip(
                1 - IMPLIED_TOTAL_GAIN * (opp_odds["implied_total"] / avg_imp - 1),
                *GAME_ENV_CLIP))
            priced = True
    elif team_odds is not None and avg_imp:
        game_env = float(np.clip(
            1 + IMPLIED_TOTAL_GAIN * (team_odds["implied_total"] / avg_imp - 1),
            *GAME_ENV_CLIP))
        priced = True

    # Game script from the spread (offence only; K/DST have no SCRIPT_COEFF entry).
    spread = team_odds["spread"] if team_odds is not None else None
    script = 1.0
    if spread is not None and pos in SCRIPT_COEFF:
        script = float(np.clip(1 + SCRIPT_COEFF[pos] * (-spread / 10.0),
                               1 - SCRIPT_MAX, 1 + SCRIPT_MAX))

    hf = 1 + (HOME_FIELD if is_home else -HOME_FIELD)

    factor = dvp_f * game_env * script * hf
    return factor, {"dvp": round(dvp_f, 3), "game_env": round(game_env, 3),
                    "script": round(script, 3), "home": is_home,
                    "spread": spread, "priced": priced}


def project_week(season: int, week: int, store: bool = False,
                 season_proj: pd.DataFrame | None = None,
                 cfg: dict | None = None) -> pd.DataFrame:
    """Matchup-adjusted single-week projections. Columns: player_id, name, position,
    team, opponent, proj_points, floor, ceiling, components."""
    if cfg is None:
        cfg = league_config()
    if season_proj is None:
        season_proj = project_season(season, cfg=cfg)
    if season_proj.empty:
        return pd.DataFrame()

    sched = _schedule_opponents(season, week)
    dvp = dvp_factors(season, week, cfg)
    injuries = _injury_status(season, week)
    odds = _odds_by_team(season, week)
    form = _recent_form(season, week, cfg)

    # league average implied for game-environment scaling
    avg_imp = (np.mean([o["implied_total"] for o in odds.values()])
               if odds else LEAGUE_AVG_IMPLIED)

    rows = []
    for _, p in season_proj.iterrows():
        pos, team = p.position, p.team
        base_ppg = p.get("proj_ppg", p.proj_points / FULL_SLATE)
        # recent form blend (only if this player has games this season)
        if team not in sched:
            continue  # bye week or team not scheduled -> no game
        opp, is_home = sched[team]

        base = base_ppg
        if p.player_id in form.index and form[p.player_id] == form[p.player_id]:
            base = (1 - WEEK_FORM_BLEND) * base_ppg + WEEK_FORM_BLEND * float(form[p.player_id])

        factor, comp = _matchup_factor(pos, opp, is_home, odds.get(team),
                                       odds.get(opp), dvp, avg_imp)
        # injury is weekly-only, so it sits outside the shared matchup factor
        inj_mult = INJURY_MULT.get(injuries.get(p.player_id), 1.0)

        proj = base * factor * inj_mult
        # weekly dispersion from season pergame_std
        pergame_std = p.components.get("pergame_std", base * 0.5) if isinstance(p.components, dict) else base * 0.5
        floor = max(0.0, proj - PCTL_Z * pergame_std)
        ceiling = proj + PCTL_Z * pergame_std
        rows.append({
            "player_id": p.player_id, "name": p["name"], "position": pos,
            "team": team, "opponent": opp,
            "proj_points": round(proj, 2),
            "floor": round(floor, 2), "ceiling": round(ceiling, 2),
            "components": {**comp, "base_ppg": round(base, 2),
                           "injury": injuries.get(p.player_id), "inj_mult": inj_mult},
        })
    df = pd.DataFrame(rows).sort_values("proj_points", ascending=False).reset_index(drop=True)
    if store:
        _store(df, scope="week", season=season, week=week)
    return df


# ---------------------------------------------------------------------------
# Rest-of-season projection
# ---------------------------------------------------------------------------
def project_ros(season: int, week: int, store: bool = False,
                season_proj: pd.DataFrame | None = None,
                cfg: dict | None = None) -> pd.DataFrame:
    """Remaining-schedule aggregate (weeks ``week``..``last_scoring_week(cfg)``).

    The horizon is the last week that counts toward making the playoffs, NOT
    week 18: for a league configuring ``playoff_week_start: 15`` it is week 14.
    Absent that key it IS week 18, so leagues that never set it are unchanged.

    Walks each remaining week individually, applying the full shared matchup chain
    (``_matchup_factor``: opponent DvP, Vegas game environment, game script, home
    field) and summing. Byes drop out naturally because the team has no scheduled
    game that week.

        proj_ros = base_ppg * SUM over remaining weeks of matchup_factor(week)

    This replaces the earlier ``base_ppg * mean(dvp) * games_left``, which was a
    strict special case: with no odds loaded every ``game_env``/``script`` collapses
    to 1.0 and the sum reduces to the old DvP-only average (times a small home/away
    term that now cancels correctly across the schedule rather than being ignored).
    The odds-free path is therefore unchanged in substance, which is why historical
    validation still holds.

    Columns: player_id, name, position, team, games_left, proj_points, floor,
    ceiling, components.
    """
    if cfg is None:
        cfg = league_config()
    if season_proj is None:
        season_proj = project_season(season, cfg=cfg)
    if season_proj.empty:
        return pd.DataFrame()
    dvp = dvp_factors(season, week, cfg)
    weeks = range(max(week, 1), last_scoring_week(cfg) + 1)

    # Per-week context, built once. Each week normalises its implied totals against
    # its OWN slate, exactly as project_week does, so the two stay comparable.
    wk_ctx: dict[int, tuple[dict, dict, float]] = {}
    for w in weeks:
        sched = _schedule_opponents(season, w)
        odds = _odds_by_team(season, w)
        avg = (float(np.mean([o["implied_total"] for o in odds.values()]))
               if odds else LEAGUE_AVG_IMPLIED)
        wk_ctx[w] = (sched, odds, avg)

    rows = []
    for _, p in season_proj.iterrows():
        team, pos = p.team, p.position
        base_ppg = p.get("proj_ppg", p.proj_points / FULL_SLATE)

        factor_sum = dvp_sum = 0.0
        games_left = weeks_priced = 0
        for w in weeks:
            sched, odds, avg = wk_ctx[w]
            if team not in sched:
                continue                      # bye, or team not scheduled
            opp, is_home = sched[team]
            games_left += 1
            f, comp = _matchup_factor(pos, opp, is_home, odds.get(team),
                                      odds.get(opp), dvp, avg)
            factor_sum += f
            dvp_sum += comp["dvp"]
            weeks_priced += int(comp["priced"])
        if games_left == 0:
            continue

        proj = base_ppg * factor_sum
        # scale season floor/ceiling band to the remaining fraction
        frac = games_left / FULL_SLATE
        band = (p.ceiling - p.floor) / 2 * frac if p.ceiling > p.floor else proj * 0.15
        rows.append({
            "player_id": p.player_id, "name": p["name"], "position": pos, "team": team,
            "games_left": games_left,
            "proj_points": round(proj, 1),
            "floor": round(max(0.0, proj - band), 1), "ceiling": round(proj + band, 1),
            "components": {"base_ppg": round(base_ppg, 2),
                           "sos": round(dvp_sum / games_left, 3),
                           "matchup": round(factor_sum / games_left, 3),
                           "games_left": games_left,
                           "weeks_priced": weeks_priced},
        })
    df = pd.DataFrame(rows).sort_values("proj_points", ascending=False).reset_index(drop=True)
    if store:
        _store(df, scope="ros", season=season, week=week)
    return df


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
def _store(df: pd.DataFrame, scope: str, season: int, week: int) -> int:
    if df.empty:
        return 0
    out = df[["player_id", "proj_points", "floor", "ceiling"]].copy()
    out["scope"] = scope
    out["season"] = season
    out["week"] = week
    out["components_json"] = df["components"].apply(lambda c: json.dumps(c, default=str))
    with connect() as conn:
        return upsert_rows(out, "projections", conn)


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    season = int(league_config()["league"]["season"])
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)

    print(f"\n=== project_season({season}) top 15 ===")
    s = project_season(season)
    print(s[["name", "position", "team", "proj_points", "proj_ppg", "floor", "ceiling"]].head(15).to_string())
    print("\ntop-3 by position:")
    for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
        top = s[s.position == pos].head(3)
        print(f"  {pos}:", [f"{r['name']} {r.proj_points}" for _, r in top.iterrows()])

    print(f"\n=== project_week({season}, 1) top 12 ===")
    w = project_week(season, 1, season_proj=s)
    print(w[["name", "position", "team", "opponent", "proj_points", "floor", "ceiling"]].head(12).to_string())

    print(f"\n=== project_ros({season}, 1) top 10 ===")
    r = project_ros(season, 1, season_proj=s)
    print(r[["name", "position", "team", "games_left", "proj_points"]].head(10).to_string())

    # Validation against actual 2025 weekly results (odds absent -> DvP/form only)
    print("\n=== validate project_week(2025, 10) vs actuals ===")
    s25 = project_season(2025)
    w25 = project_week(2025, 10, season_proj=s25)
    actual_raw = read_df(
        "SELECT player_id, receptions, receiving_yards, receiving_tds, "
        "passing_yards, passing_tds, interceptions, rushing_yards, rushing_tds "
        "FROM weekly_stats WHERE season=2025 AND week=10")
    from ..scoring import score_frame
    actual = actual_raw[["player_id"]].copy()
    actual["fp"] = score_frame(actual_raw)
    m = w25.merge(actual, on="player_id")
    m["ae"] = (m.proj_points - m.fp).abs()
    print("players compared:", len(m))
    print(m.groupby("position").agg(n=("ae", "size"), MAE=("ae", "mean")).round(2).to_string())
