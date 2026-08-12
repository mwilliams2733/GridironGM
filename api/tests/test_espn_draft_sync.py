"""ESPN draft-sync merge.

The ESPN fetch itself can't be tested without credentials and a live draft, so
these cover the half that is testable and that would silently corrupt a draft if
wrong: the merge. Re-syncing during a draft must append only new picks, never
duplicate a player, and never overwrite something typed manually.

`fetch_draft_picks` is stubbed — see its docstring for why the real call reads
`draftDetail.picks` directly instead of going through espn_api's League.draft.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import league_config
from app.main import app
from app.routers import draft as draft_router

client = TestClient(app)
LEAGUE = "league1"


def _espn_pick(overall: int, espn_team_id: int, round_pick: int, player_id: int, rnd: int = 1):
    return {
        "espn_team_id": espn_team_id, "espn_player_id": player_id,
        "round": rnd, "round_pick": round_pick, "overall": overall, "keeper": False,
    }


@pytest.fixture
def espn_player_ids():
    """Real espn_ids from the players table, so resolution actually succeeds."""
    from app.db import read_df
    df = read_df("SELECT espn_id FROM players WHERE espn_id IS NOT NULL LIMIT 6")
    # espn_id is stored float-like ('4429202.0'), which is why resolve_espn_player
    # compares via float() rather than int().
    ids = [int(float(x)) for x in df["espn_id"].tolist()]
    if len(ids) < 4:
        pytest.skip("not enough espn_id-mapped players in the local DB")
    return ids


@pytest.fixture(autouse=True)
def clean_draft():
    """Each test starts and ends with an empty draft for LEAGUE."""
    client.post(f"/api/draft/reset?league_id={LEAGUE}", json={})
    yield
    client.post(f"/api/draft/reset?league_id={LEAGUE}", json={})


def test_sync_is_idempotent(monkeypatch, espn_player_ids):
    picks = [_espn_pick(i + 1, 100 + i, i + 1, pid) for i, pid in enumerate(espn_player_ids[:4])]
    monkeypatch.setattr(draft_router.espn_etl, "fetch_draft_picks", lambda _lid: picks)

    first = client.post(f"/api/draft/sync-espn?league_id={LEAGUE}").json()
    assert first["added"] == 4, first

    second = client.post(f"/api/draft/sync-espn?league_id={LEAGUE}").json()
    assert second["added"] == 0, "re-syncing must not duplicate picks"
    assert second["total_picks"] == 4


def test_sync_appends_only_new_picks(monkeypatch, espn_player_ids):
    two = [_espn_pick(i + 1, 100 + i, i + 1, pid) for i, pid in enumerate(espn_player_ids[:2])]
    monkeypatch.setattr(draft_router.espn_etl, "fetch_draft_picks", lambda _lid: two)
    assert client.post(f"/api/draft/sync-espn?league_id={LEAGUE}").json()["added"] == 2

    four = [_espn_pick(i + 1, 100 + i, i + 1, pid) for i, pid in enumerate(espn_player_ids[:4])]
    monkeypatch.setattr(draft_router.espn_etl, "fetch_draft_picks", lambda _lid: four)
    again = client.post(f"/api/draft/sync-espn?league_id={LEAGUE}").json()
    assert again["added"] == 2, "only the two new picks should land"
    assert again["total_picks"] == 4


def test_sync_never_drafts_the_same_player_twice(monkeypatch, espn_player_ids):
    """A player already entered manually must not be re-added under an ESPN pick."""
    dupe = espn_player_ids[0]
    picks = [_espn_pick(1, 100, 1, dupe), _espn_pick(2, 101, 2, dupe)]
    monkeypatch.setattr(draft_router.espn_etl, "fetch_draft_picks", lambda _lid: picks)

    client.post(f"/api/draft/sync-espn?league_id={LEAGUE}")
    board = client.get(f"/api/draft/board?limit=1&league_id={LEAGUE}").json()
    ids = [p["player_id"] for t in board["teams"] for p in t["picks"]]
    assert len(ids) == len(set(ids)), "the same player was drafted twice"


def test_slot_comes_from_round_one_order_not_espn_team_id(monkeypatch, espn_player_ids):
    """ESPN team ids are arbitrary; draft slot must come from round-1 pick order."""
    # Team 900 picks 3rd, team 901 picks 1st — ids deliberately unordered.
    picks = [
        _espn_pick(1, 901, 1, espn_player_ids[0]),
        _espn_pick(3, 900, 3, espn_player_ids[1]),
    ]
    monkeypatch.setattr(draft_router.espn_etl, "fetch_draft_picks", lambda _lid: picks)
    client.post(f"/api/draft/sync-espn?league_id={LEAGUE}")

    board = client.get(f"/api/draft/board?limit=1&league_id={LEAGUE}").json()
    by_slot = {t["slot"]: [p["player_id"] for p in t["picks"]] for t in board["teams"]}
    assert by_slot[1], "team that picked first should hold slot 1"
    assert by_slot[3], "team that picked third should hold slot 3"


def test_a_corrected_pick_does_not_create_a_second_pick_at_the_same_slot(
    monkeypatch, espn_player_ids
):
    """ESPN may re-report an overall pick with a different player (a correction).

    The player-id guard can't catch this — the new player isn't drafted yet — so
    the overall-number guard is what keeps the draft from gaining a phantom extra
    pick and shifting every later slot by one.
    """
    first = [_espn_pick(1, 100, 1, espn_player_ids[0])]
    monkeypatch.setattr(draft_router.espn_etl, "fetch_draft_picks", lambda _lid: first)
    client.post(f"/api/draft/sync-espn?league_id={LEAGUE}")

    corrected = [_espn_pick(1, 100, 1, espn_player_ids[1])]  # same overall, new player
    monkeypatch.setattr(draft_router.espn_etl, "fetch_draft_picks", lambda _lid: corrected)
    result = client.post(f"/api/draft/sync-espn?league_id={LEAGUE}").json()

    assert result["total_picks"] == 1, "overall pick 1 must not be recorded twice"
    assert result["added"] == 0

    board = client.get(f"/api/draft/board?limit=1&league_id={LEAGUE}").json()
    overalls = [p["overall"] for t in board["teams"] for p in t["picks"]]
    assert len(overalls) == len(set(overalls)), "duplicate overall pick numbers"


def test_unknown_league_is_404(monkeypatch):
    monkeypatch.setattr(draft_router.espn_etl, "fetch_draft_picks", lambda _lid: [])
    assert client.post("/api/draft/sync-espn?league_id=nope").status_code == 404


def test_espn_failure_is_502_not_500(monkeypatch):
    def boom(_lid):
        raise RuntimeError("ESPN unreachable")

    monkeypatch.setattr(draft_router.espn_etl, "fetch_draft_picks", boom)
    r = client.post(f"/api/draft/sync-espn?league_id={LEAGUE}")
    assert r.status_code == 502
    assert "ESPN" in r.json()["detail"]


def test_teams_count_matches_league_size():
    board = client.get(f"/api/draft/board?limit=1&league_id={LEAGUE}").json()
    assert len(board["teams"]) == league_config(LEAGUE)["league"]["teams"]
