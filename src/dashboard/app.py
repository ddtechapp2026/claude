"""FastAPI dashboard + control API for the multi-bot engine.

Run with:  uvicorn src.dashboard.app:app --host 127.0.0.1 --port 8000

Read endpoints power the tabbed UI; write endpoints let you turn bots on/off,
edit their controls, and set a strategy in plain English (translated to a spec
via OpenRouter, with a rule-based fallback). Sits behind nginx Basic Auth.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import Request

from .. import backtest, llm, market, strategy_engine
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


@app.get("/api/ai/test")
def api_ai_test(model: str = "") -> JSONResponse:
    """Make one real OpenRouter call and report the result, for diagnostics."""
    return JSONResponse(llm.diagnostic(model or None))


# Live list of free models, cached so the dropdown always shows newly released
# ":free" models without hammering OpenRouter.
_models_cache: dict = {"ts": 0.0, "free": []}
_MODELS_TTL = 600.0  # 10 minutes


@app.get("/api/models")
def api_models(refresh: int = 0) -> JSONResponse:
    now = time.monotonic()
    if refresh or not _models_cache["free"] or (now - _models_cache["ts"]) > _MODELS_TTL:
        try:
            resp = requests.get(f"{settings.openrouter_base_url}/models", timeout=20)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            free = sorted(
                m["id"] for m in data
                if str(m.get("id", "")).endswith(":free")
                or (str((m.get("pricing") or {}).get("prompt", "1")) in ("0", "0.0")
                    and str((m.get("pricing") or {}).get("completion", "1")) in ("0", "0.0"))
            )
            _models_cache.update(ts=now, free=free)
            return JSONResponse({"free": free, "cached": False, "count": len(free)})
        except Exception as exc:  # noqa: BLE001
            # Fall back to whatever we have cached (may be empty on first failure).
            return JSONResponse({"free": _models_cache["free"], "cached": True,
                                 "count": len(_models_cache["free"]), "error": str(exc)})
    return JSONResponse({"free": _models_cache["free"], "cached": True,
                         "count": len(_models_cache["free"])})


@app.get("/api/bots")
def api_bots() -> JSONResponse:
    bots = [_bot_view(b) for b in _db.list_bots()]
    return JSONResponse({"bots": bots, "ai_enabled": settings.ai_enabled,
                         "model": settings.effective_model,
                         "free_only": settings.openrouter_free_only,
                         "universe": list(settings.crypto_universe),
                         "alpaca_enabled": settings.alpaca_trading_enabled,
                         "alpaca_paper": settings.alpaca_paper})


@app.get("/api/overview")
def api_overview() -> JSONResponse:
    """Portfolio-wide totals and per-bot ranking for the Overview tab."""
    bots = [_bot_view(b) for b in _db.list_bots()]
    agg = {"gross_realized": 0.0, "fees": 0.0, "est_tax": 0.0,
           "net_realized": 0.0, "unrealized": 0.0, "total_pnl": 0.0}
    total_equity = total_start = 0.0
    running = live = 0
    wins = losses = 0
    for b in bots:
        c = b["costs"]
        for k in agg:
            agg[k] += c.get(k, 0.0)
        total_equity += b["equity"]
        total_start += b["starting_cash"]
        running += 1 if b["enabled"] else 0
        live += 1 if b.get("live_trading") else 0
        for t in _db.closed_trades(b["id"], limit=100000):
            if (t.get("pnl") or 0) > 0:
                wins += 1
            elif (t.get("pnl") or 0) < 0:
                losses += 1
    ranking = sorted(
        ({"id": b["id"], "name": b["name"], "symbol": b["symbol"],
          "enabled": b["enabled"], "live_trading": b.get("live_trading", 0),
          "equity": b["equity"], "pnl": b["pnl"], "pnl_pct": b["pnl_pct"],
          "total_pnl": b["costs"]["total_pnl"]} for b in bots),
        key=lambda x: x["total_pnl"], reverse=True)
    closed = wins + losses
    return JSONResponse({
        "totals": {
            "equity": total_equity, "starting": total_start,
            "pnl": total_equity - total_start,
            "pnl_pct": ((total_equity - total_start) / total_start * 100) if total_start else 0.0,
            **agg,
            "bots": len(bots), "running": running, "live": live,
            "closed_trades": closed, "wins": wins,
            "win_rate": (wins / closed * 100) if closed else 0.0,
        },
        "ranking": ranking,
        "alpaca_enabled": settings.alpaca_trading_enabled,
        "alpaca_paper": settings.alpaca_paper,
    })


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
    model: Optional[str] = None                # per-bot AI model ('' = global default)
    live_trading: Optional[bool] = None        # also send real orders to Alpaca


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
    if cfg.model is not None:
        fields["model"] = cfg.model.strip()
    if cfg.live_trading is not None:
        fields["live_trading"] = 1 if cfg.live_trading else 0
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
    spec, summary, source = llm.translate_strategy(body.text, model=bot.get("model") or None)
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


class BacktestIn(BaseModel):
    symbol: str = "BTC/USD"
    strategy_text: Optional[str] = None   # plain English -> spec (if no from_bot)
    from_bot: Optional[int] = None        # copy an existing bot's strategy
    days: float = 30
    timeframe: str = "auto"               # "auto" or 1Min/5Min/15Min/1Hour/1Day
    starting_cash: float = 10000
    max_trade_usd: float = 1000
    stop_loss_pct: float = 5              # percent
    take_profit_pct: float = 10           # percent
    fee_pct: float = 0.1                  # percent
    size_fraction: float = 50            # percent of cash per buy
    ai_control: bool = True
    model: Optional[str] = None


@app.post("/api/backtest")
def api_backtest(body: BacktestIn) -> JSONResponse:
    # Resolve the strategy spec.
    source = "spec"
    summary = ""
    if body.from_bot:
        bot = _db.get_bot(body.from_bot)
        if not bot:
            raise HTTPException(404, "bot not found")
        spec = strategy_engine.validate_spec(json.loads(bot["strategy_spec"] or "{}"))
    elif body.strategy_text:
        spec, summary, source = llm.translate_strategy(body.strategy_text, model=body.model)
    else:
        raise HTTPException(400, "provide strategy_text or from_bot")

    # A backtest needs a concrete coin, never "AUTO".
    symbol = (body.symbol or "").strip()
    if symbol.upper() == "AUTO" or "/" not in symbol:
        symbol = settings.crypto_universe[0] if settings.crypto_universe else "BTC/USD"

    # Resolve timeframe (finest available is 1-minute) and cap the window so a
    # fine granularity can't fetch a runaway number of bars.
    tf = body.timeframe if body.timeframe in market.TIMEFRAMES else market.timeframe_for_days(body.days)
    eff_days = market.cap_days(body.days, tf)
    note = None
    if eff_days < body.days:
        note = (f"Window shortened to the most recent {eff_days:g} days: {tf} bars over "
                f"{body.days:g} days is too many to fetch/replay. Use a coarser timeframe "
                f"for a longer window.")
    try:
        bars = market.get_bars(symbol, eff_days, timeframe=tf)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"could not fetch history: {exc}"}, status_code=200)

    result = backtest.run_backtest(
        bars, spec=spec,
        starting_cash=body.starting_cash, max_trade_usd=body.max_trade_usd,
        stop_pct=body.stop_loss_pct / 100.0, tp_pct=body.take_profit_pct / 100.0,
        fee_pct=body.fee_pct / 100.0, size_fraction=body.size_fraction / 100.0,
        ai_control=body.ai_control,
    )
    result["spec_summary"] = strategy_engine.describe(spec)
    result["source"] = source
    result["timeframe"] = tf
    if note:
        result["note"] = note
    return JSONResponse(result)


@app.get("/")
def index(request: Request):
    return _templates.TemplateResponse(request, "index.html")
