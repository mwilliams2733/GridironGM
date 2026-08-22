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

# Kicker / DST anchored models (no weekly_stats for these positions).
K_BASE_PPG = 8.0                 # ~ mean fantasy points/game for a startable kicker
K_IMPLIED_GAIN = 0.45            # kicker ppg sensitivity to team implied total vs league avg
DST_BASE_PPG = 7.0               # ~ mean DST fantasy points/game
DST_IMPLIED_GAIN = 0.55          # DST sensitivity to (inverted) opponent implied total


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


@lru_cache(maxsize=4)
def _weekly_raw(season: int) -> pd.DataFrame:
    """Raw regular-season stat lines for the seasons feeding ``season``.

    Cached on season alone, NOT on league: the stat line is the same for
    everyone, only the scoring differs. Scoring is applied by `_weekly`, which
    is cheap enough (a vectorized pass over ~18k rows) not to need its own cache.
    """
    seasons = _completed_seasons(season)
    q = (
        f"SELECT {', '.join(_WEEKLY_RAW_COLUMNS)} "
        f"FROM weekly_stats WHERE season IN ({','.join('?' for _ in seasons)}) "
        f"AND week <= {REG_SEASON_WEEKS}"
    )
    return read_df(q, tuple(seasons))


def _weekly(season: int, cfg: dict | None = None) -> pd.DataFrame:
    """Weekly stat lines with an `fp` column scored for ``cfg``'s league."""
    from ..scoring import score_frame

    df = _weekly_raw(season).copy()
    if df.empty:
        return df
    df["fp"] = score_frame(df, cfg)
    return df


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


def _weight_seasons(per_season: pd.DataFrame, newest: int) -> dict:
    """Recency- and reliability-weighted aggregation of a player's seasons.

    ``per_season`` columns: season, games, ppg, opp_pg (opportunities/game).
    Returns base_ppg, proj_games, usage_trend, and pooled dispersion inputs.
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
    if newest in per_season.index and len(per_season) >= 2:
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


def project_season(season: int, store: bool = False, cfg: dict | None = None) -> pd.DataFrame:
    """Full-season projections (scored per ``cfg``'s league) for every offensive
    player with history, plus anchored K/DST models. Columns: player_id, name,
    position, team, proj_points, floor, ceiling, components (dict)."""
    if cfg is None:
        cfg = league_config()
    wk = _weekly(season, cfg).copy()
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
    kdst = _project_k_dst_season(season)
    if not kdst.empty:
        df = pd.concat([df, kdst], ignore_index=True)
    df = df.sort_values("proj_points", ascending=False).reset_index(drop=True)
    if store:
        _store(df, scope="season", season=season, week=0)
    return df


# ---------------------------------------------------------------------------
# Kicker / DST anchored season models
# ---------------------------------------------------------------------------
def _team_implied_totals() -> dict[str, float]:
    """team -> mean implied total from cached odds (empty dict if no odds)."""
    from ..etl.odds import game_lines
    gl = game_lines()
    if gl.empty:
        return {}
    return gl.groupby("team")["implied_total"].mean().to_dict()


def _opp_implied_totals(season: int) -> dict[str, float]:
    """team -> mean implied total of its opponents across the season schedule.

    Uses cached odds where available; falls back to league average so DST always
    gets a number. Simplified: averages every scheduled opponent's team implied
    total (from odds), else LEAGUE_AVG_IMPLIED."""
    it = _team_implied_totals()
    sch = read_df(
        "SELECT week, home_team, away_team FROM schedules WHERE season=? AND week<=?",
        (season, REG_SEASON_WEEKS),
    )
    if sch.empty:
        return {}
    opp_tot: dict[str, list[float]] = {}
    for _, r in sch.iterrows():
        opp_tot.setdefault(r.home_team, []).append(it.get(r.away_team, LEAGUE_AVG_IMPLIED))
        opp_tot.setdefault(r.away_team, []).append(it.get(r.home_team, LEAGUE_AVG_IMPLIED))
    return {t: float(np.mean(v)) for t, v in opp_tot.items()}


def _project_k_dst_season(season: int) -> pd.DataFrame:
    """K and DST projections from Vegas implied totals.

    Kickers: ppg scales with the team's own implied total. DST: ppg scales with the
    INVERSE of opponents' implied totals (weaker offenses faced => more DST points).
    Both are simple, documented models because these positions have no weekly_stats.

    NOTE: the ADP table supplies only the *universe* (which kickers/defenses exist and
    their teams). ``adp`` is echoed into components for display but is NOT an input to
    the projection -- see docs/MODELING.md 4 and 10.6.
    """
    adp = read_df("SELECT player_name, position, team, adp FROM adp WHERE position IN ('PK','DST')")
    if adp.empty:
        return pd.DataFrame()
    team_it = _team_implied_totals()
    opp_it = _opp_implied_totals(season)
    players = _players()
    rows = []
    for _, r in adp.iterrows():
        pos = "K" if r.position == "PK" else "DST"
        team = norm_team(r.team)
        if pos == "K":
            it = team_it.get(team, LEAGUE_AVG_IMPLIED)
            ppg = K_BASE_PPG + K_IMPLIED_GAIN * (it - LEAGUE_AVG_IMPLIED)
            ppg = max(4.0, ppg)
            # resolve player_id via K roster (kickers exist in players table)
            pid = _resolve_k_id(r.player_name, team, players)
        else:
            oit = opp_it.get(team, LEAGUE_AVG_IMPLIED)
            ppg = DST_BASE_PPG + DST_IMPLIED_GAIN * (LEAGUE_AVG_IMPLIED - oit)
            ppg = max(3.0, ppg)
            pid = f"DST_{team}"
        proj_games = FULL_SLATE
        proj = ppg * proj_games
        rows.append({
            "player_id": pid, "name": r.player_name, "position": pos, "team": team,
            "proj_points": round(proj, 1), "proj_ppg": round(ppg, 2),
            "floor": round(proj * 0.80, 1), "ceiling": round(proj * 1.20, 1),
            "components": {"model": "adp+implied", "ppg": round(ppg, 2),
                          "implied_used": round(team_it.get(team, LEAGUE_AVG_IMPLIED) if pos == "K"
                                                else opp_it.get(team, LEAGUE_AVG_IMPLIED), 1),
                          "adp": float(r.adp)},
        })
    return pd.DataFrame(rows)


def _resolve_k_id(name: str, team: str, players: pd.DataFrame) -> str:
    cand = players[(players.position == "K")]
    m = cand[cand.name.str.lower() == name.lower()]
    if not m.empty:
        return m.iloc[0].player_id
    return f"K_{team}"


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
    own scoring; the underlying read is cached by `_weekly_raw`.
    """
    wk = _weekly(season, cfg)
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
    """Mean fp over the prior WEEK_FORM_LOOKBACK games this season, per player."""
    lo = max(1, week - WEEK_FORM_LOOKBACK)
    wk = _weekly(season, cfg)
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
    """Remaining-schedule aggregate (weeks ``week``..18).

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
    weeks = range(max(week, 1), REG_SEASON_WEEKS + 1)

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
    actual = read_df(
        "SELECT player_id, fantasy_points_half_ppr fp FROM weekly_stats WHERE season=2025 AND week=10")
    m = w25.merge(actual, on="player_id")
    m["ae"] = (m.proj_points - m.fp).abs()
    print("players compared:", len(m))
    print(m.groupby("position").agg(n=("ae", "size"), MAE=("ae", "mean")).round(2).to_string())
