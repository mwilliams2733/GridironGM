"""GridironGM API — FastAPI application shell."""
from __future__ import annotations

import logging
from importlib import import_module

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import (
    UnknownLeague,
    active_league_id,
    league_config,
    leagues,
    migrate_legacy_storage,
)
from .db import init_db
from .etl.sync import run_sync, sync_status

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="GridironGM", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(UnknownLeague)
def _unknown_league(request, exc: UnknownLeague):
    """A bad league_id is a client error, not a server fault."""
    return JSONResponse(status_code=404, content={"detail": str(exc.args[0])})


@app.on_event("startup")
def _startup() -> None:
    init_db()
    for move in migrate_legacy_storage():
        log.info("migrated single-league data: %s", move)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/config")
def get_config(league_id: str | None = None) -> dict:
    try:
        return league_config(league_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/leagues")
def get_leagues() -> dict:
    """Every configured league, for the switcher.

    `configured` reports whether the league is ready for ESPN sync; a league
    with no espn_league_id still drafts fine in manual mode.
    """
    return {
        "active": active_league_id(),
        "leagues": [
            {
                "id": lg["id"],
                "name": lg.get("name", lg["id"]),
                "teams": league_config(lg["id"])["league"]["teams"],
                "my_slot": (league_config(lg["id"]).get("draft") or {}).get("my_slot"),
                "espn_configured": bool(lg.get("espn_league_id")),
            }
            for lg in leagues()
        ],
    }


@app.post("/api/sync")
def sync(scope: str = "all") -> dict:
    return {"results": run_sync(scope)}


@app.get("/api/sync/status")
def get_sync_status() -> dict:
    return {"status": sync_status()}


# Feature routers (draft, waivers, lineup, league, dashboard) mount here.
for _mod in ("league", "draft", "waivers", "lineup", "dashboard"):
    try:
        module = import_module(f".routers.{_mod}", package=__package__)
        app.include_router(module.router, prefix="/api")
    except ModuleNotFoundError:
        log.warning("router %s not present yet", _mod)
