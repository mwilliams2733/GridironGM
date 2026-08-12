"""Projection-engine tests.

The odds-matching tests are fully synthetic (game_lines and the schedule are
monkeypatched) so they pin the pairing *logic*, not whatever happens to be in
the DB this week. The remaining tests use the real read-only SQLite DB.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.config import league_config
from app.models import projections as proj


# ---------------------------------------------------------------------------
# Odds -> week matching
# ---------------------------------------------------------------------------
def _home_and_home_lines() -> pd.DataFrame:
    """KC/DEN played twice: KC home in wk 1, KC away in wk 12. Books price both,
    so game_lines() holds two rows per team with identical (team, opponent)."""
    return pd.DataFrame([
        # KC hosts DEN
        {"team": "KC", "opponent": "DEN", "spread": -6.0, "total": 45.0,
         "implied_total": 25.5, "is_home": True},
        {"team": "DEN", "opponent": "KC", "spread": 6.0, "total": 45.0,
         "implied_total": 19.5, "is_home": False},
        # DEN hosts KC
        {"team": "DEN", "opponent": "KC", "spread": -1.0, "total": 41.0,
         "implied_total": 21.0, "is_home": True},
        {"team": "KC", "opponent": "DEN", "spread": 1.0, "total": 41.0,
         "implied_total": 20.0, "is_home": False},
    ])


@pytest.fixture
def home_and_home(monkeypatch):
    lines = _home_and_home_lines()
    monkeypatch.setattr("app.etl.odds.game_lines", lambda: lines)
    monkeypatch.setattr(
        proj, "_schedule_opponents",
        lambda season, week: (
            {"KC": ("DEN", True), "DEN": ("KC", False)} if week == 1
            else {"KC": ("DEN", False), "DEN": ("KC", True)}
        ),
    )


def test_odds_match_the_home_leg_not_the_away_leg(home_and_home):
    """Week 1 is KC's home game — it must get the home line, not the rematch's."""
    odds = proj._odds_by_team(2026, 1)
    assert odds["KC"]["is_home"] is True
    assert odds["KC"]["spread"] == -6.0        # favored at home
    assert odds["KC"]["implied_total"] == 25.5
    assert odds["DEN"]["spread"] == 6.0


def test_odds_match_the_away_leg_in_the_rematch(home_and_home):
    """Week 12 is the same pairing with venues flipped — the other line applies."""
    odds = proj._odds_by_team(2026, 12)
    assert odds["KC"]["is_home"] is False
    assert odds["KC"]["spread"] == 1.0         # underdog on the road
    assert odds["KC"]["implied_total"] == 20.0
    assert odds["DEN"]["spread"] == -1.0


def test_odds_ignored_when_pairing_does_not_match(monkeypatch):
    """A line for a matchup that isn't on this week's schedule is dropped."""
    monkeypatch.setattr("app.etl.odds.game_lines", lambda: _home_and_home_lines())
    monkeypatch.setattr(proj, "_schedule_opponents",
                        lambda season, week: {"KC": ("LV", True), "LV": ("KC", False)})
    assert proj._odds_by_team(2026, 5) == {}


def test_no_odds_degrades_to_empty(monkeypatch):
    monkeypatch.setattr("app.etl.odds.game_lines", lambda: pd.DataFrame())
    assert proj._odds_by_team(2026, 1) == {}


# ---------------------------------------------------------------------------
# Real-data invariants
# ---------------------------------------------------------------------------
def test_live_odds_never_contradict_the_schedule_venue():
    """Guards the whole loaded slate: every odds row accepted for a week must
    agree with the schedule on who is at home."""
    for week in range(1, proj.REG_SEASON_WEEKS + 1):
        sched = proj._schedule_opponents(2026, week)
        for team, o in proj._odds_by_team(2026, week).items():
            assert o["is_home"] == sched[team][1], f"venue mismatch {team} wk{week}"


def test_age_multiplier_peaks_at_one_and_declines():
    assert proj.age_multiplier("RB", 25) == 1.0
    assert proj.age_multiplier("RB", 30) < proj.age_multiplier("RB", 27) < 1.0
    assert proj.age_multiplier("QB", 30) == 1.0
    # RBs must fall off harder than QBs at the same age
    assert proj.age_multiplier("RB", 32) < proj.age_multiplier("QB", 32)


def test_age_multiplier_neutral_when_unknown():
    assert proj.age_multiplier("RB", None) == 1.0
    assert proj.age_multiplier("K", 30) == 1.0


def test_weight_seasons_prefers_recent_and_penalises_small_samples():
    newest = 2025
    full = pd.DataFrame([
        {"season": 2025, "games": 17, "ppg": 10.0, "opp_pg": 15.0},
        {"season": 2024, "games": 17, "ppg": 20.0, "opp_pg": 15.0},
    ])
    agg = full.pipe(proj._weight_seasons, newest)
    # 0.50*10 + 0.30*20 over 0.80 => 13.75; strictly between, nearer the recent year
    assert 10.0 < agg["base_ppg"] < 15.0

    # Same seasons, but the old one is a 2-game sample -> down-weighted further.
    thin = full.copy()
    thin.loc[thin.season == 2024, "games"] = 2
    assert proj._weight_seasons(thin, newest)["base_ppg"] < agg["base_ppg"]


def test_projected_games_regresses_toward_the_league_mean():
    perfect = pd.DataFrame([{"season": 2025, "games": 17, "ppg": 10.0, "opp_pg": 10.0}])
    # a fully durable player still projects below a full slate
    assert 16.0 < proj._weight_seasons(perfect, 2025)["proj_games"] < proj.FULL_SLATE
    fragile = pd.DataFrame([{"season": 2025, "games": 4, "ppg": 10.0, "opp_pg": 10.0}])
    # ...and a fragile one projects above his own history
    assert proj._weight_seasons(fragile, 2025)["proj_games"] > 4


# ---------------------------------------------------------------------------
# Shared matchup factor (pure — no DB)
# ---------------------------------------------------------------------------
NEUTRAL_DVP: dict = {}


def _line(implied, spread=0.0):
    return {"implied_total": implied, "spread": spread, "is_home": True}


def test_matchup_factor_is_neutral_without_odds():
    f, c = proj._matchup_factor("RB", "SEA", True, None, None, NEUTRAL_DVP, 22.9)
    assert c["game_env"] == 1.0 and c["script"] == 1.0
    assert c["priced"] is False
    assert f == pytest.approx(1 + proj.HOME_FIELD)


def test_offence_scales_with_its_own_implied_total():
    hi, _ = proj._matchup_factor("WR", "SEA", True, _line(28.0), _line(18.0),
                                 NEUTRAL_DVP, 22.9)
    lo, _ = proj._matchup_factor("WR", "SEA", True, _line(17.0), _line(28.0),
                                 NEUTRAL_DVP, 22.9)
    assert hi > lo


def test_dst_scales_inversely_with_the_opponent_implied_total():
    """A DST facing a weak offence must project HIGHER — the opposite direction to
    an offensive player, and the same direction as the season DST model (4)."""
    vs_weak, c = proj._matchup_factor("DST", "CLE", True, _line(24.0), _line(16.0),
                                      NEUTRAL_DVP, 22.9)
    vs_strong, _ = proj._matchup_factor("DST", "BUF", True, _line(24.0), _line(30.0),
                                        NEUTRAL_DVP, 22.9)
    assert vs_weak > vs_strong, "DST facing a weak offence must project higher"
    assert c["priced"] is True
    # and it must key off the OPPONENT, not its own team total
    same_opp_diff_own, _ = proj._matchup_factor(
        "DST", "CLE", True, _line(12.0), _line(16.0), NEUTRAL_DVP, 22.9)
    assert same_opp_diff_own == pytest.approx(vs_weak)


def test_game_script_favours_rb_and_fades_qb():
    fav, wk = -10.0, 10.0                       # 10-point favourite / underdog
    rb_fav, _ = proj._matchup_factor("RB", "X", True, _line(22.9, fav), None,
                                     NEUTRAL_DVP, 22.9)
    rb_dog, _ = proj._matchup_factor("RB", "X", True, _line(22.9, wk), None,
                                     NEUTRAL_DVP, 22.9)
    qb_fav, _ = proj._matchup_factor("QB", "X", True, _line(22.9, fav), None,
                                     NEUTRAL_DVP, 22.9)
    qb_dog, _ = proj._matchup_factor("QB", "X", True, _line(22.9, wk), None,
                                     NEUTRAL_DVP, 22.9)
    assert rb_fav > rb_dog          # favourites run to bleed clock
    assert qb_fav < qb_dog          # underdogs are forced to throw


def test_matchup_factor_respects_the_clips():
    absurd, _ = proj._matchup_factor("WR", "X", True, _line(90.0, -40.0), None,
                                     NEUTRAL_DVP, 22.9)
    ceiling = proj.GAME_ENV_CLIP[1] * (1 + proj.SCRIPT_MAX) * (1 + proj.HOME_FIELD)
    assert absurd <= ceiling + 1e-9


# ---------------------------------------------------------------------------
# ROS <-> weekly agreement
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def season_proj_2026():
    return proj.project_season(2026)


def test_ros_equals_sum_of_weekly_projections(season_proj_2026):
    """The point of routing both through _matchup_factor: rest-of-season is the
    weekly model integrated over the schedule, not a second model that can drift.

    Exact only when no injury designations and no in-season form exist for the
    horizon (true in the preseason, which is when ROS actually drives decisions).
    """
    s = season_proj_2026
    if s.empty:
        pytest.skip("no season projection data available")
    if not proj._injury_status(2026, 1) == {}:
        pytest.skip("2026 injury rows exist — weekly applies a factor ROS cannot")

    ros = proj.project_ros(2026, 1, season_proj=s)
    totals: dict[str, float] = {}
    for w in range(1, proj.REG_SEASON_WEEKS + 1):
        for _, r in proj.project_week(2026, w, season_proj=s).iterrows():
            totals[r.player_id] = totals.get(r.player_id, 0.0) + r.proj_points

    assert len(ros) > 100
    for _, r in ros.iterrows():
        assert r.player_id in totals
        # tolerance is pure rounding: ROS rounds once to 1dp, the sum stacks up to
        # 17 values each rounded to 2dp.
        assert r.proj_points == pytest.approx(totals[r.player_id], abs=0.15)


def test_ros_uses_odds_when_they_exist(season_proj_2026):
    s = season_proj_2026
    if s.empty:
        pytest.skip("no season projection data available")
    ros = proj.project_ros(2026, 1, season_proj=s)
    priced = [c["weeks_priced"] for c in ros.components]
    assert max(priced) > 0, "full-season odds are loaded; ROS should be using them"


def test_ros_falls_back_to_dvp_only_without_odds(monkeypatch, season_proj_2026):
    """No odds => game_env and script go neutral and ROS reduces to the DvP-and-
    home-field product. Guards the historical/offseason path."""
    s = season_proj_2026
    if s.empty:
        pytest.skip("no season projection data available")
    monkeypatch.setattr("app.etl.odds.game_lines", lambda: pd.DataFrame())
    ros = proj.project_ros(2026, 1, season_proj=s)
    assert all(c["weeks_priced"] == 0 for c in ros.components)

    dvp = proj.dvp_factors(2026, 1)
    row = ros.iloc[0]
    expected = 0.0
    for w in range(1, proj.REG_SEASON_WEEKS + 1):
        sched = proj._schedule_opponents(2026, w)
        if row.team not in sched:
            continue
        opp, is_home = sched[row.team]
        hf = 1 + (proj.HOME_FIELD if is_home else -proj.HOME_FIELD)
        expected += dvp.get((opp, row.position), 1.0) * hf
    base = s[s.player_id == row.player_id].iloc[0].proj_ppg
    assert row.proj_points == pytest.approx(base * expected, abs=0.15)


def test_ros_shrinks_as_the_season_progresses(season_proj_2026):
    s = season_proj_2026
    if s.empty:
        pytest.skip("no season projection data available")
    early = proj.project_ros(2026, 1, season_proj=s).set_index("player_id")
    late = proj.project_ros(2026, 14, season_proj=s).set_index("player_id")
    common = early.index.intersection(late.index)
    assert len(common) > 100
    assert (late.loc[common, "games_left"] < early.loc[common, "games_left"]).all()
    assert (late.loc[common, "proj_points"] < early.loc[common, "proj_points"]).all()


def test_ros_games_left_accounts_for_the_bye(season_proj_2026):
    s = season_proj_2026
    if s.empty:
        pytest.skip("no season projection data available")
    ros = proj.project_ros(2026, 1, season_proj=s)
    byes = proj._bye_weeks(2026)
    # a full 18-week horizon minus one bye = 17 games for every team on a bye
    for _, r in ros.head(50).iterrows():
        expected = proj.REG_SEASON_WEEKS - (1 if r.team in byes else 0)
        assert r.games_left == expected


def test_season_projection_is_sane():
    season = int(league_config()["league"]["season"])
    df = proj.project_season(season)
    assert not df.empty
    assert (df.floor <= df.proj_points).all()
    assert (df.proj_points <= df.ceiling).all()
    assert (df.floor >= 0).all()
    # every fantasy-relevant position is represented
    assert {"QB", "RB", "WR", "TE", "K", "DST"} <= set(df.position)
