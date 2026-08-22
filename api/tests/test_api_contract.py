"""Fields the web app reads. These are the contracts nothing currently checks.

Every defect these guard shipped while 109 tests passed and `tsc` exited 0,
because the backend suite stops at the router and types.ts index signatures make
any property access typecheck.
"""
from __future__ import annotations

import copy

from fastapi.testclient import TestClient

from app.config import league_config
from app.main import app
from app.models.lineup import LineupResult, optimize

client = TestClient(app)


def _sundt_cfg():
    """A superflex config (SUPER_FLEX slot present), same fixture shape as
    tests/test_flex_slots.py's SUNDT_ROSTER -- kept local and minimal since
    this file only needs the one slot for the empty-placeholder regression."""
    cfg = copy.deepcopy(league_config())
    cfg["roster"] = copy.deepcopy(cfg["roster"])
    cfg["roster"]["starters"] = {"SUPER_FLEX": 1}
    cfg["roster"]["flex_eligible"] = ["RB", "WR", "TE"]
    cfg["roster"]["flex_slots"] = {
        "SUPER_FLEX": {"eligible": ["QB", "RB", "WR", "TE"],
                       "share": {"QB": .90, "RB": .04, "WR": .04, "TE": .02}},
    }
    return cfg


def _recs():
    return {f"p{i}": {"player_id": f"p{i}", "name": f"P{i}", "position": pos,
                      "team": "BUF", "opponent": "MIA", "proj_points": pts,
                      "floor": pts * .8, "ceiling": pts * 1.2}
            for i, (pos, pts) in enumerate(
                [("QB", 22.), ("RB", 19.), ("RB", 14.), ("WR", 20.), ("WR", 17.),
                 ("TE", 10.), ("K", 8.), ("DST", 6.), ("RB", 9.)])}


def _frame(recs):
    import pandas as pd
    return pd.DataFrame(list(recs.values()))


def test_lineup_starters_carry_team():
    """Waivers and Start/Sit both render `team`; it was absent from both."""
    recs = _recs()
    res = optimize(list(recs), 2026, 1, week_proj=_frame(recs))
    for slot, player in res.slots.items():
        if player.get("player_id"):
            assert "team" in player, f"slot {slot} lost `team`"


def test_lineup_bench_entries_carry_team():
    recs = _recs()
    res = optimize(list(recs), 2026, 1, week_proj=_frame(recs))
    for b in res.bench:
        assert "team" in b


def test_current_total_and_delta_are_populated_when_a_lineup_is_supplied():
    """These were permanently None: the router never passed current_lineup."""
    recs = _recs()
    ids = list(recs)
    res = optimize(ids, 2026, 1, week_proj=_frame(recs), current_lineup=ids[:8])
    assert res.current_total is not None
    assert res.delta is not None


def test_delta_is_zero_when_the_current_lineup_is_already_optimal():
    recs = _recs()
    res = optimize(list(recs), 2026, 1, week_proj=_frame(recs))
    optimal_ids = [p["player_id"] for p in res.slots.values() if p.get("player_id")]
    again = optimize(list(recs), 2026, 1, week_proj=_frame(recs),
                     current_lineup=optimal_ids)
    assert again.delta == 0.0


def test_empty_super_flex_slot_position_is_a_single_word():
    """Carry-forward from Task 5: an empty slot's `position` used to be the
    joined eligibility list ("QB/RB/WR/TE"), which PositionBadge renders as
    one run-on blob instead of a real position label."""
    cfg = _sundt_cfg()
    recs = _recs()
    res = optimize([], 2026, 1, cfg=cfg, week_proj=_frame(recs))
    empty = res.slots["SUPER_FLEX"]
    assert empty["player_id"] is None
    assert "/" not in empty["position"]
    assert empty["position"] == "SUPER_FLEX"


# --- wiring-level: the router, not just the model -------------------------
# A test against `optimize()` alone would not catch defect 1 -- the router
# never called it with `current_lineup`. These exercise `get_optimal` itself.


def _fake_roster(*_a, **_kw):
    ids = [f"p{i}" for i in range(9)]
    return {"mode": "espn", "team": {"name": "My Team",
            "roster": [{"player_id": pid} for pid in ids],
            "starters": ids[:8]}}


def test_get_optimal_wires_current_lineup_from_the_roster(monkeypatch):
    """Router-level: current_total/delta must not be null when a roster with
    `starters` is available -- this was the actual live defect."""
    import app.routers.lineup as lineup_router

    monkeypatch.setattr(lineup_router.platform, "get_my_roster", _fake_roster)
    monkeypatch.setattr(lineup_router, "_roster_ids",
                        lambda *a, **kw: [f"p{i}" for i in range(9)])
    r = client.get("/api/lineup/optimal?season=2025&week=10")
    assert r.status_code == 200
    body = r.json()
    assert body["current_total"] is not None
    assert body["delta"] is not None


def test_get_optimal_starters_and_bench_carry_team(monkeypatch):
    import app.routers.lineup as lineup_router

    monkeypatch.setattr(lineup_router.platform, "get_my_roster", _fake_roster)
    monkeypatch.setattr(lineup_router, "_roster_ids",
                        lambda *a, **kw: [f"p{i}" for i in range(9)])
    r = client.get("/api/lineup/optimal?season=2025&week=10")
    assert r.status_code == 200
    body = r.json()
    for row in body["starters"]:
        if row["player"] and row["player"].get("player_id"):
            assert "team" in row["player"]
    for row in body["bench"]:
        assert "team" in row


# --- CRITICAL regression: an empty starter slot must not blank the page ----
# Sleeper's `starters` mix resolved player_ids with `None` for an unfilled
# slot. `_current_lineup_ids` used to hand `None` straight to
# `resolve_roster_entry`, which does `"player_id" in entry` and raises
# `TypeError` on a non-dict -- caught by `get_optimal`'s broad `except` and
# silently returned as `starters=[]`/`bench=[]`, blanking the whole page
# instead of merely leaving `current_total` null. Verified live against the
# synced Sundt (Sleeper) roster cache: 2 of 12 teams have an empty slot today.


def test_current_lineup_ids_skips_none_entries():
    from app.routers.lineup import _current_lineup_ids

    assert _current_lineup_ids({"starters": ["p1", None, "p2"]}) == ["p1", "p2"]


def _fake_roster_with_empty_slot(*_a, **_kw):
    ids = [f"p{i}" for i in range(9)]
    return {"mode": "sleeper", "team": {"name": "My Team",
            "roster": [{"player_id": pid} for pid in ids],
            "starters": ids[:7] + [None]}}


def test_get_optimal_does_not_blank_the_page_on_an_empty_starter_slot(monkeypatch):
    import app.routers.lineup as lineup_router

    monkeypatch.setattr(lineup_router.platform, "get_my_roster", _fake_roster_with_empty_slot)
    monkeypatch.setattr(lineup_router, "_roster_ids",
                        lambda *a, **kw: [f"p{i}" for i in range(9)])
    r = client.get("/api/lineup/optimal?season=2025&week=10")
    assert r.status_code == 200
    body = r.json()
    assert body["starters"] != []
    assert body["bench"] != []
    assert body["current_total"] is not None
    assert "warning" not in body


# --- ESPN's raw-dict starter shape is exercised, not just read -------------


def test_current_lineup_ids_resolves_espn_raw_roster_entries():
    """`_fake_roster()` above supplies Sleeper-shaped resolved-id strings for
    every wiring test so far, so the ESPN branch (`resolve_roster_entry` on a
    raw `espn_id`/`name`/`position` dict) was previously verified only by
    reading the code, not by running it."""
    import pandas as pd

    from app.routers._common import norm_name
    from app.routers.lineup import _current_lineup_ids

    players = pd.DataFrame([
        {"player_id": "00-1", "name": "Josh Allen", "position": "QB",
         "team": "BUF", "espn_id": 111, "status": "ACT"},
    ])
    players["norm"] = players["name"].map(norm_name)

    team = {"starters": [
        {"espn_id": 111, "name": "Josh Allen", "position": "QB", "lineup_slot": "QB"},
    ]}
    assert _current_lineup_ids(team, players=players) == ["00-1"]


def test_starters_from_roster_excludes_bench_and_ir():
    from app.etl.espn import _starters_from_roster

    roster = [
        {"lineup_slot": "QB", "name": "A"},
        {"lineup_slot": "BE", "name": "B"},
        {"lineup_slot": "IR", "name": "C"},
        {"lineup_slot": "BN", "name": "D"},
        {"lineup_slot": "RB", "name": "E"},
    ]
    starters = _starters_from_roster(roster)
    assert [s["name"] for s in starters] == ["A", "E"]


def test_waiver_rows_carry_team():
    r = client.get("/api/waivers/rankings")
    assert r.status_code == 200
    body = r.json()
    assert len(body["rankings"]) > 0, "no rankings returned -- assertion below would be vacuous"
    for row in body["rankings"]:
        assert "team" in row


# --- FAAB (defect 5): budget - faab_used, and a None fallback, not zero ----


def test_faab_remaining_subtracts_faab_used_from_the_configured_budget(monkeypatch):
    import app.routers.waivers as waivers_router

    monkeypatch.setattr(waivers_router.platform, "get_my_roster",
                        lambda *a, **kw: {"team": {"faab_used": 50}})
    monkeypatch.setattr(waivers_router, "league_config",
                        lambda *a, **kw: {"waivers": {"faab_budget": 200}})
    assert waivers_router._faab_remaining(None) == 150.0


def test_faab_remaining_is_none_when_the_platform_omits_faab_used(monkeypatch):
    """ESPN rosters don't report usage. `rank_free_agents` must fall back to
    a full budget on `None`, not treat a missing key as zero spent."""
    import app.routers.waivers as waivers_router

    monkeypatch.setattr(waivers_router.platform, "get_my_roster",
                        lambda *a, **kw: {"team": {"name": "My Team"}})
    assert waivers_router._faab_remaining(None) is None


def test_get_rankings_wires_faab_remaining_into_rank_free_agents(monkeypatch):
    """Wiring-level: `get_rankings` must actually pass `_faab_remaining`'s
    result into `rank_free_agents`, not just have both pieces exist."""
    import pandas as pd

    import app.routers.waivers as waivers_router

    captured = {}

    def fake_rank(fa_ids, my_roster, season, week, faab_remaining=None, cfg=None):
        captured["faab_remaining"] = faab_remaining
        captured["cfg"] = cfg
        return pd.DataFrame([{
            "player_id": fa_ids[0], "name": "X", "pos": "WR", "team": "BUF",
            "ros_value": 1.0, "next_week_value": 1.0, "score": 1.0,
            "breakout_flags": [], "suggested_drop": None, "suggested_drop_id": None,
            "faab_bid": 1, "confidence": "Low", "rationale": "x",
        }])

    monkeypatch.setattr(waivers_router, "_my_roster_ids", lambda *a, **kw: [])
    monkeypatch.setattr(waivers_router, "_free_agent_ids", lambda *a, **kw: (["p1"], None))
    monkeypatch.setattr(waivers_router, "_faab_remaining", lambda *a, **kw: 123.0)
    monkeypatch.setattr(waivers_router.waivers_model, "rank_free_agents", fake_rank)

    r = client.get("/api/waivers/rankings")
    assert r.status_code == 200
    assert captured["faab_remaining"] == 123.0
    # ...and the league's config, without which the ranking is scored under the
    # active league's rules while the free-agent pool was built under this one's.
    assert captured["cfg"] is not None


# --- Cache invalidation (defect 4): a cached value must actually change ----


def test_run_sync_clears_projection_caches():
    """Regression for the wrong-name bug: `_weekly_raw` (uncached) instead of
    `_weekly_rows` (the actual `@lru_cache`'d function) would either raise
    `AttributeError` or silently no-op. Uses an unrecognized scope so no
    branch in `run_sync`'s if/elif chain fires -- no network calls, only the
    unconditional cache-clearing tail that runs after the loop."""
    from app.etl import sync
    from app.models import projections as proj
    from app.models import vorp
    from app.routers import draft as draft_router

    # populate every cache the invalidation block is responsible for
    proj._players()
    proj._weekly_rows((2025,))
    proj._snaps(2025)
    proj._completed_seasons(2025)
    vorp._players_indexed()
    draft_router._season_proj_cache[(2025, "sentinelkey")] = "sentinel"

    assert proj._players.cache_info().currsize > 0
    assert proj._weekly_rows.cache_info().currsize > 0
    assert proj._snaps.cache_info().currsize > 0
    assert proj._completed_seasons.cache_info().currsize > 0
    # `_players_indexed` backs ADP name resolution: stale here means `resolve_adp`
    # matches newly-synced players against the pre-sync players table.
    assert vorp._players_indexed.cache_info().currsize > 0
    assert draft_router._season_proj_cache

    sync.run_sync("__no_such_scope_used_only_to_reach_the_cache_clear_tail__")

    assert proj._players.cache_info().currsize == 0
    assert proj._weekly_rows.cache_info().currsize == 0
    assert proj._snaps.cache_info().currsize == 0
    assert proj._completed_seasons.cache_info().currsize == 0
    assert vorp._players_indexed.cache_info().currsize == 0
    assert draft_router._season_proj_cache == {}


# --- I4: the season-projection cache must be keyed by scoring profile -------


def test_season_proj_cache_is_keyed_by_scoring_profile():
    """The old comment ("scoring is shared, so key by season only") stopped being
    true when Sundt got its own scoring block; the cache then served one
    league's projections to another."""
    from app.config import league_config
    from app.routers import draft as draft_router
    from app.scoring import profile_key

    espn, sundt = league_config("league1"), league_config("sundt")
    assert profile_key(espn) != profile_key(sundt), "premise: scoring differs"

    draft_router._season_proj_cache.clear()
    draft_router._season_proj_cache[(2025, profile_key(espn))] = "espn-frame"
    assert draft_router._season_proj(2025, espn) == "espn-frame"
    # The Sundt call must MISS this entry rather than reuse the ESPN frame.
    assert draft_router._season_proj_cache.get((2025, profile_key(sundt))) is None
    draft_router._season_proj_cache.clear()


# --- I7: a failed roster read must not look like an empty roster -----------


def test_waiver_rankings_surface_a_roster_read_failure(monkeypatch):
    """An empty roster makes worst_drop_val 0.0, so every free agent's upgrade
    becomes his full ROS value and FAAB is sized against nothing — plausible
    output from a total failure. It must warn instead."""
    import app.routers.waivers as waivers_router

    def _boom(*a, **kw):
        raise RuntimeError("sleeper cache unreadable")

    monkeypatch.setattr(waivers_router.platform, "get_my_roster", _boom)

    r = client.get("/api/waivers/rankings")
    assert r.status_code == 200
    body = r.json()
    assert body["rankings"] == []
    assert "sleeper cache unreadable" in body.get("warning", "")
