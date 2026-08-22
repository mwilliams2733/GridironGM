"""Waiver-wire ranking: value-add over your droppable worst, breakout detection,
suggested drop, and FAAB bid sizing.

``rank_free_agents`` blends rest-of-season upgrade value with next-week value,
flags breakouts from in-season usage trends (snap %, target share, red-zone proxy),
picks the weakest bench player to drop, and sizes a FAAB bid from the remaining
budget scaled by the value gap and a confidence tier.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..config import league_config
from ..db import read_df
from . import projections as proj

# Score blend: ROS upgrade dominates; next-week value is a tie-breaker/streamer bump.
ROS_WEIGHT = 1.0
NEXT_WEEK_WEIGHT = 0.5

# Breakout thresholds (in-season usage trend: last 3 games vs earlier games).
BREAKOUT_LOOKBACK = 3
SNAP_RISE = 0.12          # +12pp offense snap share
TARGET_RISE = 0.05        # +5pp target share
RZ_RISE = 1.0             # +1 combined rush+rec TD over the window
# Fallback (offseason) breakout: season projection usage_trend above this.
USAGE_TREND_FLAG = 1.05

# FAAB sizing.
FAAB_FULL_BID_GAP = 60.0  # a ROS upgrade of this many points warrants a max-share bid
FAAB_MAX_SHARE = 0.55     # never spend more than this fraction on one player
CONF_MULT = {"High": 1.0, "Medium": 0.65, "Low": 0.35}


def _starters_needed(cfg: dict) -> dict[str, int]:
    s = cfg["roster"]["starters"]
    return s


def _my_lineup_split(my_roster, ros, cfg):
    """Split my roster into projected starters vs bench, by ROS value and slots."""
    starters = cfg["roster"]["starters"]
    flex_elig = cfg["roster"]["flex_eligible"]
    ros_my = ros[ros.player_id.isin(my_roster)].copy().sort_values(
        "proj_points", ascending=False)
    used = set()
    # fill fixed slots
    for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
        need = starters.get(pos, 0)
        pool = ros_my[(ros_my.position == pos) & (~ros_my.player_id.isin(used))]
        for pid in pool.head(need).player_id:
            used.add(pid)
    # flex
    flexn = starters.get("FLEX", 0)
    fpool = ros_my[(ros_my.position.isin(flex_elig)) & (~ros_my.player_id.isin(used))]
    for pid in fpool.head(flexn).player_id:
        used.add(pid)
    bench = ros_my[~ros_my.player_id.isin(used)]
    return ros_my, used, bench


def _breakout_flags(season: int, week: int, fa_ids: list[str]) -> dict[str, list[str]]:
    """In-season usage-trend flags per free-agent id. Empty when no current-season
    data exists yet (offseason) — the caller falls back to YoY usage trend."""
    flags: dict[str, list[str]] = {pid: [] for pid in fa_ids}
    if week <= 1:
        return flags
    ws = read_df(
        "SELECT player_id, week, target_share, receptions, targets, carries, "
        "rushing_tds, receiving_tds FROM weekly_stats WHERE season=? AND week<? AND week>=?",
        (season, week, max(1, week - 8)))
    sn = read_df(
        "SELECT player_id, week, offense_pct FROM snap_counts WHERE season=? AND week<? AND week>=?",
        (season, week, max(1, week - 8)))
    if ws.empty:
        return flags
    cut = week - BREAKOUT_LOOKBACK
    for pid in fa_ids:
        pw = ws[ws.player_id == pid]
        if pw.empty:
            continue
        recent, prior = pw[pw.week >= cut], pw[pw.week < cut]
        if prior.empty or recent.empty:
            continue
        if (recent.target_share.mean() - prior.target_share.mean()) >= TARGET_RISE:
            flags[pid].append("target share rising")
        rz_recent = (recent.rushing_tds.fillna(0) + recent.receiving_tds.fillna(0)).mean()
        rz_prior = (prior.rushing_tds.fillna(0) + prior.receiving_tds.fillna(0)).mean()
        if (rz_recent - rz_prior) >= RZ_RISE / BREAKOUT_LOOKBACK:
            flags[pid].append("red-zone TD uptick")
        ps = sn[sn.player_id == pid]
        if not ps.empty:
            pr, pp = ps[ps.week >= cut], ps[ps.week < cut]
            if not pr.empty and not pp.empty and \
                    (pr.offense_pct.mean() - pp.offense_pct.mean()) >= SNAP_RISE:
                flags[pid].append("snap share rising")
    return flags


def rank_free_agents(free_agents: list[str], my_roster: list[str],
                     season: int, week: int,
                     faab_remaining: float | None = None) -> pd.DataFrame:
    """Rank available players. Columns: player_id, name, pos, ros_value,
    next_week_value, score, breakout_flags, suggested_drop, faab_bid, confidence,
    rationale."""
    cfg = league_config()
    budget = float(faab_remaining if faab_remaining is not None
                   else cfg["waivers"]["faab_budget"])

    season_proj = proj.project_season(season)
    ros = proj.project_ros(season, week, season_proj=season_proj)
    wk = proj.project_week(season, week, season_proj=season_proj)
    ros_pts = ros.set_index("player_id")["proj_points"].to_dict()
    wk_pts = wk.set_index("player_id")["proj_points"].to_dict()
    names = season_proj.set_index("player_id")[["name", "position", "team"]]

    _, _, bench = _my_lineup_split(my_roster, ros, cfg)
    # worst droppable bench player per position and overall
    worst_drop_id = bench.sort_values("proj_points").iloc[0].player_id if not bench.empty else None
    worst_drop_val = ros_pts.get(worst_drop_id, 0.0) if worst_drop_id else 0.0
    worst_name = names.loc[worst_drop_id, "name"] if worst_drop_id in names.index else None

    flags = _breakout_flags(season, week, list(free_agents))
    trend = season_proj.set_index("player_id")

    rows = []
    for pid in free_agents:
        rv = ros_pts.get(pid, 0.0)
        nv = wk_pts.get(pid, 0.0)
        if pid not in names.index:
            continue
        pos = names.loc[pid, "position"]
        upgrade = rv - worst_drop_val
        score = ROS_WEIGHT * upgrade + NEXT_WEEK_WEIGHT * nv

        fl = list(flags.get(pid, []))
        if not fl and pid in trend.index:
            comp = trend.loc[pid, "components"]
            if isinstance(comp, dict) and comp.get("usage_trend", 1.0) >= USAGE_TREND_FLAG:
                fl.append("usage trending up (YoY)")

        # confidence tier
        if upgrade >= 30 and fl:
            conf = "High"
        elif upgrade >= 12:
            conf = "Medium"
        else:
            conf = "Low"

        base_pct = float(np.clip(upgrade / FAAB_FULL_BID_GAP, 0, FAAB_MAX_SHARE))
        faab = int(round(budget * base_pct * CONF_MULT[conf]))
        if score > 0:
            faab = max(faab, 1)

        rationale = (f"ROS +{upgrade:.0f} over {worst_name or 'bench'}"
                     + (f"; nextwk {nv:.0f}" if nv else "")
                     + (f"; {', '.join(fl)}" if fl else "")
                     + f"; {conf} confidence")
        rows.append({
            "player_id": pid, "name": names.loc[pid, "name"], "pos": pos,
            "team": names.loc[pid, "team"],
            "ros_value": round(rv, 1), "next_week_value": round(nv, 1),
            "score": round(score, 1), "breakout_flags": fl,
            "suggested_drop": worst_name, "suggested_drop_id": worst_drop_id,
            "faab_bid": faab, "confidence": conf, "rationale": rationale,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("score", ascending=False).reset_index(drop=True)


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    # Validate in-season on 2025 week 10: treat mid-tier RB/WRs as "free agents".
    season, week = 2025, 10
    s = proj.project_season(season)
    ros = proj.project_ros(season, week, season_proj=s)
    # my roster: a mediocre team (ranks 40-55 by season proj) so upgrades exist
    pool = s.sort_values("proj_points", ascending=False)
    my_roster = list(pool.iloc[40:55].player_id)
    # free agents: next batch of players not on my roster
    fas = list(pool.iloc[55:120].player_id)
    out = rank_free_agents(fas, my_roster, season, week)
    print(f"=== waivers for 2025 wk10 (my roster = ranks 40-55) ===")
    print(out[["name", "pos", "ros_value", "next_week_value", "score",
               "breakout_flags", "suggested_drop", "faab_bid", "confidence"]].head(12).to_string())
