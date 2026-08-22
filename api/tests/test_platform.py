"""Platform dispatch: leagues route to their own ETL."""
from __future__ import annotations

import copy

import pytest

from app.config import league_config, league_ids, validate_league_config
from app.etl.platform import platform_of


def test_leagues_without_a_platform_default_to_espn():
    assert platform_of("league1") == "espn"


def test_the_sundt_league_is_registered_as_sleeper():
    assert "sundt" in league_ids()
    assert platform_of("sundt") == "sleeper"


def test_sundt_carries_full_ppr_scoring():
    assert league_config("sundt")["scoring"]["receiving"]["reception"] == 1.0


def test_sundt_carries_minus_one_interceptions():
    assert league_config("sundt")["scoring"]["passing"]["interception"] == -1


def test_sundt_declares_a_super_flex_slot():
    roster = league_config("sundt")["roster"]
    assert roster["starters"]["SUPER_FLEX"] == 1
    assert "QB" in roster["flex_slots"]["SUPER_FLEX"]["eligible"]


def test_sundt_playoffs_start_week_15():
    assert league_config("sundt")["league"]["playoff_week_start"] == 15


def test_espn_leagues_do_not_declare_playoff_week_start():
    """The gate: adding the key to ESPN leagues would change their ROS."""
    assert league_config("league1")["league"].get("playoff_week_start") is None


# ---------------------------------------------------------------------------
# Carry-forward 1: flex_slots share dicts must sum to 1.0.
# A hand-authored config summing to 0.8 or 1.2 silently under/over-allocates
# replacement demand with no error anywhere -- validate_league_config must
# catch it at load time.
# ---------------------------------------------------------------------------
def test_sundt_flex_slot_shares_sum_to_one():
    cfg = league_config("sundt")
    for label, d in cfg["roster"]["flex_slots"].items():
        assert sum(d["share"].values()) == pytest.approx(1.0)


def test_flex_slot_share_not_summing_to_one_is_rejected():
    cfg = copy.deepcopy(league_config("sundt"))
    cfg["roster"]["flex_slots"]["FLEX"]["share"] = {"RB": 0.5, "WR": 0.2, "TE": 0.1}
    with pytest.raises(ValueError, match="sum to 1.0"):
        validate_league_config(cfg, "sundt")


# ---------------------------------------------------------------------------
# Carry-forward 2: playoff_week_start must be a positive integer in a sane
# range. `last_scoring_week` computes min(REG_SEASON_WEEKS, start - 1), so a
# configured 0 yields -1, silently zeroing every ROS projection.
# ---------------------------------------------------------------------------
def test_playoff_week_start_zero_is_rejected():
    cfg = copy.deepcopy(league_config("sundt"))
    cfg["league"]["playoff_week_start"] = 0
    with pytest.raises(ValueError, match="playoff_week_start"):
        validate_league_config(cfg, "sundt")


def test_playoff_week_start_out_of_range_is_rejected():
    cfg = copy.deepcopy(league_config("sundt"))
    cfg["league"]["playoff_week_start"] = 30
    with pytest.raises(ValueError, match="playoff_week_start"):
        validate_league_config(cfg, "sundt")


def test_playoff_week_start_non_integer_is_rejected():
    cfg = copy.deepcopy(league_config("sundt"))
    cfg["league"]["playoff_week_start"] = "15"
    with pytest.raises(ValueError, match="playoff_week_start"):
        validate_league_config(cfg, "sundt")


def test_leagues_that_omit_the_key_pass_validation():
    """The four ESPN leagues set neither key -- validation must not start
    rejecting configs that were always legal."""
    validate_league_config(league_config("league1"), "league1")


# ---------------------------------------------------------------------------
# resolve_roster_entry: the router-level guard that stops a Sleeper-shaped
# roster entry's already-resolved player_id from being silently overwritten
# with None by the ESPN resolver (see app/routers/_common.py).
# ---------------------------------------------------------------------------
import pandas as pd

from app.routers._common import norm_name, resolve_espn_player, resolve_roster_entry


def _fake_players() -> pd.DataFrame:
    df = pd.DataFrame([
        {"player_id": "00-0031234", "name": "Test Player", "position": "WR",
         "team": "KC", "espn_id": 12345.0},
    ])
    df["norm"] = df["name"].map(norm_name)
    return df


def test_resolve_roster_entry_sleeper_shape_returns_id_unchanged():
    players = _fake_players()
    entry = {"player_id": "00-0033077"}
    assert resolve_roster_entry(entry, players) == "00-0033077"


def test_resolve_roster_entry_sleeper_shape_preserves_unresolved_none():
    """An entry the identity cascade could not place carries player_id=None;
    resolve_roster_entry must return that None, not re-resolve it via ESPN."""
    players = _fake_players()
    entry = {"player_id": None}
    assert resolve_roster_entry(entry, players) is None


def test_resolve_roster_entry_espn_shape_routes_through_espn_resolver():
    players = _fake_players()
    entry = {"espn_id": 12345.0, "name": "wrong name", "position": "WR"}
    assert resolve_roster_entry(entry, players) == "00-0031234"


def test_resolve_roster_entry_guards_against_the_regression_it_fixes():
    """Direct evidence of the bug the `"player_id" in entry` branch prevents:
    feeding a Sleeper-shaped entry straight into resolve_espn_player (what
    happened before this fix) resolves against an empty name and loses the
    id. resolve_roster_entry must not do that."""
    players = _fake_players()
    entry = {"player_id": "00-0033077"}
    regressed = resolve_espn_player(entry.get("espn_id"), entry.get("name", ""),
                                    entry.get("position"), players)
    assert regressed is None  # the bug this guard prevents is real, not theoretical
    assert resolve_roster_entry(entry, players) == "00-0033077"


# ---------------------------------------------------------------------------
# run_sync("espn"): the "espn" scope must dispatch each configured league to
# its own platform's sync function, and one league's failure must not stop
# the others.
# ---------------------------------------------------------------------------
def test_run_sync_espn_scope_dispatches_by_platform(monkeypatch):
    import app.config as config_mod
    import app.etl.espn as espn_mod
    import app.etl.platform as platform_mod
    import app.etl.sleeper as sleeper_mod
    from app.etl.sync import run_sync

    monkeypatch.setattr(config_mod, "league_ids", lambda: ["espn_lg", "sleeper_lg"])
    monkeypatch.setattr(platform_mod, "platform_of",
                        lambda lid: "sleeper" if lid == "sleeper_lg" else "espn")
    monkeypatch.setattr(espn_mod, "espn_available", lambda lid: True)
    monkeypatch.setattr(espn_mod, "sync_espn", lambda lid: {"platform": "espn", "lid": lid})
    monkeypatch.setattr(sleeper_mod, "sleeper_available", lambda lid: True)
    monkeypatch.setattr(sleeper_mod, "sync_sleeper", lambda lid: {"platform": "sleeper", "lid": lid})

    out = run_sync("espn")

    assert out["espn"]["espn_lg"] == {"platform": "espn", "lid": "espn_lg"}
    assert out["espn"]["sleeper_lg"] == {"platform": "sleeper", "lid": "sleeper_lg"}


def test_run_sync_espn_scope_isolates_one_leagues_failure(monkeypatch):
    """The existing per-league try/except contract: a Sleeper sync exception
    must be captured for that league only, leaving the ESPN league's result
    untouched."""
    import app.config as config_mod
    import app.etl.espn as espn_mod
    import app.etl.platform as platform_mod
    import app.etl.sleeper as sleeper_mod
    from app.etl.sync import run_sync

    monkeypatch.setattr(config_mod, "league_ids", lambda: ["espn_lg", "sleeper_lg"])
    monkeypatch.setattr(platform_mod, "platform_of",
                        lambda lid: "sleeper" if lid == "sleeper_lg" else "espn")
    monkeypatch.setattr(espn_mod, "espn_available", lambda lid: True)
    monkeypatch.setattr(espn_mod, "sync_espn", lambda lid: {"platform": "espn", "lid": lid})
    monkeypatch.setattr(sleeper_mod, "sleeper_available", lambda lid: True)

    def _boom(lid):
        raise RuntimeError("sleeper API down")

    monkeypatch.setattr(sleeper_mod, "sync_sleeper", _boom)

    out = run_sync("espn")

    assert out["espn"]["espn_lg"] == {"platform": "espn", "lid": "espn_lg"}
    assert "error: sleeper API down" in out["espn"]["sleeper_lg"]
