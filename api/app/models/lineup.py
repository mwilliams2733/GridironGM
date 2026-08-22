"""Start/Sit optimizer: the highest-projected legal lineup for a given week.

``optimize`` fills the config's starter slots (QB/RB/WR/TE/FLEX/K/DST) to maximize
total projected points, reports bench, the delta vs a supplied current lineup,
per-player start confidence (from projection variance), and the two closest
start/sit calls.

Optimality: with position-exclusive slots and a single FLEX that accepts any
flex-eligible leftover, filling each fixed position with its top-N projected
players and then assigning FLEX to the best remaining eligible player is provably
optimal (each fixed slot is independent; FLEX can only improve by taking the best
unused eligible). The smoke block verifies this against brute-force enumeration.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..config import league_config
from . import projections as proj

# Confidence tiers from projection coefficient of variation (band/proj).
CONF_HIGH_CV = 0.35      # tighter than this => High
CONF_LOW_CV = 0.65       # wider than this => Low


@dataclass
class LineupResult:
    slots: dict[str, dict]                 # slot label -> player record
    bench: list[dict]
    total: float
    current_total: float | None = None
    delta: float | None = None
    close_calls: list[dict] = field(default_factory=list)

    def to_frame(self) -> pd.DataFrame:
        rows = [{"slot": s, **rec} for s, rec in self.slots.items()]
        return pd.DataFrame(rows)


def _confidence(rec: dict) -> str:
    proj_pts = rec.get("proj_points", 0) or 0
    band = (rec.get("ceiling", 0) - rec.get("floor", 0)) / 2
    if proj_pts <= 0:
        return "Low"
    cv = band / proj_pts
    if cv <= CONF_HIGH_CV:
        return "High"
    if cv >= CONF_LOW_CV:
        return "Low"
    return "Medium"


def slot_plan(cfg: dict) -> list[tuple[str, tuple[str, ...]]]:
    """Ordered (label, eligible_positions) for every starting slot.

    Fixed positions expand to numbered labels (RB -> RB1, RB2). Flex-type slots
    come from `flex_slot_defs`, so SUPER_FLEX needs no code change here.

    ORDER IS LOAD-BEARING. Slots are sorted by ascending eligibility breadth so
    the most restrictive is filled first. With FLEX (RB/WR/TE) and SUPER_FLEX
    (QB/RB/WR/TE) and only a WR 25 and a QB 20 left, filling SUPER_FLEX first
    takes the WR and strands FLEX with nobody eligible: 25 instead of 45.
    Because FLEX is a subset of SUPER_FLEX the eligibility family is laminar,
    and restrictive-first greedy is optimal on it. `_brute_force_best` pins that.
    """
    from .vorp import flex_slot_defs

    starters = cfg["roster"]["starters"]
    flex_defs = flex_slot_defs(cfg)

    plan: list[tuple[str, tuple[str, ...]]] = []
    for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
        n = starters.get(pos, 0)
        if n == 1:
            plan.append((pos, (pos,)))
        else:
            plan.extend((f"{pos}{i + 1}", (pos,)) for i in range(n))

    for label, d in flex_defs.items():
        n = starters.get(label, 0)
        elig = tuple(d["eligible"])
        if n == 1:
            plan.append((label, elig))
        else:
            plan.extend((f"{label}{i + 1}", elig) for i in range(n))

    plan.sort(key=lambda p: len(p[1]))
    return plan


def optimize(roster_ids: list[str], season: int, week: int,
             current_lineup: list[str] | None = None,
             week_proj: pd.DataFrame | None = None,
             cfg: dict | None = None) -> LineupResult:
    """Compute the optimal legal lineup for ``roster_ids`` in a given week."""
    cfg = cfg or league_config()

    if week_proj is None:
        week_proj = proj.project_week(season, week)
    pw = week_proj[week_proj.player_id.isin(roster_ids)].copy()
    recs = {r.player_id: {"player_id": r.player_id, "name": r["name"],
                          "position": r.position, "opponent": r.get("opponent"),
                          "proj_points": r.proj_points, "floor": r.floor,
                          "ceiling": r.ceiling} for _, r in pw.iterrows()}
    # roster players with no weekly projection (bye/out) -> 0 pts, still benchable
    for pid in roster_ids:
        recs.setdefault(pid, {"player_id": pid, "name": pid, "position": "?",
                              "opponent": None, "proj_points": 0.0,
                              "floor": 0.0, "ceiling": 0.0})

    used: set[str] = set()
    slots: dict[str, dict] = {}
    # margins captured while filling, for close-call detection
    margins: list[dict] = []

    def pick(eligible_positions, label):
        pool = sorted(
            (r for r in recs.values()
             if r["player_id"] not in used and _eligible(r, eligible_positions)),
            key=lambda r: r["proj_points"], reverse=True)
        if not pool:
            slots[label] = {"name": "(empty)", "position": "/".join(eligible_positions),
                            "proj_points": 0.0, "floor": 0.0, "ceiling": 0.0,
                            "confidence": "Low", "player_id": None}
            return
        chosen = pool[0]
        used.add(chosen["player_id"])
        rec = {**chosen, "confidence": _confidence(chosen)}
        slots[label] = rec
        # runner-up margin for this slot (start/sit closeness)
        if len(pool) > 1:
            margins.append({"slot": label, "starter": chosen["name"],
                            "alt": pool[1]["name"],
                            "margin": round(chosen["proj_points"] - pool[1]["proj_points"], 2),
                            "alt_proj": pool[1]["proj_points"]})

    def _eligible(r, eligible_positions):
        return r["position"] in eligible_positions

    # Fill slots in ascending eligibility breadth (most restrictive first) so a
    # wide slot like SUPER_FLEX never strands a narrower one like FLEX.
    for label, elig in slot_plan(cfg):
        pick(elig, label)

    total = round(sum(s["proj_points"] for s in slots.values()), 2)
    bench = [{**r, "confidence": _confidence(r)}
             for pid, r in recs.items() if pid not in used]
    bench.sort(key=lambda r: r["proj_points"], reverse=True)

    # current-vs-optimal delta
    current_total = delta = None
    if current_lineup is not None:
        current_total = round(sum(recs[p]["proj_points"] for p in current_lineup
                                  if p in recs), 2)
        delta = round(total - current_total, 2)

    # two smallest-margin decisions where a bench alt is genuinely competitive
    close = sorted((m for m in margins if m["margin"] >= 0),
                   key=lambda m: m["margin"])[:2]

    return LineupResult(slots=slots, bench=bench, total=total,
                        current_total=current_total, delta=delta, close_calls=close)


def _brute_force_best(recs: dict, cfg: dict) -> float:
    """Reference optimum by exhaustive assignment (validation only).

    Slot-generic, so it validates superflex the same way it validates the
    single-FLEX case. Exponential in slot count, which is fine for the 9-10
    slots and <=20 players a fantasy roster holds.
    """
    plan = slot_plan(cfg)
    players = list(recs.values())

    def best(slot_i: int, used: frozenset) -> float:
        if slot_i >= len(plan):
            return 0.0
        _, elig = plan[slot_i]
        options = [p for p in players
                   if p["player_id"] not in used and p["position"] in elig]
        if not options:
            return best(slot_i + 1, used)
        return max(
            p["proj_points"] + best(slot_i + 1, used | {p["player_id"]})
            for p in options
        )

    return round(best(0, frozenset()), 2)


if __name__ == "__main__":
    pd.set_option("display.width", 220)
    season, week = 2025, 10
    wk = proj.project_week(season, week)

    # Build a plausible full roster from top players who actually played that week.
    def top_ids(pos, n):
        return list(wk[wk.position == pos].head(n).player_id)

    roster = (top_ids("QB", 2) + top_ids("RB", 4) + top_ids("WR", 4)
              + top_ids("TE", 2) + top_ids("K", 1) + top_ids("DST", 1))
    # A legal but SUBOPTIMAL current lineup: start the optimal set, then bench the
    # FLEX in favor of a worse eligible bench player (demonstrates a positive delta).
    opt = optimize(roster, season, week, week_proj=wk)
    current = [r["player_id"] for r in opt.slots.values() if r["player_id"]]
    bench_rb = next((b["player_id"] for b in opt.bench if b["position"] == "RB"), None)
    if bench_rb:
        current[current.index(opt.slots["FLEX"]["player_id"])] = bench_rb
    res = optimize(roster, season, week, week_proj=wk, current_lineup=current)

    print(f"=== optimal lineup 2025 wk{week} ===")
    for slot, r in res.slots.items():
        print(f"  {slot:5} {r['name'][:22]:22} {r['position']:3} "
              f"proj {r['proj_points']:5.1f}  ({r.get('confidence')})")
    print(f"  TOTAL {res.total}")
    print(f"  current(first 9)={res.current_total}  delta={res.delta}")
    print("  close calls:")
    for c in res.close_calls:
        print(f"    {c['slot']}: {c['starter']} over {c['alt']} by {c['margin']}")
    print("  bench:", [f"{b['name']} {b['proj_points']}" for b in res.bench[:5]])

    # Verify optimality vs brute force
    cfg = league_config()
    recs = {r.player_id: {"player_id": r.player_id, "position": r.position,
                          "proj_points": r.proj_points}
            for _, r in wk[wk.player_id.isin(roster)].iterrows()}
    bf = _brute_force_best(recs, cfg)
    print(f"\n  greedy total={res.total}  brute-force optimum={bf}  match={abs(res.total-bf)<0.01}")
