"""Lineup optimizer tests: slot legality vs config, optimizer >= random legal lineups
(property-style, 25 samples), close_calls length <= 2."""
from __future__ import annotations

import random

import pytest

from app.config import league_config
from app.models import lineup as lineup_model
from app.models import projections as proj

SEASON, WEEK = 2025, 10


@pytest.fixture(scope="module")
def week_proj():
    return proj.project_week(SEASON, WEEK)


@pytest.fixture(scope="module")
def roster(week_proj):
    if week_proj.empty:
        pytest.skip("no weekly projection data available")

    def top_ids(pos, n):
        return list(week_proj[week_proj.position == pos].head(n).player_id)

    return (top_ids("QB", 2) + top_ids("RB", 5) + top_ids("WR", 5)
            + top_ids("TE", 2) + top_ids("K", 2) + top_ids("DST", 2))


def test_lineup_legal_slot_counts(roster, week_proj):
    cfg = league_config()
    starters = cfg["roster"]["starters"]
    result = lineup_model.optimize(roster, SEASON, WEEK, week_proj=week_proj)

    counts = {}
    for slot, rec in result.slots.items():
        base_pos = "FLEX" if slot.startswith("FLEX") else "".join(c for c in slot if not c.isdigit())
        counts[base_pos] = counts.get(base_pos, 0) + 1

    for pos, n in starters.items():
        assert counts.get(pos, 0) == n


def test_flex_eligibility_honored(roster, week_proj):
    cfg = league_config()
    flex_elig = cfg["roster"]["flex_eligible"]
    result = lineup_model.optimize(roster, SEASON, WEEK, week_proj=week_proj)
    flex_slots = [s for s in result.slots if s.startswith("FLEX")]
    for slot in flex_slots:
        rec = result.slots[slot]
        if rec.get("player_id"):
            assert rec["position"] in flex_elig


def test_no_player_double_booked(roster, week_proj):
    result = lineup_model.optimize(roster, SEASON, WEEK, week_proj=week_proj)
    starter_ids = [r["player_id"] for r in result.slots.values() if r.get("player_id")]
    assert len(starter_ids) == len(set(starter_ids))


def _random_legal_lineup(roster, week_proj, cfg):
    """Build a random *legal* lineup: fill each fixed slot from eligible players,
    then FLEX from the remaining eligible pool."""
    starters = cfg["roster"]["starters"]
    flex_elig = cfg["roster"]["flex_eligible"]
    by_pos = {}
    for pid in roster:
        rows = week_proj[week_proj.player_id == pid]
        pos = rows.iloc[0].position if not rows.empty else None
        by_pos.setdefault(pos, []).append(pid)

    used = set()
    chosen = []
    for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
        pool = [p for p in by_pos.get(pos, []) if p not in used]
        random.shuffle(pool)
        take = pool[: starters.get(pos, 0)]
        used.update(take)
        chosen.extend(take)
    flex_pool = [p for p in roster if p not in used and
                 (week_proj[week_proj.player_id == p].position.iloc[0]
                  if not week_proj[week_proj.player_id == p].empty else None) in flex_elig]
    random.shuffle(flex_pool)
    take = flex_pool[: starters.get("FLEX", 0)]
    used.update(take)
    chosen.extend(take)
    return chosen


def test_optimizer_beats_or_ties_random_legal_lineups(roster, week_proj):
    cfg = league_config()
    result = lineup_model.optimize(roster, SEASON, WEEK, week_proj=week_proj)
    pts = week_proj.set_index("player_id")["proj_points"].to_dict()

    for _ in range(25):
        rand_lineup = _random_legal_lineup(roster, week_proj, cfg)
        rand_total = sum(pts.get(p, 0.0) for p in rand_lineup)
        assert result.total >= rand_total - 1e-6


def test_close_calls_length_at_most_two(roster, week_proj):
    result = lineup_model.optimize(roster, SEASON, WEEK, week_proj=week_proj)
    assert len(result.close_calls) <= 2
