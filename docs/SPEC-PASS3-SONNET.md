# Pass 3 Spec — Plumbing, Tests, Frontend (Sonnet)

Two workstreams. Both implement EXACTLY the contracts below — no redesigning the models
(they are Opus deliverables, documented in docs/MODELING.md). League rules always come
from `league_config()` / `GET /api/config`, never constants.

## A. Backend routers (api/app/routers/) + tests (api/tests/)

`main.py` auto-mounts routers named league/draft/waivers/lineup/dashboard exposing
`router = APIRouter()`. All responses JSON; DataFrames via `df.to_dict(orient="records")`
with NaN→null (use a shared `records()` helper). Errors: 4xx with `{"detail": msg}`;
missing data (e.g. no sync yet) → 200 with empty lists + `"warning"` field, never 500.

### league.py
- `GET /league/roster` → `{mode: "espn"|"manual"|"none", team: {...}}` (espn.get_my_roster,
  with each roster player resolved to `player_id` via players.espn_id or name match)
- `POST /league/roster` body `{players: [{name, position, team?}]}` → resolve names against
  players table (case-insensitive, then fuzzy), store via set_manual_roster; return
  `{resolved: [...], unresolved: [names]}`
- `GET /league/teams` → ESPN teams cache (standings data) or `[]`
- `POST /league/my-team` body `{team_id}` → set_my_team
- `GET /league/players?search=&position=&limit=` → players joined to latest ADP + season
  projection (for pickers/autocomplete)

### draft.py — draft state persisted in data/draft_state.json (survives restart)
- `GET /draft/board?limit=` → vorp_board minus drafted, plus `drafted_count`, `current_pick`,
  `my_next_pick` (snake math from config teams/rounds + my_slot), positional run detector
  (`runs`: last-8-picks position counts), `tier_depth` per position
- `POST /draft/pick {player_id, by_me: bool}` / `POST /draft/undo` / `POST /draft/reset {my_slot?}`
- `GET /draft/recommendation` → `{recommended: row, alternatives: [4 rows]}` — vorp_board
  head filtered by need; each row includes proj, vorp, tier, bye, adp, adp_delta, rationale
- `GET /draft/my-roster` → picks by_me with slot suggestions

### waivers.py
- `GET /waivers/rankings?week=` → rank_free_agents over ESPN free-agent cache (fallback:
  top undrafted-by-ADP players not on my roster); rows include faab_bid, confidence,
  breakout_flags, suggested_drop, rationale

### lineup.py
- `GET /lineup/optimal?week=&season=` → LineupResult serialized: `{starters: [{slot, player}],
  bench: [...], current_total, optimal_total, delta, close_calls: [...], week, season}`
  Roster from get_my_roster (resolved player_ids). Default week: next unplayed 2026 week, else 1.

### dashboard.py
- `GET /dashboard` → `{my_team, optimal_lineup (summary), top_waivers (5), standings,
  odds_board (game_lines records), sync_status}` — one call renders the home page.

### tests (pytest, no network; use the real SQLite db read-only)
- test_scoring.py: score_offense vs hand-computed lines; points_allowed_score tier edges;
  half-PPR reception value from config honored
- test_vorp.py: replacement_levels derived from config (change config fixture → levels move);
  vorp = proj − replacement proj; drafted players excluded from board
- test_lineup.py: legality (slot counts match config incl. FLEX eligibility), optimizer ≥ any
  random legal lineup (property-style, 25 samples), close_calls length ≤ 2
- test_api.py: FastAPI TestClient smoke on /api/health, /api/config, /api/draft/board,
  /api/lineup/optimal (assert legal shape + no 500s)

## B. Frontend (web/) — Vite + React 18 + TypeScript + Tailwind + shadcn/ui + Recharts

Scaffold with Vite react-ts template. Proxy `/api` → http://localhost:8000 in vite.config.
Dark, broadcast-grade sports-analytics aesthetic: near-black background, one saturated
accent (electric green or amber), tier/heat coloring, dense sortable tables, big stat
tiles, Recharts for visuals. NOT a generic admin template.

Pages (react-router):
1. **Dashboard `/`** — my team card, this-week optimal lineup strip, top-5 waiver targets,
   standings table, Vegas board (implied totals bar chart)
2. **Draft Room `/draft`** — setup (my_slot) → live board: recommendation hero card + 4
   alternatives, searchable/sortable available-players table (tier color-coded, ADP delta
   badges), my-roster sidebar with slot fill, positional-run alert, tier-depth mini bars,
   pick/undo/reset controls. Every action optimistic + refetch.
3. **Waivers `/waivers`** — ranked add table with FAAB bid, confidence pill, breakout flag
   chips, suggested drop, expandable rationale
4. **Start/Sit `/lineup`** — week selector, optimal vs current side-by-side, per-slot player
   cards with adjusted proj + confidence bar, delta banner, close-calls panel; manual-roster
   entry modal when mode="none" (name autocomplete via /league/players)

Cross-cutting: typed API client (src/lib/api.ts) with fetch wrappers + zod-free TS types
mirroring the contracts above; TanStack Query for caching/loading/error; skeleton loaders;
empty states with "Run sync" call-to-action hitting POST /api/sync; error toasts; a top
nav with sync button + last-synced indicator. No console errors.

## Definition of done
- `npm run dev` boots both; every page renders real data end-to-end
- `npm test` (pytest) green
- `npm --prefix web run build` passes typecheck
