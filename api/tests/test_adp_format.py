"""ADP is keyed by SCORING FORMAT, not by team count.

FantasyFootballCalculator's `teams` parameter is a no-op: measured 2026-08-27,
`teams=10` and `teams=12` return byte-identical player sets AND identical ADP
values, and the response does not echo the parameter back. What genuinely
differs is the scoring format — half-PPR and full-PPR disagree on 120 of 228
common players by >=3 picks (largest swing 40.9), and full-PPR carries 38
players half-PPR does not list at all.

So a full-PPR league reading half-PPR ADP gets the wrong market, and these
tests pin the format dimension rather than the team-count one.
"""
from __future__ import annotations

import copy

import pandas as pd
import pytest

from app.config import league_config
from app.etl.adp import FFC_FORMATS, adp_format


def _cfg(reception: float) -> dict:
    cfg = copy.deepcopy(league_config("league1"))
    cfg["scoring"]["receiving"]["reception"] = reception
    return cfg


@pytest.fixture
def tmp_adp_table(tmp_path, monkeypatch):
    """A throwaway database holding two formats' ADP for the same season.

    Points the storage layer at a temp file rather than the developer's real
    `gridiron.db`, so these tests neither depend on a synced machine nor
    mutate one. `_players_indexed` is `lru_cache`d, so it is cleared on both
    sides of the swap.
    """
    import app.db as db
    from app.models import vorp

    rows = [
        # (format, player_name, position, team, adp)
        ("half-ppr", "Half Only", "WR", "BUF", 30.0),
        ("half-ppr", "In Both", "WR", "BUF", 50.0),
        ("ppr", "Full Only", "WR", "MIN", 40.0),
        ("ppr", "In Both", "WR", "BUF", 20.0),
    ]
    players = [
        ("p_half", "Half Only", "WR", "BUF"),
        ("p_both", "In Both", "WR", "BUF"),
        ("p_full", "Full Only", "WR", "MIN"),
    ]

    original = db.DB_PATH
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "adp_test.db")
    vorp._players_indexed.cache_clear()
    db.init_db()
    with db.connect() as conn:
        conn.executemany(
            "INSERT INTO adp (format, player_name, position, team, adp, adp_formatted, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, '', '')", rows)
        conn.executemany(
            "INSERT INTO players (player_id, name, position, team) VALUES (?, ?, ?, ?)",
            players)
    yield
    vorp._players_indexed.cache_clear()
    monkeypatch.setattr(db, "DB_PATH", original)


# --- format derivation ------------------------------------------------------

def test_full_ppr_maps_to_the_ppr_endpoint():
    assert adp_format(_cfg(1.0)) == "ppr"


def test_half_ppr_maps_to_the_half_ppr_endpoint():
    assert adp_format(_cfg(0.5)) == "half-ppr"


def test_no_ppr_maps_to_the_standard_endpoint():
    assert adp_format(_cfg(0.0)) == "standard"


def test_an_unmapped_reception_value_raises_rather_than_guessing():
    """Loud, not silent.

    Picking a nearest format for a 0.75-PPR league would hand it the wrong
    market with no error -- the same silent-wrong shape that let half-PPR ADP
    reach a full-PPR league in the first place.
    """
    with pytest.raises(ValueError, match="reception"):
        adp_format(_cfg(0.75))


def test_every_mapped_format_is_a_real_ffc_endpoint_name():
    assert set(FFC_FORMATS.values()) <= {"standard", "half-ppr", "ppr"}


def test_the_shipped_leagues_resolve_to_their_real_formats():
    """The four ESPN leagues are half-PPR; Sundt is full PPR."""
    assert adp_format(league_config("league1")) == "half-ppr"
    assert adp_format(league_config("sundt")) == "ppr"


# --- storage and retrieval --------------------------------------------------

def test_resolve_adp_returns_only_the_configured_leagues_format(tmp_adp_table):
    """The wiring, not just the helper.

    A league must see its own format's rows. Without the filter every league
    reads whichever rows happen to be in the table.
    """
    from app.models.vorp import resolve_adp

    half = resolve_adp(2026, cfg=_cfg(0.5))
    full = resolve_adp(2026, cfg=_cfg(1.0))

    assert set(half["player_name"]) == {"Half Only", "In Both"}
    assert set(full["player_name"]) == {"Full Only", "In Both"}


def test_resolve_adp_returns_the_formats_own_values_for_a_shared_player(tmp_adp_table):
    """`In Both` is listed at different ADP under each format -- the real
    half-PPR vs full-PPR disagreement in miniature."""
    from app.models.vorp import resolve_adp

    half = resolve_adp(2026, cfg=_cfg(0.5))
    full = resolve_adp(2026, cfg=_cfg(1.0))

    assert float(half[half.player_name == "In Both"].iloc[0].adp) == 50.0
    assert float(full[full.player_name == "In Both"].iloc[0].adp) == 20.0


def test_vorp_board_adp_column_comes_from_the_leagues_format(tmp_adp_table):
    """End-to-end: the board a league actually renders must carry ITS market.

    A test that only exercised `resolve_adp` would pass even if `vorp_board`
    dropped the cfg on the way through -- the exact defect class that cost
    four review rounds on the preceding branch.
    """
    from app.models.vorp import vorp_board

    season_proj = pd.DataFrame([
        {"player_id": "p_both", "name": "In Both", "position": "WR", "team": "BUF",
         "proj_points": 200.0, "proj_ppg": 12.0, "floor": 180.0, "ceiling": 220.0,
         "components": {}},
    ])
    half = vorp_board(season=2026, season_proj=season_proj, cfg=_cfg(0.5))
    full = vorp_board(season=2026, season_proj=season_proj, cfg=_cfg(1.0))

    assert float(half.iloc[0].adp) == 50.0
    assert float(full.iloc[0].adp) == 20.0
