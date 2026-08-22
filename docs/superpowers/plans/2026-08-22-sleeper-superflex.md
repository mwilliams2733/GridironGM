# Sleeper Support and Superflex/Full-PPR Modeling — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Sundt Redraft — a 12-team full-PPR superflex league on Sleeper — as a fifth league, with correct valuations rather than approximations.

**Architecture:** Scoring moves from a single column baked at ingest to a data-driven term table applied at read time. Raw stat frames are cached league-agnostically and scored on demand. Flex slots and replacement levels become declarative config so SUPER_FLEX is expressible. K and DST move off Vegas anchors onto ingested statistics. A new Sleeper ETL mirrors the ESPN one behind a platform dispatch.

**Tech Stack:** Python 3.11, pandas 2.3, numpy 2.4, FastAPI, SQLite, nflreadpy, httpx, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-22-sleeper-superflex-design.md` — read it before starting. This plan argues from it.

## Global Constraints

- **No new dependencies.** scipy is NOT installed and must not be added.
- **Run tests from the `api/` directory:** `cd api && ../.venv/Scripts/python.exe -m pytest`. On this Windows box the venv python is at `.venv/Scripts/python.exe` from the repo root.
- **All 109 existing tests must stay green after every task.** Never commit red.
- **ESPN leagues must not change numerically** until Task 13. Tasks 1–12 preserve `league1`–`league4` behaviour exactly. This is the gate that makes the refactor safe.
- **League settings always come from `league_config()`.** Never hardcode scoring or roster values.
- **Derive, don't duplicate.** When two code paths must agree, make one call the other rather than asserting they match.
- **A test that still passes when you break the implementation is not a test.** Mutate the code a guard protects and confirm the test fails before calling it a guard.
- Canonical team abbreviations: `TEAM_NORM = {"AZ": "ARI", "LAR": "LA"}` in `projections.py`. Sleeper spells team defense `DEF`; the app uses `DST`. Normalize at the ETL boundary only.
- Sundt league ids: `sleeper_league_id "1395500725785591808"`, `sleeper_draft_id "1395500726964195328"`.

**Deliberate refinement of spec §4.2.** The spec says to key projection caches by scoring profile. Implement instead: cache the **raw** stat frame (league-agnostic, one entry per season) and apply scoring on demand, because `score_frame` over 18,533 rows is a vectorized pass costing milliseconds. This achieves §4.2's stated goal — caches do not multiply by league — with one cache rather than two and no registry to thread `cfg` into `lru_cache`d functions. Only `project_season` stays profile-keyed, since its per-player Python loop is the expensive part.

---

## File Structure

| File | Responsibility |
|---|---|
| `api/app/scoring.py` | MODIFY — term table, row-wise and vectorized scorers, profile key |
| `api/app/models/projections.py` | MODIFY — raw/scored split, ROS horizon, K/DST models |
| `api/app/models/vorp.py` | MODIFY — declarative flex slots in replacement levels |
| `api/app/models/lineup.py` | MODIFY — config-derived slots, generalized brute-force oracle |
| `api/app/etl/nfl_data.py` | MODIFY — stop baking points; ingest K and team defense |
| `api/app/db.py` | MODIFY — drop points column, add two tables, add schedule scores |
| `api/app/etl/sleeper.py` | CREATE — Sleeper REST client and cache |
| `api/app/etl/platform.py` | CREATE — dispatch roster/free-agent reads on `platform` |
| `config/league.yaml` | MODIFY — `platform`, `flex_slots`, `playoff_week_start`, Sundt entry |
| `api/tests/test_scoring_terms.py` | CREATE — term table and scorer equivalence |
| `api/tests/test_flex_slots.py` | CREATE — replacement levels and slot generation |
| `api/tests/test_sleeper.py` | CREATE — Sleeper payload parsing and identity cascade |
| `api/tests/fixtures/sleeper_roster.json` | CREATE — captured payload, no network in tests |

---

### Task 1: Scoring term table and vectorized scorer

Turns scoring rules into data so one implementation serves both the row-wise and bulk paths.

**Files:**
- Modify: `api/app/scoring.py`
- Test: `api/tests/test_scoring_terms.py` (create)

**Interfaces:**
- Consumes: `league_config()` from `api/app/config.py`
- Produces: `scoring_terms(cfg=None) -> dict[str, float]`, `score_frame(df, cfg=None) -> pd.Series`, `profile_key(cfg=None) -> str`. `score_offense(stats, cfg=None) -> float` keeps its existing behaviour and gains an optional second parameter.

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_scoring_terms.py`:

```python
"""Term-table scoring: one rule set, two scorers."""
from __future__ import annotations

import pandas as pd

from app.config import league_config
from app.scoring import profile_key, score_frame, score_offense, scoring_terms

STATS = {
    "passing_yards": 310, "passing_tds": 3, "interceptions": 2,
    "passing_2pt_conversions": 1, "rushing_yards": 41, "rushing_tds": 1,
    "rushing_2pt_conversions": 0, "receptions": 4, "receiving_yards": 52,
    "receiving_tds": 0, "receiving_2pt_conversions": 0,
    "rushing_fumbles_lost": 0, "receiving_fumbles_lost": 0,
    "sack_fumbles_lost": 1, "special_teams_tds": 0,
}


def test_yardage_divisors_become_multipliers():
    t = scoring_terms()
    assert t["passing_yards"] == 1 / 25
    assert t["rushing_yards"] == 1 / 10
    assert t["receiving_yards"] == 1 / 10


def test_reception_value_comes_from_config():
    t = scoring_terms()
    assert t["receptions"] == league_config()["scoring"]["receiving"]["reception"]


def test_all_three_fumble_columns_share_the_fumble_value():
    t = scoring_terms()
    lost = league_config()["scoring"]["misc"]["fumble_lost"]
    for c in ("rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost"):
        assert t[c] == lost


def test_score_frame_agrees_with_score_offense_row_for_row():
    """The invariant the bulk path depends on. Derive, don't duplicate."""
    df = pd.DataFrame([STATS, {**STATS, "receptions": 9, "passing_tds": 0}])
    bulk = score_frame(df)
    for i in range(len(df)):
        assert bulk.iloc[i] == score_offense(df.iloc[i].to_dict())


def test_score_frame_treats_missing_columns_as_zero():
    df = pd.DataFrame([{"receptions": 4, "receiving_yards": 52}])
    expected = score_offense({"receptions": 4, "receiving_yards": 52})
    assert score_frame(df).iloc[0] == expected


def test_score_frame_treats_nan_as_zero():
    df = pd.DataFrame([{**STATS, "receiving_yards": float("nan")}])
    expected = score_offense({**STATS, "receiving_yards": 0})
    assert score_frame(df).iloc[0] == expected


def test_a_different_scoring_block_gives_a_different_profile_key():
    cfg = league_config()
    import copy
    full_ppr = copy.deepcopy(cfg)
    full_ppr["scoring"]["receiving"]["reception"] = 1.0
    assert profile_key(cfg) != profile_key(full_ppr)


def test_profile_key_is_stable_across_calls():
    assert profile_key() == profile_key()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_scoring_terms.py -q`
Expected: FAIL — `ImportError: cannot import name 'scoring_terms' from 'app.scoring'`

- [ ] **Step 3: Write the implementation**

In `api/app/scoring.py`, add imports at the top (`hashlib`, `json`, `pandas as pd`) and add these functions. Then rewrite `score_offense` to consume the term table.

```python
def scoring_terms(cfg: dict | None = None) -> dict[str, float]:
    """Canonical stat column -> points per unit.

    Yardage divisors in league.yaml (`yards_per_point: 25`) become multipliers
    (1/25) so every rule is a single multiply and the row-wise and vectorized
    scorers can share one table.

    LINEAR TERMS ONLY. Tiered defensive points-allowed is not expressible as a
    multiplier and stays in `points_allowed_score`.
    """
    s = (cfg or league_config())["scoring"]
    p, r, rec, misc = s["passing"], s["rushing"], s["receiving"], s["misc"]
    fum = misc["fumble_lost"]
    return {
        "passing_yards": 1.0 / p["yards_per_point"],
        "passing_tds": p["touchdown"],
        "interceptions": p["interception"],
        "passing_2pt_conversions": p["two_point"],
        "rushing_yards": 1.0 / r["yards_per_point"],
        "rushing_tds": r["touchdown"],
        "rushing_2pt_conversions": r["two_point"],
        "receptions": rec["reception"],
        "receiving_yards": 1.0 / rec["yards_per_point"],
        "receiving_tds": rec["touchdown"],
        "receiving_2pt_conversions": rec["two_point"],
        "rushing_fumbles_lost": fum,
        "receiving_fumbles_lost": fum,
        "sack_fumbles_lost": fum,
        "special_teams_tds": misc["return_touchdown"],
    }


def score_offense(stats: Mapping, cfg: dict | None = None) -> float:
    """Score a QB/RB/WR/TE stat line. Keys follow nflverse weekly column names."""
    return round(sum(_num(stats, k) * v for k, v in scoring_terms(cfg).items()), 2)


def score_frame(df: pd.DataFrame, cfg: dict | None = None) -> pd.Series:
    """Vectorized `score_offense` over a whole frame.

    Same term table as the row-wise scorer, so the two cannot drift. Columns the
    frame lacks contribute zero, matching `_num`'s treatment of missing keys.
    """
    terms = scoring_terms(cfg)
    cols = [c for c in terms if c in df.columns]
    if not cols:
        return pd.Series(0.0, index=df.index)
    mat = df[cols].apply(pd.to_numeric, errors="coerce").fillna(0.0)
    weights = pd.Series({c: terms[c] for c in cols}, dtype=float)
    return (mat * weights).sum(axis=1).round(2)


def profile_key(cfg: dict | None = None) -> str:
    """Stable short hash of a scoring block. Identical scoring -> identical key."""
    s = (cfg or league_config())["scoring"]
    blob = json.dumps(s, sort_keys=True, default=str).encode()
    return hashlib.sha1(blob).hexdigest()[:12]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_scoring_terms.py tests/test_scoring.py -q`
Expected: PASS — 8 new plus the 9 existing scoring tests. The existing tests must pass **unchanged**; if any fails, the term table has a wrong value.

- [ ] **Step 5: Mutation-test the equivalence guard**

Temporarily change `score_frame`'s `.round(2)` to `.round(1)`. Run the tests. `test_score_frame_agrees_with_score_offense_row_for_row` must FAIL. Revert.

If it passes, the test isn't comparing what it claims to.

- [ ] **Step 6: Run the full suite**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest -q`
Expected: 117 passed (109 + 8).

- [ ] **Step 7: Commit**

```bash
git add api/app/scoring.py api/tests/test_scoring_terms.py
git commit -m "Make scoring a data-driven term table shared by both scorers"
```

---

### Task 2: Split raw and scored weekly reads

Separates the league-agnostic database read from league-specific scoring, so one cached raw frame serves all five leagues.

**Files:**
- Modify: `api/app/models/projections.py:113-124` (`_weekly`), `:444-472` (`dvp_factors`), `:484-492` (`_recent_form`)
- Test: `api/tests/test_projection_scoring.py` (create)

**Interfaces:**
- Consumes: `score_frame`, `scoring_terms` from Task 1
- Produces: `_weekly_raw(season) -> pd.DataFrame` (lru_cached, no `fp` column), `_weekly(season, cfg=None) -> pd.DataFrame` (adds `fp`), `dvp_factors(season, upto_week, cfg=None)`, `_recent_form(season, week, cfg=None)`

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_projection_scoring.py`:

```python
"""Raw reads are league-agnostic; scoring is applied per league."""
from __future__ import annotations

import copy

import pandas as pd

from app.config import league_config
from app.models import projections as proj
from app.scoring import score_offense

RAW = pd.DataFrame([
    {"season": 2025, "week": 1, "player_id": "p1", "team": "BUF", "opponent": "MIA",
     "position": "WR", "carries": 0, "targets": 8, "receptions": 6, "attempts": 0,
     "passing_yards": 0, "passing_tds": 0, "interceptions": 0,
     "passing_2pt_conversions": 0, "rushing_yards": 0, "rushing_tds": 0,
     "rushing_2pt_conversions": 0, "receiving_yards": 84, "receiving_tds": 1,
     "receiving_2pt_conversions": 0, "rushing_fumbles_lost": 0,
     "receiving_fumbles_lost": 0, "sack_fumbles_lost": 0, "special_teams_tds": 0},
])


def test_scored_frame_uses_the_supplied_league_scoring():
    half = league_config()
    full = copy.deepcopy(half)
    full["scoring"]["receiving"]["reception"] = 1.0

    from app.scoring import score_frame
    half_pts = score_frame(RAW, half).iloc[0]
    full_pts = score_frame(RAW, full).iloc[0]

    # 6 receptions x 0.5 extra per reception
    assert round(full_pts - half_pts, 2) == 3.0


def test_scored_frame_matches_row_wise_scorer_on_real_shape():
    from app.scoring import score_frame
    assert score_frame(RAW).iloc[0] == score_offense(RAW.iloc[0].to_dict())


def test_weekly_raw_has_no_points_column():
    """Raw reads must be league-agnostic; a points column would bake in one league."""
    df = proj._weekly_raw(2025)
    assert "fp" not in df.columns
    assert "fantasy_points_half_ppr" not in df.columns


def test_weekly_adds_fp_for_the_given_config():
    df = proj._weekly(2025, league_config())
    assert "fp" in df.columns
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_projection_scoring.py -q`
Expected: FAIL — `AttributeError: module 'app.models.projections' has no attribute '_weekly_raw'`

- [ ] **Step 3: Write the implementation**

Replace `_weekly` in `api/app/models/projections.py` with the raw/scored split:

```python
# Raw stat columns pulled for scoring. League-agnostic — every league scores the
# same underlying stat line through its own term table.
_WEEKLY_RAW_COLUMNS = (
    "season", "week", "player_id", "team", "opponent", "position",
    "carries", "targets", "receptions", "attempts",
    "passing_yards", "passing_tds", "interceptions", "passing_2pt_conversions",
    "rushing_yards", "rushing_tds", "rushing_2pt_conversions",
    "receiving_yards", "receiving_tds", "receiving_2pt_conversions",
    "rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost",
    "special_teams_tds",
)


@lru_cache(maxsize=4)
def _weekly_raw(season: int) -> pd.DataFrame:
    """Raw regular-season stat lines for the seasons feeding ``season``.

    Cached on season alone, NOT on league: the stat line is the same for
    everyone, only the scoring differs. Scoring is applied by `_weekly`, which
    is cheap enough (a vectorized pass over ~18k rows) not to need its own cache.
    """
    seasons = _completed_seasons(season)
    q = (
        f"SELECT {', '.join(_WEEKLY_RAW_COLUMNS)} "
        f"FROM weekly_stats WHERE season IN ({','.join('?' for _ in seasons)}) "
        f"AND week <= {REG_SEASON_WEEKS}"
    )
    return read_df(q, tuple(seasons))


def _weekly(season: int, cfg: dict | None = None) -> pd.DataFrame:
    """Weekly stat lines with an `fp` column scored for ``cfg``'s league."""
    from ..scoring import score_frame

    df = _weekly_raw(season).copy()
    if df.empty:
        return df
    df["fp"] = score_frame(df, cfg)
    return df
```

Rewrite `dvp_factors` to compute from the in-memory frame rather than SQL:

```python
def dvp_factors(season: int, upto_week: int, cfg: dict | None = None) -> dict:
    """Opponent defense-vs-position multipliers.

    For each (defense_team, position): mean fantasy points allowed to that
    position per game, divided by the league average, clipped to DVP_CLIP. Uses
    the current season up to ``upto_week`` if >=4 weeks of data exist, else the
    prior season. Returns {(team, pos): factor}. Neutral (missing key -> 1.0).

    Computed from the scored frame rather than SQL so it reflects the league's
    own scoring; the underlying read is cached by `_weekly_raw`.
    """
    wk = _weekly(season, cfg)
    if wk.empty:
        return {}

    def compute(sea: int, wk_max: int) -> pd.DataFrame:
        sub = wk[(wk.season == sea) & (wk.week < wk_max) & (wk.week <= REG_SEASON_WEEKS)]
        if sub.empty:
            return sub
        return sub.groupby(["opponent", "position", "week"], as_index=False)["fp"].sum()

    per_game = compute(season, upto_week)
    if per_game.empty or per_game.week.nunique() < 4:
        per_game = compute(season - 1, REG_SEASON_WEEKS + 1)
    if per_game.empty:
        return {}
    team_pos = per_game.groupby(["opponent", "position"])["fp"].mean()
    league = per_game.groupby("position")["fp"].mean()
    out = {}
    for (team, pos), val in team_pos.items():
        lg = league.get(pos, val)
        f = float(np.clip(val / lg if lg else 1.0, *DVP_CLIP))
        out[(team, pos)] = f
    return out
```

Rewrite `_recent_form` the same way:

```python
def _recent_form(season: int, week: int, cfg: dict | None = None) -> pd.Series:
    """Mean fp over the prior WEEK_FORM_LOOKBACK games this season, per player."""
    lo = max(1, week - WEEK_FORM_LOOKBACK)
    wk = _weekly(season, cfg)
    if wk.empty:
        return pd.Series(dtype=float)
    sub = wk[(wk.season == season) & (wk.week >= lo) & (wk.week < week)]
    if sub.empty:
        return pd.Series(dtype=float)
    return sub.groupby("player_id")["fp"].mean()
```

`dvp_factors` loses its `@lru_cache` decorator — delete it. The cost now sits in `_weekly_raw`, which is cached.

Update the three call sites inside `project_season`, `project_week` and `project_ros` to pass `cfg` through. `project_season(season, store=False, cfg=None)`, `project_week(..., cfg=None)`, `project_ros(..., cfg=None)`; each defaults to `league_config()` when `cfg is None` and forwards to `_weekly`, `dvp_factors` and `_recent_form`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_projection_scoring.py -q`
Expected: PASS — 4 tests.

- [ ] **Step 5: Prove ESPN projections did not move**

This is the gate. Save the current board, then compare after the change:

```bash
cd "C:/Users/mwill/Documents/mwilliams2733/Gridiron GM"
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'api')
from app.models import projections as proj
s = proj.project_season(2026)
print(len(s))
print(s[['name','position','proj_points']].head(20).to_string(index=False))
"
```

Expected, unchanged from the pre-task baseline: **827 players**, Josh Allen 355.4, Jahmyr Gibbs 320.0, Bijan Robinson 315.7, Bo Nix 307.9, Ja'Marr Chase 290.7.

If any number moved, STOP. The term table has a wrong value or a raw column is missing.

- [ ] **Step 6: Run the full suite**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest -q`
Expected: 121 passed.

- [ ] **Step 7: Commit**

```bash
git add api/app/models/projections.py api/tests/test_projection_scoring.py
git commit -m "Split raw weekly reads from per-league scoring"
```

---

### Task 3: Drop the stored points column

Removes the last place a single league's scoring is baked into storage.

**Files:**
- Modify: `api/app/db.py:32` (schema), `api/app/etl/nfl_data.py` (`normalize_weekly`), `api/app/models/projections.py:756` (validation block)
- Test: `api/tests/test_weekly_ingest.py` (modify)

**Interfaces:**
- Consumes: `score_frame` from Task 1
- Produces: `normalize_weekly(df) -> pd.DataFrame` — the `rec_val` parameter is REMOVED

- [ ] **Step 1: Update the ingest tests**

In `api/tests/test_weekly_ingest.py`, drop the `rec_val` argument from all six `normalize_weekly(...)` calls, and replace `test_scoring_a_normalized_row_reproduces_the_stored_points_column` with:

```python
def test_a_normalized_row_scores_the_same_as_nflverse_standard_plus_ppr():
    """The invariant per-league scoring depends on.

    There is no stored points column any more, so score the normalized row
    directly and compare against independently-computed nflverse standard
    scoring plus the configured per-reception value.
    """
    from app.config import league_config
    row_in = {**NFLVERSE_ROW, "receptions": 4, "receiving_yards": 52}
    out = normalize_weekly(_frame(receptions=4, receiving_yards=52))
    rec_val = league_config()["scoring"]["receiving"]["reception"]
    expected = round(_nflverse_points(row_in) + 4 * rec_val, 2)
    assert score_offense(out.iloc[0].to_dict()) == expected


def test_normalized_frame_has_no_baked_points_column():
    out = normalize_weekly(_frame())
    assert "fantasy_points_half_ppr" not in out.columns
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_weekly_ingest.py -q`
Expected: FAIL — `TypeError: normalize_weekly() missing 1 required positional argument: 'rec_val'`

- [ ] **Step 3: Write the implementation**

In `api/app/etl/nfl_data.py`, change the signature and drop the derived column:

```python
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
```

Update `sync_weekly_stats` to drop the `rec_val` lookup and the `league_config` import if now unused:

```python
def sync_weekly_stats() -> int:
    import nflreadpy as nfl

    df = _pull_seasons("weekly", lambda y: nfl.load_player_stats(y, summary_level="week"), _seasons())
    out = normalize_weekly(df)
    out = out[out["position"].isin(["QB", "RB", "WR", "TE"])]
    with connect() as conn:
        n = replace_table(out, "weekly_stats", conn)
    mark_synced("weekly_stats", f"{n} rows, seasons={_seasons()}")
    return n
```

In `api/app/db.py`, delete the line `fantasy_points_half_ppr REAL,` from the `weekly_stats` DDL.

In `api/app/models/projections.py`, the `__main__` validation block at the bottom selects `fantasy_points_half_ppr fp` from `weekly_stats`. Change it to read raw columns and score them:

```python
    actual_raw = read_df(
        "SELECT player_id, receptions, receiving_yards, receiving_tds, "
        "passing_yards, passing_tds, interceptions, rushing_yards, rushing_tds "
        "FROM weekly_stats WHERE season=2025 AND week=10")
    from ..scoring import score_frame
    actual = actual_raw[["player_id"]].copy()
    actual["fp"] = score_frame(actual_raw)
```

- [ ] **Step 4: Rebuild the database**

The schema changed, so the existing table must be recreated:

```bash
cd "C:/Users/mwill/Documents/mwilliams2733/Gridiron GM"
cp data/gridiron.db data/gridiron.db.bak-task3
.venv/Scripts/python.exe -c "
import sys, sqlite3; sys.path.insert(0,'api')
from app.config import DB_PATH
sqlite3.connect(DB_PATH).execute('DROP TABLE IF EXISTS weekly_stats')
"
cd api && ../.venv/Scripts/python.exe -c "
from app.db import init_db; from app.etl.nfl_data import sync_weekly_stats
init_db(); print('rows:', sync_weekly_stats())
"
```

Expected: ~18,533 rows, about 6 seconds (parquet cache is warm).

- [ ] **Step 5: Run tests and re-verify the gate**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest -q`
Expected: 123 passed.

Then re-run the Task 2 Step 5 projection check. Expected **identical** output: 827 players, Allen 355.4, Gibbs 320.0, Bijan 315.7, Chase 290.7.

If a number moved, STOP — a raw column is missing from `_WEEKLY_RAW_COLUMNS` or `WEEKLY_COLUMNS`.

- [ ] **Step 6: Commit**

```bash
git add api/app/db.py api/app/etl/nfl_data.py api/app/models/projections.py api/tests/test_weekly_ingest.py
git commit -m "Drop the baked half-PPR points column; score at read time"
```

---

### Task 4: Declarative flex slots and superflex replacement levels

Makes SUPER_FLEX expressible and moves the flex demand split into config.

**Files:**
- Modify: `api/app/models/vorp.py:36-63` (`replacement_levels`), `config/league.yaml`
- Test: `api/tests/test_flex_slots.py` (create)

**Interfaces:**
- Consumes: `league_config()`
- Produces: `flex_slot_defs(cfg) -> dict[str, dict]` returning `{label: {"eligible": [...], "share": {...}}}`; `replacement_levels(cfg=None) -> dict[str, int]` unchanged in signature

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_flex_slots.py`:

```python
"""Flex slots are declarative, so SUPER_FLEX is expressible without code changes."""
from __future__ import annotations

import copy

from app.config import league_config
from app.models.vorp import flex_slot_defs, replacement_levels

SUNDT_ROSTER = {
    "starters": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1,
                 "SUPER_FLEX": 1, "K": 1, "DST": 1},
    "flex_eligible": ["RB", "WR", "TE"],
    "flex_slots": {
        "FLEX": {"eligible": ["RB", "WR", "TE"],
                 "share": {"RB": .45, "WR": .45, "TE": .10}},
        "SUPER_FLEX": {"eligible": ["QB", "RB", "WR", "TE"],
                       "share": {"QB": .90, "RB": .04, "WR": .04, "TE": .02}},
    },
    "bench": 7, "ir": 2,
}


def _sundt_cfg():
    cfg = copy.deepcopy(league_config())
    cfg["league"]["teams"] = 12
    cfg["roster"] = copy.deepcopy(SUNDT_ROSTER)
    return cfg


def test_espn_replacement_levels_are_unchanged():
    """The gate: four existing leagues must not move."""
    assert replacement_levels(league_config())["QB"] == 12


def test_absent_flex_slots_synthesises_the_legacy_flex():
    cfg = league_config()
    defs = flex_slot_defs(cfg)
    assert set(defs) == {"FLEX"}
    assert defs["FLEX"]["eligible"] == cfg["roster"]["flex_eligible"]
    assert defs["FLEX"]["share"]["RB"] == 0.45


def test_superflex_moves_qb_replacement_from_12_to_23():
    levels = replacement_levels(_sundt_cfg())
    assert levels["QB"] == 23


def test_superflex_replacement_levels_match_the_spec():
    levels = replacement_levels(_sundt_cfg())
    assert levels["RB"] == 30
    assert levels["WR"] == 30
    assert levels["TE"] == 13
    assert levels["K"] == 12
    assert levels["DST"] == 12


def test_a_ten_team_league_ranks_lower_than_a_twelve_team_league():
    cfg10 = _sundt_cfg()
    cfg10["league"]["teams"] = 10
    assert replacement_levels(cfg10)["QB"] < replacement_levels(_sundt_cfg())["QB"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_flex_slots.py -q`
Expected: FAIL — `ImportError: cannot import name 'flex_slot_defs' from 'app.models.vorp'`

- [ ] **Step 3: Write the implementation**

In `api/app/models/vorp.py`, replace the module-level `flex_share` usage inside `replacement_levels`:

```python
# Historical share of flex usage by position, used when a league declares
# `flex_eligible` but not the richer `flex_slots` block. RB/WR carry flex far
# more often than TE.
LEGACY_FLEX_SHARE = {"RB": 0.45, "WR": 0.45, "TE": 0.10}


def flex_slot_defs(cfg: dict) -> dict[str, dict]:
    """Flex-type slots as {label: {"eligible": [...], "share": {pos: fraction}}}.

    A league that declares `roster.flex_slots` is taken at its word. One that
    does not gets the legacy single FLEX synthesised from `flex_eligible`, so
    configs written before superflex existed keep their exact behaviour.
    """
    roster = cfg["roster"]
    declared = roster.get("flex_slots")
    if declared:
        return {label: {"eligible": list(d["eligible"]), "share": dict(d["share"])}
                for label, d in declared.items()}
    elig = roster.get("flex_eligible", ["RB", "WR", "TE"])
    share = {pos: LEGACY_FLEX_SHARE.get(pos, 1 / len(elig)) for pos in elig}
    return {"FLEX": {"eligible": list(elig), "share": share}}


def replacement_levels(cfg: dict | None = None) -> dict[str, int]:
    """Replacement RANK per position = how many of that position the league is
    expected to roster as startable. Derived from fixed starters plus each
    flex-type slot's demand, spread over its eligible positions.

    Example (12-team, 1QB/2RB/2WR/1TE/1FLEX/1K/1DST): QB12, TE12, K12, DST12,
    and RB/WR each get 2*12 starters plus their share of the flex pool.

    With a SUPER_FLEX slot the QB line moves sharply: 12 fixed QB starters plus
    ~0.90 of 12 superflex slots puts replacement near QB23 rather than QB12.
    """
    cfg = cfg or league_config()
    teams = int(cfg["league"]["teams"])
    starters = cfg["roster"]["starters"]

    levels: dict[str, float] = {}
    for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
        levels[pos] = starters.get(pos, 0) * teams

    for label, d in flex_slot_defs(cfg).items():
        slots = starters.get(label, 0) * teams
        if not slots:
            continue
        for pos in d["eligible"]:
            levels[pos] = levels.get(pos, 0) + slots * d["share"].get(pos, 0.0)

    return {pos: int(round(rank)) for pos, rank in levels.items()}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_flex_slots.py tests/test_vorp.py -q`
Expected: PASS — 6 new plus the 11 existing vorp tests unchanged.

- [ ] **Step 5: Mutation-test the guard**

Temporarily change `SUPER_FLEX`'s QB share in the test fixture from `.90` to `.50`. `test_superflex_moves_qb_replacement_from_12_to_23` must FAIL (QB would land at 18). Revert.

- [ ] **Step 6: Run the full suite and commit**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest -q`
Expected: 129 passed.

```bash
git add api/app/models/vorp.py api/tests/test_flex_slots.py
git commit -m "Make flex slots declarative so SUPER_FLEX is expressible"
```

---

### Task 5: Config-derived lineup slots and a generalized optimality oracle

Stops the optimizer silently dropping a SUPER_FLEX starter, and proves the greedy fill is still optimal.

**Files:**
- Modify: `api/app/models/lineup.py:56-66` (`_slot_labels`), `:100-128` (fill loop), `:150-176` (`_brute_force_best`)
- Test: `api/tests/test_superflex_lineup.py` (create)

**Interfaces:**
- Consumes: `flex_slot_defs` from Task 4
- Produces: `slot_plan(cfg) -> list[tuple[str, tuple[str, ...]]]` — ordered `(label, eligible_positions)` pairs; `_brute_force_best(recs, cfg) -> float`

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_superflex_lineup.py`:

```python
"""SUPER_FLEX slots, and the fill order that makes greedy optimal."""
from __future__ import annotations

import copy

from app.config import league_config
from app.models.lineup import _brute_force_best, optimize, slot_plan
from tests.test_flex_slots import SUNDT_ROSTER


def _sundt_cfg():
    cfg = copy.deepcopy(league_config())
    cfg["league"]["teams"] = 12
    cfg["roster"] = copy.deepcopy(SUNDT_ROSTER)
    return cfg


def _rec(pid, pos, pts):
    return {"player_id": pid, "name": pid, "position": pos, "opponent": "OPP",
            "proj_points": pts, "floor": pts * 0.8, "ceiling": pts * 1.2}


def test_espn_config_produces_nine_slots():
    plan = slot_plan(league_config())
    assert len(plan) == 9
    assert [lbl for lbl, _ in plan][:3] == ["QB", "RB1", "RB2"]


def test_sundt_config_produces_ten_slots_including_super_flex():
    labels = [lbl for lbl, _ in slot_plan(_sundt_cfg())]
    assert len(labels) == 10
    assert "SUPER_FLEX" in labels


def test_slots_are_ordered_by_ascending_eligibility_breadth():
    """Fill order is load-bearing, not cosmetic — see the next test."""
    plan = slot_plan(_sundt_cfg())
    widths = [len(elig) for _, elig in plan]
    assert widths == sorted(widths)
    assert plan[-1][0] == "SUPER_FLEX"


def test_flex_is_filled_before_super_flex():
    """The counterexample from the spec.

    Two slots left, a WR worth 25 and a QB worth 20. Filling SUPER_FLEX first
    takes the WR and strands FLEX with nobody eligible: 25 instead of 45.
    """
    cfg = _sundt_cfg()
    cfg["roster"]["starters"] = {"FLEX": 1, "SUPER_FLEX": 1}
    recs = {"wr": _rec("wr", "WR", 25.0), "qb": _rec("qb", "QB", 20.0)}
    assert _brute_force_best(recs, cfg) == 45.0

    res = optimize(["wr", "qb"], 2026, 1, cfg=cfg,
                   week_proj=_frame_from(recs))
    assert res.total == 45.0


def test_greedy_matches_brute_force_on_a_full_superflex_roster():
    cfg = _sundt_cfg()
    recs = {}
    for i, (pos, pts) in enumerate([
        ("QB", 22.0), ("QB", 18.5), ("RB", 19.0), ("RB", 14.0), ("RB", 11.0),
        ("WR", 20.5), ("WR", 17.0), ("WR", 12.5), ("TE", 10.0), ("TE", 7.5),
        ("K", 8.0), ("DST", 6.0),
    ]):
        recs[f"p{i}"] = _rec(f"p{i}", pos, pts)
    res = optimize(list(recs), 2026, 1, cfg=cfg, week_proj=_frame_from(recs))
    assert res.total == _brute_force_best(recs, cfg)


def test_the_second_qb_lands_in_super_flex():
    cfg = _sundt_cfg()
    recs = {}
    for i, (pos, pts) in enumerate([
        ("QB", 22.0), ("QB", 18.5), ("RB", 19.0), ("RB", 14.0),
        ("WR", 20.5), ("WR", 17.0), ("TE", 10.0), ("K", 8.0), ("DST", 6.0),
        ("RB", 9.0),
    ]):
        recs[f"p{i}"] = _rec(f"p{i}", pos, pts)
    res = optimize(list(recs), 2026, 1, cfg=cfg, week_proj=_frame_from(recs))
    assert res.slots["SUPER_FLEX"]["position"] == "QB"


def _frame_from(recs):
    import pandas as pd
    return pd.DataFrame([{**r, "name": r["player_id"]} for r in recs.values()])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_superflex_lineup.py -q`
Expected: FAIL — `ImportError: cannot import name 'slot_plan' from 'app.models.lineup'`

- [ ] **Step 3: Write the implementation**

In `api/app/models/lineup.py`, replace `_slot_labels` with `slot_plan`:

```python
def slot_plan(cfg: dict) -> list[tuple[str, tuple[str, ...]]]:
    """Ordered (label, eligible_positions) for every starting slot.

    Fixed positions expand to numbered labels (RB -> RB1, RB2). Flex-type slots
    come from `flex_slot_defs`, so SUPER_FLEX needs no code change here.

    ORDER IS LOAD-BEARING. Slots are sorted by ascending eligibility breadth so
    the most restrictive is filled first. With FLEX (RB/WR/TE) and SUPER_FLEX
    (QB/RB/WR/TE) and only a WR 25 and a QB 20 left, filling SUPER_FLEX first
    takes the WR and strands FLEX with nobody eligible: 25 instead of 45.
    Because FLEX is a subset of SUPER_FLEX the eligibility family is laminar,
    and restrictive-first greedy is optimal on it. `_brute_force_best` pins that.
    """
    from .vorp import flex_slot_defs

    starters = cfg["roster"]["starters"]
    flex_defs = flex_slot_defs(cfg)

    plan: list[tuple[str, tuple[str, ...]]] = []
    for pos in ("QB", "RB", "WR", "TE", "K", "DST"):
        n = starters.get(pos, 0)
        if n == 1:
            plan.append((pos, (pos,)))
        else:
            plan.extend((f"{pos}{i + 1}", (pos,)) for i in range(n))

    for label, d in flex_defs.items():
        n = starters.get(label, 0)
        elig = tuple(d["eligible"])
        if n == 1:
            plan.append((label, elig))
        else:
            plan.extend((f"{label}{i + 1}", elig) for i in range(n))

    plan.sort(key=lambda p: len(p[1]))
    return plan
```

Rewrite the fill loop in `optimize` to consume the plan. `optimize` gains a `cfg` parameter:

```python
def optimize(roster_ids: list[str], season: int, week: int,
             current_lineup: list[str] | None = None,
             week_proj: pd.DataFrame | None = None,
             cfg: dict | None = None) -> LineupResult:
    """Compute the optimal legal lineup for ``roster_ids`` in a given week."""
    cfg = cfg or league_config()
    ...
    def _eligible(r, eligible_positions):
        return r["position"] in eligible_positions

    for label, elig in slot_plan(cfg):
        pick(elig, label)
```

`pick`'s first parameter becomes the eligible-positions tuple; the `(empty)` placeholder's `"position"` becomes `"/".join(pos_filter)`.

Replace `_brute_force_best` with a slot-generic recursive search:

```python
def _brute_force_best(recs: dict, cfg: dict) -> float:
    """Reference optimum by exhaustive assignment (validation only).

    Slot-generic, so it validates superflex the same way it validates the
    single-FLEX case. Exponential in slot count, which is fine for the 9-10
    slots and <=20 players a fantasy roster holds.
    """
    plan = slot_plan(cfg)
    players = list(recs.values())

    def best(slot_i: int, used: frozenset) -> float:
        if slot_i >= len(plan):
            return 0.0
        _, elig = plan[slot_i]
        options = [p for p in players
                   if p["player_id"] not in used and p["position"] in elig]
        if not options:
            return best(slot_i + 1, used)
        return max(
            p["proj_points"] + best(slot_i + 1, used | {p["player_id"]})
            for p in options
        )

    return round(best(0, frozenset()), 2)
```

Update the `__main__` smoke block's `_brute_force_best(recs, cfg["roster"]["starters"], cfg["roster"]["flex_eligible"])` call to `_brute_force_best(recs, cfg)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_superflex_lineup.py tests/test_lineup.py -q`
Expected: PASS — 7 new plus the 5 existing lineup tests.

- [ ] **Step 5: Mutation-test the ordering guard**

Temporarily change `plan.sort(key=lambda p: len(p[1]))` to `plan.sort(key=lambda p: -len(p[1]))`. `test_flex_is_filled_before_super_flex` and `test_greedy_matches_brute_force_on_a_full_superflex_roster` must FAIL. Revert.

If they pass, the ordering claim is untested and the counterexample is wrong.

- [ ] **Step 6: Run the full suite and commit**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest -q`
Expected: 136 passed.

```bash
git add api/app/models/lineup.py api/tests/test_superflex_lineup.py
git commit -m "Derive lineup slots from config; add SUPER_FLEX with a proof"
```

---

### Task 6: Ingest team defense and schedule scores

Gives DST a real statistical basis for the first time.

**Files:**
- Modify: `api/app/db.py` (schema), `api/app/etl/nfl_data.py` (`sync_schedules`, new `sync_team_defense`, `sync_all_stats`)
- Test: `api/tests/test_defense_ingest.py` (create)

**Interfaces:**
- Produces: `normalize_team_defense(df, scores) -> pd.DataFrame`, `sync_team_defense() -> int`, `DEFENSE_INPUT_COLUMNS: tuple[str, ...]`

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_defense_ingest.py`:

```python
"""Team-defense ingest: nflverse team stats plus points allowed from schedules."""
from __future__ import annotations

import pandas as pd

from app.etl.nfl_data import DEFENSE_INPUT_COLUMNS, normalize_team_defense

TEAM_STATS = pd.DataFrame([
    {"season": 2025, "week": 1, "team": "HOU", "opponent_team": "IND",
     "def_sacks": 4, "def_interceptions": 2, "def_fumbles_forced": 1,
     "fumble_recovery_opp": 1, "def_tds": 1, "def_safeties": 0,
     "def_punt_blocks": 0, "def_pat_blocks": 0, "def_fg_blocks": 1,
     "special_teams_tds": 0},
])
SCORES = pd.DataFrame([
    {"season": 2025, "week": 1, "home_team": "HOU", "away_team": "IND",
     "home_score": 27, "away_score": 13},
])


def test_points_allowed_is_the_opponents_score():
    out = normalize_team_defense(TEAM_STATS, SCORES)
    assert out.set_index("team").loc["HOU", "points_allowed"] == 13


def test_every_defense_scoring_input_survives():
    out = normalize_team_defense(TEAM_STATS, SCORES)
    missing = [c for c in DEFENSE_INPUT_COLUMNS if c not in out.columns]
    assert not missing, f"defense scoring inputs dropped: {missing}"
    nulls = [c for c in DEFENSE_INPUT_COLUMNS if out[c].isna().any()]
    assert not nulls, f"defense scoring inputs arrived null: {nulls}"


def test_opponent_is_carried_through():
    out = normalize_team_defense(TEAM_STATS, SCORES)
    assert out.iloc[0]["opponent"] == "IND"


def test_a_row_with_no_matching_schedule_is_dropped_not_nulled():
    """A silent NaN points_allowed would score as a shutout. Drop instead."""
    orphan = pd.concat([TEAM_STATS, TEAM_STATS.assign(week=99)], ignore_index=True)
    out = normalize_team_defense(orphan, SCORES)
    assert len(out) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_defense_ingest.py -q`
Expected: FAIL — `ImportError: cannot import name 'DEFENSE_INPUT_COLUMNS'`

- [ ] **Step 3: Write the implementation**

In `api/app/db.py`, add to `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS team_defense (
    season INTEGER, week INTEGER, team TEXT, opponent TEXT,
    def_sacks REAL, def_interceptions REAL, def_fumbles_forced REAL,
    fumble_recovery_opp REAL, def_tds REAL, def_safeties REAL,
    def_punt_blocks REAL, def_pat_blocks REAL, def_fg_blocks REAL,
    special_teams_tds REAL, points_allowed REAL,
    PRIMARY KEY (season, week, team)
);
```

and add `home_score REAL, away_score REAL` to the `schedules` table DDL.

In `api/app/etl/nfl_data.py`:

```python
# Columns `scoring.score_dst` reads off a stored team-defense row.
DEFENSE_INPUT_COLUMNS = (
    "def_sacks", "def_interceptions", "def_fumbles_forced", "fumble_recovery_opp",
    "def_tds", "def_safeties", "def_punt_blocks", "def_pat_blocks",
    "def_fg_blocks", "special_teams_tds", "points_allowed",
)

_DEFENSE_COLUMNS = ("season", "week", "team", "opponent") + DEFENSE_INPUT_COLUMNS


def normalize_team_defense(df: pd.DataFrame, scores: pd.DataFrame) -> pd.DataFrame:
    """Team-defense rows with points allowed joined from the schedule.

    Points allowed is the OPPONENT's score, so each schedule row contributes two
    team rows. A defense row with no matching scheduled game is dropped rather
    than left null: a null would score as a shutout, which is the most valuable
    outcome in every points-allowed ladder.
    """
    df = df.rename(columns={"opponent_team": "opponent"})
    home = scores.rename(columns={"home_team": "team", "away_score": "points_allowed"})
    away = scores.rename(columns={"away_team": "team", "home_score": "points_allowed"})
    pa = pd.concat([
        home[["season", "week", "team", "points_allowed"]],
        away[["season", "week", "team", "points_allowed"]],
    ], ignore_index=True)

    out = df.merge(pa, on=["season", "week", "team"], how="inner")
    for c in DEFENSE_INPUT_COLUMNS:
        if c not in out.columns:
            out[c] = 0.0
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
    return out[[c for c in _DEFENSE_COLUMNS if c in out.columns]].copy()


def sync_team_defense() -> int:
    import nflreadpy as nfl

    df = _pull_seasons("team_stats", lambda y: nfl.load_team_stats([y]), _seasons())
    scores = read_df(
        "SELECT season, week, home_team, away_team, home_score, away_score FROM schedules"
    )
    out = normalize_team_defense(df, scores)
    with connect() as conn:
        n = replace_table(out, "team_defense", conn)
    mark_synced("team_defense", f"{n} rows")
    return n
```

Add `home_score` and `away_score` to `sync_schedules`'s output frame:

```python
        "gameday": df["gameday"].astype(str), "weekday": df["weekday"],
        "home_score": df.get("home_score"), "away_score": df.get("away_score"),
```

Add `"team_defense": sync_team_defense(),` to `sync_all_stats`, **after** `"schedules"` since it reads from that table. Import `read_df` in `nfl_data.py`.

- [ ] **Step 4: Run tests, rebuild, verify**

```bash
cd api && ../.venv/Scripts/python.exe -m pytest tests/test_defense_ingest.py -q
cd "C:/Users/mwill/Documents/mwilliams2733/Gridiron GM"
.venv/Scripts/python.exe -c "
import sys, sqlite3; sys.path.insert(0,'api')
from app.config import DB_PATH
sqlite3.connect(DB_PATH).execute('DROP TABLE IF EXISTS schedules')
"
cd api && ../.venv/Scripts/python.exe -c "
from app.db import init_db
from app.etl.nfl_data import sync_schedules, sync_team_defense
init_db(); print('sched:', sync_schedules()); print('def:', sync_team_defense())
"
```

Expected: ~1,127 schedule rows, ~1,100 team-defense rows (2 per completed game).

Sanity-check one known result:
```bash
cd "C:/Users/mwill/Documents/mwilliams2733/Gridiron GM"
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'api')
from app.db import read_df
print(read_df(\"SELECT * FROM team_defense WHERE season=2025 AND week=1 LIMIT 4\").to_string(index=False))
"
```
Expected: `points_allowed` equals the opponent's score in that game.

- [ ] **Step 5: Commit**

```bash
git add api/app/db.py api/app/etl/nfl_data.py api/tests/test_defense_ingest.py
git commit -m "Ingest team defense stats and schedule scores"
```

---

### Task 7: Ingest kicker statistics

`score_kicker` already reads exactly these column names and has never had data behind it.

**Files:**
- Modify: `api/app/db.py` (schema), `api/app/etl/nfl_data.py`
- Test: `api/tests/test_kicking_ingest.py` (create)

**Interfaces:**
- Produces: `normalize_kicking(df) -> pd.DataFrame`, `sync_kicking_stats() -> int`, `KICKING_INPUT_COLUMNS: tuple[str, ...]`

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_kicking_ingest.py`:

```python
"""Kicker ingest. score_kicker has always read these names; nothing supplied them."""
from __future__ import annotations

import pandas as pd

from app.etl.nfl_data import KICKING_INPUT_COLUMNS, normalize_kicking
from app.scoring import score_kicker

NFLVERSE_K = pd.DataFrame([
    {"season": 2025, "week": 1, "player_id": "00-0000009", "team": "SEA",
     "opponent_team": "SF", "position": "K",
     "fg_made_0_19": 0, "fg_made_20_29": 1, "fg_made_30_39": 1,
     "fg_made_40_49": 1, "fg_made_50_59": 1, "fg_made_60_": 0,
     "fg_missed": 1, "pat_made": 2, "pat_missed": 0},
    {"season": 2025, "week": 1, "player_id": "00-0000010", "team": "BUF",
     "opponent_team": "MIA", "position": "QB",
     "fg_made_0_19": 0, "fg_made_20_29": 0, "fg_made_30_39": 0,
     "fg_made_40_49": 0, "fg_made_50_59": 0, "fg_made_60_": 0,
     "fg_missed": 0, "pat_made": 0, "pat_missed": 0},
])


def test_only_kickers_are_kept():
    out = normalize_kicking(NFLVERSE_K)
    assert list(out["player_id"]) == ["00-0000009"]


def test_every_kicking_scoring_input_survives():
    out = normalize_kicking(NFLVERSE_K)
    missing = [c for c in KICKING_INPUT_COLUMNS if c not in out.columns]
    assert not missing, f"kicking scoring inputs dropped: {missing}"


def test_a_normalized_kicker_row_scores_correctly():
    out = normalize_kicking(NFLVERSE_K)
    # 20-29, 30-39 -> 3 each; 40-49 -> 4; 50-59 -> 5; one miss -> -1; 2 XP -> 2
    assert score_kicker(out.iloc[0].to_dict()) == 3 + 3 + 4 + 5 - 1 + 2


def test_opponent_is_carried_through():
    out = normalize_kicking(NFLVERSE_K)
    assert out.iloc[0]["opponent"] == "SF"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_kicking_ingest.py -q`
Expected: FAIL — `ImportError: cannot import name 'KICKING_INPUT_COLUMNS'`

- [ ] **Step 3: Write the implementation**

In `api/app/db.py`, add to `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS kicking_stats (
    season INTEGER, week INTEGER, player_id TEXT, team TEXT, opponent TEXT,
    fg_made_0_19 REAL, fg_made_20_29 REAL, fg_made_30_39 REAL,
    fg_made_40_49 REAL, fg_made_50_59 REAL, fg_made_60_ REAL,
    fg_missed REAL, pat_made REAL, pat_missed REAL,
    PRIMARY KEY (season, week, player_id)
);
```

In `api/app/etl/nfl_data.py`:

```python
# Columns `scoring.score_kicker` reads. These are nflverse's own names, which is
# why score_kicker needs no translation layer.
KICKING_INPUT_COLUMNS = (
    "fg_made_0_19", "fg_made_20_29", "fg_made_30_39", "fg_made_40_49",
    "fg_made_50_59", "fg_made_60_", "fg_missed", "pat_made", "pat_missed",
)

_KICKING_COLUMNS = ("season", "week", "player_id", "team", "opponent") + KICKING_INPUT_COLUMNS


def normalize_kicking(df: pd.DataFrame) -> pd.DataFrame:
    """Kicker rows from a raw nflverse weekly frame.

    Kickers are filtered out of `weekly_stats` because their stat line shares no
    columns with offensive players; they get their own table rather than adding
    nine mostly-null columns to 18k offensive rows.
    """
    df = df.rename(columns={k: v for k, v in _RENAMES.items()
                            if k in df.columns and v not in df.columns})
    out = df[df["position"] == "K"].copy()
    for c in KICKING_INPUT_COLUMNS:
        if c not in out.columns:
            out[c] = 0.0
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
    return out[[c for c in _KICKING_COLUMNS if c in out.columns]].copy()


def sync_kicking_stats() -> int:
    import nflreadpy as nfl

    df = _pull_seasons("weekly", lambda y: nfl.load_player_stats(y, summary_level="week"), _seasons())
    out = normalize_kicking(df)
    with connect() as conn:
        n = replace_table(out, "kicking_stats", conn)
    mark_synced("kicking_stats", f"{n} rows")
    return n
```

Add `"kicking_stats": sync_kicking_stats(),` to `sync_all_stats`.

- [ ] **Step 4: Run tests, sync, verify**

```bash
cd api && ../.venv/Scripts/python.exe -m pytest tests/test_kicking_ingest.py -q
cd api && ../.venv/Scripts/python.exe -c "
from app.db import init_db; from app.etl.nfl_data import sync_kicking_stats
init_db(); print('rows:', sync_kicking_stats())
"
```
Expected: ~1,700 kicker rows across three seasons.

- [ ] **Step 5: Run the full suite and commit**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest -q`
Expected: 144 passed.

```bash
git add api/app/db.py api/app/etl/nfl_data.py api/tests/test_kicking_ingest.py
git commit -m "Ingest kicker statistics so score_kicker has data behind it"
```

---

### Task 8: History-driven K and DST projections

Retires the one-variable Vegas anchors.

**Files:**
- Modify: `api/app/models/projections.py:387-438` (`_project_k_dst_season`, `_resolve_k_id`)
- Test: `api/tests/test_kdst_projection.py` (create)

**Interfaces:**
- Consumes: `kicking_stats`, `team_defense` tables from Tasks 6–7; `_weight_seasons`
- Produces: `_project_k_dst_season(season, cfg=None) -> pd.DataFrame` — same output columns as before

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_kdst_projection.py`:

```python
"""K and DST projections come from history, scored per league."""
from __future__ import annotations

import copy

import pandas as pd

from app.config import league_config
from app.models.projections import _dst_per_season, _kicker_per_season


DEF_ROWS = pd.DataFrame([
    {"season": 2025, "week": w, "team": "HOU", "opponent": "IND",
     "def_sacks": 3, "def_interceptions": 1, "def_fumbles_forced": 1,
     "fumble_recovery_opp": 1, "def_tds": 0, "def_safeties": 0,
     "def_punt_blocks": 0, "def_pat_blocks": 0, "def_fg_blocks": 0,
     "special_teams_tds": 0, "points_allowed": 17}
    for w in range(1, 6)
])


def test_dst_scores_through_the_leagues_own_points_allowed_ladder():
    """17 allowed is worth 1 point in the ESPN ladder and 1 in Sleeper's, but a
    shutout is worth 5 vs 10 — so the ladders must not be shared."""
    espn = league_config()
    sleeper = copy.deepcopy(espn)
    sleeper["scoring"]["dst"]["points_allowed_tiers"] = [
        [0, 10], [6, 7], [13, 4], [20, 1], [27, 0], [34, -1], [999, -4]]

    shutout = DEF_ROWS.assign(points_allowed=0)
    e = _dst_per_season(shutout, espn)
    s = _dst_per_season(shutout, sleeper)
    assert s.iloc[0]["ppg"] - e.iloc[0]["ppg"] == 5.0


def test_dst_per_season_aggregates_games_and_ppg():
    out = _dst_per_season(DEF_ROWS, league_config())
    row = out.iloc[0]
    assert row["games"] == 5
    assert row["season"] == 2025
    assert row["ppg"] > 0


def test_kicker_per_season_aggregates_games_and_ppg():
    k = pd.DataFrame([
        {"season": 2025, "week": w, "player_id": "k1", "team": "SEA",
         "fg_made_0_19": 0, "fg_made_20_29": 1, "fg_made_30_39": 0,
         "fg_made_40_49": 1, "fg_made_50_59": 0, "fg_made_60_": 0,
         "fg_missed": 0, "pat_made": 2, "pat_missed": 0}
        for w in range(1, 5)
    ])
    out = _kicker_per_season(k, league_config())
    assert out.iloc[0]["games"] == 4
    assert out.iloc[0]["ppg"] == 9.0  # 3 + 4 + 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_kdst_projection.py -q`
Expected: FAIL — `ImportError: cannot import name '_dst_per_season'`

- [ ] **Step 3: Write the implementation**

In `api/app/models/projections.py`, add aggregation helpers and rewrite the season model:

```python
def _dst_per_season(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """Per (team, season) games and points-per-game for a team defense.

    Points-allowed is tiered, not linear, so it cannot ride the term table and
    is scored per row by `score_dst`.
    """
    from ..scoring import score_dst

    if df.empty:
        return pd.DataFrame(columns=["team", "season", "games", "ppg", "opp_pg"])
    scored = df.copy()
    scored["pts"] = [score_dst(r, cfg) for r in df.to_dict("records")]
    out = scored.groupby(["team", "season"], as_index=False).agg(
        games=("pts", "size"), ppg=("pts", "mean"))
    out["opp_pg"] = out["ppg"]      # no usage concept for a defense
    return out


def _kicker_per_season(df: pd.DataFrame, cfg: dict | None = None) -> pd.DataFrame:
    """Per (player, season) games and points-per-game for a kicker."""
    from ..scoring import score_kicker

    if df.empty:
        return pd.DataFrame(columns=["player_id", "season", "games", "ppg", "opp_pg"])
    scored = df.copy()
    scored["pts"] = [score_kicker(r, cfg) for r in df.to_dict("records")]
    out = scored.groupby(["player_id", "season"], as_index=False).agg(
        games=("pts", "size"), ppg=("pts", "mean"))
    out["opp_pg"] = out["ppg"]
    return out


def _project_k_dst_season(season: int, cfg: dict | None = None) -> pd.DataFrame:
    """K and DST season projections from ingested history.

    Replaces the previous one-variable Vegas anchors (`K_BASE_PPG`,
    `DST_BASE_PPG`). Baselines come from the same recency-weighted aggregation
    offence uses -- `_weight_seasons` -- so K/DST cannot drift from how everyone
    else is aggregated. The Vegas signal is NOT discarded: it remains the
    matchup factor applied by `project_week` and `project_ros`.

    `usage_trend` is meaningless for these positions; `opp_pg` is set equal to
    `ppg` so the trend ratio is 1.0 and the term drops out.
    """
    cfg = cfg or league_config()
    seasons = _completed_seasons(season)
    placeholders = ",".join("?" for _ in seasons)
    newest = max(seasons)

    kdf = read_df(
        f"SELECT * FROM kicking_stats WHERE season IN ({placeholders})", tuple(seasons))
    ddf = read_df(
        f"SELECT * FROM team_defense WHERE season IN ({placeholders})", tuple(seasons))

    players = _players().set_index("player_id")
    rows = []

    for pid, per in _kicker_per_season(kdf, cfg).groupby("player_id"):
        agg = _weight_seasons(per[["season", "games", "ppg", "opp_pg"]], newest)
        if agg["n_seasons"] == 0 or agg["base_ppg"] <= 0:
            continue
        name = players.loc[pid, "name"] if pid in players.index else pid
        team = norm_team(players.loc[pid, "team"]) if pid in players.index else None
        proj = agg["base_ppg"] * agg["proj_games"]
        rows.append({
            "player_id": pid, "name": name, "position": "K", "team": team,
            "proj_points": round(proj, 1), "proj_ppg": round(agg["base_ppg"], 2),
            "floor": round(proj * 0.80, 1), "ceiling": round(proj * 1.20, 1),
            "components": {"model": "history", "n_seasons": agg["n_seasons"],
                           "proj_games": round(agg["proj_games"], 1)},
        })

    for team, per in _dst_per_season(ddf, cfg).groupby("team"):
        agg = _weight_seasons(per[["season", "games", "ppg", "opp_pg"]], newest)
        if agg["n_seasons"] == 0:
            continue
        t = norm_team(team)
        proj = agg["base_ppg"] * agg["proj_games"]
        rows.append({
            "player_id": f"DST_{t}", "name": f"{t} DST", "position": "DST", "team": t,
            "proj_points": round(proj, 1), "proj_ppg": round(agg["base_ppg"], 2),
            "floor": round(proj * 0.80, 1), "ceiling": round(proj * 1.20, 1),
            "components": {"model": "history", "n_seasons": agg["n_seasons"],
                           "proj_games": round(agg["proj_games"], 1)},
        })

    return pd.DataFrame(rows)
```

Add a `cfg` parameter to `score_kicker` and `score_dst` in `api/app/scoring.py` (`def score_kicker(stats, cfg=None)`, `def score_dst(stats, cfg=None)`), and to `points_allowed_score(points_allowed, cfg=None)`, each falling back to `league_config()`.

Delete `K_BASE_PPG`, `K_IMPLIED_GAIN`, `DST_BASE_PPG`, `DST_IMPLIED_GAIN`, `_resolve_k_id`, `_team_implied_totals` and `_opp_implied_totals` if no longer referenced. Keep `LEAGUE_AVG_IMPLIED`, which `_matchup_factor` still uses.

Update `project_season` to call `_project_k_dst_season(season, cfg)`.

- [ ] **Step 4: Run tests and sanity-check output**

```bash
cd api && ../.venv/Scripts/python.exe -m pytest tests/test_kdst_projection.py -q
cd "C:/Users/mwill/Documents/mwilliams2733/Gridiron GM"
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'api')
from app.models import projections as proj
s = proj.project_season(2026)
for pos in ('K','DST'):
    print(pos, [(r['name'], r.proj_points) for _, r in s[s.position==pos].head(5).iterrows()])
"
```

Expected: kickers roughly 100–160 season points, DSTs roughly 80–160. If DSTs come out negative or above 250, the points-allowed ladder is being applied wrong.

- [ ] **Step 5: Run the full suite and commit**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest -q`
Expected: 148 passed.

```bash
git add api/app/models/projections.py api/app/scoring.py api/tests/test_kdst_projection.py
git commit -m "Project K and DST from ingested history instead of Vegas anchors"
```

---

### Task 9: Rest-of-season horizon cap

Stops valuing weeks that cannot help a league make its playoffs.

**Files:**
- Modify: `api/app/models/projections.py` (`project_ros`), `config/league.yaml`
- Test: `api/tests/test_ros_horizon.py` (create)

**Interfaces:**
- Produces: `last_scoring_week(cfg) -> int`

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_ros_horizon.py`:

```python
"""ROS stops at the last week that counts toward making the playoffs."""
from __future__ import annotations

import copy

from app.config import league_config
from app.models.projections import REG_SEASON_WEEKS, last_scoring_week


def test_absent_playoff_week_start_keeps_the_full_regular_season():
    """The gate: ESPN leagues do not set the key and must not move."""
    assert last_scoring_week(league_config()) == REG_SEASON_WEEKS


def test_playoffs_starting_week_15_caps_at_week_14():
    cfg = copy.deepcopy(league_config())
    cfg["league"]["playoff_week_start"] = 15
    assert last_scoring_week(cfg) == 14


def test_a_late_playoff_start_never_exceeds_the_regular_season():
    cfg = copy.deepcopy(league_config())
    cfg["league"]["playoff_week_start"] = 25
    assert last_scoring_week(cfg) == REG_SEASON_WEEKS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_ros_horizon.py -q`
Expected: FAIL — `ImportError: cannot import name 'last_scoring_week'`

- [ ] **Step 3: Write the implementation**

In `api/app/models/projections.py`:

```python
def last_scoring_week(cfg: dict | None = None) -> int:
    """Final week that counts toward making the fantasy playoffs.

    A league whose playoffs start week 15 stops accruing regular-season value at
    week 14; weeks 15-17 pay out only if you qualify and week 18 never does.
    Since waiver rankings are ROS-driven, an uncapped horizon systematically
    overvalues players with strong late schedules.

    Absent `playoff_week_start`, returns REG_SEASON_WEEKS so behaviour is
    unchanged -- which is what keeps the four ESPN leagues identical.
    """
    cfg = cfg or league_config()
    start = cfg["league"].get("playoff_week_start") or (REG_SEASON_WEEKS + 1)
    return min(REG_SEASON_WEEKS, int(start) - 1)
```

In `project_ros`, replace `weeks = range(max(week, 1), REG_SEASON_WEEKS + 1)` with:

```python
    weeks = range(max(week, 1), last_scoring_week(cfg) + 1)
```

- [ ] **Step 4: Run tests and verify the gate**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_ros_horizon.py tests/test_projections.py -q`
Expected: PASS — 3 new plus the 20 existing projection tests, including `test_ros_equals_sum_of_weekly_projections`.

- [ ] **Step 5: Commit**

```bash
git add api/app/models/projections.py api/tests/test_ros_horizon.py
git commit -m "Cap the ROS horizon at each league's last regular-season week"
```

---

### Task 10: Sleeper ETL and player identity

**Files:**
- Create: `api/app/etl/sleeper.py`, `api/tests/test_sleeper.py`, `api/tests/fixtures/sleeper_roster.json`
- Test: `api/tests/test_sleeper.py`

**Interfaces:**
- Consumes: `resolve_player_name` from `api/app/routers/_common.py`
- Produces: `resolve_sleeper_player(entry, players, espn_lut) -> str | None`, `sleeper_available(league_id) -> bool`, `sync_sleeper(league_id) -> dict`, `sleeper_player_map() -> dict`, `parse_roster(raw, player_map, players) -> dict`

- [ ] **Step 1: Capture the test fixture**

```bash
cd "C:/Users/mwill/Documents/mwilliams2733/Gridiron GM"
mkdir -p api/tests/fixtures
curl -s "https://api.sleeper.app/v1/league/1395500725785591808/rosters" \
  -o api/tests/fixtures/sleeper_roster.json
```

Tests must never hit the network. This fixture is the recorded payload.

- [ ] **Step 2: Write the failing test**

Create `api/tests/test_sleeper.py`:

```python
"""Sleeper payload parsing and the player-identity cascade.

No network: everything here runs off the captured fixture.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.etl.sleeper import parse_roster, resolve_sleeper_player

FIXTURE = Path(__file__).parent / "fixtures" / "sleeper_roster.json"


def _rosters():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_has_twelve_rosters():
    assert len(_rosters()) == 12


def test_starters_are_ten_ids_in_slot_order():
    """This is the current_lineup data the optimizer has never had."""
    mine = [r for r in _rosters() if r["roster_id"] == 12][0]
    assert len(mine["starters"]) == 10


def test_remaining_faab_comes_from_the_roster_settings():
    mine = [r for r in _rosters() if r["roster_id"] == 12][0]
    assert "waiver_budget_used" in mine["settings"]


def test_gsis_id_is_used_when_present():
    players = _fake_players()
    entry = {"gsis_id": "00-0033077", "full_name": "Dak Prescott", "position": "QB"}
    assert resolve_sleeper_player(entry, players, {}) == "00-0033077"


def test_name_matching_is_the_fallback_when_gsis_is_missing():
    """Sleeper omits gsis_id for most post-2020 entrants, so this is the
    PRIMARY path in practice: 194/194 rostered players resolve this way."""
    players = _fake_players()
    entry = {"gsis_id": None, "full_name": "Jalen Hurts", "position": "QB"}
    assert resolve_sleeper_player(entry, players, {}) == "00-0036389"


def test_team_defenses_map_to_synthetic_dst_ids():
    assert resolve_sleeper_player(
        {"player_id": "HOU", "position": "DEF"}, _fake_players(), {}) == "DST_HOU"


def test_an_unresolvable_entry_returns_none_rather_than_guessing():
    entry = {"gsis_id": None, "full_name": "Nobody Atallhere", "position": "WR"}
    assert resolve_sleeper_player(entry, _fake_players(), {}) is None


def _fake_players():
    import pandas as pd
    from app.routers._common import norm_name
    df = pd.DataFrame([
        {"player_id": "00-0033077", "name": "Dak Prescott", "position": "QB",
         "team": "DAL", "espn_id": None, "status": "ACT"},
        {"player_id": "00-0036389", "name": "Jalen Hurts", "position": "QB",
         "team": "PHI", "espn_id": None, "status": "ACT"},
    ])
    df["norm"] = df["name"].map(norm_name)
    return df
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_sleeper.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.etl.sleeper'`

- [ ] **Step 4: Write the implementation**

Create `api/app/etl/sleeper.py`:

```python
"""Sleeper league sync. Read-only REST, no authentication required.

Sleeper's public API needs no key, no cookies and no OAuth, which makes it the
simplest source in the app. Rate limit is 1000 calls/minute; a full sync is
under ten calls plus the player map.

Player identity is the one hard part. Sleeper carries a `gsis_id` cross-
reference, but populates it mostly for pre-2020 entrants -- measured at 17% of
players rostered in this league, missing Hurts, St. Brown, Jonathan Taylor and
Trevor Lawrence. Normalized name + position resolves 194/194 with zero
collisions and agrees with `gsis_id` on every player where both exist, so name
matching is the primary path and gsis_id is a confirmation, not the reverse.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

from ..config import PARQUET_DIR, ensure_dirs, league_cache_dir, league_config, resolve_league
from ..db import mark_synced

log = logging.getLogger(__name__)

BASE = "https://api.sleeper.app/v1"
TIMEOUT = 30

# Sleeper spells team defense DEF; the app uses DST everywhere else. Normalize
# here, at the boundary, never in the models.
SLEEPER_POS = {"DEF": "DST"}


def _get(path: str):
    resp = httpx.get(f"{BASE}/{path}", timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _settings(league_id: str | None = None) -> dict:
    lg = league_config(league_id).get("league", {})
    return {"league_id": lg.get("sleeper_league_id"),
            "draft_id": lg.get("sleeper_draft_id")}


def sleeper_available(league_id: str | None = None) -> bool:
    return bool(_settings(league_id)["league_id"])


def sleeper_player_map() -> dict:
    """All Sleeper players, cached to parquet. ~14MB; refresh daily at most."""
    ensure_dirs()
    path = PARQUET_DIR / "sleeper_players.parquet"
    try:
        data = _get("players/nfl")
        pd.DataFrame([{"player_id": k, "blob": json.dumps(v)}
                      for k, v in data.items()]).to_parquet(path, index=False)
        return data
    except Exception as exc:
        if path.exists():
            log.warning("sleeper player map fetch failed (%s); using cache", exc)
            df = pd.read_parquet(path)
            return {r.player_id: json.loads(r.blob) for r in df.itertuples()}
        raise


def resolve_sleeper_player(entry: dict, players: pd.DataFrame,
                           espn_lut: dict) -> str | None:
    """Resolve one Sleeper player entry to our player_id.

    Cascade: gsis_id -> espn_id -> normalized name + position. Returns None
    rather than guessing when all three miss, so callers can report it.
    """
    from ..routers._common import resolve_player_name

    pos = SLEEPER_POS.get(entry.get("position"), entry.get("position"))
    if pos == "DST":
        team = entry.get("player_id") or entry.get("team")
        return f"DST_{team}" if team else None

    gsis = entry.get("gsis_id")
    if gsis and gsis in set(players.player_id):
        return gsis

    espn = entry.get("espn_id")
    if espn is not None:
        try:
            hit = espn_lut.get(float(espn))
            if hit:
                return hit
        except (TypeError, ValueError):
            pass

    return resolve_player_name(entry.get("full_name") or "", pos, players)


def _espn_lut(players: pd.DataFrame) -> dict:
    esp = players.dropna(subset=["espn_id"]).copy()
    if esp.empty:
        return {}
    return dict(zip(esp["espn_id"].astype(float), esp["player_id"]))


def parse_roster(raw: dict, player_map: dict, players: pd.DataFrame) -> dict:
    """One Sleeper roster -> {roster_id, players, starters, faab_used, unresolved}."""
    lut = _espn_lut(players)

    def resolve(pid):
        return resolve_sleeper_player(
            {**player_map.get(pid, {}), "player_id": pid}, players, lut)

    ids, unresolved = [], []
    for pid in (raw.get("players") or []):
        got = resolve(pid)
        (ids if got else unresolved).append(got or pid)
    starters = [resolve(pid) for pid in (raw.get("starters") or [])]
    return {
        "roster_id": raw.get("roster_id"),
        "owner_id": raw.get("owner_id"),
        "players": ids,
        "starters": starters,
        "faab_used": (raw.get("settings") or {}).get("waiver_budget_used", 0),
        "unresolved": unresolved,
    }


def _write_cache(name: str, payload, league_id: str | None = None) -> None:
    path = league_cache_dir(league_id) / f"{name}.json"
    path.write_text(json.dumps(
        {"fetched_at": datetime.now(timezone.utc).isoformat(), "data": payload},
        indent=1), encoding="utf-8")


def read_cache(name: str, league_id: str | None = None):
    path = league_cache_dir(league_id) / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def sync_sleeper(league_id: str | None = None) -> dict:
    """Pull rosters, users and league settings. Returns a summary dict."""
    from ..routers._common import all_players

    lid = resolve_league(league_id)
    s = _settings(lid)
    if not s["league_id"]:
        raise RuntimeError(f"sleeper_league_id not configured for '{lid}'.")

    league = _get(f"league/{s['league_id']}")
    raw_rosters = _get(f"league/{s['league_id']}/rosters")
    users = _get(f"league/{s['league_id']}/users")
    player_map = sleeper_player_map()
    players = all_players()

    by_user = {u["user_id"]: u for u in users}
    rosters = []
    for r in raw_rosters:
        parsed = parse_roster(r, player_map, players)
        owner = by_user.get(r.get("owner_id")) or {}
        parsed["name"] = (owner.get("metadata") or {}).get("team_name") \
            or owner.get("display_name") or f"Roster {parsed['roster_id']}"
        rosters.append(parsed)

    _check_scoring_drift(league, lid)

    _write_cache("teams", rosters, lid)
    _write_cache("league", league, lid)
    unresolved = sum(len(r["unresolved"]) for r in rosters)
    mark_synced(f"sleeper:{lid}", f"{len(rosters)} rosters, {unresolved} unresolved")
    return {"league_id": lid, "rosters": len(rosters), "unresolved": unresolved}


def _check_scoring_drift(league: dict, lid: str) -> None:
    """Warn if the commissioner changed scoring since league.yaml was authored.

    A sync-time check rather than a test, because it needs the network.
    """
    live = league.get("scoring_settings") or {}
    cfg = league_config(lid)["scoring"]
    checks = {
        "rec": cfg["receiving"]["reception"],
        "pass_td": cfg["passing"]["touchdown"],
        "pass_int": cfg["passing"]["interception"],
        "rush_td": cfg["rushing"]["touchdown"],
    }
    for key, ours in checks.items():
        theirs = live.get(key)
        if theirs is not None and float(theirs) != float(ours):
            log.warning(
                "sleeper %s: scoring drift on %s — league.yaml has %s, Sleeper has %s",
                lid, key, ours, theirs)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_sleeper.py -q`
Expected: PASS — 7 tests.

- [ ] **Step 6: Verify identity resolution against the live league**

```bash
cd "C:/Users/mwill/Documents/mwilliams2733/Gridiron GM"
.venv/Scripts/python.exe -c "
import sys, json; sys.path.insert(0,'api')
from app.etl.sleeper import parse_roster, sleeper_player_map
from app.routers._common import all_players
rosters = json.load(open('api/tests/fixtures/sleeper_roster.json'))
pm, players = sleeper_player_map(), all_players()
tot = unres = 0
for r in rosters:
    p = parse_roster(r, pm, players)
    tot += len(p['players']) + len(p['unresolved']); unres += len(p['unresolved'])
print(f'resolved {tot-unres}/{tot}')
"
```
Expected: **resolved 204/204**. Anything less means the cascade regressed from the measured baseline.

- [ ] **Step 7: Commit**

```bash
git add api/app/etl/sleeper.py api/tests/test_sleeper.py api/tests/fixtures/sleeper_roster.json
git commit -m "Add Sleeper ETL with a measured player-identity cascade"
```

---

### Task 11: Platform dispatch and league registration

**Files:**
- Create: `api/app/etl/platform.py`
- Modify: `config/league.yaml`, `api/app/routers/league.py`, `api/app/routers/waivers.py`, `api/app/routers/lineup.py`, `api/app/etl/sync.py`
- Test: `api/tests/test_platform.py` (create)

**Interfaces:**
- Produces: `get_my_roster(league_id) -> dict`, `read_cache(name, league_id)`, `free_agents(league_id, season) -> tuple[list[str], str | None]`

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_platform.py`:

```python
"""Platform dispatch: leagues route to their own ETL."""
from __future__ import annotations

from app.config import league_config, league_ids
from app.etl.platform import platform_of


def test_leagues_without_a_platform_default_to_espn():
    assert platform_of("league1") == "espn"


def test_the_sundt_league_is_registered_as_sleeper():
    assert "sundt" in league_ids()
    assert platform_of("sundt") == "sleeper"


def test_sundt_carries_full_ppr_scoring():
    assert league_config("sundt")["scoring"]["receiving"]["reception"] == 1.0


def test_sundt_carries_minus_one_interceptions():
    assert league_config("sundt")["scoring"]["passing"]["interception"] == -1


def test_sundt_declares_a_super_flex_slot():
    roster = league_config("sundt")["roster"]
    assert roster["starters"]["SUPER_FLEX"] == 1
    assert "QB" in roster["flex_slots"]["SUPER_FLEX"]["eligible"]


def test_sundt_playoffs_start_week_15():
    assert league_config("sundt")["league"]["playoff_week_start"] == 15


def test_espn_leagues_do_not_declare_playoff_week_start():
    """The gate: adding the key to ESPN leagues would change their ROS."""
    assert league_config("league1")["league"].get("playoff_week_start") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_platform.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.etl.platform'`

- [ ] **Step 3: Add the Sundt league to `config/league.yaml`**

Append to the `leagues:` list. Translate all 43 Sleeper keys from
`~/Downloads/sundtredraftleaguecontext.md` §11 into the canonical nested shape:

```yaml
  - id: sundt
    name: "Sundt Redraft"
    platform: sleeper
    teams: 12
    playoff_week_start: 15
    sleeper_league_id: "1395500725785591808"
    sleeper_draft_id: "1395500726964195328"
    draft: { my_slot: 10, rounds: 17 }
    waivers: { system: faab, faab_budget: 200 }
    scoring:
      passing:   { yards_per_point: 25, touchdown: 4, interception: -1, two_point: 2 }
      rushing:   { yards_per_point: 10, touchdown: 6, two_point: 2 }
      receiving: { reception: 1.0, yards_per_point: 10, touchdown: 6, two_point: 2 }
      misc:      { fumble_lost: -2, return_touchdown: 6 }
      kicking:
        fg_0_39: 3
        fg_40_49: 4
        fg_50_plus: 5
        fg_missed: -1
        xp_made: 1
        xp_missed: -1
      dst:
        sack: 1
        interception: 2
        fumble_recovery: 2
        forced_fumble: 1
        touchdown: 6
        safety: 2
        block_kick: 2
        points_allowed_tiers:
          - [0, 10]
          - [6, 7]
          - [13, 4]
          - [20, 1]
          - [27, 0]
          - [34, -1]
          - [999, -4]
    roster:
      starters: { QB: 1, RB: 2, WR: 2, TE: 1, FLEX: 1, SUPER_FLEX: 1, K: 1, DST: 1 }
      flex_eligible: [RB, WR, TE]
      flex_slots:
        FLEX:       { eligible: [RB, WR, TE],     share: { RB: .45, WR: .45, TE: .10 } }
        SUPER_FLEX: { eligible: [QB, RB, WR, TE], share: { QB: .90, RB: .04, WR: .04, TE: .02 } }
      bench: 7
      ir: 2
```

`league_config` must also copy `playoff_week_start` and the two Sleeper ids from the
league entry into `cfg["league"]`. In `api/app/config.py`'s `league_config`, after the
existing `league["name"] = ...` line, add:

```python
    for key in ("playoff_week_start", "sleeper_league_id", "sleeper_draft_id", "platform"):
        if entry.get(key) is not None:
            league[key] = entry[key]
```

- [ ] **Step 4: Write the dispatch module**

Create `api/app/etl/platform.py`:

```python
"""Dispatch roster and free-agent reads to the league's platform.

Routers consume one shape regardless of platform; the per-platform modules
(`etl/espn.py`, `etl/sleeper.py`) keep their own cache layouts.
"""
from __future__ import annotations

from ..config import league_config, resolve_league

DEFAULT_PLATFORM = "espn"


def platform_of(league_id: str | None = None) -> str:
    return league_config(league_id)["league"].get("platform") or DEFAULT_PLATFORM


def get_my_roster(league_id: str | None = None) -> dict:
    lid = resolve_league(league_id)
    if platform_of(lid) == "sleeper":
        from . import sleeper
        cache = sleeper.read_cache("teams", lid)
        if not cache:
            return {"mode": "none", "team": {"name": "My Team", "roster": []}}
        slot = (league_config(lid).get("draft") or {}).get("my_slot")
        rosters = cache["data"]
        mine = next((r for r in rosters if r["roster_id"] == slot), rosters[0])
        return {"mode": "sleeper", "team": {
            "name": mine["name"],
            "roster": [{"player_id": p} for p in mine["players"]],
            "starters": mine["starters"],
            "faab_used": mine["faab_used"],
        }}
    from . import espn
    return espn.get_my_roster(lid)


def free_agents(league_id: str | None = None, season: int | None = None):
    """(player_ids, warning). Sleeper has no free-agent endpoint, so the pool is
    derived: every rostered player across the league is subtracted from the
    scored player universe, then capped -- with the cap reported, never silent.
    """
    lid = resolve_league(league_id)
    if platform_of(lid) != "sleeper":
        return None, None          # caller falls through to its ESPN path

    from . import sleeper
    cache = sleeper.read_cache("teams", lid)
    if not cache:
        return [], "no Sleeper sync yet"
    taken = {p for r in cache["data"] for p in r["players"]}

    from ..models import projections as proj
    s = proj.project_season(season or league_config(lid)["league"]["season"],
                            cfg=league_config(lid))
    pool = [pid for pid in s.sort_values("proj_points", ascending=False).player_id
            if pid not in taken]
    capped, limit = pool[:200], 200
    warning = (f"showing top {limit} of {len(pool)} available"
               if len(pool) > limit else None)
    return capped, warning
```

Point `routers/league.py`, `routers/waivers.py` and `routers/lineup.py` at
`etl.platform.get_my_roster` instead of `espn_etl.get_my_roster`, and have
`routers/waivers.py:_free_agent_ids` try `platform.free_agents` first, falling through
to its existing ESPN-then-ADP logic when it returns `(None, None)`.

In `api/app/etl/sync.py`, extend the `espn` scope to sync Sleeper leagues too:

```python
            elif name == "espn":
                from ..config import league_ids
                from .platform import platform_of
                per_league = {}
                for lid in league_ids():
                    try:
                        if platform_of(lid) == "sleeper":
                            from .sleeper import sleeper_available, sync_sleeper
                            per_league[lid] = (sync_sleeper(lid) if sleeper_available(lid)
                                               else "skipped (no sleeper_league_id)")
                        else:
                            from .espn import espn_available, sync_espn
                            per_league[lid] = (sync_espn(lid) if espn_available(lid)
                                               else "skipped (no league_id — manual mode)")
                    except Exception as exc:
                        log.exception("league sync failed for %s", lid)
                        per_league[lid] = f"error: {exc}"
                results["espn"] = per_league
```

- [ ] **Step 5: Run tests and sync the live league**

```bash
cd api && ../.venv/Scripts/python.exe -m pytest tests/test_platform.py -q
cd api && ../.venv/Scripts/python.exe -c "
from app.etl.sleeper import sync_sleeper
print(sync_sleeper('sundt'))
"
```
Expected: `{'league_id': 'sundt', 'rosters': 12, 'unresolved': 0}`

- [ ] **Step 6: Run the full suite and commit**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest -q`
Expected: 162 passed.

```bash
git add config/league.yaml api/app/config.py api/app/etl/platform.py api/app/etl/sync.py api/app/routers/ api/tests/test_platform.py
git commit -m "Register Sundt Redraft and dispatch reads on league platform"
```

---

### Task 12: Wire the five defect fixes

**Files:**
- Modify: `api/app/routers/lineup.py`, `api/app/routers/waivers.py`, `api/app/models/waivers.py`, `api/app/models/lineup.py`, `api/app/etl/sync.py`, `web/src/pages/Waivers.tsx`, `web/src/lib/types.ts`
- Test: `api/tests/test_api_contract.py` (create)

**Interfaces:**
- Consumes: `get_my_roster` from Task 11 (Sleeper rosters carry `starters` and `faab_used`)

- [ ] **Step 1: Write the failing test**

Create `api/tests/test_api_contract.py`:

```python
"""Fields the web app reads. These are the contracts nothing currently checks.

Every defect these guard shipped while 109 tests passed and `tsc` exited 0,
because the backend suite stops at the router and types.ts index signatures make
any property access typecheck.
"""
from __future__ import annotations

from app.models.lineup import LineupResult, optimize


def _recs():
    return {f"p{i}": {"player_id": f"p{i}", "name": f"P{i}", "position": pos,
                      "team": "BUF", "opponent": "MIA", "proj_points": pts,
                      "floor": pts * .8, "ceiling": pts * 1.2}
            for i, (pos, pts) in enumerate(
                [("QB", 22.), ("RB", 19.), ("RB", 14.), ("WR", 20.), ("WR", 17.),
                 ("TE", 10.), ("K", 8.), ("DST", 6.), ("RB", 9.)])}


def _frame(recs):
    import pandas as pd
    return pd.DataFrame(list(recs.values()))


def test_lineup_starters_carry_team():
    """Waivers and Start/Sit both render `team`; it was absent from both."""
    recs = _recs()
    res = optimize(list(recs), 2026, 1, week_proj=_frame(recs))
    for slot, player in res.slots.items():
        if player.get("player_id"):
            assert "team" in player, f"slot {slot} lost `team`"


def test_lineup_bench_entries_carry_team():
    recs = _recs()
    res = optimize(list(recs), 2026, 1, week_proj=_frame(recs))
    for b in res.bench:
        assert "team" in b


def test_current_total_and_delta_are_populated_when_a_lineup_is_supplied():
    """These were permanently None: the router never passed current_lineup."""
    recs = _recs()
    ids = list(recs)
    res = optimize(ids, 2026, 1, week_proj=_frame(recs), current_lineup=ids[:8])
    assert res.current_total is not None
    assert res.delta is not None


def test_delta_is_zero_when_the_current_lineup_is_already_optimal():
    recs = _recs()
    res = optimize(list(recs), 2026, 1, week_proj=_frame(recs))
    optimal_ids = [p["player_id"] for p in res.slots.values() if p.get("player_id")]
    again = optimize(list(recs), 2026, 1, week_proj=_frame(recs),
                     current_lineup=optimal_ids)
    assert again.delta == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest tests/test_api_contract.py -q`
Expected: FAIL — `AssertionError: slot QB lost 'team'`

- [ ] **Step 3: Write the implementation**

**`team` on lineup records** — in `api/app/models/lineup.py`'s `optimize`, add `"team": r.get("team")` to the `recs` dict comprehension and to the fallback `recs.setdefault(...)`.

**`team` on waiver rows** — in `api/app/models/waivers.py`, `names` already selects from `season_proj`; change it to `[["name", "position", "team"]]` and add `"team": names.loc[pid, "team"],` to the appended row dict.

**`current_lineup`** — in `api/app/routers/lineup.py`'s `get_optimal`:

```python
    result_roster = platform.get_my_roster(league_id)
    current = (result_roster.get("team") or {}).get("starters")
    result = lineup_model.optimize(roster_ids, season, week,
                                   current_lineup=[c for c in (current or []) if c],
                                   cfg=league_config(league_id))
```

For ESPN leagues, derive `starters` in `etl/espn.py:get_my_roster` from each player's
`lineup_slot`, excluding slots named `BE`, `IR` and `BN`.

**Real FAAB** — in `api/app/routers/waivers.py:get_rankings`, read `faab_used` off the
roster and pass `faab_remaining=budget - faab_used` into `rank_free_agents`.

**Cache invalidation** — at the end of `run_sync` in `api/app/etl/sync.py`:

```python
    # A sync updates the database; without this the process keeps serving
    # pre-sync projections until restart.
    from ..models import projections as proj
    proj._weekly_raw.cache_clear()
    proj._players.cache_clear()
    proj._snaps.cache_clear()
    proj._completed_seasons.cache_clear()
    from ..routers import draft as draft_router
    draft_router._season_proj_cache.clear()
```

**Waivers warning** — in `web/src/pages/Waivers.tsx`, render it above the table:

```tsx
{rankingsQuery.data?.warning && (
  <div className="mb-4 flex items-center gap-2 rounded-md border border-amber-600/40 bg-amber-500/5 px-3 py-2 text-sm text-amber-500">
    <TriangleAlert className="h-4 w-4 shrink-0" />
    {rankingsQuery.data.warning}
  </div>
)}
```

Import `TriangleAlert` from `lucide-react`. In `web/src/lib/types.ts`, fix
`PlayersResponse` to match the API's actual shape:

```ts
export interface PlayersResponse {
  players: (Player & { adp?: Num; proj?: Num })[];
}
```

and update `web/src/pages/Lineup.tsx` to read `suggestQuery.data?.players` in both the
length check and the `.slice(0, 6)` call.

- [ ] **Step 4: Run tests and verify in the browser**

```bash
cd api && ../.venv/Scripts/python.exe -m pytest -q
cd web && npx tsc -b
```
Expected: 166 passed; `tsc` exits 0.

Then start the app (`npm run dev`) and confirm with claude-in-chrome — do not infer
appearance from markup:
- Start/Sit shows a real number in "leaves — points on the bench", not an em-dash
- Start/Sit slot cards show an NFL team instead of `—`
- Waivers rows show real teams instead of `FA` on every row
- Waivers shows the amber ADP-fallback warning when the free-agent cache is missing

- [ ] **Step 5: Commit**

```bash
git add api/app web/src
git commit -m "Fix five verified API/UI contract defects"
```

---

### Task 13: Re-validate accuracy and update the modeling docs

**Files:**
- Modify: `docs/MODELING.md`, `README.md`

- [ ] **Step 1: Re-run the MAE harness**

```bash
cd "C:/Users/mwill/Documents/mwilliams2733/Gridiron GM"
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'api')
from app.db import read_df
from app.models import projections as proj
from app.scoring import score_frame
s = proj.project_season(2025)
for wk in (6, 10, 14):
    w = proj.project_week(2025, wk, season_proj=s)
    raw = read_df('SELECT * FROM weekly_stats WHERE season=2025 AND week=?', (wk,))
    raw['fp'] = score_frame(raw)
    m = w.merge(raw[['player_id','fp']], on='player_id')
    m['ae'] = (m.proj_points - m.fp).abs()
    print(f'week {wk}: n={len(m)} overall MAE={m.ae.mean():.2f}')
    print(m.groupby('position').agg(n=('ae','size'), MAE=('ae','mean')).round(2).to_string())
"
```

**Expected: offensive MAE unchanged** at roughly 4.09–4.37 overall. The scoring
equivalence proof says these numbers must not move; if they did, a previous task
changed behaviour it shouldn't have. Investigate before continuing.

- [ ] **Step 2: Establish the K/DST baseline that does not exist yet**

K and DST moved from Vegas anchors to history in Task 8, so their accuracy is
genuinely unmeasured. Score their 2025 actuals and compare:

```bash
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'api')
from app.db import read_df
from app.models import projections as proj
from app.scoring import score_kicker, score_dst
s = proj.project_season(2025)
k = read_df('SELECT * FROM kicking_stats WHERE season=2025 AND week=10')
k['actual'] = [score_kicker(r) for r in k.to_dict('records')]
w = proj.project_week(2025, 10, season_proj=s)
m = w.merge(k[['player_id','actual']], on='player_id')
print('K   n=%d MAE=%.2f' % (len(m), (m.proj_points-m.actual).abs().mean()))
d = read_df('SELECT * FROM team_defense WHERE season=2025 AND week=10')
d['actual'] = [score_dst(r) for r in d.to_dict('records')]
d['player_id'] = 'DST_' + d.team
m2 = w.merge(d[['player_id','actual']], on='player_id')
print('DST n=%d MAE=%.2f' % (len(m2), (m2.proj_points-m2.actual).abs().mean()))
"
```

Record whatever the numbers are. **If DST MAE exceeds ~4.5 the history model is
worse than the anchor it replaced** — say so plainly rather than shipping it
quietly, and raise reverting Task 8 with the user.

- [ ] **Step 3: Update the docs**

In `docs/MODELING.md`:
- §4: replace the anchored K/DST section with the history-driven model
- §5.1: replace the fixed `flex_share` with declarative `flex_slots`; add the Sundt levels
- §7: note the config-derived slot plan and the ascending-breadth fill order
- §8.2: add the re-measured MAE table, and a new §8.4 for the K/DST baseline
- §9: remove `K_BASE_PPG`, `K_IMPLIED_GAIN`, `DST_BASE_PPG`, `DST_IMPLIED_GAIN`; add `flex_slots` shares
- §10: strike 10.2, 10.3 and 10.6 as done; add the ROS horizon note

In `README.md`, add a Sleeper section: no credentials needed, `platform: sleeper`,
`sleeper_league_id` from the league URL, and that `npm run sync` covers both platforms.

- [ ] **Step 4: Run the full suite and commit**

Run: `cd api && ../.venv/Scripts/python.exe -m pytest -q`
Expected: 166 passed.

```bash
git add docs/MODELING.md README.md
git commit -m "Re-validate accuracy and document the superflex and Sleeper work"
```

---

## Plan Self-Review

**Spec coverage:** §4.1→T1, §4.2→T2, §4.3→T11, §4.4→T3/T6/T7, §4.5→T4, §4.6→T5, §4.7→T8, §4.8→T10/T11, §4.9→T9, §4.10→T10, §5→T12, §6→task order, §7→tests throughout, §8.1→T13 Step 2. No gaps.

**Type consistency:** `scoring_terms`/`score_frame`/`profile_key` (T1) are used with those exact names in T2, T3, T8. `flex_slot_defs` (T4) is consumed by `slot_plan` (T5). `_brute_force_best(recs, cfg)` has the same two-argument signature in T5's implementation and tests. `resolve_sleeper_player(entry, players, espn_lut)` (T10) is called with three arguments in `parse_roster`. `normalize_weekly(df)` loses `rec_val` in T3 and all six call sites are updated in the same task.

**Known deviation from spec:** §4.2's profile-keyed caches, implemented instead as a raw/scored split — rationale in Global Constraints. `profile_key` is still built in T1 because `routers/draft.py:_season_proj_cache` needs it to key `project_season` per league.
