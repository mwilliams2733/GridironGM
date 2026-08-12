"""Multi-league config: shared scoring/roster, per-league overrides.

These guard the invariant that makes multi-league safe — a league entry may
change team count, draft slot and ESPN id, and nothing else. Scoring drift
between leagues would silently corrupt projections for three of the four.
"""
from __future__ import annotations

import pytest

from app.config import (
    active_league_id,
    league_config,
    league_ids,
    leagues,
    resolve_league,
)
from app.models import vorp


def test_all_configured_leagues_are_listed():
    ids = league_ids()
    assert len(ids) >= 2, "multi-league support needs more than one league configured"
    assert len(ids) == len(set(ids)), "league ids must be unique — they key stored draft state"


def test_active_league_is_a_real_league():
    assert active_league_id() in league_ids()


def test_resolve_league_defaults_to_active_and_rejects_unknown():
    assert resolve_league(None) == active_league_id()
    for lid in league_ids():
        assert resolve_league(lid) == lid
    with pytest.raises(KeyError):
        resolve_league("not-a-league")


def test_team_count_comes_from_the_league_entry():
    for entry in leagues():
        cfg = league_config(entry["id"])
        assert cfg["league"]["teams"] == entry["teams"]
        assert cfg["league"]["id"] == entry["id"]


def test_scoring_and_roster_are_shared_across_leagues():
    ids = league_ids()
    base = league_config(ids[0])
    for lid in ids[1:]:
        other = league_config(lid)
        assert other["scoring"] == base["scoring"]
        assert other["roster"] == base["roster"]


def test_bare_league_config_returns_the_active_league():
    assert league_config()["league"]["id"] == active_league_id()


def test_replacement_levels_differ_between_league_sizes():
    """The reason VORP must be per-league: a smaller league has a shallower
    replacement level, so the same player is worth more."""
    sizes = {}
    for entry in leagues():
        cfg = league_config(entry["id"])
        sizes[cfg["league"]["teams"]] = vorp.replacement_levels(cfg)

    if len(sizes) < 2:
        pytest.skip("all configured leagues are the same size")

    small, large = min(sizes), max(sizes)
    for pos in ("QB", "RB", "WR"):
        assert sizes[small][pos] < sizes[large][pos], (
            f"{pos} replacement level should be shallower in a {small}-team league"
        )
