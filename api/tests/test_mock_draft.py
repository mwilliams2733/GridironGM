"""Mock draft simulation.

The guard that matters most is isolation: simulating must never touch the draft
state you use for real. Everything else here is about the mock being usable —
deterministic under a seed, stopping when you're on the clock, and producing a
draft that doesn't look absurd (no kickers in round 2, no third quarterback).
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import draft_state_path, league_config
from app.main import app

client = TestClient(app)
LEAGUE = "league1"
MY_SLOT = 4


def _reset(mock: bool, my_slot: int | None = MY_SLOT):
    body = {"my_slot": my_slot} if my_slot else {}
    return client.post(
        f"/api/draft/reset?league_id={LEAGUE}&mock={str(mock).lower()}", json=body
    )


def _simulate(**body):
    body.setdefault("seed", 11)
    return client.post(
        f"/api/draft/simulate?league_id={LEAGUE}&mock=true", json=body
    ).json()


def _board(mock: bool):
    return client.get(
        f"/api/draft/board?limit=1&league_id={LEAGUE}&mock={str(mock).lower()}"
    ).json()


@pytest.fixture(autouse=True)
def clean():
    _reset(mock=True)
    yield
    _reset(mock=True, my_slot=None)


def test_simulating_never_touches_the_real_draft():
    """The whole reason mock state exists. If this breaks, a practice draft
    silently poisons the board you draft from for real."""
    _reset(mock=False, my_slot=None)
    real_before = draft_state_path(LEAGUE, mock=False).read_text(encoding="utf-8")

    _reset(mock=True)
    result = _simulate(mode="picks", count=40)
    assert result["added"] > 0

    real_after = draft_state_path(LEAGUE, mock=False).read_text(encoding="utf-8")
    assert real_before == real_after, "the real draft file changed during a mock"
    assert json.loads(real_after)["picks"] == []
    assert _board(mock=False)["drafted_count"] == 0
    assert _board(mock=True)["drafted_count"] > 0


def test_mock_and_real_state_are_different_files():
    assert draft_state_path(LEAGUE, mock=True) != draft_state_path(LEAGUE, mock=False)


def test_same_seed_gives_the_same_draft():
    first = _simulate(mode="picks", count=24, seed=99)["picks"]
    _reset(mock=True)
    second = _simulate(mode="picks", count=24, seed=99)["picks"]
    assert [p["player_id"] for p in first] == [p["player_id"] for p in second]


def test_different_seeds_give_different_drafts():
    a = _simulate(mode="picks", count=24, seed=1)["picks"]
    _reset(mock=True)
    b = _simulate(mode="picks", count=24, seed=2)["picks"]
    assert [p["player_id"] for p in a] != [p["player_id"] for p in b]


def test_stops_when_i_am_on_the_clock():
    r = _simulate(mode="to_my_pick")
    assert r["stopped"] == "my_pick"
    assert r["on_the_clock"] == MY_SLOT
    # It stopped BEFORE picking for me.
    assert all(not p["by_me"] for p in r["picks"])


def test_to_my_pick_requires_a_draft_slot():
    _reset(mock=True, my_slot=None)
    r = client.post(
        f"/api/draft/simulate?league_id={LEAGUE}&mock=true", json={"mode": "to_my_pick"}
    )
    assert r.status_code == 400
    assert "slot" in r.json()["detail"].lower()


def test_no_player_is_drafted_twice():
    _simulate(mode="picks", count=500)
    board = _board(mock=True)
    ids = [p["player_id"] for t in board["teams"] for p in t["picks"]]
    assert len(ids) == len(set(ids))


def test_full_draft_fills_every_pick_exactly():
    cfg = league_config(LEAGUE)
    expected = int(cfg["league"]["teams"]) * int(cfg["draft"]["rounds"])
    r = _simulate(mode="picks", count=expected + 50)
    assert r["stopped"] == "draft_complete"
    assert _board(mock=True)["drafted_count"] == expected


def test_kickers_and_defenses_go_late():
    """A mock that takes a kicker in round 3 is useless for reading the board."""
    cfg = league_config(LEAGUE)
    rounds = int(cfg["draft"]["rounds"])
    _simulate(mode="picks", count=cfg["league"]["teams"] * rounds)

    board = _board(mock=True)
    early = [
        p for t in board["teams"] for p in t["picks"]
        if p["position"] in ("K", "DST") and p["round"] < rounds - 1
    ]
    assert not early, f"K/DST drafted before the last two rounds: {early[:3]}"


def test_no_team_hoards_a_scarce_position():
    _simulate(mode="picks", count=500)
    board = _board(mock=True)
    for team in board["teams"]:
        counts = team["position_counts"]
        assert counts.get("QB", 0) <= 2, f"team {team['slot']} drafted {counts.get('QB')} QBs"
        assert counts.get("TE", 0) <= 2
        assert counts.get("K", 0) <= 1
        assert counts.get("DST", 0) <= 1


def test_picks_are_attributed_in_snake_order():
    r = _simulate(mode="picks", count=26)
    teams = int(league_config(LEAGUE)["league"]["teams"])
    for p in r["picks"]:
        rnd = (p["overall"] - 1) // teams + 1
        pos_in_round = (p["overall"] - 1) % teams + 1
        expected = pos_in_round if rnd % 2 == 1 else teams - pos_in_round + 1
        assert p["slot"] == expected, f"pick {p['overall']} went to slot {p['slot']}"


def test_simulated_picks_are_labelled_as_simulated():
    """Source has to distinguish sim from manual, or an ESPN re-sync can't tell
    what it is allowed to reconcile."""
    r = _simulate(mode="picks", count=5)
    assert all(p["source"] == "sim" for p in r["picks"])
