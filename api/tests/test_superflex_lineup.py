"""SUPER_FLEX slots, and the fill order that makes greedy optimal."""
from __future__ import annotations

import copy

from app.config import league_config
from app.models.lineup import _brute_force_best, optimize, slot_plan
from app.routers.draft import _slot_suggestions
from tests.test_flex_slots import SUNDT_ROSTER


def _sundt_cfg():
    cfg = copy.deepcopy(league_config())
    cfg["league"]["teams"] = 12
    cfg["roster"] = copy.deepcopy(SUNDT_ROSTER)
    return cfg


def _rec(pid, pos, pts):
    return {"player_id": pid, "name": pid, "position": pos, "opponent": "OPP",
            "proj_points": pts, "floor": pts * 0.8, "ceiling": pts * 1.2}


def test_espn_config_produces_nine_slots():
    plan = slot_plan(league_config())
    assert len(plan) == 9
    assert [lbl for lbl, _ in plan][:3] == ["QB", "RB1", "RB2"]


def test_sundt_config_produces_ten_slots_including_super_flex():
    labels = [lbl for lbl, _ in slot_plan(_sundt_cfg())]
    assert len(labels) == 10
    assert "SUPER_FLEX" in labels


def test_slots_are_ordered_by_ascending_eligibility_breadth():
    """Fill order is load-bearing, not cosmetic — see the next test."""
    plan = slot_plan(_sundt_cfg())
    widths = [len(elig) for _, elig in plan]
    assert widths == sorted(widths)
    assert plan[-1][0] == "SUPER_FLEX"


def test_flex_is_filled_before_super_flex():
    """The counterexample from the spec.

    Two slots left, a WR worth 25 and a QB worth 20. Filling SUPER_FLEX first
    takes the WR and strands FLEX with nobody eligible: 25 instead of 45.
    """
    cfg = _sundt_cfg()
    cfg["roster"]["starters"] = {"FLEX": 1, "SUPER_FLEX": 1}
    recs = {"wr": _rec("wr", "WR", 25.0), "qb": _rec("qb", "QB", 20.0)}
    assert _brute_force_best(recs, cfg) == 45.0

    res = optimize(["wr", "qb"], 2026, 1, cfg=cfg,
                   week_proj=_frame_from(recs))
    assert res.total == 45.0


def test_greedy_matches_brute_force_on_a_full_superflex_roster():
    cfg = _sundt_cfg()
    recs = {}
    for i, (pos, pts) in enumerate([
        ("QB", 22.0), ("QB", 18.5), ("RB", 19.0), ("RB", 14.0), ("RB", 11.0),
        ("WR", 20.5), ("WR", 17.0), ("WR", 12.5), ("TE", 10.0), ("TE", 7.5),
        ("K", 8.0), ("DST", 6.0),
    ]):
        recs[f"p{i}"] = _rec(f"p{i}", pos, pts)
    res = optimize(list(recs), 2026, 1, cfg=cfg, week_proj=_frame_from(recs))
    assert res.total == _brute_force_best(recs, cfg)


def test_the_second_qb_lands_in_super_flex():
    cfg = _sundt_cfg()
    recs = {}
    for i, (pos, pts) in enumerate([
        ("QB", 22.0), ("QB", 18.5), ("RB", 19.0), ("RB", 14.0),
        ("WR", 20.5), ("WR", 17.0), ("TE", 10.0), ("K", 8.0), ("DST", 6.0),
        ("RB", 9.0),
    ]):
        recs[f"p{i}"] = _rec(f"p{i}", pos, pts)
    res = optimize(list(recs), 2026, 1, cfg=cfg, week_proj=_frame_from(recs))
    assert res.slots["SUPER_FLEX"]["position"] == "QB"


def _frame_from(recs):
    import pandas as pd
    return pd.DataFrame([{**r, "name": r["player_id"]} for r in recs.values()])


def test_superflex_filled_by_second_qb():
    """Regression test: before the fix, SUPER_FLEX read as 1 (unfilled) here
    because the old loop compared it against filled.get("SUPER_FLEX"), a
    position no player has, instead of drawing from the flex-eligible pool.
    """
    cfg = _sundt_cfg()
    filled = {"QB": 2, "RB": 3, "WR": 3, "TE": 1, "K": 1, "DST": 1}
    result = _slot_suggestions(filled, cfg)
    assert result["SUPER_FLEX"] == 0
    assert all(v == 0 for v in result.values())


def test_superflex_unfilled_with_one_qb():
    cfg = _sundt_cfg()
    filled = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DST": 1}
    result = _slot_suggestions(filled, cfg)
    assert result["SUPER_FLEX"] == 1
    assert result["FLEX"] == 1


def test_flex_and_superflex_do_not_double_count_one_surplus():
    """Exactly one surplus flex-eligible player (a second QB, eligible only
    for SUPER_FLEX): it can satisfy at most one flex slot, so the two needs
    must sum to 1, not 0 or 2.

    A naive fix that computes each flex slot's surplus independently (no
    shared pool) would double-count here in the other direction: FLEX has no
    RB/WR/TE surplus of its own (need 1), and the old hardcoded branch never
    lets SUPER_FLEX see the QB surplus either (stuck at its full need, 1),
    so the untouched surplus QB is invisible to both and the sum comes out
    as 2, not 1.
    """
    cfg = _sundt_cfg()
    filled = {"QB": 2, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DST": 1}
    result = _slot_suggestions(filled, cfg)
    assert result["FLEX"] + result["SUPER_FLEX"] == 1


def test_single_flex_league_unchanged():
    cfg = league_config()
    filled = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "K": 1, "DST": 1}
    result = _slot_suggestions(filled, cfg)
    assert result == {"QB": 0, "RB": 0, "WR": 0, "TE": 0, "FLEX": 0, "K": 0, "DST": 0}


def test_empty_roster_returns_full_starter_counts():
    cfg = _sundt_cfg()
    result = _slot_suggestions({}, cfg)
    assert result == cfg["roster"]["starters"]
