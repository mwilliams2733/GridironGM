# Findings

Dated investigation notes. Live invariants and commands belong in README.md.

## ESPN live-draft sync is unverified (2026-08-12)

`POST /api/draft/sync-espn` reads ESPN's `mDraftDetail` view directly rather than
going through `espn_api`'s `League.draft`. The reason, from `espn_api` 0.46
`BaseLeague._fetch_draft`:

```python
if not data.get('draftDetail', {}).get('drafted'):
    return
```

`drafted` is the flag ESPN sets when a draft **completes**, so `league.draft` is
expected to return nothing while a draft is running — precisely the case we need.
`etl/espn.py:fetch_draft_picks` reads `draftDetail.picks` regardless of the flag.

**What is verified:** the merge. `tests/test_espn_draft_sync.py` covers
idempotent re-sync, append-only behaviour, no duplicate players, no duplicate
overall pick numbers on a corrected pick, slot derived from round-1 order rather
than ESPN's arbitrary `teamId`, and 502-not-500 on an ESPN failure.

**What is NOT verified:** that ESPN populates `draftDetail.picks` incrementally
during a live draft, and the exact payload field names mid-draft. No ESPN
credentials were configured at the time of writing (`espn_settings()` returned
league_id/espn_s2/swid all unset), so no call was ever made against a real league.

**Before draft day:** run an ESPN mock draft, make a few picks, and hit
`POST /api/draft/sync-espn?league_id=<id>`. Confirm `added` climbs as picks are
made. If it stays 0, the flag or field names differ mid-draft — manual entry is
unaffected and remains the primary path.

Related: ESPN team ids are arbitrary and unrelated to draft position, so draft
slot is derived from the order teams actually picked in round 1.

## Odds cache holds the whole season (2026-08-12)

`odds_games` carries no `week` column and books price all 272 games by August, so
`game_lines()` unscoped returns 544 team-rows (32 teams × 17 weeks). Ranking
"top 16 implied totals" over that surfaced the same team up to five times. Week is
recovered by joining `schedules` on `(season, home_team, away_team)`, which is
unique — 0 duplicate pairs across 272 games, and team codes match exactly between
the odds feed and nflverse (32/32, no diff either direction).

## web/src/lib/types.ts drifts from the API (2026-08-12)

Seven field mismatches shipped at once because `types.ts` is hand-written and most
interfaces end with `[key: string]: unknown`, which makes any property access
typecheck. Only mismatches feeding a numeric formatter get caught (the index
signature yields `unknown`, which `fmt1(x: Num)` rejects); name/string mismatches
render blank and look like missing data. Fixed in `c6b9550`. The durable fix is
generating the file from `/openapi.json`.
