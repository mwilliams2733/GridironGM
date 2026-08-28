"""ADP ingest from FantasyFootballCalculator's free API, keyed by scoring format.

ADP is stored per SCORING FORMAT, not per league and not per team count.

Measured against the live API on 2026-08-27:

* The `teams` parameter is a **no-op**. `teams=10` and `teams=12` return
  byte-identical player sets AND identical ADP values, and the response does
  not echo the parameter back. It is therefore not sent — re-adding it would
  imply a distinction the endpoint does not make.
* The scoring **format** is what genuinely differs. half-PPR and full-PPR
  disagree on 120 of 228 common players by >=3 picks (largest swing 40.9
  picks), and full-PPR lists 266 players against half-PPR's 228 — 38 players,
  including a team defense and several kickers, that half-PPR omits entirely.

So a full-PPR league reading half-PPR ADP gets the wrong market. Leagues that
share a format share one fetch: five configured leagues resolve to two.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
import pandas as pd

from ..config import league_config, league_ids
from ..db import connect, mark_synced, replace_table

log = logging.getLogger(__name__)

FFC_BASE = "https://fantasyfootballcalculator.com/api/v1/adp"

# Per-reception value -> FantasyFootballCalculator endpoint name. Anything not
# listed raises rather than falling back: picking a "nearest" format for, say,
# a 0.75-PPR league would hand it the wrong market silently, which is the exact
# failure this module exists to fix.
FFC_FORMATS = {0.0: "standard", 0.5: "half-ppr", 1.0: "ppr"}


def adp_format(cfg: dict | None = None) -> str:
    """The FFC endpoint matching a league's per-reception value."""
    reception = float((cfg or league_config())["scoring"]["receiving"]["reception"])
    try:
        return FFC_FORMATS[reception]
    except KeyError:
        raise ValueError(
            f"no FantasyFootballCalculator ADP format for reception={reception!r}; "
            f"known values are {sorted(FFC_FORMATS)}"
        ) from None


def configured_formats() -> dict[str, list[str]]:
    """format -> the league ids that use it, across every configured league."""
    out: dict[str, list[str]] = {}
    for lid in league_ids():
        out.setdefault(adp_format(league_config(lid)), []).append(lid)
    return out


def fetch_adp(fmt: str, season: int) -> list[dict]:
    """One format's ADP board. `teams` is deliberately not sent — see module docstring."""
    resp = httpx.get(f"{FFC_BASE}/{fmt}", params={"year": season}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("players", [])


def _rows(players: list[dict], fmt: str, now: str) -> list[dict]:
    return [
        {
            "format": fmt,
            "player_name": p["name"],
            "position": "DST" if p["position"] == "DEF" else p["position"],
            "team": p.get("team"),
            "adp": p.get("adp"),
            "adp_formatted": p.get("adp_formatted"),
            "fetched_at": now,
        }
        for p in players
    ]


def sync_adp() -> int:
    """Fetch every distinct format across configured leagues into one table."""
    season = int(league_config()["league"]["season"])
    now = datetime.now(timezone.utc).isoformat()

    rows: list[dict] = []
    detail: list[str] = []
    for fmt, lids in configured_formats().items():
        players = fetch_adp(fmt, season)
        if not players:
            log.warning("no ADP rows for format %s (season %s)", fmt, season)
        rows.extend(_rows(players, fmt, now))
        detail.append(f"{fmt}:{len(players)} ({','.join(lids)})")

    if not rows:
        mark_synced("adp", "0 rows")
        return 0

    with connect() as conn:
        n = replace_table(pd.DataFrame(rows), "adp", conn)
    mark_synced("adp", f"{n} rows — {'; '.join(detail)}")
    return n
