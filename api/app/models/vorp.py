"""Value Over Replacement (VORP) + draft board.

``replacement_levels`` derives the "replacement" player rank per position from the
league's real starter+flex demand (never hard-coded). ``vorp_board`` ranks the
undrafted pool by value over that replacement level, adds gap-based tiers, ADP
context, and a roster-need score for the drafting team.

ADP rows (FantasyFootballCalculator) carry names without suffixes and DSTs as team
names, so a fuzzy resolver (``resolve_adp``) maps them to ``players.player_id``.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

import numpy as np
import pandas as pd

from ..config import league_config
from ..db import read_df
from . import projections as proj

# Gap-based tiering: a new tier starts when the drop to the next player exceeds
# TIER_GAP_MULT * the median within-position gap (position-relative, so it adapts).
TIER_GAP_MULT = 1.8
# Roster-need weights (see docs/MODELING.md).
NEED_SCARCITY_W = 1.0     # positional VORP scarcity of the best available at pos
NEED_UNFILLED_W = 6.0     # points per still-unfilled starter slot at the position
NEED_BYE_PENALTY = 4.0    # penalty when a pick stacks an existing starter's bye week


# ---------------------------------------------------------------------------
# Replacement levels
# ---------------------------------------------------------------------------
# Historical share of flex usage by position, used when a league declares
# `flex_eligible` but not the richer `flex_slots` block. RB/WR carry flex far
# more often than TE.
LEGACY_FLEX_SHARE = {"RB": 0.45, "WR": 0.45, "TE": 0.10}


def flex_slot_defs(cfg: dict) -> dict[str, dict]:
    """Flex-type slots as {label: {"eligible": [...], "share": {pos: fraction}}}.

    A league that declares `roster.flex_slots` is taken at its word. One that
    does not gets the legacy single FLEX synthesised from `flex_eligible`, so
    configs written before superflex existed keep their exact behaviour.
    """
    roster = cfg["roster"]
    declared = roster.get("flex_slots")
    if declared:
        return {label: {"eligible": list(d["eligible"]), "share": dict(d["share"])}
                for label, d in declared.items()}
    elig = roster.get("flex_eligible", ["RB", "WR", "TE"])
    share = {pos: LEGACY_FLEX_SHARE.get(pos, 1 / len(elig)) for pos in elig}
    return {"FLEX": {"eligible": list(elig), "share": share}}


def replacement_levels(cfg: dict | None = None) -> dict[str, int]:
    """Replacement RANK per position = how many of that position the league is
    expected to roster as startable. Derived from fixed starters plus each
    flex-type slot's demand, spread over its eligible positions.

    Example (12-team, 1QB/2RB/2WR/1TE/1FLEX/1K/1DST): QB12, TE12, K12, DST12,
    and RB/WR each get 2*12 starters plus their share of the flex pool.

    With a SUPER_FLEX slot the QB line moves sharply: 12 fixed QB starters plus
    ~0.90 of 12 superflex slots puts replacement near QB23 rather than QB12.
    """
    cfg = cfg or league_config()
    teams = int(cfg["league"]["teams"])
    starters = cfg["roster"]["starters"]

    levels: dict[str, float] = {}
    for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
        levels[pos] = starters.get(pos, 0) * teams

    for label, d in flex_slot_defs(cfg).items():
        slots = starters.get(label, 0) * teams
        if not slots:
            continue
        for pos in d["eligible"]:
            levels[pos] = levels.get(pos, 0) + slots * d["share"].get(pos, 0.0)

    return {pos: int(round(rank)) for pos, rank in levels.items()}


def replacement_points(season_proj: pd.DataFrame, cfg: dict | None = None) -> dict[str, float]:
    """Projected points of the replacement-level player at each position (the value
    of the Nth-ranked player where N = replacement rank)."""
    levels = replacement_levels(cfg)
    out: dict[str, float] = {}
    for pos, rank in levels.items():
        pool = season_proj[season_proj.position == pos].sort_values(
            "proj_points", ascending=False)
        if pool.empty:
            out[pos] = 0.0
        else:
            idx = min(max(rank - 1, 0), len(pool) - 1)
            out[pos] = float(pool.iloc[idx].proj_points)
    return out


# ---------------------------------------------------------------------------
# ADP fuzzy name resolver
# ---------------------------------------------------------------------------
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def _norm(name: str) -> str:
    """Normalize a name: strip accents, punctuation, suffixes; lowercase."""
    n = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode()
    n = re.sub(r"[.'`]", "", n.lower())
    n = re.sub(r"[^a-z\s]", " ", n)
    toks = [t for t in n.split() if t not in _SUFFIXES]
    return " ".join(toks)


@lru_cache(maxsize=1)
def _players_indexed() -> pd.DataFrame:
    p = read_df("SELECT player_id, name, position, team FROM players")
    p["norm"] = p["name"].map(_norm)
    return p


def resolve_adp(season: int, cfg: dict | None = None) -> pd.DataFrame:
    """Map every ADP row to a player_id. DSTs -> synthetic ``DST_<team>`` id; PK
    (kicker) and offensive players fuzzy-matched to the players table by
    normalized name, disambiguated by position and (when needed) team.

    Scoped to the league's own scoring format: a full-PPR league reading
    half-PPR ADP gets a market that disagrees on 120 of 228 players. See
    ``etl/adp.py`` for the measurement.

    Returns the adp frame with added columns: player_id, match (exact|team|pos|none).
    """
    from ..etl.adp import adp_format

    adp = read_df(
        "SELECT player_name, position, team, adp, adp_formatted FROM adp WHERE format=?",
        (adp_format(cfg),))
    players = _players_indexed()
    # candidate pool: players with recent projection-relevant history (name reuse
    # across eras is common, so prefer players who actually appear in weekly_stats).
    active_ids = set(read_df(
        "SELECT DISTINCT player_id FROM weekly_stats WHERE season>=?",
        (season - 3,))["player_id"])

    results = []
    for _, r in adp.iterrows():
        pos = r.position
        team = proj.norm_team(r.team)
        if pos == "DST":
            results.append({**r.to_dict(), "team": team,
                            "player_id": f"DST_{team}", "match": "dst"})
            continue
        want_pos = "K" if pos == "PK" else pos
        key = _norm(r.player_name)
        cand = players[(players.norm == key) & (players.position == want_pos)]
        match = "exact"
        if len(cand) > 1:
            # disambiguate by team, then by activity
            by_team = cand[cand.team.map(proj.norm_team) == team]
            if not by_team.empty:
                cand, match = by_team, "team"
            else:
                act = cand[cand.player_id.isin(active_ids)]
                cand = act if not act.empty else cand
                match = "pos"
        elif cand.empty:
            # last-ditch: match on normalized name ignoring position
            cand = players[players.norm == key]
            match = "loose" if not cand.empty else "none"
        pid = cand.iloc[0].player_id if not cand.empty else None
        results.append({**r.to_dict(), "player_id": pid, "match": match})
    return pd.DataFrame(results)


# ---------------------------------------------------------------------------
# Tiers
# ---------------------------------------------------------------------------
def assign_tiers(df_pos: pd.DataFrame) -> pd.Series:
    """Gap-based tiers within a single position (df sorted by proj desc).
    A tier break occurs where the point drop to the next player exceeds
    TIER_GAP_MULT * median gap."""
    vals = df_pos["proj_points"].to_numpy()
    if len(vals) <= 1:
        return pd.Series([1] * len(vals), index=df_pos.index)
    gaps = -np.diff(vals)                      # positive drops
    med = np.median(gaps[gaps > 0]) if np.any(gaps > 0) else 0.0
    thresh = TIER_GAP_MULT * med
    tiers = [1]
    t = 1
    for g in gaps:
        if med > 0 and g > thresh:
            t += 1
        tiers.append(t)
    return pd.Series(tiers, index=df_pos.index)


# ---------------------------------------------------------------------------
# Draft board
# ---------------------------------------------------------------------------
def depth_map() -> dict[str, int]:
    """player_id -> current depth chart rank (1 = starter at his position group).

    Empty when depth charts have never been synced; every caller treats a missing
    rank as unknown rather than as a demotion.
    """
    try:
        df = read_df("SELECT player_id, depth_rank FROM depth_charts")
    except Exception:
        return {}
    if df.empty:
        return {}
    return df.dropna(subset=["depth_rank"]).set_index("player_id")["depth_rank"].astype(int).to_dict()


def _bye_map(season: int) -> dict[str, int]:
    return proj._bye_weeks(season)


def vorp_board(drafted_ids: set[str] | None = None,
               my_roster: list[str] | None = None,
               season: int | None = None,
               season_proj: pd.DataFrame | None = None,
               pick_number: int | None = None,
               cfg: dict | None = None) -> pd.DataFrame:
    """Draft board of undrafted players ranked by VORP with tiers, ADP context and
    roster-need score for the drafting team.

    Pass `cfg` to build the board for a specific league — replacement level is
    ``starters × teams``, so a 10-team and a 12-team league rank the same player
    differently. Defaults to the active league.

    Columns: player_id, name, pos, team, proj, vorp, tier, bye, adp, adp_delta,
    need_score, rationale.
    """
    drafted_ids = set(drafted_ids or set())
    my_roster = my_roster or []
    cfg = cfg or league_config()
    season = season or int(cfg["league"]["season"])
    if season_proj is None:
        season_proj = proj.project_season(season, cfg=cfg)

    repl = replacement_points(season_proj, cfg)
    adp = resolve_adp(season, cfg)
    adp_by_id = adp.dropna(subset=["player_id"]).set_index("player_id")["adp"].to_dict()
    byes = _bye_map(season)

    # my roster composition (positions filled + bye weeks used)
    proj_by_id = season_proj.set_index("player_id")
    my_pos_count: dict[str, int] = {}
    my_byes: list[int] = []
    for pid in my_roster:
        if pid in proj_by_id.index:
            pos = proj_by_id.loc[pid, "position"]
            my_pos_count[pos] = my_pos_count.get(pos, 0) + 1
            team = proj_by_id.loc[pid, "team"]
            if team in byes:
                my_byes.append(byes[team])

    starters = cfg["roster"]["starters"]

    board = season_proj[~season_proj.player_id.isin(drafted_ids)
                        & ~season_proj.player_id.isin(my_roster)].copy()
    board["vorp"] = board.apply(
        lambda r: round(r.proj_points - repl.get(r.position, 0.0), 1), axis=1)

    # tiers per position
    board["tier"] = 0
    for pos, sub in board.groupby("position"):
        sub = sub.sort_values("proj_points", ascending=False)
        board.loc[sub.index, "tier"] = assign_tiers(sub)

    board["bye"] = board.team.map(byes).astype("Int64")
    board["adp"] = board.player_id.map(adp_by_id)
    # Current depth chart rank — the preseason signal that actually moves value.
    # Left as NA when unknown so the UI can distinguish "not synced" from "buried".
    board["depth_rank"] = board.player_id.map(depth_map()).astype("Int64")
    # adp_delta = how many picks past his ADP a player is still available.
    # Positive => he has FALLEN (market says he should be gone; he is a value here).
    # Negative => taking him now is a REACH relative to the market.
    ctx = pick_number if pick_number is not None else (len(drafted_ids) + 1)
    board["adp_delta"] = board["adp"].apply(
        lambda a: round(ctx - a, 1) if a == a else np.nan)

    # Unfilled starter slots per position, given what I've already drafted.
    def _unfilled(pos: str) -> int:
        # Flex demand comes from `flex_slot_defs` -- the same source
        # `replacement_levels` and `slot_plan` use -- not a hardcoded "FLEX"
        # against `flex_eligible`. A superflex league's second QB starter is
        # invisible to the hardcoded form, so the board reports a starting QB
        # as surplus.
        need_starters = starters.get(pos, 0)
        for label, d in flex_slot_defs(cfg).items():
            if pos in d["eligible"]:
                need_starters += starters.get(label, 0)
        return max(0, need_starters - my_pos_count.get(pos, 0))

    board["unfilled"] = board.position.map(_unfilled)

    # need score per player
    def need(r) -> float:
        score = NEED_UNFILLED_W * r.unfilled + NEED_SCARCITY_W * max(r.vorp, 0) / 10.0
        # bye stacking penalty
        if not pd.isna(r.bye) and int(r.bye) in my_byes:
            score -= NEED_BYE_PENALTY
        return round(score, 1)

    board["need_score"] = board.apply(need, axis=1)

    def rationale(r) -> str:
        bits = [f"{r.pos} #{int(r.tier)} tier", f"VORP {r.vorp:+.0f}"]
        # Only worth calling out when he is NOT the starter — "RB1" is the
        # expected case and would be noise on every line.
        if not pd.isna(r.depth_rank) and int(r.depth_rank) > 1:
            bits.append(f"{r.pos}{int(r.depth_rank)} on depth chart")
        if r.adp == r.adp:
            if r.adp_delta == r.adp_delta and r.adp_delta > 8:
                bits.append(f"falling (ADP {r.adp:.0f}, pick {ctx})")
            elif r.adp_delta == r.adp_delta and r.adp_delta < -8:
                bits.append(f"reach (ADP {r.adp:.0f}, pick {ctx})")
            else:
                bits.append(f"ADP {r.adp:.0f}")
        if not pd.isna(r.bye) and int(r.bye) in my_byes:
            bits.append(f"bye {int(r.bye)} stacks")
        if r.unfilled > 0:
            bits.append("fills starter need")
        return "; ".join(bits)

    board = board.rename(columns={"proj_points": "proj", "position": "pos"})
    board["rationale"] = board.apply(rationale, axis=1)
    board = board.sort_values(["vorp"], ascending=False).reset_index(drop=True)
    cols = ["player_id", "name", "pos", "team", "proj", "vorp", "tier",
            "bye", "adp", "adp_delta", "depth_rank", "need_score", "rationale"]
    return board[cols]


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    cfg = league_config()
    season = int(cfg["league"]["season"])

    print("=== replacement_levels ===")
    print(replacement_levels(cfg))
    s = proj.project_season(season)
    print("\n=== replacement_points ===")
    print({k: round(v, 1) for k, v in replacement_points(s, cfg).items()})

    print("\n=== ADP resolver match rate ===")
    adp = resolve_adp(season)
    n = len(adp)
    matched = adp.player_id.notna().sum()
    print(f"{matched}/{n} matched ({matched/n:.0%})")
    print(adp.match.value_counts().to_dict())
    unm = adp[adp.player_id.isna()]
    if not unm.empty:
        print("UNMATCHED:", list(unm.player_name))

    print("\n=== vorp_board top 20 (empty draft) ===")
    b = vorp_board(season=season, season_proj=s)
    print(b.head(20).to_string())

    print("\n=== vorp_board with a roster (2 RB drafted by me) ===")
    my = list(s[s.position == "RB"].head(2).player_id)
    b2 = vorp_board(my_roster=my, season=season, season_proj=s, pick_number=25)
    print(b2[["name", "pos", "vorp", "tier", "bye", "adp", "adp_delta", "need_score", "rationale"]].head(10).to_string())
