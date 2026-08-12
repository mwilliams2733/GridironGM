"""Mock draft simulation — how a draft plays out if the room drafts to ADP.

The point is to watch the board fall, so opponents pick by ADP rather than by any
model of their own. Two things keep it from being a straight ADP readout:

* **Noise.** Real rooms do not pick in ADP order. Each pick draws from the
  available pool with gaussian jitter on ADP, so runs and slides happen.
* **Roster sanity.** Nobody drafts three quarterbacks in round 6, and kickers go
  at the end. Without these the tail of a mock is nonsense, because ADP alone
  says nothing about what a team already has.

ADP runs out before the draft does — 212 ranked players against 192 picks in a
12-team, 16-round league — so once a team is choosing among unranked players it
falls back to VORP, which is the same ordering the real board uses.

Seedable, so a mock is reproducible and the tests are deterministic.
"""
from __future__ import annotations

import random

import pandas as pd

# Gaussian jitter added to a player's ADP before sorting, in picks. ~24 at the
# default setting, so a mid-round player can move roughly a round in either
# direction — enough for realistic runs without scrambling round 1.
RANDOMNESS_SIGMA = {"chalk": 2.0, "realistic": 8.0, "chaotic": 20.0}
DEFAULT_RANDOMNESS = "realistic"

# How deep a team will go at a position before it stops being plausible.
POSITION_CAPS = {"QB": 2, "TE": 2, "K": 1, "DST": 1}
# Kickers and defenses only become plausible inside the last N rounds.
LATE_ONLY = {"K", "DST"}
LATE_ROUND_WINDOW = 2


def _eligible(pos: str, counts: dict[str, int], rnd: int, total_rounds: int) -> bool:
    """Would a real team consider this position right now?"""
    if pos in LATE_ONLY and rnd < max(1, total_rounds - LATE_ROUND_WINDOW + 1):
        return False
    cap = POSITION_CAPS.get(pos)
    if cap is not None and counts.get(pos, 0) >= cap:
        return False
    return True


def choose_pick(
    board: pd.DataFrame,
    roster_counts: dict[str, int],
    rnd: int,
    total_rounds: int,
    rng: random.Random,
    randomness: str = DEFAULT_RANDOMNESS,
) -> pd.Series | None:
    """Pick one player for the team on the clock.

    `board` is the undrafted pool as produced by `vorp_board` (columns include
    pos, adp, vorp). Returns the chosen row, or None if the pool is empty.
    """
    if board.empty:
        return None

    sigma = RANDOMNESS_SIGMA.get(randomness, RANDOMNESS_SIGMA[DEFAULT_RANDOMNESS])

    pool = board[board["pos"].map(lambda p: _eligible(p, roster_counts, rnd, total_rounds))]
    if pool.empty:
        # Every position is capped (deep benches late) — relax the caps rather
        # than stall the draft, but keep K/DST out until their window.
        pool = board[board["pos"].map(lambda p: _eligible(p, {}, rnd, total_rounds))]
    if pool.empty:
        pool = board

    ranked = pool[pool["adp"].notna()]
    if not ranked.empty:
        jittered = ranked["adp"].astype(float) + [rng.gauss(0, sigma) for _ in range(len(ranked))]
        return ranked.loc[jittered.idxmin()]

    # Past the end of the ADP list: fall back to raw value, with the same jitter
    # expressed in VORP terms so late rounds are not perfectly deterministic.
    unranked = pool[pool["vorp"].notna()]
    if unranked.empty:
        return pool.iloc[0]
    jittered = unranked["vorp"].astype(float) + [rng.gauss(0, sigma / 2) for _ in range(len(unranked))]
    return unranked.loc[jittered.idxmax()]
