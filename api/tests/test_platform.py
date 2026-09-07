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
    """The three ESPN leagues set neither key -- validation must not start
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


# ---------------------------------------------------------------------------
# get_my_roster, Sleeper branch. Previously untested entirely, which is how it
# shipped matching `draft.my_slot` (a snake DRAFT slot) against `roster_id`:
# both are 1..teams, so it silently returned a stranger's roster and the
# `None` fallback never fired.
# ---------------------------------------------------------------------------
import json
from pathlib import Path

from app.etl import platform as platform_mod

SLEEPER_FIXTURE = Path(__file__).parent / "fixtures" / "sleeper_roster.json"
MY_SLEEPER_USER_ID = "1396235398669176832"   # owns roster_id 12


def _sleeper_teams_cache():
    """The `teams` cache shape `sync_sleeper` writes, built from the fixture."""
    raw = json.loads(SLEEPER_FIXTURE.read_text(encoding="utf-8"))
    return {"data": [{
        "roster_id": r["roster_id"],
        "owner_id": r["owner_id"],
        "name": f"Team {r['roster_id']}",
        "players": r["players"],
        "starters": r["starters"],
        "faab_used": r["settings"]["waiver_budget_used"],
        "unresolved": [],
    } for r in raw]}


def _patch_cache(monkeypatch):
    import app.etl.sleeper as sleeper_mod
    monkeypatch.setattr(sleeper_mod, "read_cache",
                        lambda name, lid=None: _sleeper_teams_cache())


def test_sleeper_roster_is_the_one_owned_by_the_configured_user(monkeypatch):
    """End-to-end: league.yaml's sleeper_user_id must survive config.py's
    per-league copy loop and select the roster that user owns."""
    _patch_cache(monkeypatch)
    assert league_config("sundt")["league"]["sleeper_user_id"] == MY_SLEEPER_USER_ID
    out = platform_mod.get_my_roster("sundt")
    assert out["mode"] == "sleeper"
    assert out["team"]["name"] == "Team 12"


def test_sleeper_roster_is_not_selected_by_the_draft_slot(monkeypatch):
    """The regression this fixes: my_slot=10 is a draft slot, and roster_id 10
    exists and belongs to someone else."""
    _patch_cache(monkeypatch)
    assert (league_config("sundt").get("draft") or {}).get("my_slot") == 10
    assert platform_mod.get_my_roster("sundt")["team"]["name"] != "Team 10"


def test_an_owner_id_matching_no_roster_raises(monkeypatch):
    """Silent-wrong is the defect. Loud-wrong is acceptable; falling through to
    another manager's roster is not."""
    _patch_cache(monkeypatch)
    monkeypatch.setattr(platform_mod, "league_config", lambda lid=None: {
        "league": {"platform": "sleeper", "sleeper_user_id": "nobody"}})
    with pytest.raises(LookupError, match="owns none"):
        platform_mod.get_my_roster("sundt")


def test_a_league_with_no_sleeper_user_id_returns_an_empty_roster(monkeypatch):
    """Degrade to 'no roster', never to rosters[0]."""
    _patch_cache(monkeypatch)
    monkeypatch.setattr(platform_mod, "league_config", lambda lid=None: {
        "league": {"platform": "sleeper"}})
    out = platform_mod.get_my_roster("sundt")
    assert out["team"]["roster"] == []
    assert out["team"]["name"] != "Team 1"


# ---------------------------------------------------------------------------
# I8: a per-league override of a shared block must MERGE, not replace. A
# shallow `dict.update` drops every sibling key the override omits, with no
# error until something downstream KeyErrors on a key that was always there.
# ---------------------------------------------------------------------------
def test_a_partial_roster_override_keeps_the_shared_siblings():
    from app.config import _deep_merge

    base = {"starters": {"QB": 1, "RB": 2}, "flex_eligible": ["RB", "WR", "TE"],
            "bench": 6, "ir": 1}
    merged = _deep_merge(base, {"starters": {"QB": 2}})
    assert merged["bench"] == 6 and merged["ir"] == 1
    assert merged["flex_eligible"] == ["RB", "WR", "TE"]
    assert merged["starters"] == {"QB": 2, "RB": 2}, "nested keys merge too"


def test_a_partial_scoring_override_keeps_the_other_scoring_blocks():
    from app.config import _deep_merge

    base = {"passing": {"touchdown": 4, "interception": -2},
            "receiving": {"reception": 0.5}}
    merged = _deep_merge(base, {"receiving": {"reception": 1.0}})
    assert merged["passing"] == {"touchdown": 4, "interception": -2}
    assert merged["receiving"]["reception"] == 1.0


def test_lists_are_replaced_wholesale_not_concatenated():
    from app.config import _deep_merge

    merged = _deep_merge({"tiers": [[0, 5], [999, -4]]}, {"tiers": [[0, 10]]})
    assert merged["tiers"] == [[0, 10]]


def test_the_shipped_sundt_config_still_carries_every_roster_key():
    """End-to-end: the real config must not lose a key to the merge change."""
    roster = league_config("sundt")["roster"]
    for key in ("starters", "flex_eligible", "flex_slots", "bench", "ir"):
        assert key in roster
    assert roster["starters"]["SUPER_FLEX"] == 1
    assert league_config("sundt")["waivers"]["faab_budget"] == 200


def test_league_config_merges_a_partial_block_at_the_call_site(monkeypatch):
    """Call-site guard, not just `_deep_merge` in isolation: `league_config`
    itself must deep-merge, so reverting it to `dict.update` fails here."""
    import app.config as config_mod

    fake = {"id": "partial_lg", "name": "Partial", "teams": 12,
            "roster": {"starters": {"QB": 2}},
            "scoring": {"receiving": {"reception": 1.0}}}
    monkeypatch.setattr(config_mod, "leagues", lambda: (fake,))
    config_mod.league_config.cache_clear()
    try:
        cfg = config_mod.league_config("partial_lg")
        # siblings the override never mentioned must survive
        assert "bench" in cfg["roster"] and "flex_eligible" in cfg["roster"]
        assert cfg["roster"]["starters"]["RB"] == 2      # shared key kept
        assert cfg["roster"]["starters"]["QB"] == 2      # override applied
        assert "passing" in cfg["scoring"] and "dst" in cfg["scoring"]
        assert cfg["scoring"]["receiving"]["reception"] == 1.0
        assert cfg["scoring"]["receiving"]["touchdown"] == 6
    finally:
        config_mod.league_config.cache_clear()
