"""SUPER_FLEX slots, and the fill order that makes greedy optimal."""
from __future__ import annotations

import copy

from app.config import league_config
from app.models.lineup import _brute_force_best, optimize, slot_plan
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
