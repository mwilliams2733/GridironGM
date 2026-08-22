"""nflverse ingest via nflreadpy: weekly stats, snaps, schedules, rosters, injuries.

nflreadpy (the maintained successor to nfl_data_py) returns Polars frames and
handles nflverse's current release URLs. Raw pulls are cached to parquet
(data/cache/) so re-syncs and offline work are cheap; normalized frames land in
SQLite. Seasons that don't exist yet (e.g. the upcoming season before week 1)
are skipped, not fatal.
"""
from __future__ import annotations

import logging

import pandas as pd

from ..config import PARQUET_DIR, current_season, ensure_dirs, history_seasons
from ..db import connect, init_db, mark_synced, replace_table

log = logging.getLogger(__name__)


def _seasons() -> list[int]:
    return sorted(history_seasons() + [current_season()])


def _cached_pull(name: str, fetch) -> pd.DataFrame:
    """Fetch via nflreadpy (per call), cache to parquet; fall back to cache offline."""
    ensure_dirs()
    path = PARQUET_DIR / f"{name}.parquet"
    try:
        df = fetch()
        if hasattr(df, "to_pandas"):  # polars -> pandas
            df = df.to_pandas()
        df.to_parquet(path, index=False)
        return df
    except Exception as exc:
        if path.exists():
            log.warning("fetch %s failed (%s); using cached parquet", name, exc)
            return pd.read_parquet(path)
        raise


def _pull_seasons(name: str, loader, seasons: list[int]) -> pd.DataFrame:
    """Pull per season so a not-yet-published season 404 doesn't sink the batch."""
    frames = []
    for yr in seasons:
        try:
            frames.append(_cached_pull(f"{name}_{yr}", lambda y=yr: loader(y)))
        except Exception as exc:
            log.warning("season %s unavailable for %s (%s); skipping", yr, name, exc)
    if not frames:
        raise RuntimeError(f"no seasons available for {name}")
    return pd.concat(frames, ignore_index=True)


_RENAMES = {  # tolerate schema drift between nflverse releases
    "recent_team": "team",
    "team_abbr": "team",
    "opponent": "opponent",
    "opponent_team": "opponent",
    # nflverse renamed thrown picks to `passing_interceptions`; the bare name now
    # belongs to the DEFENSIVE stat. Without this entry the keep-filter below
    # silently drops the column and weekly_stats.interceptions stays NULL, which
    # leaves score_offense blind to picks whenever it is fed a stored row.
    "passing_interceptions": "interceptions",
}

# Columns we persist to weekly_stats, spelled the way WE store them.
WEEKLY_COLUMNS = [
    "season", "week", "player_id", "team", "opponent", "position",
    "completions", "attempts", "passing_yards", "passing_tds", "interceptions",
    "sacks_suffered", "carries", "rushing_yards", "rushing_tds",
    "receptions", "targets", "receiving_yards", "receiving_tds",
    "target_share", "air_yards_share",
    "rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost",
    "passing_2pt_conversions", "rushing_2pt_conversions", "receiving_2pt_conversions",
    "special_teams_tds",
]

# The subset `scoring.score_offense` actually reads off a stored row. Dropping one
# of these makes scoring quietly wrong rather than loud, so tests assert on it.
SCORING_INPUT_COLUMNS = (
    "passing_yards", "passing_tds", "interceptions", "passing_2pt_conversions",
    "rushing_yards", "rushing_tds", "rushing_2pt_conversions",
    "receptions", "receiving_yards", "receiving_tds", "receiving_2pt_conversions",
    "rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost",
    "special_teams_tds",
)


def normalize_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Map a raw nflverse weekly frame onto our schema.

    Pure — no DB, no network — so the column contract is unit-testable without a
    sync. Stores raw components only: points are scored at read time through
    each league's own term table, so no scoring profile is baked in here.
    """
    df = df.rename(columns={k: v for k, v in _RENAMES.items()
                            if k in df.columns and v not in df.columns})
    out = df[[c for c in WEEKLY_COLUMNS if c in df.columns]].copy()
    if "sacks_suffered" in out.columns:
        out = out.rename(columns={"sacks_suffered": "sacks"})
    return out


def sync_weekly_stats() -> int:
    import nflreadpy as nfl

    df = _pull_seasons("weekly", lambda y: nfl.load_player_stats(y, summary_level="week"), _seasons())
    out = normalize_weekly(df)
    out = out[out["position"].isin(["QB", "RB", "WR", "TE"])]
    with connect() as conn:
        n = replace_table(out, "weekly_stats", conn)
    mark_synced("weekly_stats", f"{n} rows, seasons={_seasons()}")
    return n


def sync_players() -> int:
    import nflreadpy as nfl

    df = _cached_pull("players", lambda: nfl.load_players())
    ids = _cached_pull("ff_ids", lambda: nfl.load_ff_playerids())
    espn_map = (
        ids.dropna(subset=["gsis_id", "espn_id"])
        .drop_duplicates("gsis_id")
        .set_index("gsis_id")["espn_id"]
    )
    out = pd.DataFrame({
        "player_id": df["gsis_id"],
        "name": df["display_name"],
        "position": df["position"],
        "team": df.get("latest_team", df.get("team_abbr")),
        "birthdate": df.get("birth_date", pd.Series(dtype=str)).astype(str),
        "status": df.get("status", pd.Series(dtype=str)),
    }).dropna(subset=["player_id"])
    out["espn_id"] = out["player_id"].map(espn_map)
    out = out[out["position"].isin(["QB", "RB", "WR", "TE", "K"])]
    with connect() as conn:
        n = replace_table(out, "players", conn)
    mark_synced("players", f"{n} rows")
    return n


def sync_snap_counts() -> int:
    import nflreadpy as nfl

    df = _pull_seasons("snaps", lambda y: nfl.load_snap_counts(y), _seasons())
    ids = _cached_pull("ff_ids", lambda: nfl.load_ff_playerids())
    pfr_map = (
        ids.dropna(subset=["pfr_id", "gsis_id"])
        .drop_duplicates("pfr_id")
        .set_index("pfr_id")["gsis_id"]
    )
    out = pd.DataFrame({
        "season": df["season"], "week": df["week"],
        "player_id": df["pfr_player_id"].map(pfr_map),
        "offense_snaps": df["offense_snaps"], "offense_pct": df["offense_pct"],
    }).dropna(subset=["player_id"]).drop_duplicates(["season", "week", "player_id"])
    with connect() as conn:
        n = replace_table(out, "snap_counts", conn)
    mark_synced("snap_counts", f"{n} rows")
    return n


def sync_depth_charts() -> int:
    """Current depth chart position per player, for the season being drafted.

    nflverse publishes a running series of snapshots (`dt`), one per scrape —
    145 of them between March and August in 2026 — so we keep only the most
    recent, which is the depth chart as of now. `pos_rank` is the ordinal within
    the position group (1 = starter), which is the preseason signal that
    actually moves fantasy value: box-score production in August does not.

    Keyed on gsis_id, which is our player_id.
    """
    import nflreadpy as nfl

    season = current_season()
    df = _cached_pull(f"depth_charts_{season}", lambda: nfl.load_depth_charts([season]))
    if df.empty:
        log.warning("no depth chart data for %s", season)
        return 0

    latest_dt = df["dt"].max()
    cur = df[df["dt"] == latest_dt]

    out = pd.DataFrame({
        "player_id": cur["gsis_id"],
        # Raw nflverse abbreviation; the board's team column comes from the
        # projections frame, which is already normalized. We join on player_id.
        "team": cur["team"],
        "pos": cur["pos_abb"],
        "depth_rank": pd.to_numeric(cur["pos_rank"], errors="coerce"),
        "updated_at": str(latest_dt),
    }).dropna(subset=["player_id", "depth_rank"])

    # A player can appear in several formation groups (e.g. 3WR sets); his depth
    # is the best rank he holds at his own position.
    out = (out.sort_values("depth_rank")
              .drop_duplicates(["player_id", "pos"], keep="first")
              .drop_duplicates("player_id", keep="first"))
    out["depth_rank"] = out["depth_rank"].astype(int)

    with connect() as conn:
        n = replace_table(out, "depth_charts", conn)
    mark_synced("depth_charts", f"{n} players @ {latest_dt}")
    return n


def sync_schedules() -> int:
    import nflreadpy as nfl

    df = _cached_pull("schedules", lambda: nfl.load_schedules(_seasons()))
    df = df[df["season"].isin(_seasons())]
    out = pd.DataFrame({
        "season": df["season"], "week": df["week"], "game_id": df["game_id"],
        "home_team": df["home_team"], "away_team": df["away_team"],
        "gameday": df["gameday"].astype(str), "weekday": df["weekday"],
    })
    with connect() as conn:
        n = replace_table(out, "schedules", conn)
    mark_synced("schedules", f"{n} rows")
    return n


def sync_injuries() -> int:
    import nflreadpy as nfl

    try:
        df = _pull_seasons("injuries", lambda y: nfl.load_injuries(y), _seasons())
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
        "depth_charts": sync_depth_charts(),
    }
