# GridironGM

Fantasy football decision support across **multiple** half-PPR ESPN leagues:
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
- **ESPN private leagues** — one cookie pair covers every league on the same ESPN
  account. Add to `shared.env`:
  ```
  ESPN_S2=...
  SWID={...}
  ```
  Each league's `espn_league_id` (from the ESPN league URL, not a secret) goes in the
  `leagues:` block of `config/league.yaml`. For a league on a *different* ESPN account,
  add per-league `espn_s2:`/`swid:` to that entry.
  With no ESPN credentials the app runs in **manual mode**: paste your roster in the UI.

## Multiple leagues

All four leagues share scoring and roster rules; a `leagues:` entry overrides only
what differs — team count, your draft slot, and the ESPN league id:

```yaml
leagues:
  - id: work              # slug; keys stored draft state, don't rename mid-season
    name: "Work League"
    teams: 12             # drives VORP replacement level, so it must be right
    espn_league_id: 123456
    draft: { my_slot: 4, rounds: 16 }
active: work              # default for a browser that hasn't picked one
```

Switch leagues from the dropdown in the header; the selection is per-browser and
every page follows it. Each league keeps its own draft state
(`data/drafts/<id>.json`) and ESPN cache (`data/espn_cache/<id>/`).

Team count is not cosmetic: replacement level is `starters × teams`, so a 10-team
league ranks the same player lower than a 12-team one.

### Drafting

Enter picks in order and each is attributed to the team on the clock automatically
(snake order, reversing every round). Override the team with the **Log pick to**
dropdown for trades or out-of-order entry. Every pick is stored, so
**Rosters by team** shows what every opponent has taken — which is what makes the
recommendation aware of positional runs and your own remaining needs.

The board shows each player's NFL **team** and **depth chart rank** (RB1/WR2/…),
refreshed by **Update data** in the draft room. That runs a targeted `draft-day`
sync — depth charts, injuries and ADP, about 6 seconds — rather than the full
sync in the header, which re-pulls three seasons of weekly stats and takes
minutes. Preseason box scores are deliberately not ingested; see
docs/FINDINGS.md for why depth charts are the signal that matters instead.

If a league has `espn_league_id` set, **ESPN** in the draft room pulls picks made in
the ESPN draft room. It's idempotent — re-sync as often as you like; it only appends
what's new and never overwrites a pick you typed. Verify it against an ESPN **mock
draft** before relying on it on draft day (see docs/FINDINGS.md).

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
