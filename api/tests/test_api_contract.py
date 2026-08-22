"""Fields the web app reads. These are the contracts nothing currently checks.

Every defect these guard shipped while 109 tests passed and `tsc` exited 0,
because the backend suite stops at the router and types.ts index signatures make
any property access typecheck.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.models.lineup import LineupResult, optimize

client = TestClient(app)


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


def test_waiver_rows_carry_team():
    r = client.get("/api/waivers/rankings")
    assert r.status_code == 200
    body = r.json()
    for row in body["rankings"]:
        assert "team" in row
