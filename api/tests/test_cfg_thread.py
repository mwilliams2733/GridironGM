"""The `cfg` thread: a league's config must reach the PROJECTION call.

The whole branch exists to make one non-active league (Sundt: full-PPR,
superflex, playoffs week 15) correct. Every other superflex/ROS test passes
`week_proj=`/`season_proj=` explicitly, which is exactly why two surfaces
shipped building their own projections under the ACTIVE league's config --
Sundt slot plans filled with half-PPR numbers, Sundt waiver rankings run to
week 18. These are the end-to-end assertions the suite structurally lacked.
"""
from __future__ import annotations

import pandas as pd

from app.config import league_config
from app.models import lineup as lineup_model
from app.models import projections as proj
from app.models import waivers as waivers_model
from app.models.projections import last_scoring_week

SUNDT = "sundt"


def _empty_week_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["player_id", "name", "position", "team",
                                 "opponent", "proj_points", "floor", "ceiling"])


def _empty_season_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["player_id", "name", "position", "team",
                                 "proj_points", "components"])


def test_sundt_cfg_reaches_project_week_from_the_lineup_optimizer(monkeypatch):
    """`optimize` built its own weekly projections with no cfg: the slot plan
    was Sundt's (SUPER_FLEX and all) while every number in it was the active
    league's half-PPR, -2-INT projection."""
    seen = {}

    def spy(season, week, store=False, season_proj=None, cfg=None):
        seen["cfg"] = cfg
        return _empty_week_frame()

    monkeypatch.setattr(proj, "project_week", spy)
    cfg = league_config(SUNDT)
    lineup_model.optimize([], 2025, 10, cfg=cfg)

    assert seen["cfg"] is cfg
    assert seen["cfg"]["scoring"]["receiving"]["reception"] == 1.0


def test_sundt_cfg_reaches_every_projection_call_from_waiver_ranking(monkeypatch):
    """`rank_free_agents` called `league_config()` (the ACTIVE league) and passed
    no cfg onward, so the Sundt pool was scored half-PPR against an uncapped
    week-18 ROS horizon -- the exact opposite of what spec 4.9 is for."""
    seen = {}

    def spy_season(season, store=False, cfg=None):
        seen["season"] = cfg
        return _empty_season_frame()

    def spy_ros(season, week, store=False, season_proj=None, cfg=None):
        seen["ros"] = cfg
        return pd.DataFrame(columns=["player_id", "position", "proj_points"])

    def spy_week(season, week, store=False, season_proj=None, cfg=None):
        seen["week"] = cfg
        return pd.DataFrame(columns=["player_id", "proj_points"])

    monkeypatch.setattr(proj, "project_season", spy_season)
    monkeypatch.setattr(proj, "project_ros", spy_ros)
    monkeypatch.setattr(proj, "project_week", spy_week)

    cfg = league_config(SUNDT)
    waivers_model.rank_free_agents([], [], 2025, 10, cfg=cfg)

    assert seen["season"] is cfg and seen["ros"] is cfg and seen["week"] is cfg
    # ...and that the cfg which arrived is the one with the shorter horizon.
    assert last_scoring_week(seen["ros"]) == 14


def test_the_waivers_router_hands_the_requested_leagues_cfg_to_the_ranker(monkeypatch):
    """Router-level half of the same thread: /waivers/rankings?league_id=sundt
    must rank under Sundt's config, not the active league's."""
    from app.routers import waivers as waivers_router

    seen = {}

    def spy_rank(fa_ids, my_roster, season, week, faab_remaining=None, cfg=None):
        seen["cfg"] = cfg
        return pd.DataFrame()

    monkeypatch.setattr(waivers_router, "_my_roster_ids", lambda lid=None: ["x"])
    monkeypatch.setattr(waivers_router, "_free_agent_ids",
                        lambda roster, season, lid=None: (["y"], None))
    monkeypatch.setattr(waivers_router, "_faab_remaining", lambda lid: 100.0)
    monkeypatch.setattr(waivers_router.waivers_model, "rank_free_agents", spy_rank)

    waivers_router.get_rankings(week=10, league_id=SUNDT)

    assert seen["cfg"] is not None
    assert seen["cfg"]["league"]["id"] == SUNDT
    assert last_scoring_week(seen["cfg"]) == 14


def test_the_active_league_is_unchanged_by_the_new_cfg_parameter():
    """The gate: an ESPN league still gets the full regular season."""
    assert last_scoring_week(league_config("league1")) == 18
