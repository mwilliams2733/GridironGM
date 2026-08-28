"""SQLite storage layer: schema DDL, connections, and DataFrame upsert helpers."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pandas as pd

from .config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    player_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    position TEXT,
    team TEXT,
    birthdate TEXT,
    status TEXT,
    espn_id TEXT
);
CREATE TABLE IF NOT EXISTS weekly_stats (
    season INTEGER, week INTEGER, player_id TEXT,
    team TEXT, opponent TEXT, position TEXT,
    completions REAL, attempts REAL, passing_yards REAL, passing_tds REAL,
    interceptions REAL, sacks REAL,
    carries REAL, rushing_yards REAL, rushing_tds REAL,
    receptions REAL, targets REAL, receiving_yards REAL, receiving_tds REAL,
    target_share REAL, air_yards_share REAL,
    rushing_fumbles_lost REAL, receiving_fumbles_lost REAL, sack_fumbles_lost REAL,
    passing_2pt_conversions REAL, rushing_2pt_conversions REAL, receiving_2pt_conversions REAL,
    special_teams_tds REAL,
    PRIMARY KEY (season, week, player_id)
);
CREATE TABLE IF NOT EXISTS snap_counts (
    season INTEGER, week INTEGER, player_id TEXT,
    offense_snaps REAL, offense_pct REAL,
    PRIMARY KEY (season, week, player_id)
);
CREATE TABLE IF NOT EXISTS schedules (
    season INTEGER, week INTEGER, game_id TEXT PRIMARY KEY,
    home_team TEXT, away_team TEXT, gameday TEXT, weekday TEXT,
    home_score REAL, away_score REAL
);
CREATE TABLE IF NOT EXISTS team_defense (
    season INTEGER, week INTEGER, team TEXT, opponent TEXT,
    def_sacks REAL, def_interceptions REAL, def_fumbles_forced REAL,
    fumble_recovery_opp REAL, def_tds REAL, def_safeties REAL,
    def_punt_blocks REAL, def_pat_blocks REAL, def_fg_blocks REAL,
    special_teams_tds REAL, points_allowed REAL,
    PRIMARY KEY (season, week, team)
);
CREATE TABLE IF NOT EXISTS kicking_stats (
    season INTEGER, week INTEGER, player_id TEXT, team TEXT, opponent TEXT,
    fg_made_0_19 REAL, fg_made_20_29 REAL, fg_made_30_39 REAL,
    fg_made_40_49 REAL, fg_made_50_59 REAL, fg_made_60_ REAL,
    fg_missed REAL, pat_made REAL, pat_missed REAL,
    PRIMARY KEY (season, week, player_id)
);
CREATE TABLE IF NOT EXISTS injuries (
    season INTEGER, week INTEGER, player_id TEXT,
    report_status TEXT, practice_status TEXT,
    PRIMARY KEY (season, week, player_id)
);
CREATE TABLE IF NOT EXISTS odds_games (
    event_id TEXT PRIMARY KEY,
    commence_time TEXT, home_team TEXT, away_team TEXT,
    spread_home REAL, total REAL, ml_home REAL, ml_away REAL,
    implied_home REAL, implied_away REAL,
    fetched_at TEXT
);
-- ADP keyed by SCORING FORMAT (half-ppr / ppr / standard), not by team count:
-- FFC's `teams` parameter is a measured no-op, while half-PPR and full-PPR
-- disagree on 120 of 228 common players. See etl/adp.py.
CREATE TABLE IF NOT EXISTS adp (
    format TEXT, player_name TEXT, position TEXT, team TEXT,
    adp REAL, adp_formatted TEXT, fetched_at TEXT,
    PRIMARY KEY (format, player_name, position)
);
CREATE TABLE IF NOT EXISTS projections (
    scope TEXT, season INTEGER, week INTEGER, player_id TEXT,
    proj_points REAL, floor REAL, ceiling REAL, components_json TEXT,
    PRIMARY KEY (scope, season, week, player_id)
);
-- Current depth chart position (1 = starter at that position group). Holds the
-- latest nflverse snapshot only, not the historical series.
CREATE TABLE IF NOT EXISTS depth_charts (
    player_id TEXT PRIMARY KEY,
    team TEXT, pos TEXT, depth_rank INTEGER, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS sync_log (
    scope TEXT PRIMARY KEY, last_synced TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_weekly_player ON weekly_stats(player_id);
CREATE INDEX IF NOT EXISTS idx_weekly_season ON weekly_stats(season, week);
"""


def init_db() -> None:
    ensure_dirs()
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def replace_table(df: pd.DataFrame, table: str, conn: sqlite3.Connection) -> int:
    """Full-replace a table's contents from a DataFrame (schema stays authoritative)."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    keep = [c for c in cols if c in df.columns]
    conn.execute(f"DELETE FROM {table}")
    df[keep].to_sql(table, conn, if_exists="append", index=False)
    return len(df)


def upsert_rows(df: pd.DataFrame, table: str, conn: sqlite3.Connection) -> int:
    """INSERT OR REPLACE rows from a DataFrame into `table` (PK-aware upsert)."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    keep = [c for c in cols if c in df.columns]
    placeholders = ",".join("?" for _ in keep)
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(keep)}) VALUES ({placeholders})"
    rows = [tuple(None if pd.isna(v) else v for v in row) for row in df[keep].itertuples(index=False)]
    conn.executemany(sql, rows)
    return len(rows)


def read_df(query: str, params: tuple = ()) -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query(query, conn, params=params)


def mark_synced(scope: str, detail: str = "") -> None:
    from datetime import datetime, timezone
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO sync_log (scope, last_synced, detail) VALUES (?, ?, ?)",
            (scope, datetime.now(timezone.utc).isoformat(), detail),
        )
