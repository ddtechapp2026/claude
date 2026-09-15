"""FastAPI dashboard.

Run with:  uvicorn src.dashboard.app:app --host 127.0.0.1 --port 8000

Serves a single HTML page plus a JSON API the page polls for live updates.
Live account/position data comes from Alpaca; history comes from the SQLite DB
the bot writes to. Designed to sit behind nginx (see deploy/), which is what
adds the password login and TLS.
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from ..alpaca_client import AlpacaClient
from ..config import settings
from ..database import Database

app = FastAPI(title="Stonks Dashboard")

_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_db = Database(settings.database_path)

# Alpaca calls are cached briefly so a page full of viewers can't hammer the API.
_cache: dict = {"account": None, "positions": None, "ts": 0.0}
_CACHE_TTL = 10.0  # seconds


def _get_client() -> AlpacaClient | None:
    if not settings.has_credentials:
        return None
    try:
        return AlpacaClient(settings)
    except Exception:  # noqa: BLE001
        return None


def _live_snapshot() -> dict:
    """Account + positions from Alpaca, cached for a few seconds."""
    now = time.monotonic()
    if _cache["account"] is not None and (now - _cache["ts"]) < _CACHE_TTL:
        return {"account": _cache["account"], "positions": _cache["positions"], "error": None}

    client = _get_client()
    if client is None:
        return {"account": None, "positions": [], "error": "Alpaca credentials not configured (.env)."}

    try:
        account = client.get_account()
        positions = client.get_positions()
        _cache.update(account=account, positions=positions, ts=now)
        return {"account": account, "positions": positions, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"account": None, "positions": [], "error": f"Alpaca error: {exc}"}


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/api/status")
def api_status() -> JSONResponse:
    live = _live_snapshot()
    payload = {
        "config": {
            "symbol": settings.trade_symbol,
            "paper": settings.paper,
            "dry_run": settings.dry_run,
            "order_notional_usd": settings.order_notional_usd,
            "poll_interval_seconds": settings.poll_interval_seconds,
        },
        "bot_status": _db.all_status(),
        "account": live["account"],
        "positions": live["positions"],
        "trades": _db.recent_trades(25),
        "signals": _db.recent_signals(15),
        "equity_curve": _db.equity_curve(500),
        "error": live["error"],
    }
    return JSONResponse(payload)


@app.get("/")
def index(request: Request):
    # Starlette signature: request first, then template name.
    return _templates.TemplateResponse(request, "index.html")
