"""GridironGM API — FastAPI application shell."""
from __future__ import annotations

import logging
from importlib import import_module

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import league_config
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


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/config")
def get_config() -> dict:
    return league_config()


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
