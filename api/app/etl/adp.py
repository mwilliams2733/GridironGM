"""ADP ingest from FantasyFootballCalculator's free API (half-PPR, 12-team)."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pandas as pd

from ..config import league_config
from ..db import connect, mark_synced, replace_table

FFC_URL = "https://fantasyfootballcalculator.com/api/v1/adp/half-ppr"


def sync_adp() -> int:
    cfg = league_config()["league"]
    resp = httpx.get(FFC_URL, params={"teams": cfg["teams"], "year": cfg["season"]}, timeout=30)
    resp.raise_for_status()
    players = resp.json().get("players", [])
    if not players:
        mark_synced("adp", "0 rows")
        return 0
    now = datetime.now(timezone.utc).isoformat()
    df = pd.DataFrame([
        {
            "player_name": p["name"],
            "position": "DST" if p["position"] == "DEF" else p["position"],
            "team": p.get("team"),
            "adp": p.get("adp"),
            "adp_formatted": p.get("adp_formatted"),
            "fetched_at": now,
        }
        for p in players
    ])
    with connect() as conn:
        n = replace_table(df, "adp", conn)
    mark_synced("adp", f"{n} rows")
    return n
