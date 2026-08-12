"""FastAPI TestClient smoke tests: health, config, draft board, lineup optimal —
assert legal shape + no 500s."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_config():
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert "league" in body
    assert "roster" in body
    assert "scoring" in body


def test_draft_board_shape():
    r = client.get("/api/draft/board?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert "board" in body
    assert "drafted_count" in body
    assert "current_pick" in body
    assert isinstance(body["board"], list)
    if body["board"]:
        row = body["board"][0]
        for key in ("player_id", "name", "pos", "proj", "vorp", "tier"):
            assert key in row


def test_lineup_optimal_shape_and_no_500():
    r = client.get("/api/lineup/optimal?season=2025&week=10")
    assert r.status_code == 200
    body = r.json()
    for key in ("starters", "bench", "current_total", "optimal_total", "delta",
                "close_calls", "week", "season"):
        assert key in body
    assert isinstance(body["starters"], list)
    assert isinstance(body["bench"], list)


def test_lineup_optimal_no_roster_returns_empty_not_500(monkeypatch):
    # No ESPN/manual roster configured -> 200 with empty lists, never 500.
    # Patched rather than relying on data/espn_cache being empty, since a real
    # manual roster may be configured locally.
    monkeypatch.setattr("app.routers.lineup._roster_ids", lambda *a, **kw: [])
    r = client.get("/api/lineup/optimal")
    assert r.status_code == 200
    body = r.json()
    assert body["starters"] == []


@pytest.mark.parametrize("path", [
    "/api/league/roster",
    "/api/league/teams",
    "/api/league/players?limit=5",
    "/api/draft/board",
    "/api/draft/recommendation",
    "/api/waivers/rankings",
    "/api/dashboard",
])
def test_no_500s_across_endpoints(path):
    r = client.get(path)
    assert r.status_code != 500, f"{path} returned 500: {r.text}"
    assert r.status_code < 500


def test_draft_pick_undo_reset_roundtrip():
    r = client.post("/api/draft/reset", json={"my_slot": 1})
    assert r.status_code == 200

    board = client.get("/api/draft/board?limit=1").json()
    if not board["board"]:
        pytest.skip("no draft board available")
    pid = board["board"][0]["player_id"]

    r = client.post("/api/draft/pick", json={"player_id": pid, "by_me": True})
    assert r.status_code == 200
    assert r.json()["picks"] == 1

    r = client.post("/api/draft/undo")
    assert r.status_code == 200
    assert r.json()["picks"] == 0

    r = client.post("/api/draft/reset")
    assert r.status_code == 200
