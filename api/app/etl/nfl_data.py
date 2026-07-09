"""nflverse ingest via nfl_data_py: weekly stats, snaps, schedules, rosters, injuries.

Raw pulls are cached to parquet (data/cache/) so re-syncs and offline work are cheap;
normalized frames land in SQLite.
"""
from __future__ import annotations

import logging

import pandas as pd

from ..config import PARQUET_DIR, current_season, ensure_dirs, history_seasons
from ..db import connect, init_db, mark_synced, replace_table, upsert_rows

log = logging.getLogger(__name__)

WEEKLY_COLS = [
    "season", "week", "player_id", "recent_team", "opponent_team", "position",
    "completions", "attempts", "passing_yards", "passing_tds", "interceptions",
    "sacks", "carries", "rushing_yards", "rushing_tds",
    "receptions", "targets", "receiving_yards", "receiving_tds",
    "target_share", "air_yards_share",
    "rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost",
    "passing_2pt_conversions", "rushing_2pt_conversions", "receiving_2pt_conversions",
    "special_teams_tds", "fantasy_points_ppr", "fantasy_points",
]


def _cached_pull(name: str, fetch) -> pd.DataFrame:
    """Fetch via nfl_data_py, caching to parquet; fall back to cache offline."""
    ensure_dirs()
    path = PARQUET_DIR / f"{name}.parquet"
    try:
        df = fetch()
        df.to_parquet(path, index=False)
        return df
    except Exception as exc:  # network down / source unavailable
        if path.exists():
            log.warning("fetch %s failed (%s); using cached parquet", name, exc)
            return pd.read_parquet(path)
        raise


def sync_weekly_stats(seasons: list[int] | None = None) -> int:
    import nfl_data_py as nfl

    seasons = seasons or (history_seasons() + [current_season()])
    df = _cached_pull("weekly", lambda: nfl.import_weekly_data(seasons))
    keep = [c for c in WEEKLY_COLS if c in df.columns]
    out = df[keep].rename(columns={"recent_team": "team", "opponent_team": "opponent"})
    # league points = nflverse standard points + configured per-reception value
    # (nflverse standard already matches ESPN base: pass TD 4, INT -2, fumble -2)
    from ..config import league_config
    rec_val = league_config()["scoring"]["receiving"]["reception"]
    out["fantasy_points_half_ppr"] = df["fantasy_points"] + df["receptions"].fillna(0) * rec_val
    out = out.drop(columns=[c for c in ("fantasy_points_ppr", "fantasy_points") if c in out.columns])
    out = out[out["position"].isin(["QB", "RB", "WR", "TE"])]
    with connect() as conn:
        n = replace_table(out, "weekly_stats", conn)
    mark_synced("weekly_stats", f"{n} rows, seasons={seasons}")
    return n


def sync_players(seasons: list[int] | None = None) -> int:
    import nfl_data_py as nfl

    seasons = seasons or (history_seasons() + [current_season()])
    df = _cached_pull("rosters", lambda: nfl.import_seasonal_rosters(seasons))
    latest = df.sort_values("season").drop_duplicates("player_id", keep="last")
    out = pd.DataFrame({
        "player_id": latest["player_id"],
        "name": latest["player_name"],
        "position": latest["position"],
        "team": latest["team"],
        "birthdate": latest.get("birth_date", pd.Series(dtype=str)).astype(str),
        "status": latest.get("status", pd.Series(dtype=str)),
    })
    out = out[out["position"].isin(["QB", "RB", "WR", "TE", "K"])]
    with connect() as conn:
        n = replace_table(out, "players", conn)
    mark_synced("players", f"{n} rows")
    return n


def sync_snap_counts(seasons: list[int] | None = None) -> int:
    import nfl_data_py as nfl

    seasons = seasons or (history_seasons() + [current_season()])
    df = _cached_pull("snaps", lambda: nfl.import_snap_counts(seasons))
    out = pd.DataFrame({
        "season": df["season"], "week": df["week"],
        "player_id": df["pfr_player_id"],
        "offense_snaps": df["offense_snaps"], "offense_pct": df["offense_pct"],
    })
    # map pfr ids -> gsis ids where possible
    try:
        ids = _cached_pull("ids", lambda: nfl.import_ids())
        id_map = ids.dropna(subset=["pfr_id", "gsis_id"]).set_index("pfr_id")["gsis_id"]
        out["player_id"] = out["player_id"].map(id_map).fillna(out["player_id"])
    except Exception as exc:
        log.warning("id map unavailable (%s); keeping pfr ids", exc)
    out = out.dropna(subset=["player_id"]).drop_duplicates(["season", "week", "player_id"])
    with connect() as conn:
        n = replace_table(out, "snap_counts", conn)
    mark_synced("snap_counts", f"{n} rows")
    return n


def sync_schedules(seasons: list[int] | None = None) -> int:
    import nfl_data_py as nfl

    seasons = seasons or (history_seasons() + [current_season()])
    df = _cached_pull("schedules", lambda: nfl.import_schedules(seasons))
    out = pd.DataFrame({
        "season": df["season"], "week": df["week"], "game_id": df["game_id"],
        "home_team": df["home_team"], "away_team": df["away_team"],
        "gameday": df["gameday"].astype(str), "weekday": df["weekday"],
    })
    with connect() as conn:
        n = replace_table(out, "schedules", conn)
    mark_synced("schedules", f"{n} rows")
    return n


def sync_injuries(seasons: list[int] | None = None) -> int:
    import nfl_data_py as nfl

    seasons = seasons or [current_season()]
    try:
        df = _cached_pull("injuries", lambda: nfl.import_injuries(seasons))
    except Exception as exc:
        log.warning("injuries unavailable (%s); skipping", exc)
        return 0
    out = pd.DataFrame({
        "season": df["season"], "week": df["week"], "player_id": df["gsis_id"],
        "report_status": df["report_status"], "practice_status": df["practice_status"],
    }).dropna(subset=["player_id"]).drop_duplicates(["season", "week", "player_id"])
    with connect() as conn:
        n = replace_table(out, "injuries", conn)
    mark_synced("injuries", f"{n} rows")
    return n


def sync_all_stats() -> dict[str, int]:
    init_db()
    return {
        "players": sync_players(),
        "weekly_stats": sync_weekly_stats(),
        "snap_counts": sync_snap_counts(),
        "schedules": sync_schedules(),
        "injuries": sync_injuries(),
    }
