"""Configuration: league settings from config/league.yaml, secrets from env / shared.env.

Secrets are NEVER logged, printed, or committed. The Odds API key is exposed as
ODDS_API_KEY; the user's shared.env stores it under the name `TheODDSAPI`, so we
accept both.
"""
from __future__ import annotations

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
def league_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def reload_config() -> dict:
    league_config.cache_clear()
    return league_config()


def current_season() -> int:
    return int(league_config()["league"]["season"])


def history_seasons() -> list[int]:
    cfg = league_config()["league"]
    season = int(cfg["season"])
    n = int(cfg.get("history_seasons", 3))
    return [season - i for i in range(1, n + 1)]  # last n COMPLETED seasons


def espn_settings() -> dict:
    """ESPN connection settings; secrets resolved at call time, never cached to disk."""
    cfg = league_config().get("espn", {}) or {}
    return {
        "league_id": cfg.get("league_id") or get_secret("ESPN_LEAGUE_ID"),
        "year": cfg.get("year") or current_season(),
        "espn_s2": get_secret("ESPN_S2"),
        "swid": get_secret("SWID"),
    }


def ensure_dirs() -> None:
    for d in (DATA_DIR, PARQUET_DIR, ESPN_CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)
