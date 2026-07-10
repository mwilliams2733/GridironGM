# GridironGM

Fantasy football decision support for a 12-team, half-PPR ESPN league:
**Draft Assistant** · **Waiver Wire Manager** · **Weekly Start/Sit Optimizer** — all
driven by one projections engine over 3 seasons of nflverse data, Vegas lines from
The Odds API, and live ESPN league state.

## Setup

Prereqs: Python 3.11+, Node 18+.

```powershell
npm install          # root dev tooling (concurrently)
npm run setup        # web deps + Python venv + api deps
npm run sync         # first data sync (nflverse stats, odds, ADP, ESPN if configured)
```

### Secrets & credentials (never committed)

- **The Odds API** — key is read at runtime from the `ODDS_API_KEY` env var, falling
  back to `C:\Users\mwill\.secrets\shared.env` (accepted names: `ODDS_API_KEY`,
  `TheODDSAPI`).
- **ESPN private league** — add to `shared.env`:
  ```
  ESPN_LEAGUE_ID=1234567
  ESPN_S2=...
  SWID={...}
  ```
  and set `espn.league_id`/`espn.year` in `config/league.yaml` (league id is not secret).
  With no ESPN credentials the app runs in **manual mode**: paste your roster in the UI.

## Run

```powershell
npm run dev          # boots FastAPI (:8100) + Vite web app (:5173+) together
```

Open the URL Vite prints (http://localhost:5173, or the next free port).
API docs at http://localhost:8100/docs.

## Refresh data

```powershell
npm run sync                             # everything
# or scoped: stats | odds | adp | espn
cd api; ..\.venv\Scripts\python -m app.etl.sync odds
```

## Tests

```powershell
npm test             # pytest: scoring engine, VORP, lineup optimizer
```

## League settings

Everything (teams, scoring, lineup slots, bench size, draft type, FAAB budget)
lives in `config/league.yaml`. No scoring or roster rule is hard-coded.

## Docs

- `docs/PLAN.md` — architecture & build plan
- `docs/MODELING.md` — every projection/VORP/optimizer formula and assumption
- `docs/REVIEW.md` — final review findings
