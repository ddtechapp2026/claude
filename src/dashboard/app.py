"""FastAPI dashboard + control API for the multi-bot engine.

Run with:  uvicorn src.dashboard.app:app --host 127.0.0.1 --port 8000

Read endpoints power the tabbed UI; write endpoints let you turn bots on/off,
edit their controls, and set a strategy in plain English (translated to a spec
via OpenRouter, with a rule-based fallback). Sits behind nginx Basic Auth.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

from .. import llm, strategy_engine
from ..config import settings
from ..database import Database

app = FastAPI(title="Stonks Dashboard")
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_db = Database(settings.database_path)


def _bot_view(bot: dict) -> dict:
    wallet = _db.get_wallet(bot["id"]) or {}
    equity = wallet.get("equity", bot["starting_cash"])
    pnl = equity - bot["starting_cash"]
    try:
        spec = json.loads(bot["strategy_spec"] or "{}")
        summary = strategy_engine.describe(spec)
    except Exception:  # noqa: BLE001
        summary = "(invalid strategy)"

    # --- P&L breakdown: gross trading -> fees -> taxes -> net ---------------
    gross_realized = wallet.get("gross_realized", 0.0) or 0.0
    net_after_fees = wallet.get("realized_pnl", 0.0) or 0.0     # already fee-adjusted
    fees = gross_realized - net_after_fees                       # fees on closed trades
    tax_pct = bot.get("tax_pct", 0.0) or 0.0
    est_tax = tax_pct * max(0.0, net_after_fees)                 # taxed only on net gains
    net_realized = net_after_fees - est_tax
    # Unrealized (open position), net of the exit fee it would incur.
    qty = wallet.get("position_qty", 0.0) or 0.0
    entry = wallet.get("entry_price") or 0.0
    unrealized = 0.0
    if qty > 0 and entry:
        cur_price = (equity - wallet.get("cash", 0.0)) / qty if qty else entry
        unrealized = (cur_price - entry) * qty
    costs = {
        "gross_realized": gross_realized,
        "fees": fees,
        "net_after_fees": net_after_fees,
        "tax_pct": tax_pct,
        "est_tax": est_tax,
        "net_realized": net_realized,
        "unrealized": unrealized,
        "total_pnl": net_realized + unrealized,   # everything accounted for
    }
    return {
        **bot,
        "wallet": wallet,
        "equity": equity,
        "pnl": pnl,
        "pnl_pct": (pnl / bot["starting_cash"] * 100) if bot["starting_cash"] else 0.0,
        "strategy_summary": summary,
        "costs": costs,
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/api/bots")
def api_bots() -> JSONResponse:
    bots = [_bot_view(b) for b in _db.list_bots()]
    return JSONResponse({"bots": bots, "ai_enabled": settings.ai_enabled,
                         "model": settings.openrouter_model,
                         "universe": list(settings.crypto_universe)})


@app.get("/api/bots/{bot_id}/export")
def api_export(bot_id: int) -> JSONResponse:
    """Full decision journal + trades for a bot — the learning dataset."""
    bot = _db.get_bot(bot_id)
    if not bot:
        raise HTTPException(404, "bot not found")
    payload = {
        "bot": {k: bot[k] for k in ("id", "name", "symbol", "strategy_text", "strategy_spec")},
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "trades": _db.all_trades(bot_id),
        "decisions": _db.all_signals(bot_id),
        "reviews": _db.recent_reviews(bot_id, 1000),
    }
    fname = f"bot{bot_id}_{bot['name'].replace(' ', '_')}_data.json"
    return JSONResponse(payload, headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/bots/{bot_id}")
def api_bot(bot_id: int) -> JSONResponse:
    bot = _db.get_bot(bot_id)
    if not bot:
        raise HTTPException(404, "bot not found")
    return JSONResponse({
        "bot": _bot_view(bot),
        "trades": _db.recent_trades(bot_id, 30),
        "signals": _db.recent_signals(bot_id, 15),
        "equity_curve": _db.equity_curve(bot_id, 500),
        "reviews": _db.recent_reviews(bot_id, 10),
    })


# NOTE: Pydantic evaluates these annotations at runtime, so use typing.Optional
# rather than the `X | None` syntax (which needs Python 3.10+; the VM runs 3.8).
class ConfigIn(BaseModel):
    name: Optional[str] = None
    symbol: Optional[str] = None
    starting_cash: Optional[float] = None
    max_trade_usd: Optional[float] = None
    stop_loss_pct: Optional[float] = None      # sent as a percent number, e.g. 5 == 5%
    take_profit_pct: Optional[float] = None
    fee_pct: Optional[float] = None            # per-side trading fee, percent (0.1 == 0.1%)
    tax_pct: Optional[float] = None            # est. tax on net gains, percent (30 == 30%)
    run_until: Optional[str] = None            # ISO ts, "" clears it
    auto_adjust: Optional[bool] = None
    ai_control: Optional[bool] = None


def _norm_pct(v: float | None) -> float | None:
    # The dashboard always sends these as percent numbers (5 == 5%), so convert
    # to the fraction the engine stores. 0.1 -> 0.001, 30 -> 0.30.
    if v is None:
        return None
    return v / 100.0


@app.post("/api/bots/{bot_id}/config")
def api_config(bot_id: int, cfg: ConfigIn) -> JSONResponse:
    bot = _db.get_bot(bot_id)
    if not bot:
        raise HTTPException(404, "bot not found")
    fields: dict = {}
    for key in ("name", "symbol", "max_trade_usd", "starting_cash"):
        val = getattr(cfg, key)
        if val is not None:
            fields[key] = val
    if cfg.stop_loss_pct is not None:
        fields["stop_loss_pct"] = _norm_pct(cfg.stop_loss_pct)
    if cfg.take_profit_pct is not None:
        fields["take_profit_pct"] = _norm_pct(cfg.take_profit_pct)
    if cfg.fee_pct is not None:
        fields["fee_pct"] = _norm_pct(cfg.fee_pct)
    if cfg.tax_pct is not None:
        fields["tax_pct"] = _norm_pct(cfg.tax_pct)
    if cfg.run_until is not None:
        fields["run_until"] = cfg.run_until or None
    if cfg.auto_adjust is not None:
        fields["auto_adjust"] = 1 if cfg.auto_adjust else 0
    if cfg.ai_control is not None:
        fields["ai_control"] = 1 if cfg.ai_control else 0
    _db.update_bot(bot_id, **fields)
    return JSONResponse({"ok": True, "bot": _bot_view(_db.get_bot(bot_id))})


@app.post("/api/bots/{bot_id}/toggle")
def api_toggle(bot_id: int) -> JSONResponse:
    bot = _db.get_bot(bot_id)
    if not bot:
        raise HTTPException(404, "bot not found")
    new_state = 0 if bot["enabled"] else 1
    _db.update_bot(bot_id, enabled=new_state, status="running" if new_state else "idle")
    return JSONResponse({"ok": True, "enabled": bool(new_state)})


class StrategyIn(BaseModel):
    text: str


@app.post("/api/bots/{bot_id}/strategy")
def api_strategy(bot_id: int, body: StrategyIn) -> JSONResponse:
    bot = _db.get_bot(bot_id)
    if not bot:
        raise HTTPException(404, "bot not found")
    spec, summary, source = llm.translate_strategy(body.text)
    _db.update_bot(bot_id, strategy_text=body.text, strategy_spec=json.dumps(spec))
    return JSONResponse({"ok": True, "summary": summary, "source": source, "spec": spec})


@app.post("/api/bots/{bot_id}/reset")
def api_reset(bot_id: int) -> JSONResponse:
    bot = _db.get_bot(bot_id)
    if not bot:
        raise HTTPException(404, "bot not found")
    _db.update_bot(bot_id, enabled=0, status="idle", last_reason="reset")
    _db.reset_wallet(bot_id, bot["starting_cash"])
    return JSONResponse({"ok": True})


@app.get("/")
def index(request: Request):
    return _templates.TemplateResponse(request, "index.html")
