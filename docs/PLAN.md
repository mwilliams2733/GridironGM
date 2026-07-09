# GridironGM — Build Plan (Pass 1: Fable 5, Architect)

A local, single-user fantasy football decision-support app for one 12-team, half-PPR ESPN league.
Three core features share one projections engine: **Draft Assistant**, **Waiver Manager**, **Start/Sit Optimizer**.

## Tech stack (decided)

| Layer     | Choice                                                        | Why |
|-----------|---------------------------------------------------------------|-----|
| Backend   | Python 3.11, FastAPI, uvicorn                                 | House standard; pandas-friendly |
| Storage   | SQLite (`data/gridiron.db`) + Parquet cache for raw nflverse pulls | Fast, offline-capable, zero-ops |
| Stats     | `nfl_data_py` (nflverse): weekly stats, snaps, schedules, rosters, injuries | Free, 3 seasons of history |
| Odds      | The Odds API (`ODDS_API_KEY` from `C:\Users\mwill\.secrets\shared.env`, var `TheODDSAPI`) | Spreads, totals, implied team totals |
| ADP       | FantasyFootballCalculator free API (half-PPR, 12-team)        | Keyless; nflverse/Odds API lack fantasy ADP |
| League    | `espn-api` (LEAGUE_ID/YEAR + `espn_s2`/`SWID` for private)    | Rosters, free agents, matchups; manual-entry fallback |
| Frontend  | Vite + React + TypeScript, Tailwind, shadcn/ui, Recharts      | Broadcast-grade dashboard aesthetic |
| Dev run   | Root `npm run dev` → concurrently boots uvicorn (:8000) + Vite (:5173, proxies `/api`) | One command |

## Repo layout

```
config/league.yaml          # ALL league settings — nothing hard-coded in logic
api/
  app/
    config.py               # league.yaml loader + secrets loader (env → shared.env fallback)
    db.py                   # SQLite connection, schema DDL, upsert helpers
    scoring.py              # scoring engine: stat line → fantasy points (from league.yaml)
    etl/
      nfl_data.py           # nflverse ingest → parquet cache → SQLite
      odds.py               # Odds API client → odds_games table (implied totals derived)
      adp.py                # FFC ADP ingest
      espn.py               # ESPN league sync (rosters, FAs, matchups) + manual mode
      sync.py               # orchestrator: `python -m app.etl.sync [--full]`
    models/                 # OPUS PASS — analytical core
      projections.py        # single source of truth for player projections
      vorp.py                # value-over-replacement + tiers (draft)
      waivers.py             # waiver ranking, breakout flags, FAAB suggestions
      lineup.py              # weekly optimal-lineup optimizer + matchup adjustments
    routers/                # SONNET PASS — REST plumbing
      league.py draft.py waivers.py lineup.py dashboard.py
    main.py                 # FastAPI app, CORS, router mounting
  tests/                    # scoring, VORP, lineup optimizer, projections sanity
web/                        # SONNET PASS — Vite React app
docs/MODELING.md            # OPUS PASS — every formula & assumption
docs/REVIEW.md              # FABLE PASS 4 — findings & fixes
```

## Data schema (SQLite)

- `players(player_id TEXT PK, name, position, team, birthdate, status)`
- `weekly_stats(season, week, player_id, team, opponent, ...raw stat cols..., fantasy_points_half_ppr, PK(season,week,player_id))`
- `snap_counts(season, week, player_id, offense_snaps, offense_pct)`
- `schedules(season, week, game_id, home_team, away_team, gameday, weekday)`
- `injuries(season, week, player_id, report_status, practice_status)`
- `odds_games(event_id PK, commence_time, home_team, away_team, spread_home, total, ml_home, ml_away, implied_home, implied_away, fetched_at)`
- `adp(player_name, position, team, adp, adp_formatted, fetched_at)`
- `projections(scope TEXT, season, week, player_id, proj_points, floor, ceiling, components_json, PK(scope,season,week,player_id))` — scope ∈ {`season`, `week`, `ros`}
- ESPN state cached as JSON under `data/espn_cache/` (rosters, free agents, matchups) — refreshed per sync, readable offline.

Team-name mapping: odds books use full names ("Kansas City Chiefs"), nflverse uses abbreviations ("KC"). A static map lives in `etl/odds.py`.

## Module interfaces (contracts Opus/Sonnet implement against)

```python
# projections.py
def project_season(season: int) -> pd.DataFrame      # player_id, proj_points, floor, ceiling, components
def project_week(season: int, week: int) -> pd.DataFrame  # matchup-adjusted weekly projections
def project_ros(season: int, week: int) -> pd.DataFrame   # rest-of-season

# vorp.py
def replacement_levels(league_cfg) -> dict[str, float]
def vorp_board(drafted_ids: set[str], my_roster: list[str]) -> pd.DataFrame
    # columns: player_id, name, pos, proj, vorp, tier, bye, adp, adp_delta, need_score, rationale

# waivers.py
def rank_free_agents(free_agents, my_roster, season, week) -> pd.DataFrame
    # columns: player_id, ros_value, next_week_value, breakout_flags, suggested_drop, faab_bid, confidence, rationale

# lineup.py
def optimize(roster_ids, season, week) -> LineupResult
    # optimal slot assignment, current-vs-optimal delta, per-player confidence, close_calls[2]
```

## API surface (FastAPI, prefix `/api`)

- `GET /api/health`, `GET /api/config`
- `POST /api/sync` (+ `?scope=stats|odds|espn|adp`), `GET /api/sync/status`
- `GET /api/dashboard` — my team, optimal lineup, top waivers, standings
- Draft: `GET /api/draft/board`, `POST /api/draft/pick {player_id, by_me}`, `POST /api/draft/undo`, `POST /api/draft/reset`, `GET /api/draft/recommendation`
- Waivers: `GET /api/waivers/rankings`
- Lineup: `GET /api/lineup/optimal?week=N`, roster from ESPN or manual
- League: `GET /api/league/roster`, `POST /api/league/roster` (manual mode), `GET /api/league/standings`

## Pass breakdown

1. **Fable (this pass):** plan, scaffold, `league.yaml`, config/secrets loaders, DB layer, scoring engine, Odds/ADP/ESPN/nflverse clients + sync orchestrator, FastAPI shell, root dev scripts, initial commit.
2. **Opus:** `models/` analytical core + `docs/MODELING.md`. Weighted 3-season projections (recency, games played, usage trend, age, role), VORP with 12-team half-PPR replacement levels, waiver model, lineup optimizer with Vegas/DvP/game-script adjustments.
3. **Sonnet:** ETL hardening, routers, tests (scoring/VORP/optimizer), full React frontend (dashboard, draft room, waivers, start/sit), loading/error/empty states.
4. **Fable review:** boot app, run tests, audit models vs MODELING.md, UI check via browser, `docs/REVIEW.md`.

## Acceptance checklist

Mirrors the brief: one-command boot; league.yaml drives everything; 3 seasons cached + refresh works;
odds key loads from secrets and odds render; ESPN sync or manual mode; draft rec + top-5 alternatives
with rationale; waiver ranks with drop/FAAB; legal optimal lineup with adjustments; professional UI,
no console errors; tests pass.
