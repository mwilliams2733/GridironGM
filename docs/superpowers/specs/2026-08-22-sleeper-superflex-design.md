# Sleeper platform support and superflex / full-PPR modeling

**Date:** 2026-08-22
**Status:** approved design, not yet implemented
**Supersedes:** nothing. Extends `docs/MODELING.md` §4, §5.1, §7 and closes §10.2, §10.3, §10.6.

---

## 1. Goal

Add a fifth league — **Sundt Redraft**, a 12-team full-PPR **superflex** redraft on
**Sleeper** — to GridironGM, and make its valuations correct rather than
approximate.

The league's draft is complete (204 picks, status `in_season`). The surfaces that
matter for it are **Start/Sit** and **Waivers**. Draft-room support is out of scope.

Two properties of the league drive everything:

1. **Superflex.** A second QB starts every week, so up to 24 QBs are startable
   across 12 teams. QB replacement level moves from QB12 to QB23.
2. **Full PPR with −1 interceptions.** The app's stored points column is half-PPR
   with −2 interceptions, baked at ingest. It cannot serve both leagues.

## 2. Scope

**In scope**

- Per-league scoring applied at read time, replacing the single stored points column
- `SUPER_FLEX` in replacement levels and the lineup optimizer
- Real K and DST scoring from ingested statistics, replacing the Vegas-anchored models
- A Sleeper ETL module (rosters, users, matchups, player map)
- Four in-path defects: `current_lineup`, missing `team` on rows, the hidden Waivers
  warning, projection cache invalidation
- Real remaining FAAB, closing `MODELING.md` §10.3

**Out of scope** — deliberate, not overlooked

Sleeper draft-room support; trade valuation; IR/taxi handling; in-game substitution
logic; bench-lock notifications; the unreachable manual-roster dialog (`REVIEW-BRIEF.md`
§17.3), since Sleeper syncs the roster directly; rookie projections (`MODELING.md` §10.1);
constant fitting (§10.7).

## 3. Prerequisite — already completed 2026-08-22

Per-league scoring requires recomputing points from stored components. That was
blocked: `weekly_stats.interceptions` was 100% NULL across all 18,493 rows because
nflverse renamed the column to `passing_interceptions` and the permissive keep-filter
in `sync_weekly_stats` dropped it silently.

Fixed by extracting `normalize_weekly()` as a pure function, adding the rename, and
adding `SCORING_INPUT_COLUMNS` with tests that fail when any scoring input is dropped.
Verified: **18,533 / 18,533 rows (100.00%)** agreement between `score_offense` computed
from components and the stored column, max diff 0.000, with a sensitivity check
confirming the comparison can still fail (35.01% under a mutated reception value).

**This equivalence is the licence to delete the stored column.** Do not proceed with
§4.1 if it ever stops holding.

## 4. Architecture

### 4.1 Scoring becomes a data-driven term table

One canonical representation of scoring rules, consumed by every scorer.

```python
# api/app/scoring.py
def scoring_terms(cfg: dict) -> dict[str, float]:
    """Canonical stat column -> points per unit.

    {"passing_yards": 0.04, "passing_tds": 4, "interceptions": -2,
     "receptions": 0.5, "receiving_yards": 0.1, ...}

    Yardage divisors in league.yaml (`yards_per_point: 25`) become multipliers
    (0.04) here so every rule is one multiply.
    """

def score_offense(stats: Mapping, cfg: dict | None = None) -> float:   # row-wise
def score_frame(df: pd.DataFrame, cfg: dict | None = None) -> pd.Series  # vectorized
```

`score_offense` keeps its current signature so existing tests and callers are
unaffected. Both scorers consume `scoring_terms()`, so the rules exist once.

**Tiered points-allowed is not a multiplier** and stays a separate lookup,
`points_allowed_score(pa, cfg)`. The term table covers linear terms only. Document
this at the function.

### 4.2 Scoring profiles key the caches

```python
def profile_key(cfg: dict) -> str:
    """Stable hash of the scoring block. Leagues with identical scoring share one."""
```

The four ESPN leagues hash identically, so caches hold **two** entries, not five.

Profile-keyed: `_weekly`, `dvp_factors`, `_recent_form`, `project_season`,
`project_week`, `project_ros`. **Not** profile-keyed: `_snaps` and `_players`, which
return usage and biographical data with no scoring applied — adding the key there would
double the cache for identical results.

### 4.3 Config

Two additions to `config/league.yaml`. Both fit the existing merge logic: `league_config()`
already merges per-league overrides of `scoring`, `roster` and `waivers`, so no merge
code changes.

```yaml
leagues:
  - id: sundt
    name: "Sundt Redraft"
    platform: sleeper            # NEW; espn is the default when absent
    teams: 12
    sleeper_league_id: "1395500725785591808"
    sleeper_draft_id: "1395500726964195328"
    draft: { rounds: 17 }
    waivers: { system: faab, faab_budget: 200 }
    scoring:                     # per-league override, canonical nested shape
      receiving: { reception: 1.0, yards_per_point: 10, touchdown: 6, two_point: 2 }
      passing:   { yards_per_point: 25, touchdown: 4, interception: -1, two_point: 2 }
      # Remaining blocks (rushing, misc, kicking, dst) are a mechanical translation
      # of all 43 Sleeper keys listed in ~/Downloads/sundtredraftleaguecontext.md §11,
      # re-verified live in §9.2. Notable differences from the ESPN block:
      #   pass_int -1 (not -2); rec 1.0 (not 0.5); fum_rec +2 (new category);
      #   ff / st_ff / st_fum_rec (new); pts_allow ladder 10/7/4/1/0/-1/-4.
      #   fgm_60p is a Sleeper BONUS on top of fgm_50p and is 0, so a 60-yarder
      #   scores 5 — map it to the same tier as 50-59, matching current behaviour.
    roster:
      # Sleeper spells team defense DEF; the app uses DST throughout. Normalize
      # DEF -> DST at the ETL boundary (etl/sleeper.py), never in the models.
      starters: { QB: 1, RB: 2, WR: 2, TE: 1, FLEX: 1, SUPER_FLEX: 1, K: 1, DST: 1 }
      flex_slots:
        FLEX:       { eligible: [RB, WR, TE],     share: { RB: .45, WR: .45, TE: .10 } }
        SUPER_FLEX: { eligible: [QB, RB, WR, TE], share: { QB: .90, RB: .04, WR: .04, TE: .02 } }
      bench: 7
      ir: 2
```

**One config shape, not two.** Sleeper's flat `scoring_settings` is translated by hand
into the canonical nested shape when authoring the YAML. The canonical schema is
extended to cover the union of both platforms' categories (forced fumbles, split
special-teams categories, own-fumble recovery).

To prevent silent drift if the commissioner edits scoring, `sync_sleeper` compares live
`scoring_settings` against the configured block and **logs a warning on mismatch**. This
is a sync-time check, not a test, because it requires network.

`platform` defaults to `espn` when absent, so the four existing leagues need no edit.

### 4.4 Data model

**Removed:** `weekly_stats.fantasy_points_half_ppr`. It is a stored derivative of
columns we now keep in full; retaining it invites exactly the drift §3 proved absent.
`dvp_factors` and `_recent_form` currently `SUM`/`AVG` it in SQL and move to scoring in
pandas — both already go through `read_df`, so this is a local change.

**Added:**

```sql
CREATE TABLE kicking_stats (            -- K rows, currently filtered out at ingest
    season INTEGER, week INTEGER, player_id TEXT, team TEXT, opponent TEXT,
    fg_made_0_19 REAL, fg_made_20_29 REAL, fg_made_30_39 REAL,
    fg_made_40_49 REAL, fg_made_50_59 REAL, fg_made_60_ REAL,
    fg_missed REAL, pat_made REAL, pat_missed REAL,
    PRIMARY KEY (season, week, player_id)
);
CREATE TABLE team_defense (             -- from nflreadpy load_team_stats
    season INTEGER, week INTEGER, team TEXT, opponent TEXT,
    def_sacks REAL, def_interceptions REAL, def_fumbles_forced REAL,
    fumble_recovery_opp REAL, def_tds REAL, def_safeties REAL,
    def_punt_blocks REAL, def_pat_blocks REAL, def_fg_blocks REAL,
    special_teams_tds REAL, points_allowed REAL,
    PRIMARY KEY (season, week, team)
);
```

Separate tables rather than widening `weekly_stats`, which would add ~15 columns that
are NULL for 18k offensive rows.

`schedules` gains `home_score` and `away_score` (present in `load_schedules`, currently
discarded) to derive `points_allowed`.

### 4.5 Replacement levels with SUPER_FLEX

`vorp.replacement_levels` currently hardcodes one FLEX slot and a module-level
`flex_share` dict. Generalise to iterate `roster.flex_slots`:

```
level[pos] = starters[pos] * teams
           + Σ over flex slots: starters[slot] * teams * share[slot][pos]
```

When `flex_slots` is absent, synthesise the current FLEX behaviour from
`flex_eligible` plus today's shares, so ESPN league numbers do not move.

Sundt result: **QB23, RB30, WR30, TE13, K12, DEF12.**

QB23 sits inside the QB18–24 target in the league context document. WR30 differs from
that document's WR36; WR36 is reachable only if the FLEX slot goes ~100% to WR.
Decision: **derive it — WR30**, keeping one flex-share rule across all five leagues. The
share is an unfitted constant in `MODELING.md` §10.7's sense and is now config, so it
can be revised without code changes.

### 4.6 Lineup optimizer

`lineup._slot_labels` hardcodes `("QB","RB","WR","TE","FLEX","K","DST")` and would
silently drop a `SUPER_FLEX` starter, producing a 9-man lineup that ignores the second QB.

Replace with a config-derived list of `(label, eligible_positions)` sorted by **ascending
eligibility breadth**: fixed positions (1) → FLEX (3) → SUPER_FLEX (4).

Fill order is load-bearing. With two slots left and players WR 25, QB 20:

| Order | Result |
|---|---|
| FLEX first | FLEX←WR(25), SUPER_FLEX←QB(20) = **45** |
| SUPER_FLEX first | SF←WR(25), FLEX has no eligible player = **25** |

Because `FLEX ⊂ SUPER_FLEX`, the eligibility family is laminar and restrictive-first
greedy is optimal on it. **Demonstrate rather than assert:** generalise
`_brute_force_best` from its hardcoded nested loops to a recursive assignment search over
arbitrary slot definitions, and pin greedy against it for both configs, including the
case above. This extends the verification pattern `lineup.py` already ships.

### 4.7 K and DST models

Both currently use one-variable Vegas anchors (`K_BASE_PPG = 8.0`, `DST_BASE_PPG = 7.0`)
because no statistics were ingested. With §4.4 they move to the same treatment as offence:

```
per-week points = score_kicker / score_dst over the new tables, league term table
base_ppg        = projections._weight_seasons(per_season_frame, newest)
proj            = base_ppg * matchup_factor    # existing shared chain, unchanged
```

Reuse `_weight_seasons` rather than writing a parallel aggregation: it already applies
`RECENCY_WEIGHTS`, reliability weighting and games regression, and reusing it means K/DST
cannot drift from how offence is aggregated. `usage_trend` is meaningless for a team
defense — pass `opp_pg` as the per-game points so the trend term degrades to a neutral
1.0, or extend `_weight_seasons` with a flag; the implementer should pick whichever keeps
`_weight_seasons` honest for its existing callers.

`scoring.score_kicker` already reads exactly the column names `load_player_stats`
returns (`fg_made_0_19` … `fg_made_60_`, `pat_missed`) — it has been correct and
unreachable because K rows are filtered out at ingest. It needs no changes beyond
consuming the term table.

For DST, points allowed comes from the opponent's score in `schedules`, and each league's
own tier ladder applies — so Sleeper's much more generous ladder (shutout 10 vs 5) is
handled with no second engine.

Retire `K_BASE_PPG`, `K_IMPLIED_GAIN`, `DST_BASE_PPG`, `DST_IMPLIED_GAIN` and
`_project_k_dst_season`'s ADP-universe dependency. The Vegas signal is **not** discarded:
it remains the matchup factor on top of a historical baseline, exactly as offence works.

Closes `MODELING.md` §10.2 and §10.6.

### 4.8 Sleeper ETL

`api/app/etl/sleeper.py`, mirroring `etl/espn.py` so routers barely change:

```python
sleeper_available(league_id) -> bool
sync_sleeper(league_id)      -> dict     # rosters, users, matchups -> per-league JSON cache
sleeper_player_map()         -> dict     # cached /v1/players/nfl, refreshed daily
fetch_draft_picks(league_id) -> list     # present but unused this pass
```

Read-only REST, no authentication, no cookies. Rate limit is 1000 calls/minute; a full
sync is under 10 calls plus the player map. The player payload is ~14 MB and is cached to
`data/cache/sleeper_players.parquet` on the existing `_cached_pull` pattern.

The existing `league_cache_dir(league_id)` mechanism is reused unchanged.

Router dispatch: `routers/league.py`, `routers/waivers.py` and `routers/lineup.py`
currently call `espn_etl` directly. Introduce a thin `etl/platform.py` that dispatches on
`cfg["league"]["platform"]` and exposes `get_my_roster`, `read_cache` and
`free_agents` with the shapes those routers already consume.

**Sleeper has no free-agent endpoint.** Unlike ESPN's `league.free_agents(size=200)`,
the pool must be derived: all players in the Sleeper player map with
`active == True` and `position in (QB, RB, WR, TE, K)`, plus all 32 team defenses, minus
the union of every roster's `players`. That set is large (~3,000), so cap it the way the
ESPN path is capped — rank by projection and keep the top 200 — and record the cap in the
response `warning` rather than truncating silently (`REVIEW-BRIEF.md` §16, "no silent caps").

### 4.9 Rest-of-season horizon

`project_ros` sums weeks `week..18` unconditionally. In a league whose playoffs start
week 15, weeks 15–17 pay out only if you qualify and week 18 never pays out at all.
Because waiver rankings are ROS-driven, the uncapped horizon systematically overvalues
players with strong late schedules — in the surface this spec exists to make correct.

**Decision: cap the horizon at the last scoring week of the fantasy regular season.**

```python
def last_scoring_week(cfg: dict) -> int:
    """Final week that counts toward making the playoffs.

    Absent `playoff_week_start`, returns REG_SEASON_WEEKS so behaviour is unchanged
    — which is what keeps the four ESPN leagues byte-identical (§6 step 3 gate).
    """
    start = (cfg["league"].get("playoff_week_start") or REG_SEASON_WEEKS + 1)
    return min(REG_SEASON_WEEKS, start - 1)
```

`project_ros` then walks `range(max(week, 1), last_scoring_week(cfg) + 1)`. Sundt sets
`playoff_week_start: 15`, giving weeks `week..14`. The ESPN leagues do not set it and are
unaffected.

This is deliberately the simple option. Weighting weeks 15–17 by playoff probability
would be more correct and is a modelling project, not a config change; it is not
attempted here. Note the ESPN leagues almost certainly have the same distortion — setting
`playoff_week_start` on them is a one-line follow-up once this is proven, but it is out of
scope because it would break the step 3 equivalence gate.

`project_ros` currently takes no `cfg`; it gains one alongside the `profile_key` thread
from §4.2.

### 4.10 Player identity

Sleeper ids are its own numeric strings; DEF arrives as a bare team abbreviation.
Resolution cascade:

1. `gsis_id` from the Sleeper player payload when present
2. `espn_id` against `players.espn_id`
3. `resolve_player_name(name, position)` from `routers/_common.py`

DEF: `"HOU"` → `DST_HOU`, matching the synthetic ids the models already use.

Measured on all 194 rostered players in this league (§9.3): 100% resolved, 0 collisions,
0 position mismatches, and unanimous agreement with `gsis_id` on all 33 players where
both paths exist. Counter-intuitively, `gsis_id` alone covers only 17% — Sleeper
populates it mostly for pre-2020 entrants — so name matching is the primary path and
`gsis_id` is a confirmation, not the reverse.

Where the two sources disagree on a player's **team** (2 of 194: Travis Hunter, Marvin
Harrison), prefer Sleeper's value for Sleeper leagues; it is the league's own view.

## 5. Defect fixes folded in

| Defect | Fix | Closes |
|---|---|---|
| `current_total`/`delta` always null | Sleeper `roster["starters"]` is 10 ids ordered to match `roster_positions`; ESPN has `lineup_slot` already captured and unused. `get_optimal` passes `current_lineup` to `optimize`. | `REVIEW-BRIEF.md` §17.1 |
| `team` missing from rows | Add to waiver rows and lineup starter/bench records. | §17.2 |
| Waivers warning discarded | Render `warning` on the Waivers page. | §17.6 |
| Projection caches never cleared | Clear at the end of `run_sync`. Mandatory now that keys include the scoring profile. | §17.4 |
| FAAB assumes full budget | `roster["settings"]["waiver_budget_used"]` gives real remaining. | `MODELING.md` §10.3 |

## 6. Rollout order

Each step leaves the app working and testable.

1. `scoring_terms` + `score_frame`; `score_offense` refactored onto the term table. No
   behaviour change; existing tests must stay green.
2. `profile_key`; thread through `projections.py` caches. Still one profile in use.
3. Drop `fantasy_points_half_ppr`; move `dvp_factors` and `_recent_form` to pandas
   scoring. **Gate:** ESPN projections byte-identical to today.
4. `flex_slots` config + generalised `replacement_levels`. **Gate:** ESPN replacement
   levels unchanged.
5. Generalised `_slot_labels` + `_brute_force_best`; superflex optimizer tests.
6. `kicking_stats`, `team_defense`, `schedules` scores; new K/DST models.
7. `etl/sleeper.py` + `etl/platform.py` dispatch; register the `sundt` league.
8. ROS horizon cap (§4.9) + the five fixes in §5.
9. Re-run `MODELING.md` §8.2 MAE and update the document.

## 7. Testing

Unit, no DB — matching the split `projections.py` documents and `test_weekly_ingest.py`
now follows:

- `scoring_terms` → `score_offense` and `score_frame` agree row-for-row
- half-PPR term table reproduces nflverse standard + reception value (already pinned)
- `replacement_levels` for both configs: ESPN QB12 unchanged; Sundt QB23/RB30/WR30/TE13
- superflex greedy == generalised brute force, including §4.6's ordering case
- `_slot_labels` from config produces 10 slots for Sundt, 9 for ESPN
- Sleeper roster payload → resolved ids, from a captured fixture
- K and DEF normalization guards, mirroring `SCORING_INPUT_COLUMNS`
- `last_scoring_week`: 14 for a config with `playoff_week_start: 15`, 18 when absent;
  `project_ros` on a Sundt-shaped config returns `games_left` counting no week > 14

**Not converted to a test:** the §3 equivalence proof, which needs a synced 18k-row DB.
This project deliberately removed DB-dependent tests (commit `f946c2a`). The unit-level
invariant in `test_weekly_ingest.py` covers the same property.

## 8. Risks and open items

1. **K/DST accuracy is unmeasured.** Moving off the anchors is a genuine model change with
   no baseline. Step 9 must establish one; if history-driven DST is worse than the anchor,
   keep the anchor and revisit.
2. **Offensive MAE should not move** — §3 proves scoring equivalence — but §8.2 must be
   re-run to confirm rather than assumed.
3. **`SUPER_FLEX` QB share (0.90) is unfitted**, like every constant in `MODELING.md` §9.
   It is config, so revising it costs nothing.
4. **Sleeper `scoring_settings` can change** if the commissioner edits them. Mitigated by
   the sync-time warning in §4.3, not eliminated.
5. **The `players` table and Sleeper disagree on team** for 2 of 194 players. Handled by
   preferring Sleeper for Sleeper leagues; a broader reconciliation is out of scope.
6. **RESOLVED — ROS horizon.** Capped at `playoff_week_start - 1` per §4.9 (week 14 for
   Sundt). The ESPN leagues do not set `playoff_week_start`, so they are unchanged and the
   step 3 equivalence gate holds. They likely carry the same distortion; correcting them
   is a deliberate follow-up, not part of this work.

## 9. Verified facts

Everything below was measured on 2026-08-22, not assumed.

### 9.1 Scoring equivalence

18,533 / 18,533 rows (100.00%) agreement, max diff 0.000. Sensitivity check: mutating the
reception value to 0.6 drops the match rate to 35.01%, so the comparison can fail.

Before the interceptions fix: 95.18%, with 892 mismatches (886 QB, 5 WR, 1 TE), every diff
a positive multiple of 2.0. Total mismatch magnitude 2,508 points = 1,254 interceptions ×
2, reconciling exactly with the interception count now stored.

### 9.2 Sleeper league, read live from the API

`league_id 1395500725785591808`, `draft_id 1395500726964195328`, 12 teams, 43 scoring keys,
`roster_positions` = QB/RB/RB/WR/WR/TE/FLEX/SUPER_FLEX/K/DEF + 7 BN, `waiver_budget` 200,
`playoff_week_start` 15, `position_limit_qb` 4. Scoring values in the league context
document at `~/Downloads/sundtredraftleaguecontext.md` §11 were verified against the live
API and match exactly.

Note `playoff_week_start: 15` means the fantasy regular season is weeks 1–14 and week 18
is worthless in this league. Addressed by the horizon cap in §4.9.

### 9.3 Player identity bridges, measured on 194 rostered players

| Bridge | Coverage |
|---|---|
| `gsis_id` | 33 / 194 (17.0%) |
| `espn_id` | 59 / 194 (30.4%) |
| name + position | **194 / 194 (100.0%)** |

Cross-validation: 33 agree, 0 disagree. Position mismatches: 0. Collisions: 0.
Team mismatches: 2 (data freshness, not identity).

### 9.4 Roster under test

Shonuff3612, `user_id 1396235398669176832`, roster_id 12, draft slot 10, 17 picks:
Barkley, Jefferson, Pickens, Prescott, Judkins, LaPorta, Metcalf, Darnold, Golden, Pierce,
Gadsden, Shakir, Houston DEF, Kamara, Myers, Dell, Kupp. Composition 8 WR / 3 RB / 2 QB /
2 TE / 1 K / 1 DEF — notably thin at QB for a superflex format, which the new replacement
levels should reflect.
