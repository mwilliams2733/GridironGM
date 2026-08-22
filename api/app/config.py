"""Configuration: league settings from config/league.yaml, secrets from env / shared.env.

Secrets are NEVER logged, printed, or committed. The Odds API key is exposed as
ODDS_API_KEY; the user's shared.env stores it under the name `TheODDSAPI`, so we
accept both.
"""
from __future__ import annotations

import copy
import os
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config" / "league.yaml"
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "gridiron.db"
PARQUET_DIR = DATA_DIR / "cache"
ESPN_CACHE_DIR = DATA_DIR / "espn_cache"
DRAFTS_DIR = DATA_DIR / "drafts"

SECRETS_FILE = Path(os.environ.get("GRIDIRON_SECRETS", r"C:\Users\mwill\.secrets\shared.env"))

# env-var aliases: canonical name -> acceptable names in env or shared.env
_SECRET_ALIASES = {
    "ODDS_API_KEY": ["ODDS_API_KEY", "TheODDSAPI", "THE_ODDS_API_KEY"],
    "ESPN_S2": ["ESPN_S2", "espn_s2"],
    "SWID": ["SWID", "swid"],
    "ESPN_LEAGUE_ID": ["ESPN_LEAGUE_ID", "LEAGUE_ID"],
}


@lru_cache(maxsize=1)
def _parse_secrets_file() -> dict[str, str]:
    values: dict[str, str] = {}
    if SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip("'\"")
    return values


def get_secret(name: str) -> str | None:
    """Resolve a secret: process env first, then shared.env, honoring aliases."""
    aliases = _SECRET_ALIASES.get(name, [name])
    for alias in aliases:
        if os.environ.get(alias):
            return os.environ[alias]
    file_vals = _parse_secrets_file()
    for alias in aliases:
        if file_vals.get(alias):
            return file_vals[alias]
    return None


@lru_cache(maxsize=1)
def _raw_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=1)
def leagues() -> tuple[dict, ...]:
    """Configured leagues, newest shape first.

    Scoring and roster rules are shared across leagues; a league entry only
    overrides what genuinely differs (team count, draft slot, ESPN id). A config
    with no `leagues:` block yields a single league derived from `league:`, so
    older single-league configs keep working untouched.
    """
    raw = _raw_config()
    entries = raw.get("leagues") or []
    if not entries:
        base = raw.get("league", {}) or {}
        entries = [{
            "id": "default",
            "name": base.get("name", "My League"),
            "teams": base.get("teams", 12),
            "espn_league_id": (raw.get("espn", {}) or {}).get("league_id"),
        }]
    out = []
    for i, e in enumerate(entries):
        entry = dict(e)
        entry.setdefault("id", f"league{i + 1}")
        entry.setdefault("name", entry["id"])
        out.append(entry)
    return tuple(out)


def league_ids() -> list[str]:
    return [lg["id"] for lg in leagues()]


def active_league_id() -> str:
    """The league used when a caller does not name one."""
    configured = _raw_config().get("active")
    ids = league_ids()
    if configured and configured in ids:
        return configured
    return ids[0]


class UnknownLeague(KeyError):
    """Raised for a league id that is not in config/league.yaml.

    Subclasses KeyError so existing `except KeyError` handling still catches it,
    while letting the API map it to a 404 rather than a 500.
    """


def resolve_league(league_id: str | None = None) -> str:
    """Validate a league id, falling back to the active league when None."""
    if league_id is None:
        return active_league_id()
    if league_id not in league_ids():
        raise UnknownLeague(
            f"unknown league '{league_id}' (configured: {', '.join(league_ids())})"
        )
    return league_id


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into a copy of ``base``.

    Nested dicts merge key by key; every other value (including lists such as
    `points_allowed_tiers` and `flex_eligible`) is replaced wholesale, since a
    partial list override has no sensible meaning.
    """
    out = copy.deepcopy(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = copy.deepcopy(val)
    return out


@lru_cache(maxsize=32)
def league_config(league_id: str | None = None) -> dict:
    """Full config for one league: shared blocks with that league's overrides merged in.

    The returned dict keeps the exact shape callers already read
    (``cfg["league"]["teams"]``, ``cfg["roster"]["starters"]``), so threading a
    league through the app does not change any downstream access.
    """
    lid = resolve_league(league_id)
    entry = next(lg for lg in leagues() if lg["id"] == lid)

    cfg = copy.deepcopy(_raw_config())
    cfg.pop("leagues", None)
    cfg.pop("active", None)

    league = cfg.setdefault("league", {})
    league["id"] = entry["id"]
    league["name"] = entry.get("name", league.get("name"))
    if entry.get("teams") is not None:
        league["teams"] = entry["teams"]
    for key in ("playoff_week_start", "sleeper_league_id", "sleeper_user_id",
                "sleeper_draft_id", "platform"):
        if entry.get(key) is not None:
            league[key] = entry[key]

    if entry.get("draft"):
        cfg.setdefault("draft", {}).update(entry["draft"])

    espn = cfg.setdefault("espn", {})
    if entry.get("espn_league_id") is not None:
        espn["league_id"] = entry["espn_league_id"]
    # Per-league cookies are optional; a single ESPN account covers all leagues.
    for key in ("espn_s2", "swid"):
        if entry.get(key):
            espn[key] = entry[key]

    # Future-proofing: a league may still override shared blocks if it ever needs to.
    # DEEP merge, not `dict.update`: a shallow update replaces a whole sub-block,
    # so a league overriding only `roster.starters` would silently lose `bench`,
    # `ir` and `flex_eligible`, and one overriding only `scoring.receiving` would
    # lose passing/rushing/kicking/dst -- with no error until something downstream
    # KeyErrors or, worse, quietly scores nothing.
    for block in ("scoring", "roster", "waivers"):
        if entry.get(block):
            cfg[block] = _deep_merge(cfg.get(block) or {}, entry[block])

    validate_league_config(cfg, lid)
    return cfg


# Sane bound for a configured playoff start week. NFL regular season is 18
# weeks; a league could plausibly start playoffs anywhere from week 2 (absurd
# but not malformed) through week 18. Anything outside this is almost
# certainly a transcription error, and `last_scoring_week` turns it into a
# silent zero-ROS failure rather than an error, so it must fail loudly here.
_PLAYOFF_WEEK_MIN = 1
_PLAYOFF_WEEK_MAX = 18


def validate_league_config(cfg: dict, league_id: str) -> None:
    """Fail loudly at load time on config shapes that would otherwise corrupt
    silently downstream (see `flex_slot_defs` and `last_scoring_week`)."""
    start = cfg["league"].get("playoff_week_start")
    if start is not None:
        if not isinstance(start, int) or isinstance(start, bool) or not (
            _PLAYOFF_WEEK_MIN <= start <= _PLAYOFF_WEEK_MAX
        ):
            raise ValueError(
                f"league '{league_id}': playoff_week_start must be an integer in "
                f"[{_PLAYOFF_WEEK_MIN}, {_PLAYOFF_WEEK_MAX}], got {start!r}"
            )

    flex_slots = cfg.get("roster", {}).get("flex_slots")
    if flex_slots:
        for label, d in flex_slots.items():
            share = d.get("share") or {}
            total = sum(share.values())
            if abs(total - 1.0) > 1e-6:
                raise ValueError(
                    f"league '{league_id}': roster.flex_slots.{label}.share must "
                    f"sum to 1.0, got {total!r} ({share!r})"
                )


def reload_config() -> dict:
    _raw_config.cache_clear()
    leagues.cache_clear()
    league_config.cache_clear()
    return league_config()


def current_season() -> int:
    """Season is global — every league is played in the same NFL season."""
    return int(_raw_config()["league"]["season"])


def history_seasons() -> list[int]:
    cfg = _raw_config()["league"]
    season = int(cfg["season"])
    n = int(cfg.get("history_seasons", 3))
    return [season - i for i in range(1, n + 1)]  # last n COMPLETED seasons


def espn_settings(league_id: str | None = None) -> dict:
    """ESPN connection settings; secrets resolved at call time, never cached to disk."""
    cfg = league_config(league_id).get("espn", {}) or {}
    return {
        "league_id": cfg.get("league_id") or get_secret("ESPN_LEAGUE_ID"),
        "year": cfg.get("year") or current_season(),
        "espn_s2": cfg.get("espn_s2") or get_secret("ESPN_S2"),
        "swid": cfg.get("swid") or get_secret("SWID"),
    }


def ensure_dirs() -> None:
    for d in (DATA_DIR, PARQUET_DIR, ESPN_CACHE_DIR, DRAFTS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def draft_state_path(league_id: str | None = None, mock: bool = False) -> Path:
    """Where one league's draft picks are persisted.

    Mock drafts live in a separate file so practising can never corrupt the
    board you draft for real — the failure mode being to walk into draft day
    with a couple hundred phantom picks already on it.
    """
    ensure_dirs()
    lid = resolve_league(league_id)
    return DRAFTS_DIR / (f"{lid}.mock.json" if mock else f"{lid}.json")


def league_cache_dir(league_id: str | None = None) -> Path:
    """Per-league ESPN cache directory (teams, free agents, roster, ...)."""
    ensure_dirs()
    d = ESPN_CACHE_DIR / resolve_league(league_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


# Files that lived at the top level back when the app was single-league.
_LEGACY_CACHE_FILES = ("teams", "free_agents", "matchups", "my_team", "manual_roster")


def migrate_legacy_storage() -> list[str]:
    """Move single-league data under the active league's id. Idempotent.

    Only moves a file when the per-league destination does not already exist,
    so a second run can never clobber newer per-league state.
    """
    ensure_dirs()
    lid = active_league_id()
    moved: list[str] = []

    legacy_draft = DATA_DIR / "draft_state.json"
    dest_draft = DRAFTS_DIR / f"{lid}.json"
    if legacy_draft.exists() and not dest_draft.exists():
        legacy_draft.replace(dest_draft)
        moved.append(f"draft_state.json -> drafts/{lid}.json")

    dest_dir = ESPN_CACHE_DIR / lid
    for name in _LEGACY_CACHE_FILES:
        legacy = ESPN_CACHE_DIR / f"{name}.json"
        dest = dest_dir / f"{name}.json"
        if legacy.exists() and not dest.exists():
            dest_dir.mkdir(parents=True, exist_ok=True)
            legacy.replace(dest)
            moved.append(f"espn_cache/{name}.json -> espn_cache/{lid}/{name}.json")

    return moved
