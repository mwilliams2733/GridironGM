"""The Odds API client: NFL spreads, totals, moneylines → implied team totals.

Key resolves via app.config.get_secret("ODDS_API_KEY") — env var first, then
the user's shared.env (stored there as `TheODDSAPI`). Never logged or persisted.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
import pandas as pd

from ..config import get_secret
from ..db import connect, mark_synced, upsert_rows

log = logging.getLogger(__name__)

BASE = "https://api.the-odds-api.com/v4"
SPORT = "americanfootball_nfl"

# Odds books use full team names; nflverse uses abbreviations.
TEAM_ABBR = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF", "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC", "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LA", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF", "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}


class OddsAPIError(RuntimeError):
    pass


def _api_key() -> str:
    key = get_secret("ODDS_API_KEY")
    if not key:
        raise OddsAPIError(
            "No Odds API key found. Set ODDS_API_KEY or add `TheODDSAPI=` to "
            r"C:\Users\mwill\.secrets\shared.env"
        )
    return key


def fetch_game_odds() -> list[dict]:
    """Fetch upcoming NFL game odds (h2h, spreads, totals) from a US book consensus."""
    params = {
        "apiKey": _api_key(),
        "regions": "us",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "american",
    }
    resp = httpx.get(f"{BASE}/sports/{SPORT}/odds", params=params, timeout=30)
    if resp.status_code == 401:
        raise OddsAPIError("Odds API rejected the key (401).")
    resp.raise_for_status()
    remaining = resp.headers.get("x-requests-remaining")
    if remaining is not None:
        log.info("Odds API requests remaining: %s", remaining)
    return resp.json()


def implied_totals(total: float | None, spread_home: float | None) -> tuple[float | None, float | None]:
    """Implied points: home = total/2 - spread_home/2 (spread_home negative when favored)."""
    if total is None or spread_home is None:
        return None, None
    home = total / 2 - spread_home / 2
    return round(home, 1), round(total - home, 1)


def _consensus(events: list[dict]) -> pd.DataFrame:
    rows = []
    for ev in events:
        home, away = ev["home_team"], ev["away_team"]
        spreads, totals, ml_home, ml_away = [], [], [], []
        for book in ev.get("bookmakers", []):
            for market in book.get("markets", []):
                outcomes = market.get("outcomes", [])
                if market["key"] == "spreads":
                    for o in outcomes:
                        if o["name"] == home and o.get("point") is not None:
                            spreads.append(o["point"])
                elif market["key"] == "totals":
                    for o in outcomes:
                        if o["name"] == "Over" and o.get("point") is not None:
                            totals.append(o["point"])
                elif market["key"] == "h2h":
                    for o in outcomes:
                        if o["name"] == home:
                            ml_home.append(o["price"])
                        elif o["name"] == away:
                            ml_away.append(o["price"])
        mean = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
        spread_home, total = mean(spreads), mean(totals)
        imp_home, imp_away = implied_totals(total, spread_home)
        rows.append({
            "event_id": ev["id"],
            "commence_time": ev["commence_time"],
            "home_team": TEAM_ABBR.get(home, home),
            "away_team": TEAM_ABBR.get(away, away),
            "spread_home": spread_home, "total": total,
            "ml_home": mean(ml_home), "ml_away": mean(ml_away),
            "implied_home": imp_home, "implied_away": imp_away,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        })
    return pd.DataFrame(rows)


def sync_odds() -> int:
    df = _consensus(fetch_game_odds())
    if df.empty:
        mark_synced("odds", "0 events (offseason?)")
        return 0
    with connect() as conn:
        n = upsert_rows(df, "odds_games", conn)
    mark_synced("odds", f"{n} events")
    return n


def game_lines() -> pd.DataFrame:
    """Latest cached odds, one row per team with its own implied total & spread."""
    from ..db import read_df

    games = read_df("SELECT * FROM odds_games")
    if games.empty:
        return pd.DataFrame(columns=["team", "opponent", "spread", "total", "implied_total", "is_home"])
    home = pd.DataFrame({
        "team": games["home_team"], "opponent": games["away_team"],
        "spread": games["spread_home"], "total": games["total"],
        "implied_total": games["implied_home"], "is_home": True,
    })
    away = pd.DataFrame({
        "team": games["away_team"], "opponent": games["home_team"],
        "spread": -games["spread_home"], "total": games["total"],
        "implied_total": games["implied_away"], "is_home": False,
    })
    return pd.concat([home, away], ignore_index=True)
