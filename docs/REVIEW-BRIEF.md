# GridironGM — Architecture, Logic & UI/UX Review Brief

A self-contained description of the application, written to be handed to a
reviewing model that has **no access to the repository**. It covers what the app
does, how every piece of logic works, what the interface looks like and how it
behaves, what is already known to be weak, and the specific questions worth
answering.

Companion docs in-repo (not reproduced here in full): `docs/MODELING.md` (614
lines — full derivations and constant rationale), `docs/FINDINGS.md` (dated
investigation notes), `README.md` (setup and operating instructions).

---

## 1. What the product is

Fantasy football decision support for one person playing in **four half-PPR ESPN
leagues** simultaneously. Three jobs, one engine:

| Surface | Question it answers |
|---|---|
| **Draft Room** | Who do I take with this pick, and what has the room already taken? |
| **Waivers** | Who should I add this week, who do I drop, what do I bid? |
| **Start / Sit** | What is my highest-projected legal lineup this week? |

All three consume a single projections engine built over three completed NFL
seasons of nflverse data, Vegas lines from The Odds API, FantasyFootballCalculator
ADP, and live ESPN league state.

Single-user, local-first. No auth, no multi-tenancy, no cloud. Runs as
`npm run dev` → FastAPI on `:8100` + Vite dev server on `:5173`, Vite proxying
`/api` to the backend.

Scale: ~8,200 lines total — ~3,600 Python (api), ~2,900 TypeScript/TSX (web),
~1,100 tests, ~960 lines of docs.

---

## 2. Stack and layout

**Backend** — Python 3.11, FastAPI, pandas, SQLite, `nflreadpy`, `espn_api`,
`httpx`, `pyyaml`. Tests: pytest (91 tests across 10 files).

**Frontend** — React 19, TypeScript, Vite 8, TailwindCSS 4, TanStack Query v5,
React Router 7, Radix primitives wrapped shadcn-style, Recharts, `sonner` toasts,
`lucide-react` icons. Lint: oxlint.

```
api/app/
  config.py        league.yaml + secrets resolution, per-league config merge
  db.py            SQLite schema DDL, connect(), DataFrame upsert/replace helpers
  scoring.py       stat line -> fantasy points (single source of scoring truth)
  main.py          FastAPI shell, CORS, startup migration, router mounting
  etl/
    sync.py        orchestrator; scopes: stats | odds | adp | espn | draft-day
    nfl_data.py    nflverse ingest: weekly stats, snaps, schedules, injuries,
                   players, depth charts
    odds.py        The Odds API -> consensus spread/total/ML -> implied team totals
    adp.py         FantasyFootballCalculator half-PPR ADP
    espn.py        ESPN league sync + live draft picks via mDraftDetail
  models/
    projections.py season / weekly / rest-of-season projections  (760 lines)
    vorp.py        replacement levels, VORP board, tiers, ADP resolver, need score
    lineup.py      start/sit optimizer
    waivers.py     free-agent ranking, breakout flags, FAAB sizing
    mocksim.py     ADP-driven opponent pick chooser for mock drafts
  routers/
    _common.py     JSON-safe records(), name normalization, player resolution
    draft.py       board, pick, undo, reset, simulate, sync-espn, recommendation
    league.py      roster (ESPN/manual), teams, my-team, player search
    waivers.py     rankings
    lineup.py      optimal
    dashboard.py   one aggregate call for the home page
config/league.yaml  scoring, roster, draft, waivers, and the `leagues:` list
data/               gridiron.db, cache/*.parquet, drafts/<id>.json, espn_cache/<id>/
web/src/
  App.tsx main.tsx index.css
  lib/     api.ts (typed client) types.ts (hand-written) league.tsx (context)
           queryClient.ts utils.ts
  components/  Layout Nav States StatTile TierBadge Confidence + ui/*
  pages/   Dashboard Draft Waivers Lineup NotFound
```

---

## 3. Configuration & multi-league model

`config/league.yaml` is the single source of truth. Nothing in application logic
hard-codes scoring or roster values.

Shared blocks (`scoring`, `roster`, `draft`, `waivers`, `espn`) apply to every
league. A `leagues:` entry overrides only what genuinely differs:

```yaml
leagues:
  - id: league1        # slug; keys stored draft state — never rename mid-season
    name: "League 1"
    teams: 12          # drives VORP replacement level, so it must be right
    espn_league_id: null
    draft: { my_slot: null, rounds: 16 }
  - id: league3
    teams: 10
    ...
active: league1        # default for a browser that hasn't picked one
```

`league_config(league_id)` deep-copies the raw YAML, strips `leagues:`/`active:`,
and merges that league's overrides in — deliberately returning **the same dict
shape callers already read** (`cfg["league"]["teams"]`, `cfg["roster"]["starters"]`)
so threading a league id through the app changed no downstream access. A config
with no `leagues:` block yields a single derived league, so older single-league
configs keep working.

`UnknownLeague` subclasses `KeyError` so existing `except KeyError` handling still
catches it while FastAPI maps it to 404 rather than 500.

**Per-league storage.** Draft state at `data/drafts/<id>.json` (and
`<id>.mock.json` for mocks); ESPN cache at `data/espn_cache/<id>/`.
`migrate_legacy_storage()` runs on startup, moving single-league files under the
active league id, idempotently and only when the destination doesn't already
exist.

**Secrets** resolve env-first then `C:\Users\mwill\.secrets\shared.env`, with
aliases (`ODDS_API_KEY` also accepts `TheODDSAPI`). `httpx`'s INFO logger is
explicitly downgraded to WARNING because it logs full request URLs, which would
leak the `apiKey` query param.

Season is global — every league plays the same NFL season.

Scoring is half-PPR: pass 0.04/yd + 4/TD − 2/INT, rush/rec 0.1/yd + 6/TD,
0.5/reception, −2 fumble lost, tiered DST points-allowed. Starters:
QB1/RB2/WR2/TE1/FLEX1/K1/DST1, bench 7, FLEX = RB/WR/TE. Snake draft, 16 rounds,
FAAB budget 100.

---

## 4. Data layer

SQLite (`data/gridiron.db`), tables:

- `players` — player_id (nflverse gsis_id, PK), name, position, team, birthdate,
  status, espn_id
- `weekly_stats` — (season, week, player_id) PK; full offensive stat line plus
  `fantasy_points_half_ppr`
- `snap_counts`, `schedules`, `injuries`
- `odds_games` — event_id PK; consensus spread_home, total, moneylines, implied
  home/away totals. **No week column.**
- `adp` — (player_name, position) PK
- `projections` — (scope, season, week, player_id) PK; persisted projections with
  a JSON components blob
- `depth_charts` — player_id PK; current depth rank only, not a historical series
- `sync_log` — scope PK; last_synced, detail

Raw nflverse pulls are cached to `data/cache/*.parquet`, so re-syncs and offline
work are cheap and a failed fetch falls back to the cached parquet.

**Sync lanes fail independently** — a dead Odds API never blocks a stats refresh,
and seasons that don't exist yet are skipped per-season rather than sinking the
batch (`_pull_seasons` catches per year).

Two sync scopes matter operationally:

- `all` — `stats | odds | adp | espn`. A full stats pull walks three seasons of
  weekly data and takes minutes. Unusable mid-draft.
- `draft-day` — `depth | injuries | adp`, about 6 seconds. These are the only
  signals that move during camp and on draft night.

**Preseason box scores are deliberately not ingested.** nflverse's
`load_player_stats` offers only `week | reg | post | reg+post`; preseason isn't in
the feed. It is also a weak predictor. Depth chart position, injuries and ADP
drift are the preseason signals that do move value, and all three sync in the
`draft-day` scope.

`load_depth_charts` publishes a running series of snapshots keyed by `dt` (145
between March and August 2026); `sync_depth_charts` keeps only the most recent and
takes the best rank a player holds at his own position (players appear in several
formation groups).

**Odds.** `odds_games` carries no week and books price all 272 games by August, so
unscoped `game_lines()` returns 544 team-rows (32 teams × 17 weeks). Week is
recovered by joining `schedules` on `(season, home_team, away_team)`, which is
unique across 272 games with exact team-code agreement between feeds.

---

## 5. The projections engine (`models/projections.py`)

The single source of truth for player value. Three scopes; everything downstream
consumes these frames.

Design rules stated in the module: league settings always come from
`league_config()`; data-fetch is separated from math so the math is unit-testable
on plain frames; every adjustment degrades to a neutral 1.0 factor rather than
raising when odds or injuries are missing; every constant lives in one block
mirrored in `MODELING.md`.

### 5.1 `project_season(season)` — full-season half-PPR totals

For each player with weekly history in the last 3 completed seasons:

```
base_ppg     = Σ(recency_w × reliability × ppg) / Σ(recency_w × reliability)
                 recency_w    = (0.50, 0.30, 0.20) newest→oldest
                 reliability  = min(games, 17) / 17
proj_games   = 0.65 × weighted_games + 0.35 × 15.5,  clipped to [6, 17]
usage_trend  = clip(1 + 0.50 × (recent_opp_pg / prior_opp_pg − 1), 0.90, 1.10)
                 opportunities = attempts+carries (QB) else carries+targets
age_mult     = piecewise-linear position age curve vs peak 1.00
                 RB peaks 23–26, falls to 0.65 by 32
                 WR peaks 24–28, TE 25–29, QB 25–34
team_mult    = 0.96 if this year's team ≠ last-played team, else 1.0

adj_ppg      = base_ppg × usage_trend × age_mult × team_mult
proj_points  = adj_ppg × proj_games

season_sigma = sqrt(pergame_std² × proj_games + (adj_ppg × games_sigma)²)
                 × 1.15 if team changed
floor/ceiling = proj ∓ 0.6745 × season_sigma      (25th / 75th percentile)
```

Kickers and DSTs have no `weekly_stats`, so they get anchored one-variable models
driven by Vegas. K ppg `= 8.0 + 0.45 × (own implied total − 22.9)`, floored at 4.
DST ppg `= 7.0 + 0.55 × (22.9 − mean opponent implied total)`, floored at 3. The
ADP table supplies only the *universe* (which kickers and defenses exist and their
teams) — ADP is echoed into components for display but is **not** an input.

### 5.2 `project_week(season, week)` — matchup-adjusted single week

```
base    = 0.60 × season adj_ppg + 0.40 × mean fp over last 4 games this season
          (form blend only when the player has current-season games)
proj    = base × matchup_factor × injury_mult
```

`matchup_factor` is a shared function (§5.4). `injury_mult` maps `report_status`
→ Out 0.0, Doubtful 0.25, Questionable 0.92. Players whose team has no scheduled
game that week (bye) drop out entirely.

Weekly floor/ceiling use the season's `pergame_std` band, not the season sigma.

### 5.3 `project_ros(season, week)` — remaining schedule

```
proj_ros = base_ppg × Σ_{w=week..18} matchup_factor(w)
```

Walks each remaining week individually applying the full matchup chain and sums.
Byes drop out naturally. Each week normalizes its implied totals against its own
slate, exactly as `project_week` does, so the two stay comparable. Floor/ceiling
scale the season band by `games_left / 17`.

This is pinned by `test_ros_equals_sum_of_weekly_projections`.

### 5.4 `_matchup_factor` — the shared chain

```
factor = dvp × game_env × script × home_field

dvp       fantasy points that defense allows to that position per game ÷ league
          average, clipped [0.80, 1.20]. Current season if ≥4 weeks of data,
          else prior season. Missing key → 1.0.
game_env  offense:  clip(1 + 0.50 × (own implied/avg − 1), 0.85, 1.20)
          DST:      clip(1 − 0.50 × (opp implied/avg − 1), 0.85, 1.20)
script    clip(1 + coeff × (−spread/10), ±0.08)
          coeff: QB −0.04, RB +0.05, WR −0.03, TE −0.01   (offense only)
home      ×1.02 home, ×0.98 road
```

Two things are **deliberately excluded** from this function because neither
generalizes past a single week: recent form (a baseline choice, not a matchup
factor — ROS has no per-week form to blend) and injury status (a `report_status`
is only valid for the week it was filed; applying "Questionable" to all 12
remaining games would be nonsense). `project_week` applies injury on top itself.

Odds rows are matched to the schedule by opponent **and venue**. Matching on
opponent alone lets the wrong leg of a divisional home-and-home win — flipping
the spread sign and taking the wrong implied total for roughly 96 team-weeks.

Team abbreviations are normalized in one place (`TEAM_NORM = {"AZ": "ARI",
"LAR": "LA"}`); the `players` table uses `AZ` and FFC ADP uses `LAR` while
schedules/weekly_stats/odds use the canonical form.

### 5.5 Measured accuracy

Weekly MAE vs actual 2025 results, odds-free (a realistic worst case — 2025 odds
aren't loaded, so only DvP, form, home/away and injuries are exercised):

| Week | N | QB | RB | WR | TE | Overall |
|------|---|----|----|----|----|---------|
| 2025 wk 6  | 255 | 5.76 | 4.76 | 4.32 | 3.25 | 4.37 |
| 2025 wk 10 | 240 | 7.52 | 4.32 | 4.03 | 3.34 | 4.32 |
| 2025 wk 14 | 246 | 7.42 | 3.81 | 4.05 | 2.80 | 4.09 |

Typical public weekly systems land at 4–6 skill-position MAE.

---

## 6. VORP & draft board (`models/vorp.py`)

**Replacement level** is derived from the league's real starter + flex demand,
never hard-coded:

```
level[pos] = starters[pos] × teams
           + flex_slots × flex_share[pos]      flex_share: RB .45 / WR .45 / TE .10
replacement_points[pos] = proj_points of the level-th ranked player at that pos
vorp = proj_points − replacement_points[pos]
```

Team count is not cosmetic: a 10-team league ranks the same player lower than a
12-team one. `vorp_board(cfg=...)` takes the league config explicitly for this
reason.

**Tiers** are gap-based within a position: a tier break occurs where the drop to
the next player exceeds `1.8 × median within-position gap`. Position-relative, so
it adapts to each position's scale rather than using a fixed point threshold.

**ADP resolver** maps every FFC ADP row to a `player_id`. FFC carries names
without suffixes and DSTs as team names. The resolver strips accents,
punctuation and suffixes, matches on normalized name + position, disambiguates by
team then by recent activity, and falls back to a position-blind name match.
DSTs map to synthetic `DST_<team>` ids. Reported 100% match rate.

**`adp_delta = current_pick − adp`.** Positive means the player has *fallen*
(market says he should be gone; he's a value here). Negative means taking him now
is a *reach*.

**Roster-need score** for the drafting team:

```
need = 6.0 × unfilled_starter_slots_at_pos
     + 1.0 × max(vorp, 0) / 10
     − 4.0 if this pick stacks an existing starter's bye week
```

**Rationale string** built per row for the UI: tier, VORP, depth-chart rank *only
when > 1* (being the starter is the expected case and would be noise on every
line), falling/reach/ADP, bye stacking, "fills starter need".

`depth_rank` is left as NA when unknown so the UI can distinguish "not synced"
from "buried".

---

## 7. Draft room logic (`routers/draft.py`)

Draft state is a JSON file per league: `{"picks": [...], "my_slot": N}`.

**Snake attribution.** `_round_and_slot(overall, teams)` is the inverse of
`_snake_pick_numbers` — odd rounds run 1..teams, even rounds mirror. Every pick
entered is attributed to the team on the clock automatically. A `slot` on the
request body overrides it for trades or out-of-order entry.

Because every pick is attributed, **Rosters by team** shows what every opponent
has taken — which is what makes the recommendation aware of positional runs and
of your own remaining needs. Without attribution the app could only tell you a
player is gone, not who has him.

**Endpoints.**

| Route | Behavior |
|---|---|
| `GET /draft/board` | VORP board + current pick/round, on_the_clock, my_next_pick, last-8-picks positional run counts, tier depth per position, all team rosters |
| `POST /draft/pick` | Append a pick; 400 if already drafted or slot out of range |
| `POST /draft/undo` | Pop the last pick |
| `POST /draft/reset` | Clear state, optionally set `my_slot` |
| `POST /draft/simulate` | Run the room by ADP (§8) |
| `POST /draft/sync-espn` | Merge live ESPN picks |
| `GET /draft/recommendation` | Top pick by `(need_score, vorp)` + 4 alternatives |
| `GET /draft/my-roster` | My picks + remaining starter slots by position |

**ESPN draft sync** deliberately does *not* use `espn_api`'s `League.draft`: its
`_fetch_draft` early-returns unless `draftDetail.drafted` is true, and ESPN only
sets that flag once a draft has *finished* — useless for the case that matters.
`fetch_draft_picks` reads `draftDetail.picks` directly regardless of the flag.

The merge is idempotent: picks are keyed on overall number, so re-syncing
mid-draft only appends what's new, and a pick you typed manually is never
overwritten by the ESPN copy. ESPN team ids are arbitrary and unrelated to draft
position, so draft slot is derived from the order teams actually picked in round 1.
An ESPN failure returns 502, not 500.

**Caveat carried in `FINDINGS.md`:** the merge is well tested, but that ESPN
populates `draftDetail.picks` *incrementally during a live draft* has never been
verified against a real league — no credentials were configured at the time of
writing. The documented pre-draft step is to run an ESPN mock, make picks, and
confirm `added` climbs.

---

## 8. Mock draft simulation (`models/mocksim.py`)

Mock mode reads and writes `data/drafts/<id>.mock.json` — a throwaway copy — so
practising can never corrupt the board you draft for real. The failure mode being
guarded against is walking into draft day with a couple hundred phantom picks on
the board. A fresh mock inherits your real draft slot. `POST /draft/simulate`
defaults to `mock=True`; simulating into a real draft requires asking explicitly.

The room picks by ADP, because the point is to watch the board fall — not to model
opponent strategy. Two things keep it from being a straight ADP readout:

- **Noise.** Gaussian jitter added to ADP before sorting.
  σ = 2.0 (Chalk) / 8.0 (Realistic) / 20.0 (Chaotic) picks. Realistic measures at
  roughly a 5-pick spread around ADP.
- **Roster sanity.** Position caps (QB 2, TE 2, K 1, DST 1) and K/DST restricted
  to the last two rounds. Without these the tail of a mock is nonsense, because
  ADP says nothing about what a team already has.

ADP runs out before the draft does — 212 ranked players against 192 picks in a
12-team, 16-round league — so past the end of the list the room falls back to
VORP with jitter expressed in VORP terms.

Seedable, so a mock is reproducible and the tests are deterministic.

**Performance note in the code:** the board is built **once** and drafted players
are dropped from the pool as the sim runs, rather than rebuilt each pick.
Opponent choice reads only `pos`/`adp`/`vorp`, none of which depend on who has
already been taken; rebuilding 190 times would take minutes.

---

## 9. Waivers (`models/waivers.py`)

```
upgrade = ros_value(free agent) − ros_value(my worst droppable bench player)
score   = 1.0 × upgrade + 0.5 × next_week_value
```

Roster is split into projected starters vs bench by ROS value against the config's
slots; the worst bench player by ROS becomes the suggested drop.

**Breakout flags** from in-season usage trend, last 3 games vs the earlier games
of an 8-game window: target share +5pp, snap share +12pp, red-zone TD uptick.
Offseason (no current-season data) falls back to the season projection's
year-over-year `usage_trend ≥ 1.05`.

**Confidence** — High if `upgrade ≥ 30` *and* a breakout flag; Medium if
`upgrade ≥ 12`; else Low.

**FAAB bid** — `budget × clip(upgrade/60, 0, 0.55) × conf_mult`, where conf_mult
is High 1.0 / Medium 0.65 / Low 0.35, floored at $1 when score > 0.

Free-agent universe comes from the ESPN cache; if unavailable it falls back to the
top 150 undrafted-by-ADP players and surfaces a warning to the UI.

---

## 10. Start/sit optimizer (`models/lineup.py`)

Fills the config's labeled slots (QB, RB1, RB2, WR1, WR2, TE, FLEX, K, DST) to
maximize projected points, greedily: fill each fixed position with its top
projected player, then assign FLEX to the best remaining eligible.

**This is provably optimal for the loaded format** — position-exclusive slots are
independent, and a single FLEX can only improve by taking the best unused eligible
player. A brute-force enumeration in the module's smoke block verifies the greedy
result matches. (It would *not* be optimal for superflex or multi-flex; that's
documented as a limitation.)

Also reports bench, delta vs a supplied current lineup, per-player start
confidence from projection variance (`cv = band/proj`; ≤0.35 High, ≥0.65 Low),
and the two smallest-margin start/sit decisions as "close calls".

Roster players with no weekly projection (bye, out) get 0 points but remain
benchable.

---

## 11. API surface

```
GET  /api/health
GET  /api/config?league_id=
GET  /api/leagues                      -> active + [{id,name,teams,my_slot,espn_configured}]
POST /api/sync?scope=all|stats|odds|adp|espn|draft-day
GET  /api/sync/status

GET  /api/league/roster                POST /api/league/roster      (manual entry)
GET  /api/league/teams                 POST /api/league/my-team
GET  /api/league/players?search=&position=&limit=

GET  /api/draft/board?limit=&mock=
POST /api/draft/pick|undo|reset|simulate|sync-espn
GET  /api/draft/recommendation         GET /api/draft/my-roster

GET  /api/waivers/rankings?week=
GET  /api/lineup/optimal?season=&week=
GET  /api/dashboard
```

Every route except `/leagues` accepts `league_id`, defaulting to the configured
active league. Unknown league → 404. All DataFrame responses go through
`records()` which converts NaN/NaT → `null`, so **numeric row fields are
`number | null` throughout**.

`/api/dashboard` is an aggregate: it calls the roster, lineup, waivers, standings,
odds and sync-status paths internally, each wrapped in its own try/except so one
failure degrades that panel rather than the page.

---

## 12. Frontend architecture

**League scoping.** `LeagueProvider` resolves which league the app is showing.
The selection lives in `localStorage`, not on the server — `config/league.yaml` is
the user's to edit, and switching leagues in a browser shouldn't rewrite it. The
YAML `active:` key is only the default for a browser that hasn't chosen. If a
stored id has been removed from the YAML the provider adopts the server default,
since a stale id would 404 every request.

The API client holds the active league at **module level** so `request()` can
attach `?league_id=` without threading it through ~18 call sites. React Query keys
must therefore include the league id (`useLeagueKey()`) or a switch would serve
the previous league's cached data. Switching invalidates every query.

`withMock()` is kept **explicit per call** rather than module-level like the
league — mock is a mode of the draft room, not of the whole session, and
defaulting it globally is how a practice draft ends up in real state.

**Data fetching.** TanStack Query, `staleTime` 30s, `retry: 1`, no refetch on
window focus. Sync status polls every 60s. Mutations toast on success/error and
invalidate the relevant query keys.

**Error/empty/loading** are handled by three shared components (`ErrorState` with
a retry button, `EmptyState` with an inline "Run sync" action, `TableSkeleton` /
`CardSkeleton`), used consistently on every page.

---

## 13. Design system

A dark, single-theme "broadcast bug" system. `color-scheme: dark`; there is no
light mode.

**Type** — Oswald (condensed, uppercase, letter-spaced) for display/headings and
nav; Inter for body; JetBrains Mono for all numbers, IDs and micro-labels. A
`.tabular` utility applies `font-variant-numeric: tabular-nums` so numeric columns
don't jitter.

**Color** — twelve near-black `field-*` surface steps from `#08090b` to `#f3f5f7`,
one acid-green `hash-500` (`#c8ff3d`) accent, amber for money/attention states,
crimson reserved for OUT/critical only, sky blue for QB. Six tier colors ramp
green → yellow → orange → red → grey.

**Texture** — the body carries a faint radial green glow at the top and a
repeating 48px horizontal rule pattern at 1.2% opacity — yard lines.

**Motion / a11y** — `prefers-reduced-motion` kills all animation and transition
durations globally. `:focus-visible` gets a 2px hash-green outline at 2px offset.
Custom scrollbar styling.

**Badge vocabulary**, used identically everywhere:

- `PositionBadge` — bordered mono chip, color-coded per position
- `TierBadge` — solid tier-color chip, `T1`…`T6`, tooltipped
- `DepthBadge` — `RB1` / `WR2`; rank 1 green, rank 2 amber, 3+ grey, `—` when not
  synced, with distinct tooltips for each case
- `AdpDeltaBadge` — `▼ falling 12.0` in green, `▲ reach 9.0` in crimson, `even`
  within ±0.5
- `ConfidencePill` / `ConfidenceBar` — High green / Medium amber / Low crimson

---

## 14. Screen-by-screen UI/UX

### Shell

Sticky 14px-tall header, backdrop-blurred, max width 1400px. Left: "G" mark +
GridironGM wordmark. Center: four nav links (Dashboard / Draft Room / Waivers /
Start / Sit) — active link gets a raised surface and the green accent. Right:
league switcher (hidden and replaced by a label when only one league is
configured), a "last synced 12m ago" radio indicator, and a **Sync** button whose
icon spins while pending.

Toasts bottom-right, dark-themed to match tokens.

### Dashboard — "Week in Review"

One aggregate API call renders the whole page.

1. **Four stat tiles** — Optimal Total (accented green), Current Total, Delta
   ("Points left on the bench", signed), Roster Mode (Linked / Manual / None with
   a player count).
2. **Vegas Board** (2/3 width) — horizontal Recharts bar chart of the top 16
   implied team totals for the week the lineup is built for. Bars at ≥24 implied
   points render green, the rest grey. Scoped to one slate — unscoped, the same
   team appeared up to five times.
3. **Top Waiver Targets** (1/3 width) — five rows: position badge, name, NFL team,
   FAAB dollar figure, confidence pill.
4. **My Team** table (2/3) and **Standings** table (1/3).

Every panel has its own empty state with an actionable description.

### Draft Room — "Live Board"

The densest screen. Header description line reads:
`League 1 · 12 teams · pick 34 (rd 3) · 33 drafted · your next pick #40`.

Header actions: **Mock draft** toggle, **Update data** (the 6-second draft-day
sync, tooltipped "Refresh depth charts, injuries and ADP"), **ESPN** (only
rendered when that league has `espn_configured`), **Undo**, **Reset**.

Then, conditionally:

- **Mock banner** (amber) when mock is on: "Picks go to a throwaway copy — your
  real draft is untouched", plus a Room randomness selector (Chalk — near ADP /
  Realistic / Chaotic — big reaches), **Sim to my pick**, **Sim full draft**.
- **On the clock bar** — green-tinted when it's your pick, neutral otherwise.
  Carries a **Log pick to** dropdown ("Auto — Team 5", or any team, with "(you)"
  marked) for trades and out-of-order entry, plus a "Back to auto" escape.
- **Setup card** when no draft slot is set: numeric input 1–teams and a "Start
  draft" button.
- **Positional run banner** (amber) when any position appears ≥3 times in the last
  8 picks: "Positional run: 4 RB in last 8 picks".

Main two-column layout:

- **Recommendation hero** — gradient card, "On the clock" eyebrow, position and
  tier badges, large name, NFL team, depth badge, then a four-metric row
  (Proj / VORP / ADP / Bye), the generated rationale sentence, and a
  **Draft <FirstName>** button.
- **Four alternative cards** below it, each with its own Draft button.
- **Available Players** table — search box, position filter (ALL/QB/RB/WR/TE/K/DST),
  sortable headers (Tier / Proj / VORP / ADP), 640px scroll region. Columns:
  Player, Pos, Team, Depth, Tier, Proj, VORP, ADP, Trend (the ADP delta badge),
  Bye, Draft. The Draft button's variant changes when it's your pick, and its
  tooltip always names the team the pick will land on.
- **Right rail** — *Tier Depth*: per position, the best tier still on the board
  and how many players remain in it, as a bar that turns crimson at ≤1 and amber
  at ≤3. This is the draft-day cliff signal. Below it, *My Roster*.
- **Rosters by team** — full-width grid of every team's picks in order, your team
  outlined in green, the team on the clock outlined lighter.

### Waivers — "Free Agency"

Week selector (This week / Week 1–18). One table: Player (+NFL team), Pos, FAAB
(green dollar figure), Confidence pill, Breakout signals (green trending-up
badges), Suggested drop, chevron. **Clicking a row expands an inline rationale
line** — the full generated sentence ("ROS +34 over Bench Guy; nextwk 11;
target share rising; High confidence").

### Start / Sit — "This Week"

If no roster is connected: an empty state with an **Enter roster** button opening
a **manual roster dialog** — repeatable rows of name / position / team with an
autocomplete dropdown querying the player database, add/remove row controls, and a
save that warns about any names it couldn't match.

Otherwise: a delta banner ("Your current lineup leaves +7.3 points on the bench
vs. the optimal set", green-tinted when meaningful), then a two-column card
layout — **Optimal Lineup** as a 2-up grid of slot cards (slot label, name,
position badge, NFL team, projected points, confidence bar), **Bench** as
wrapping chips, and a **Close Calls** rail flagging the two thinnest decisions
with margins.

---

## 15. Testing

103 pytest tests across 10 files, all passing (64s):

| File | Focus |
|---|---|
| `test_projections.py` (20) | Pure-math helpers on plain frames; ROS = Σ weekly invariant |
| `test_mock_draft.py` (12) | Determinism under seed, position caps, K/DST windows, mock isolation |
| `test_vorp.py` (11) | Replacement levels vs team count, tiering, ADP resolution |
| `test_scoring.py` (9) | Scoring engine reproduces nflverse half-PPR |
| `test_espn_draft_sync.py` (8) | Idempotent re-sync, append-only, no dup players or overall numbers, slot from round-1 order, 502-not-500 |
| `test_api.py` (7), `test_depth_charts.py` (7), `test_leagues.py` (7) | Route contracts, depth snapshot selection, per-league config merge |
| `test_lineup.py` (5), `test_draft_attribution.py` (5) | Optimizer legality, snake attribution round-trip |

Frontend has **no tests**. Every defect in §17 sits at the API↔UI boundary, which
neither the backend suite nor `tsc` covers — see §17's preamble.

---

## 16. Known weaknesses (already identified in-repo)

From `docs/MODELING.md` §10 and `docs/FINDINGS.md`:

1. **No rookie / no-history projections.** Players with no `weekly_stats` rows get
   no projection at all and are therefore absent from every board. ADP already
   resolves for them, so a placeholder could be blended in. This is the largest
   functional gap for a draft tool.
2. **DST model ignores defensive history.** Only opponent implied total is used.
3. **K/DST ignore the market.** ADP is synced and 100% resolved but unused as a prior.
4. **FAAB assumes full budget remaining** — actual remaining budget should come
   from ESPN.
5. **Single-FLEX assumption in the optimizer.** Correct for the loaded config;
   superflex would need the assignment step extended.
6. **Every constant is unfitted.** All ~30 weights were chosen from domain
   reasoning, not fitted to the three seasons in the DB. The validation harness
   already scores a parameter set against held-out weeks, so a coarse sweep over
   the highest-leverage few (`WEEK_FORM_BLEND`, `IMPLIED_TOTAL_GAIN`, `DVP_CLIP`,
   `RECENCY_WEIGHTS`) is mechanical work with a measurable answer.
7. **No calibration against the market.** Nothing checks projections against ADP
   consensus, so an outlier passes silently — the current 2026 board has Bo Nix as
   QB2 (307.9) ahead of Hurts, Mahomes and Lamar. A "biggest disagreements vs ADP"
   report would surface these as either the model's edge or its bugs.
8. **ROS quality is now tied to odds freshness** since ROS started summing the
   per-week matchup chain — lines move, so `sync odds` should run before leaning
   on waiver rankings.
9. **ESPN live-draft sync is unverified against a real running draft** (§7).
10. **`web/src/lib/types.ts` is hand-written and drifts from the API.** Most
    interfaces end with `[key: string]: unknown`, which makes any property access
    typecheck. Seven field mismatches shipped at once because of this; only
    mismatches feeding a numeric formatter get caught, while name/string
    mismatches render blank and look like missing data. The durable fix is
    generating the file from `/openapi.json`.

---

## 17. Defects found and verified against the running app

Not previously documented. Items 1–7 were **verified empirically** against a live
API (`:8100`) and dev server (`:5173`) loaded with real data (8,751 players,
18,493 weekly stat rows, 272 odds events, ADP synced 2026-08-12). Evidence is
given per item. Items 8–11 are code-inspection observations, not yet verified.

**All 103 backend tests pass with every one of these defects present.** The
failure mode is uniformly at the API↔UI boundary, which nothing tests.

### 1. `current_total` and `delta` are always null — the Start/Sit page's whole premise is dead ⚠️ highest user impact

`lineup_model.optimize()` accepts a `current_lineup` argument, but
`routers/lineup.py:get_optimal` never passes it. The only caller that does is the
module's own `__main__` smoke block (`models/lineup.py:193`).

Verified — `GET /api/lineup/optimal`:
```
current_total: None | optimal_total: 123.38 | delta: None
```

Rendered result, confirmed in-browser: the Start/Sit banner reads
**"Your current lineup leaves — points on the bench vs. the optimal set."** Two of
the Dashboard's four stat tiles (Current Total, Delta) show `—` permanently.

The app never reads your *actual* set lineup from ESPN, so there is nothing to
compare the optimum against. This isn't a formatting bug; the comparison feature
described in the page's own subtitle ("Optimal lineup vs. your current lineup,
side by side") has no data path.

### 2. Player rows lose `team` between model and UI — confirmed in two places

`rank_free_agents` returns
`player_id, name, pos, ros_value, next_week_value, score, breakout_flags,
suggested_drop, suggested_drop_id, faab_bid, confidence, rationale`. No `team`.
`lineup.optimize`'s starter/bench records are
`player_id, name, position, opponent, proj_points, floor, ceiling, confidence`.
Also no `team`.

Verified:
```
waivers  row keys -> 'team' present: False   (144 rows)
lineup   starter  -> 'team' present: False
lineup   bench    -> 'team' present: False
dashboard top_waivers -> 'team' present: False
```

Rendered result, confirmed in-browser: every Waivers row reads
**"Jalen Hurts FA", "Ja'Marr Chase FA", "Christian McCaffrey FA"** — the
`{r.team ?? "FA"}` fallback firing on all 144 rows. Every Start/Sit slot card
shows `—` where the NFL team belongs.

### 3. Manual-roster dialog is unreachable, and its autocomplete is broken anyway

Two independent defects stacked:

**(a) Unreachable.** `ManualRosterDialog` is rendered only inside the
`rosterQuery.data?.mode === "none"` branch of `Lineup.tsx` (line 202). The current
roster mode is `"manual"` with 13 players, so the dialog cannot be opened at all.
Confirmed in-browser: no "Enter roster" button appears on Start/Sit. **Once you
save a manual roster there is no way to edit or clear it from the UI.**

**(b) Autocomplete broken.** `GET /api/league/players` returns an object:
```json
{"players":[{"player_id":"00-0036322","name":"Justin Jefferson",...}]}
```
but `PlayersResponse` in `types.ts` is typed as a bare array, and the dialog reads
`suggestQuery.data?.length` / `suggestQuery.data!.slice(0, 6)`. `.length` on the
object is `undefined`, so `(undefined ?? 0) > 0` is false and the dropdown never
renders. `npx tsc -b` **exits 0** — the mismatch is fully masked, exactly as
§16.10 describes.

### 4. Projection caches are never invalidated — a sync does not reach the model

`routers/draft.py:_season_proj_cache` is a plain module-level dict with no
deletion path, plus `@lru_cache` on `_weekly`, `_players`, `_snaps`,
`_completed_seasons` and `dvp_factors` in `projections.py`. The only `cache_clear`
calls in the codebase are in `config.reload_config()`, which clears config caches
only — and nothing calls it.

Verified behaviorally against a throwaway DB copy:
```
run 1: rows = 825 | top = Josh Allen 355.4
[DELETE FROM weekly_stats WHERE season=2025  -> 12,190 rows remain]
run 2: rows = 825 | top = Josh Allen 355.4      <- unchanged
[manual _weekly/_players/_snaps.cache_clear()]
run 3: rows = 702 | top = Lamar Jackson 381.7   <- materially different
```

Deleting an entire season of history changed nothing until the caches were cleared
by hand. In the running app this means **`POST /api/sync` refreshes the database
but the process keeps serving pre-sync projections until restart.**

Partial mitigation already present: `vorp.depth_map()` reads fresh on every board
build, so the draft room's "Update data" button *does* move depth ranks. New
weekly stats, players, snaps and DvP do not.

### 5. ADP is fetched once, globally, at the active league's team count

`etl/adp.py:sync_adp` calls `league_config()` with **no league id** — which
resolves to the YAML `active:` league, not to a shared default — and writes a
single global `adp` table via `replace_table`. The table has no league column
(`player_name, position, team, adp, adp_formatted, fetched_at`).

Verified:
```
active league: league1
sync_adp would request teams = 12
  league1   teams=12
  league2   teams=12
  league3   teams=10      <- scored against 12-team ADP
  league4   teams=10      <- scored against 12-team ADP
```

Two consequences. Half the configured leagues get the wrong market: ADP shifts
materially between 10- and 12-team formats, so QB/TE go later and RB/WR earlier in
shallower leagues. And the browser's league switcher has no effect here — it
writes to `localStorage`, while the ADP fetched depends solely on YAML `active:`.
Flipping `active:` to `league3` would give all four leagues 10-team ADP.

This contaminates `adp_delta` falling/reach flags, the K/DST universe, the waiver
ADP fallback, and mock-draft room ordering — all of which read the same table.

### 6. The Waivers page silently hides its own data-quality warning

`GET /api/waivers/rankings` returned:
```
warning: "ESPN free-agent cache unavailable — using ADP fallback"
```
`Waivers.tsx` contains **no reference to `warning`** — it is used only to decide
the empty state. Confirmed in-browser: the page presents Jalen Hurts, Ja'Marr
Chase and Christian McCaffrey as $55 waiver adds with High confidence and no
indication that the free-agent pool is a synthetic ADP fallback rather than the
league's actual free agents. This is the most misleading screen in the app —
plausible-looking, confidently wrong, with the caveat already computed and thrown
away one layer up.

### 7. Dashboard week is inconsistent between panels

`routers/dashboard.py` hard-codes `week=1` for the waivers call while the lineup
panel resolves `_default_week(season)`. Verified: dashboard reported
`lineup week: 1` — they agree today only because the season hasn't started.

### 8–11. Unverified code-inspection observations

8. **`lineup.optimize` and `waivers.rank_free_agents` call `league_config()` with
   no league id.** Harmless today because roster/scoring blocks are shared, but
   `league_config` explicitly supports per-league overrides of `roster` and
   `waivers`, so the multi-league story is inconsistent between the draft path
   (league-aware) and these two (not).
9. **DST/K `proj_games` is hard-coded to `FULL_SLATE = 17`** in
   `_project_k_dst_season` — the bye week comes from the team map but the
   availability model is flat.
10. **`sqlite3` connections are opened per call with no WAL or pooling**, and
    `replace_table` does a full `DELETE` + `to_sql` append inside one transaction.
    Fine for single-user local; worth confirming there's no re-entrancy hazard when
    the aggregate dashboard call fans out.
11. **Lineup and waiver responses omit `opponent` in the UI** even though
    `optimize` carries it — a matchup-adjusted projection is shown without the
    matchup that produced it.

---

## 18. What the review should focus on

Ranked by expected value to the user, who is one person drafting four leagues in
the next few weeks.

**Modeling.**
- Are the projection formulas sound, and are any of the ~30 constants obviously
  mis-signed or mis-scaled? (`SCRIPT_COEFF` for QB is negative — favored QBs throw
  less — is that right at this magnitude?)
- Is `base_ppg`'s reliability weighting double-counting availability, given
  `proj_games` already models it separately?
- Is the usage-trend ratio stable for low-volume players, where `prior_mean` can
  be near zero?
- Is the season-sigma formula correct? It adds per-game variance × games to
  (ppg × games_sigma)², which assumes independence between per-game scoring and
  availability.
- What is the highest-leverage addition: a rookie model, market calibration, or
  fitting the existing constants?

**Correctness.**
- §17.1–7 are verified defects, not hypotheses — the useful question is
  *sequencing and root cause*, not confirmation. Five of the seven are the same
  root cause (unchecked API↔UI contract). What is the smallest change that
  eliminates the class rather than the instances: generating `types.ts` from
  `/openapi.json`, adding response models to FastAPI, or a contract test?
- §17.1 (`current_lineup` never passed) needs a product decision, not just a fix:
  where would the "current" lineup come from? ESPN `lineup_slot` is already
  captured in `_player_dict` but never used.
- §17.4 — what's the minimal correct cache invalidation? Clear on sync, TTL, or
  drop the caches and make `project_season` cheap enough not to need them?
- Confirm or refute §17.8–11, which are inspection-only.

**UI/UX.**
- The draft room is the screen that matters under time pressure. Is the
  information hierarchy right — hero recommendation, then alternatives, then
  table, then tier depth? What would a reviewer move?
- Is anything critical missing from the board at a glance (bye conflicts against
  the current roster, positional scarcity across the room, "who's likely gone
  before my next pick")?
- Keyboard support is absent — no shortcuts for draft/undo, no focus management
  in the draft table. Worth it for draft day?
- The app is dark-only, dense, and heavily reliant on color to encode meaning
  (tier, depth rank, ADP delta, confidence). How does it hold up for a
  color-vision-deficient user? Every one of those encodings also carries text, but
  the tier ramp in particular is green→red.
- Mobile: the layout uses `xl:` breakpoints for the draft two-column split and
  the tables have no horizontal scroll containers. Is drafting from a phone a real
  use case that's currently broken?

**Architecture.**
- Is the multi-league seam in the right place? Config merge + per-league files +
  a module-level client variable — or should league be a path segment
  (`/api/leagues/{id}/draft/board`)?
- Is `dashboard.py` calling other routers' handler functions directly a problem,
  or a reasonable shortcut at this scale?
- The frontend has zero tests. What is the minimum test surface worth adding
  given the API-contract drift is the demonstrated failure mode?

**Explicitly out of scope.** This is a single-user local tool. Auth, rate
limiting, multi-tenancy, horizontal scaling, and production deployment concerns
are not applicable — please don't spend the review on them.
