# GridironGM Modeling Reference (Pass 2 — Opus analytical core)

This document specifies **every** formula, weight, constant, and assumption used by the
analytical core in `api/app/models/`. It is the companion to the code: constants live in
named `UPPER_CASE` module-level variables so this document and the implementation cannot
silently drift. If you change a number in code, change it here too.

All league settings (team count, scoring, roster slots, FLEX eligibility, FAAB budget)
are read from `config/league.yaml` via `league_config()` — **nothing here hard-codes them**.
Points are always **half-PPR** as produced by `scoring.py` / the precomputed
`weekly_stats.fantasy_points_half_ppr` column.

Modules:

| File | Public functions | Purpose |
|------|------------------|---------|
| `projections.py` | `project_season`, `project_week`, `project_ros` | Single source of truth for player value |
| `vorp.py` | `replacement_levels`, `replacement_points`, `resolve_adp`, `vorp_board` | Draft value over replacement + board |
| `waivers.py` | `rank_free_agents` | Waiver ranking, breakouts, FAAB |
| `lineup.py` | `optimize` | Weekly optimal legal lineup |

---

## 0. Data conventions & universal assumptions

- **History window.** 3 completed seasons (`league.history_seasons`), i.e. 2023–2025 when
  projecting 2026. Recency-weighted; see §1.1.
- **Regular season only.** All historical aggregation filters `week <= 18`
  (`REG_SEASON_WEEKS`). Weeks 19–22 are playoffs — only 14 teams participate, biasing
  per-game rates and availability, so they are excluded.
- **A "full slate" is 17 games** (`FULL_SLATE`) in an 18-week season (each team has one bye).
- **Player universe.** Season projections are produced for every QB/RB/WR/TE with at least
  one qualifying game in the window, plus K and DST via anchored models (§4). Players with no
  `weekly_stats` history (e.g. incoming rookies) get **no** statistical projection — this is a
  known limitation (§7).
- **Team abbreviation normalization.** `weekly_stats`, `schedules`, and `odds_games` use
  `ARI`/`LA`; the `players` table uses `AZ` and FFC ADP uses `LAR`. `norm_team()` maps
  `AZ→ARI`, `LAR→LA` so joins on team (schedule opponent, bye week, odds, DST id) never silently
  miss. Verified: these were the only two discrepancies across all four tables.
- **Graceful degradation.** Every matchup adjustment (odds, injuries, DvP) falls back to a
  neutral factor of `1.0` (or is skipped) when data is missing. No function raises on stale or
  absent odds/injury rows. This matters because odds are absent for historical validation weeks.
- **Odds coverage is now full-season.** As of the 2026-08-12 sync The Odds API prices the entire
  season — **272 events, week 1 through the Super Bowl**, all with a spread and a total. Earlier
  passes were written against a 75-event, weeks-1–6 slate; anything that assumed "odds only exist
  for the imminent slate" must be re-checked against this. See §2.3 (venue matching) and §10.4.

---

## 1. Season projection — `project_season(season)`

Per player, projected full-season half-PPR points, with `floor`/`ceiling` and a component
breakdown (stored as `components_json`). The pipeline:

```
proj_points = base_ppg × usage_trend × age_mult × team_mult × proj_games
```

### 1.1 Base per-game production (`base_ppg`)

For each of the ≤3 history seasons we compute the player's mean half-PPR points per game
(`ppg`) and games played (`games`). We combine seasons with a weight that multiplies
**recency** by **reliability**:

- **Recency weights** `RECENCY_WEIGHTS = (0.50, 0.30, 0.20)` for the newest, 2nd, 3rd season.
  Rationale: NFL production is highly non-stationary (role, scheme, teammates change yearly);
  the most recent season is the best single predictor, but two prior seasons meaningfully
  reduce single-season noise. 50/30/20 is the industry-standard recency split and is gentle
  enough to keep a strong prior season relevant.
- **Reliability weight** `= min(games, 17) / 17`. A season in which a player appeared in only
  a few games carries less signal about his true per-game rate, so it is down-weighted
  proportionally to sample size.

```
eff_weight(season) = recency(season) × min(games, 17)/17
base_ppg = Σ eff_weight × ppg  /  Σ eff_weight
```

`_weight_seasons(per_season, newest)` is a pure function (no DB) and is the primary unit-test
target.

### 1.2 Projected games (`proj_games`)

Availability is projected then regressed toward the league mean:

```
weighted_games = Σ recency × games / Σ recency          # over seasons present
proj_games = 0.65 × weighted_games + 0.35 × 15.5,  clipped to [6, 17]
```

- `GAMES_REGRESS_BLEND = 0.65`, `LEAGUE_MEAN_GAMES = 15.5`. Rationale: past availability is
  partly predictive (chronic injury history, durable workhorses) but regresses hard toward the
  mean — most "injury-prone" labels don't repeat. 15.5 is roughly the mean games for a player
  who appears in a season. A perfectly durable player projects to ~16.2 games (not 17), a
  fair haircut for the base-rate risk that *someone* misses time.

### 1.3 Usage trend (`usage_trend`)

Detects players whose opportunity is trending up or down. "Opportunities per game" (`opp_pg`):
`attempts + carries` for QB, `carries + targets` for RB/WR/TE.

```
ratio = opp_pg(most_recent_season) / mean(opp_pg over earlier seasons)
usage_trend = clip(1 + 0.50 × (ratio − 1),  0.90, 1.10)
```

- `USAGE_TREND_GAIN = 0.50`, `USAGE_TREND_MAX = 0.10`. Rationale: opportunity is the single
  most stable and predictive input in fantasy, and trajectory matters (a back seeing a rising
  snap/carry share is ascending into a role). We only pass through half the raw change (some
  of it is noise/opponent-driven) and cap the effect at ±10% so a single-season blip can't
  dominate the multi-year base. Requires ≥2 seasons of data, else neutral.

### 1.4 Age curve (`age_mult`)

Position-specific multiplier vs a peak of 1.00, linearly interpolated between anchors and flat
outside the range (`age_multiplier`, pure function). `age = season − birth_year`. Neutral 1.0
if birthdate unknown.

| Pos | Curve anchors (age: multiplier) |
|-----|---------------------------------|
| RB  | 21:0.95, 23–26:1.00, 27:0.96, 28:0.91, 29:0.85, 30:0.78, 32:0.65 |
| WR  | 21:0.90, 23:0.98, 24–28:1.00, 29:0.98, 30:0.95, 31:0.90, 33:0.80, 35:0.68 |
| TE  | 22:0.85, 24:0.96, 25–29:1.00, 30:0.97, 31:0.92, 32:0.86, 34:0.75 |
| QB  | 23:0.95, 25–34:1.00, 35:0.98, 36:0.95, 37:0.90, 39:0.80 |

Rationale: these mirror well-documented aging patterns — RBs peak early (23–26) and fall off a
cliff by ~28–30 (the "RB age cliff"); WRs have a broad prime (24–28) and decline gently; TEs
develop late and hold; QBs are effectively flat through their early/mid 30s and decline only
late. Curves are intentionally conservative (small year-over-year steps) to avoid over-fading a
still-productive veteran.

### 1.5 Team-change dampener (`team_mult`)

If the player's current (2026) team differs from the team of his most recent played season:

- `team_mult = 0.96` (`TEAM_CHANGE_DAMPEN`) — a modest haircut for role/scheme uncertainty and
  the historical tendency of movers to slightly underperform naive projection.
- Variance widened by `×1.15` (`TEAM_CHANGE_VAR_MULT`), reflecting genuinely higher outcome
  uncertainty (§1.6).

### 1.6 Floor / ceiling (25th / 75th percentile)

We model season-total uncertainty as the combination of game-to-game scoring variance and
availability (games-missed) variance:

```
season_sigma = sqrt( pergame_std² × proj_games  +  (adj_ppg × games_sigma)² )
season_sigma ×= 1.15   if team changed
floor   = max(0, proj − 0.6745 × season_sigma)
ceiling =        proj + 0.6745 × season_sigma
```

- `pergame_std` = pooled std of the player's per-game half-PPR points across all history games
  (falls back to `0.5 × adj_ppg` if <2 games).
- `games_sigma` = std of games-played across seasons (fallback `DEFAULT_GAMES_SIGMA = 2.5`).
- `0.6745` (`PCTL_Z`) is the normal z-score for the 25th/75th percentiles, so the band is a
  literal interquartile range under a normal approximation. The two variance terms capture the
  two real drivers of a season busting or booming: weekly volatility scaled by games, and the
  chance of missing time.

---

## 2. Weekly projection — `project_week(season, week)`

Starts from a per-game baseline and applies multiplicative matchup adjustments:

```
base   = (1 − 0.40) × season_proj_ppg + 0.40 × recent_form   # if recent form available
proj   = base × matchup_factor × injury
       = base × (dvp × game_env × script × home) × injury
```

**`_matchup_factor` is the single source of truth for the parenthesised chain.**
`project_week` applies it to one week; `project_ros` (§3) sums it over the remaining
schedule. Neither reimplements it, so rest-of-season is the weekly model integrated over the
schedule rather than a second model that can drift — pinned by
`test_ros_equals_sum_of_weekly_projections`, which asserts
`project_ros(w) == Σ project_week(w..18)` across all 825 projected players.

Two factors sit deliberately **outside** the shared chain because neither generalises past a
single week:

- **recent form** (§2.1) is a choice of *baseline*, not a matchup adjustment, and ROS has no
  per-week form to blend.
- **injury** (§2.6) — a `report_status` is only valid for the week it was filed. Applying a
  "Questionable" to all 12 remaining games would be nonsense, so `project_week` multiplies it in
  itself and ROS ignores it.

### 2.1 Baseline & recent form

`season_proj_ppg` is `proj_points / proj_games` from §1. If the player has games in the
current season within the last `WEEK_FORM_LOOKBACK = 4` weeks, we blend in that mean at
`WEEK_FORM_BLEND = 0.40`. Rationale: recent form carries real in-season signal (role changes,
health, hot streaks) but is noisy over a 4-game sample, so the stable multi-year baseline keeps
60% of the weight. In the offseason (no current-season games) `base = season_proj_ppg`.

### 2.2 Opponent defense-vs-position (`dvp`)

`dvp_factors(season, upto_week)`: for each (defense, position) we compute **total half-PPR
points that defense allows to that position per game**, divided by the league average for that
position, clipped to `DVP_CLIP = [0.80, 1.20]`.

```
dvp(team, pos) = clip( pts_allowed_per_game(team, pos) / league_avg(pos), 0.80, 1.20 )
```

Uses the current season up to `upto_week` if ≥4 weeks of data exist, otherwise the prior full
season. Missing key → 1.0. The ±20% clip prevents small-sample defensive extremes from
swinging a projection too far. Measured league averages allowed/game (2025): QB 16.3, RB 19.9,
WR 25.3, TE 10.7 — used implicitly via the ratio.

### 2.3 Game environment — Vegas implied team total (`game_env`)

```
offence:  game_env = clip( 1 + 0.50 × (own_implied      / league_avg_implied − 1),  0.85, 1.20 )
DST:      game_env = clip( 1 − 0.50 × (opponent_implied / league_avg_implied − 1),  0.85, 1.20 )
```

- `IMPLIED_TOTAL_GAIN = 0.50`: roughly half of a player's output is tied to how many points his
  team is expected to score (the rest is share/efficiency), so we pass through half the
  team-total deviation.
- **DST inverts, and keys off the opponent.** A defense scores *more* when it faces a weak
  offense, so the sign flips and the input is the opponent's implied total, not its own — the
  same direction as the season DST model (§4). Through Pass 3 the weekly model applied the
  offensive form to DSTs, i.e. it rewarded a defense for playing on a high-scoring team, exactly
  backwards. That was a 1-game error before; once §3 began summing this factor over the whole
  remaining schedule it would have compounded ~17×, so it is corrected here and pinned by
  `test_dst_scales_inversely_with_the_opponent_implied_total`. K keeps the offensive form (more
  scoring drives → more FG/XP attempts), which was already right.
- `league_avg_implied`: mean implied total across the week's matched odds if present, else
  `LEAGUE_AVG_IMPLIED = 22.9` (measured mean of both sides across the full 2026 odds slate is
  **22.83**, so the constant is accurate to within noise).
- Odds are matched to a team **only if** the odds row's opponent equals the scheduled opponent
  for that week **and the venue agrees** (`_odds_by_team`). Venue is not optional: with the whole
  season priced, every divisional home-and-home yields two rows with an identical
  `(team, opponent)` key. Matching on opponent alone let the later row win, so **96 team-weeks
  silently took the wrong leg** — flipped spread sign and the opponent's implied total. Adding
  `is_home` makes the key collision-free across all 544 team-game lines (verified: 0 collisions),
  and `tests/test_projections.py` pins both the synthetic home-and-home case and a whole-slate
  invariant that no accepted odds row contradicts the schedule's venue.

### 2.4 Game script — spread (`script`)

Being a favorite (negative spread) shifts run/pass balance:

```
script(pos) = clip( 1 + coeff(pos) × (−spread / 10),  0.92, 1.08 )
coeff = { QB: −0.04, RB: +0.05, WR: −0.03, TE: −0.01 }   # SCRIPT_COEFF, SCRIPT_MAX = 0.08
```

Rationale: heavy favorites run more late to bleed clock → RBs gain, pass-catchers/QB give back
a little; heavy underdogs are forced to pass → QB/WR gain, RB loses. Effect capped at ±8% and
only applied when a spread is available.

### 2.5 Home/away (`home`)

`× (1 + 0.02)` at home, `× (1 − 0.02)` on the road (`HOME_FIELD = 0.02`). Small, consistent
with the modern ~1–2 point home-field edge distributed across a roster.

### 2.6 Injury dampening (`injury`)

From the `injuries.report_status` for that exact (season, week):
`INJURY_MULT = { Out: 0.0, Doubtful: 0.25, Questionable: 0.92 }`. Anything else / missing →
1.0. Rationale: "Out" is a true zero; "Doubtful" players rarely play and are heavily faded;
"Questionable" is mostly precautionary in the modern game (~80–90% play), so only a light 8%
haircut.

### 2.7 Weekly floor/ceiling

`floor/ceiling = proj ∓ 0.6745 × pergame_std` (per-game volatility from the season component).
Players on a bye or whose team isn't scheduled that week are omitted from the weekly frame.

---

## 3. Rest-of-season — `project_ros(season, week)`

Walks weeks `week..18` **individually**, applying the full shared matchup chain (§2) to each and
summing:

```
proj_ros = season_proj_ppg × Σ  matchup_factor(week)
                             w = week..18, weeks the team actually plays

matchup_factor(w) = dvp(opp, pos) × game_env(w) × script(w) × home(w)
```

`games_left` falls out of the walk — a bye is simply a week the team has no scheduled game.

**Why this changed.** The previous formula was `season_proj_ppg × mean(dvp) × games_left`,
written when The Odds API only priced the imminent slate, so there was nothing to look up for
week 12. Books now price all 272 games (§0), so every remaining week has a real spread and
implied total. The old formula is a strict special case of the new one: with no odds loaded,
every `game_env` and `script` collapses to 1.0 and the sum reduces to the DvP-only average
(times a home/away term that now cancels correctly across the schedule instead of being ignored
entirely). `test_ros_falls_back_to_dvp_only_without_odds` pins that reduction, so the
odds-free/historical path is unchanged in substance.

**Measured effect** on the 2026 board vs the old formula: mean absolute change **4.4%** (3.4
pts), max 12%; mean rank movement in the top 60 is **4.7 places**, max 20. Direction is what
Vegas information should produce — backs on strong favored offenses rise (Gibbs +32, Kyren
Williams +31, Cook +27, McCaffrey +23), backs on weak ones fall (Achane −36, Hall −24, Judkins
−23, Jeanty −21). This feeds waiver ranking (§6), where ROS carries `ROS_WEIGHT = 1.0`.

The floor/ceiling band is still the season band scaled by the remaining fraction
`games_left / 17`. `components` reports `sos` (mean DvP alone, as before), `matchup` (mean of the
full factor) and `weeks_priced` (how many remaining weeks had odds) so a projection can be
audited for how much of it is market-driven.

**Cost:** the per-week walk takes `project_ros(2026, 1)` from ~0.2s to **0.90s** for 825 players
(18 weeks × 825). `project_season` still dominates any request that calls both.

---

## 4. Kicker & DST models (§ simplified — no `weekly_stats` for these positions)

Kickers and defenses have **no** rows in `weekly_stats` (positions are QB/RB/WR/TE only), so they
are modeled purely from Vegas implied totals. Both project a full 17-game slate. **These are
deliberately simple, documented approximations.**

> **ADP is the universe, not an input.** The ADP table only supplies *which* kickers and defenses
> exist (and their names/teams); `adp` is echoed into `components_json` but never enters the ppg
> formula. So the K ranking is exactly "whose team has the highest implied total" and carries none
> of the market's information about kicker quality or volume. See §10.6.

**Kicker** (per-game points scale with the team's own implied total — more scoring drives → more
FG/XP attempts):

```
K_ppg  = 8.0 + 0.45 × (team_implied_total − 22.9),  floored at 4.0
K_season = K_ppg × 17
```

`K_BASE_PPG = 8.0` (~ mean startable-kicker half-PPR/game), `K_IMPLIED_GAIN = 0.45`. When no
odds exist for the team, `team_implied_total` defaults to the league average (→ base 8.0/game).

**DST** (per-game points scale **inversely** with opponents' implied totals — weaker offenses
faced → more sacks/turnovers/low points-allowed):

```
DST_ppg  = 7.0 + 0.55 × (22.9 − mean_opponent_implied_total),  floored at 3.0
DST_season = DST_ppg × 17
```

`DST_BASE_PPG = 7.0`, `DST_IMPLIED_GAIN = 0.55`. `mean_opponent_implied_total` averages each
scheduled opponent's team implied total from odds where available, else the league average.
Floor/ceiling for both are simply `±20%` of the projection. DSTs are keyed by a synthetic id
`DST_<team>`; kickers resolve to their real `players` id when possible (else `K_<team>`).

**Known simplification:** we do **not** model historical per-defense sack/turnover rates
(the brief's suggested refinement) because opponent implied total already captures most of the
signal and no defensive box-score table is loaded. This is the single biggest place a future
pass could add accuracy.

---

## 5. VORP & draft board — `vorp.py`

### 5.1 Replacement levels (`replacement_levels`)

Replacement **rank** per position = number of that position rostered as startable across the
league, derived from config (never hard-coded):

```
level(pos) = starters(pos) × teams   +   (flex demand share, for flex-eligible pos)
flex_slots = starters(FLEX) × teams
flex_share = { RB: 0.45, WR: 0.45, TE: 0.10 }        # split of flex slots by typical usage
```

For the loaded 12-team config this yields **QB12, RB29, WR29, TE13, K12, DST12** — i.e. a
"replacement" RB is roughly the 29th-best RB (the caliber freely available on waivers). The
RB/WR-heavy, TE-light flex split reflects that flex slots are filled by RBs and WRs far more
often than TEs. `replacement_points()` then reads the projected points of the player at that
rank; VORP is measured against it.

### 5.2 VORP

```
vorp(player) = proj_season_points − replacement_points(player.position)
```

VORP, not raw points, is the draft currency: it makes a 290-pt WR (VORP +149) correctly worth
more than a 355-pt QB (VORP +85), because elite QB scoring is abundant relative to the single
QB start requirement while elite RB/WR scoring is scarce. Sample top of board (2026): Gibbs
+183, Bijan +178, Chase +149 — matching consensus early-round value.

### 5.3 Tiers (`assign_tiers`)

Gap-based clustering **within** each position (sorted by projection). A new tier begins where
the drop to the next player exceeds `TIER_GAP_MULT = 1.8 ×` the median within-position gap.
Rationale: tiers should reflect real talent cliffs, not fixed bucket sizes; scaling the
threshold to each position's own median gap makes it self-calibrating (a position with a flat
distribution gets few tiers; one with clear cliffs gets many). This is the actionable draft
signal — "only two players left in this tier, reach now."

### 5.4 ADP resolver (`resolve_adp`) — **100% match rate**

FFC ADP names lack suffixes and use accents/punctuation inconsistently, and DSTs appear as team
names. The resolver:

1. **DST** → synthetic id `DST_<norm_team>` directly.
2. **Offense / K** → normalize name (`_norm`: strip accents via NFKD, remove `.'` punctuation
   and Jr/Sr/II–V suffixes, lowercase) and match against the normalized `players` name filtered
   to the expected position. Ties are broken by (a) team, then (b) preferring players with
   recent `weekly_stats` activity. Falls back to a position-agnostic name match, then `none`.

Measured on the loaded 164-row ADP: **164/164 (100%)** resolved — 149 exact, 14 DST, 1 by
position disambiguation, 0 unmatched.

### 5.5 Roster-need score (`need_score`)

For the drafting team, per candidate:

```
need = 6.0 × unfilled_starter_slots(pos)  +  1.0 × max(vorp, 0)/10  −  4.0 × bye_stack?
```

- `NEED_UNFILLED_W = 6.0` per still-open starter slot at the player's position (dominant term —
  drafting for need is worth a lot early).
- `NEED_SCARCITY_W = 1.0` on `vorp/10` — rewards raw scarcity/value.
- `NEED_BYE_PENALTY = 4.0` when the player's bye week coincides with a bye already stacked by an
  existing starter (avoids crippling a single week).

`adp_delta = current_pick_context − adp` — **how many picks past his ADP a player is still
available**. Positive means he has *fallen* (the market says he should already be gone, so he is
value here); negative means taking him now is a *reach*. The sign is user-facing:
`web/src/components/TierBadge.tsx` renders `delta > 0` as "▼ falling" and `delta < 0` as
"▲ reach". (This was inverted through Pass 3 — every player was labelled "falling" at pick 1,
including a TE with ADP 45. Fixed and pinned by `tests/test_vorp.py`.)

The one-line `rationale` surfaces tier, VORP, ADP with a falling/reach note past ±8 picks, bye
stacking, and "fills starter need" — which now keys off the position's **unfilled starter slot
count**, not the composite `need_score`. The old test (`need_score >= NEED_UNFILLED_W`) was
satisfied by the scarcity term alone for any VORP ≥ 60, so a high-value player at an
already-filled position falsely claimed to fill a need.

---

## 6. Waivers — `rank_free_agents` (`waivers.py`)

For each free agent, compute rest-of-season value (`project_ros`) and next-week value
(`project_week`), then:

```
upgrade = fa_ros_points − worst_bench_ros_points          # value gained by the swap
score   = 1.0 × upgrade + 0.5 × next_week_value           # ROS_WEIGHT, NEXT_WEEK_WEIGHT
```

- **Suggested drop** = my lowest-ROS-value **non-starter**. My roster is split into projected
  starters (fills the config's fixed slots then FLEX by ROS value) vs bench; the worst bench
  player is the drop candidate, so we never suggest dropping a starter.
- `NEXT_WEEK_WEIGHT = 0.5`: ROS value dominates (you hold players for the season), but next-week
  value breaks ties and surfaces one-week streamers.

### 6.1 Breakout flags (`_breakout_flags`)

In-season usage trend, last `BREAKOUT_LOOKBACK = 3` games vs earlier games of the season:

- **"target share rising"** if Δ target_share ≥ `TARGET_RISE = 0.05` (+5pp).
- **"red-zone TD uptick"** if Δ (rush+rec TD) per game ≥ `RZ_RISE/3` — red-zone proxy, since no
  goal-line-carry column exists.
- **"snap share rising"** if Δ offense snap % ≥ `SNAP_RISE = 0.12` (+12pp).

Offseason fallback (no current-season games): flag **"usage trending up (YoY)"** if the season
projection's `usage_trend ≥ USAGE_TREND_FLAG = 1.05`. Rationale: rising snaps/targets/red-zone
work are the leading indicators of a breakout before the box score catches up.

### 6.2 FAAB bid & confidence

```
base_pct   = clip(upgrade / 60, 0, 0.55)                  # FAAB_FULL_BID_GAP, FAAB_MAX_SHARE
confidence = High  if upgrade ≥ 30 and a breakout flag
             Medium if upgrade ≥ 12
             Low    otherwise
faab_bid   = round(remaining_budget × base_pct × conf_mult)   # {High:1.0, Med:0.65, Low:0.35}
```

A ~60-point ROS upgrade justifies a near-max share of budget; the confidence multiplier keeps
speculative adds cheap. `remaining_budget` defaults to `waivers.faab_budget` (100) — actual
remaining budget would come from ESPN sync; documented assumption. Any positive-score add bids
at least 1.

---

## 7. Lineup optimizer — `optimize` (`lineup.py`)

Fills the config's starter slots to maximize total `project_week` points. Slots are expanded
from config (`QB, RB1, RB2, WR1, WR2, TE, FLEX, K, DST` for the loaded config); FLEX accepts any
`flex_eligible` position.

**Algorithm & optimality proof.** Fixed positional slots are mutually exclusive and independent,
so assigning each position its top-N projected players maximizes that position's contribution.
The single FLEX then takes the best remaining flex-eligible leftover — it can never improve the
total to move an already-optimal fixed starter to the bench in favor of a lower-projected FLEX.
Therefore greedy-per-position-then-FLEX is optimal for a single FLEX. The `__main__` block
**verifies this against brute-force enumeration** (`_brute_force_best`) — they match exactly.
(For multiple heterogeneous FLEX/superflex slots this greedy would need extending to a small
assignment/enumeration; not required by the current config.)

**Outputs (`LineupResult`):** optimal `slots`, `bench` (sorted by projection), `total`,
`current_total`/`delta` vs a supplied current lineup, per-player start **confidence**, and the
two closest **close_calls**.

- **Confidence** from projection coefficient of variation `cv = (ceiling−floor)/2 / proj`:
  High if `cv ≤ 0.35`, Low if `cv ≥ 0.65`, else Medium. Tighter distributions are safer starts.
- **Close calls** = the two slots with the smallest margin between the chosen starter and the
  best benched alternative eligible for that slot — the decisions most worth a second look.
- Roster players with no weekly projection (bye/out) are scored 0 and remain benchable rather
  than crashing the optimizer.

---

## 8. Validation

### 8.1 Sanity (season projections, 2026)

Top season projections land in the expected half-PPR ranges: elite RBs Gibbs 320 / Bijan 316,
elite WRs Chase 291 / St. Brown 269 / Nacua 267 (target 250–350 ✓), QB1 Josh Allen 355 with a
QB1 cluster 280–355 (target 350–420, top end ✓). Elite TE McBride 217. Replacement levels and
VORP order match draft consensus (Gibbs/Bijan/Chase atop the board).

### 8.2 Weekly accuracy — `project_week` vs actual 2025 results

Mean absolute error (half-PPR points) of the weekly projection vs actual, for all players who
both were projected and played. Note these validation weeks have **no odds** (2025 odds aren't
loaded), so only DvP, recent form, home/away, and injuries are exercised — a realistic
worst-case for the model:

Re-measured on the 2026-08-12 data refresh (nflverse had revised some 2025 lines; MAE moved by
≤0.1 anywhere, so the model is stable under the revision):

| Week | N | QB | RB | WR | TE | Overall |
|------|---|----|----|----|----|---------|
| 2025 wk 6  | 255 | 5.76 | 4.76 | 4.32 | 3.25 | 4.37 |
| 2025 wk 10 | 240 | 7.52 | 4.32 | 4.03 | 3.34 | 4.32 |
| 2025 wk 14 | 246 | 7.42 | 3.81 | 4.05 | 2.80 | 4.09 |

Overall MAE ~4.0–4.3 points is competitive with public weekly projection systems (typical
skill-position MAE 4–6). QB MAE is higher (~6–7) as expected given QBs' larger scoring range and
the boom/bust of rushing-QB games. TE MAE is lowest (~3) reflecting their compressed range.

### 8.3 Optimizer

The greedy optimal lineup equals the brute-force optimum on the smoke roster (2025 wk 10), and
produces a plausible fantasy lineup (Allen QB; Taylor/Achane RB; London/Jefferson WR; Bowers TE;
Cook FLEX; Myers K; Seattle DST) with sensible close calls (Jefferson over St. Brown by 0.2,
Cook over Jacobs by 0.3).

---

## 9. Constants index (all tunables in one place)

| Constant | Value | §  | Meaning |
|----------|-------|----|---------|
| `REG_SEASON_WEEKS` | 18 | 0 | Regular-season week cutoff |
| `FULL_SLATE` | 17 | 0 | Games a fully-available player plays |
| `RECENCY_WEIGHTS` | (0.50,0.30,0.20) | 1.1 | Newest→oldest season weights |
| `GAMES_REGRESS_BLEND` | 0.65 | 1.2 | Weight on player's own availability |
| `LEAGUE_MEAN_GAMES` | 15.5 | 1.2 | Availability regression target |
| `USAGE_TREND_GAIN` / `_MAX` | 0.50 / 0.10 | 1.3 | Usage-trajectory sensitivity / cap |
| `TEAM_CHANGE_DAMPEN` / `_VAR_MULT` | 0.96 / 1.15 | 1.5–1.6 | Team-change haircut / variance widen |
| `PCTL_Z` | 0.6745 | 1.6 | 25th/75th-pctile z-score |
| `DEFAULT_GAMES_SIGMA` | 2.5 | 1.6 | Availability std fallback |
| `WEEK_FORM_BLEND` / `_LOOKBACK` | 0.40 / 4 | 2.1 | Recent-form weight / window |
| `DVP_CLIP` | [0.80,1.20] | 2.2 | Defense-vs-position bounds |
| `IMPLIED_TOTAL_GAIN` | 0.50 | 2.3 | Share of output tied to team total |
| `GAME_ENV_CLIP` | [0.85,1.20] | 2.3 | Team-total factor bounds |
| `LEAGUE_AVG_IMPLIED` | 22.9 | 2.3 | Mean implied team total |
| `SCRIPT_COEFF` | QB−.04,RB+.05,WR−.03,TE−.01 | 2.4 | Spread→position response |
| `SCRIPT_MAX` | 0.08 | 2.4 | Game-script cap |
| `HOME_FIELD` | 0.02 | 2.5 | Home/road adjustment |
| `INJURY_MULT` | Out0/Dbt.25/Q.92 | 2.6 | Injury-status dampeners |
| `K_BASE_PPG` / `K_IMPLIED_GAIN` | 8.0 / 0.45 | 4 | Kicker anchor / implied sensitivity |
| `DST_BASE_PPG` / `DST_IMPLIED_GAIN` | 7.0 / 0.55 | 4 | DST anchor / implied sensitivity |
| `TIER_GAP_MULT` | 1.8 | 5.3 | Tier-break gap multiplier |
| `flex_share` | RB.45/WR.45/TE.10 | 5.1 | Flex demand split |
| `NEED_UNFILLED_W`/`SCARCITY_W`/`BYE_PENALTY` | 6.0/1.0/4.0 | 5.5 | Roster-need weights |
| `ROS_WEIGHT` / `NEXT_WEEK_WEIGHT` | 1.0 / 0.5 | 6 | Waiver score blend |
| `BREAKOUT_LOOKBACK` | 3 | 6.1 | Breakout trend window |
| `SNAP_RISE`/`TARGET_RISE`/`RZ_RISE` | 0.12/0.05/1.0 | 6.1 | Breakout thresholds |
| `USAGE_TREND_FLAG` | 1.05 | 6.1 | Offseason breakout threshold |
| `FAAB_FULL_BID_GAP`/`MAX_SHARE` | 60/0.55 | 6.2 | FAAB sizing |
| `CONF_MULT` | High1/Med.65/Low.35 | 6.2 | FAAB confidence multiplier |
| `CONF_HIGH_CV`/`LOW_CV` | 0.35/0.65 | 7 | Lineup confidence CV bounds |

---

## 10. Known limitations / future work

1. **No rookie/no-history projections.** Players without `weekly_stats` rows (incoming rookies)
   get no statistical projection. A rookie model (draft capital + landing spot + ADP prior)
   would fill this; ADP is already resolved for them, so a placeholder could be blended in.
2. **DST model ignores historical defensive rates.** Only opponent implied total is used; adding
   per-defense sack/turnover/points-allowed history (a dedicated table would be needed) is the
   biggest accuracy lever for DST.
3. **FAAB assumes full budget remaining.** Actual remaining budget should come from ESPN sync.
4. ~~**ROS leaves full-season odds on the table.**~~ **Done (2026-08-12)** — `project_ros` now
   sums the shared per-week matchup factor over the remaining schedule (§3). Note this means ROS
   quality is now tied to odds freshness: re-run `sync odds` before leaning on waiver rankings,
   since lines move. Historical validation still runs odds-free (2025 odds aren't loaded), so it
   exercises the fallback path.
5. **Single-FLEX assumption in the optimizer.** Correct for the loaded config; superflex or
   multi-flex formats would require extending the assignment step (documented in §7).
6. **K/DST ignore the market and their own history.** §4's models are one-variable; ADP is
   present but unused, and no defensive box-score table is loaded. Blending the ADP rank as a
   prior would cost nothing (the data is already synced and 100% resolved).
7. **Constants are unfitted.** Every weight in §9 was chosen from domain reasoning, not fitted to
   the 3 seasons in the DB. The validation harness in §8.2 already scores a parameter set against
   held-out weeks, so a coarse sweep over the highest-leverage few (`WEEK_FORM_BLEND`,
   `IMPLIED_TOTAL_GAIN`, `DVP_CLIP`, `RECENCY_WEIGHTS`) is mechanical work with a measurable
   answer. Fit on 2023–24, score on 2025, or the MAE will be optimistic.
8. **No calibration against the market.** Nothing checks projections against ADP consensus, so an
   outlier passes silently — the current 2026 board has Bo Nix as QB2 (307.9) ahead of Hurts,
   Mahomes and Lamar. A "biggest disagreements vs ADP" report would surface these as either the
   model's edge or its bugs, and is the fastest way to find the next one.
