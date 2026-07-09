# Pass 2 Spec — Analytical Core (Opus)

Implement `api/app/models/{projections,vorp,waivers,lineup}.py` plus `docs/MODELING.md`.
Read `docs/PLAN.md`, `api/app/db.py` (schema), `api/app/config.py`, `api/app/scoring.py`,
and `api/app/etl/odds.py` (`game_lines()`) first. Real data is loaded in `data/gridiron.db`
— validate every module against it as you go.

## Ground rules

- League settings ALWAYS from `league_config()` — never hard-code 12 teams, 0.5 PPR,
  lineup slots, FAAB budget, etc.
- pandas throughout; return DataFrames with the exact columns PLAN.md specifies.
- Store projections via the `projections` table helpers (`upsert_rows`) with scope
  `season` | `week` | `ros`; components (the "why") as JSON in `components_json`.
- Every formula, weight, and assumption goes in `docs/MODELING.md` with rationale.
- It is currently the 2026 offseason: `weekly_stats` holds 2023–2025 (2026 empty until
  week 1). Models must work in BOTH modes: draft prep (season projections from history
  only) and in-season (weekly, with current-season data + odds).
- Odds may be stale/missing for a given week — degrade to neutral adjustments, never crash.

## projections.py (single source of truth)

- `project_season(season)` — per-player projected half-PPR points for the coming season:
  3-year weighted per-game production (recency-weighted, e.g. 50/30/20 with justification),
  games-played regression, usage-trend adjustment (target share / carry share trajectory,
  snap %), age curve by position, role/team-change dampener. Include `floor`/`ceiling`
  (e.g. 25th/75th percentile outcomes) and per-player component breakdown.
- `project_week(season, week)` — baseline from recent form + season projection, adjusted by:
  opponent defense-vs-position (schedule + weekly_stats derived), Vegas implied team total
  vs league-average scaling, spread-based game-script factor (pass-lean vs run-lean),
  home/away, injury status dampening (Questionable/Doubtful/Out).
- `project_ros(season, week)` — remaining-schedule aggregate of weekly logic (strength of
  schedule at position level is enough; don't fetch per-week odds for future weeks).
- K and DST: simple defensible models (K: team implied total driven; DST: opponent implied
  total inverted + historical sack/turnover rates). Document simplifications.

## vorp.py

- `replacement_levels(cfg)` — replacement rank per position derived from starters+flex
  demand across `teams` teams (e.g. QB12, RB~30, WR~30, TE12, K12, DST12 in this format —
  derive from config, don't hard-code).
- `vorp_board(drafted_ids, my_roster)` — undrafted players with proj, VORP (proj minus
  replacement-level proj), tier (gap-based clustering within position), bye week, ADP,
  adp_delta (ADP minus current overall pick context), roster-need score (positional
  scarcity + my unfilled starters + bye stacking penalty), one-line rationale string.
- ADP names need fuzzy matching to players table (FFC names lack suffixes; DSTs are
  team names) — implement a small resolver, document match rate.

## waivers.py

- `rank_free_agents(free_agents, my_roster, season, week)` — score = blend of ROS value
  over my droppable worst player and next-week value; breakout flags from usage trends
  (snap % trend, target share trend, red-zone proxy = TDs + goal-line carries trend);
  suggested drop = my lowest-ROS-value non-starter; FAAB bid = share of remaining budget
  scaled by value gap and confidence tier (High/Medium/Low); rationale string.

## lineup.py

- `optimize(roster_ids, season, week)` — legal lineup per config starters (QB/RB/RB/WR/WR/
  TE/FLEX/K/DST); FLEX from config `flex_eligible`. Exact optimization (assignment by
  descending adjusted projection with FLEX resolved correctly — enumerate or greedy-with-
  proof, document). Return: optimal slots, bench, current-vs-optimal delta if current
  lineup given, per-player confidence (projection variance based), top-2 `close_calls`
  (smallest-margin decisions).

## Testing hooks (Sonnet writes tests later — make these easy)

Pure functions where possible; separate data-fetch from math (e.g. `_weight_seasons(df)`
testable without DB). Add a `__main__` smoke block in each module that runs against the
real DB and prints a top-10 sanity table.

## Definition of done

Each module runs its smoke block without error against data/gridiron.db; projections
look sane (elite RB/WR ~ 250–350 half-PPR pts; QB1s ~ 350–420); MODELING.md documents
every constant. Do NOT touch etl/, routers/, web/, or config files.
