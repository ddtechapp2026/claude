"""Multi-bot engine.

Run with:  python -m src.engine

Every cycle it walks all enabled bots and, for each, runs an isolated virtual
wallet against live Alpaca crypto prices:
  1. mark the wallet to market and snapshot equity,
  2. apply risk controls (stop-loss, take-profit, run-until expiry),
  3. evaluate the bot's strategy spec for entry/exit,
  4. execute the buy/sell inside that bot's own cash - no real orders, no
     interference between bots,
  5. after closed trades, run the AI/heuristic review and auto-apply tuned
     risk params within guardrails.
"""
from __future__ import annotations

import json
import logging
import signal as os_signal
import sys
import time
from datetime import datetime, timezone

from . import llm, market, strategy_engine
from .config import settings
from .database import Database
from .seed import seed_if_empty

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s  %(message)s")
log = logging.getLogger("stonks.engine")

_running = True

# Guardrails the learning loop can never exceed.
_CLAMP = {
    "stop_loss_pct": (0.005, 0.25),
    "take_profit_pct": (0.005, 0.75),
    "size_fraction": (0.05, 1.0),
}
_MIN_NOTIONAL = 1.0


def _handle_stop(signum, frame):
    global _running
    log.info("Signal %s received; stopping after this cycle.", signum)
    _running = False


def _clamp(field: str, value: float) -> float:
    lo, hi = _CLAMP[field]
    return max(lo, min(hi, value))


def _expired(run_until: str | None) -> bool:
    if not run_until:
        return False
    try:
        return datetime.now(timezone.utc) >= datetime.fromisoformat(run_until)
    except ValueError:
        return False


def _sell(db: Database, bot: dict, wallet: dict, price: float, reason: str) -> float:
    qty = wallet["position_qty"]
    entry = wallet["entry_price"] or price
    proceeds = qty * price
    pnl = proceeds - qty * entry
    db.update_wallet(bot["id"], cash=wallet["cash"] + proceeds, position_qty=0.0,
                     entry_price=None, realized_pnl=wallet["realized_pnl"] + pnl,
                     equity=wallet["cash"] + proceeds)
    db.record_trade(bot["id"], "SELL", qty, price, proceeds, pnl, reason)
    log.info("[%s] SELL %.6f @ %.2f  pnl=%+.2f (%s)", bot["name"], qty, price, pnl, reason)
    return pnl


def _buy(db: Database, bot: dict, wallet: dict, price: float, size_fraction: float, reason: str) -> None:
    notional = min(bot["max_trade_usd"], wallet["cash"] * size_fraction)
    if notional < _MIN_NOTIONAL or notional > wallet["cash"]:
        return
    qty = notional / price
    db.update_wallet(bot["id"], cash=wallet["cash"] - notional, position_qty=qty,
                     entry_price=price, equity=wallet["cash"])  # equity unchanged by a buy
    db.record_trade(bot["id"], "BUY", qty, price, notional, None, reason)
    log.info("[%s] BUY  %.6f @ %.2f  ($%.2f) (%s)", bot["name"], qty, price, notional, reason)


def run_bot(db: Database, bot: dict) -> None:
    wallet = db.get_wallet(bot["id"])
    if wallet is None:
        return

    closes = market.get_closes(bot["symbol"])
    price = closes[-1] if closes else None
    if price is None:
        db.update_bot(bot["id"], status="no data", last_cycle=None)
        return

    qty = wallet["position_qty"]
    equity = wallet["cash"] + qty * price
    db.update_wallet(bot["id"], equity=equity)
    db.record_equity(bot["id"], equity)

    # Run-until expiry: close any position and turn the bot off.
    if _expired(bot["run_until"]):
        if qty > 0:
            _sell(db, bot, wallet, price, "run ended")
        db.update_bot(bot["id"], enabled=0, status="finished",
                      last_reason="run window ended", last_cycle=_now())
        return

    try:
        spec = strategy_engine.validate_spec(json.loads(bot["strategy_spec"] or "{}"))
    except Exception as exc:  # noqa: BLE001
        db.update_bot(bot["id"], status="bad strategy", last_reason=str(exc), last_cycle=_now())
        return

    has_position = qty > 0
    decision = None

    if has_position:
        entry = wallet["entry_price"] or price
        if bot["stop_loss_pct"] > 0 and price <= entry * (1 - bot["stop_loss_pct"]):
            decision = strategy_engine.Decision(strategy_engine.SELL, "stop-loss")
        elif bot["take_profit_pct"] > 0 and price >= entry * (1 + bot["take_profit_pct"]):
            decision = strategy_engine.Decision(strategy_engine.SELL, "take-profit")
        else:
            decision = strategy_engine.evaluate(spec, closes, has_position=True, entry_price=entry)
    else:
        decision = strategy_engine.evaluate(spec, closes, has_position=False)

    db.record_signal(bot["id"], decision.signal, price, decision.reason)

    closed = False
    if decision.signal == strategy_engine.SELL and has_position:
        _sell(db, bot, wallet, price, decision.reason)
        closed = True
    elif decision.signal == strategy_engine.BUY and not has_position:
        _buy(db, bot, wallet, price, spec.get("size_fraction", 1.0), decision.reason)

    db.update_bot(bot["id"], status="running", last_reason=decision.reason, last_cycle=_now())

    if closed and bot["auto_adjust"]:
        _maybe_review(db, bot)


def _maybe_review(db: Database, bot: dict) -> None:
    """Run the learning loop every N closed trades and auto-apply within guardrails."""
    closed = db.closed_trades(bot["id"], limit=100)
    n = len(closed)
    if n < settings.ai_review_min_trades or (n % settings.ai_review_min_trades) != 0:
        return

    fresh = db.get_bot(bot["id"])  # current values before tuning
    changes, summary, source = llm.review_trades(fresh, closed[: settings.ai_review_min_trades * 2])
    applied: dict = {}

    for field in ("stop_loss_pct", "take_profit_pct", "size_fraction"):
        if field not in changes:
            continue
        proposed = changes[field]
        if field == "size_fraction":
            spec = strategy_engine.validate_spec(json.loads(fresh["strategy_spec"] or "{}"))
            current = float(spec.get("size_fraction", 1.0))
            new = current * 0.85 if proposed is None else float(proposed)
            new = _clamp(field, new)
            spec["size_fraction"] = new
            db.update_bot(bot["id"], strategy_spec=json.dumps(spec))
            applied[field] = round(new, 4)
        else:
            current = float(fresh[field])
            factor = 0.8 if field == "stop_loss_pct" else 1.0
            new = current * factor if proposed is None else float(proposed)
            new = _clamp(field, new)
            db.update_bot(bot["id"], **{field: new})
            applied[field] = round(new, 4)

    db.record_review(bot["id"], summary, applied, source)
    if applied:
        log.info("[%s] learning (%s): %s | %s", bot["name"], source, summary, applied)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    os_signal.signal(os_signal.SIGINT, _handle_stop)
    os_signal.signal(os_signal.SIGTERM, _handle_stop)

    db = Database(settings.database_path)
    created = seed_if_empty(db)
    if created:
        log.info("Seeded %d starter bots (all OFF; enable them from the dashboard).", created)

    log.info("Engine up | interval=%ss | timeframe=%s | AI=%s | model=%s",
             settings.poll_interval_seconds, settings.bar_timeframe,
             "on" if settings.ai_enabled else "off (rule-based)", settings.openrouter_model)

    while _running:
        cycle_start = time.monotonic()
        try:
            for bot in db.list_bots():
                if not _running:
                    break
                if bot["enabled"]:
                    try:
                        run_bot(db, bot)
                    except Exception as exc:  # noqa: BLE001 - isolate one bot's failure
                        log.exception("[%s] cycle error: %s", bot["name"], exc)
                        db.update_bot(bot["id"], status="error", last_reason=str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("Engine loop error: %s", exc)

        elapsed = time.monotonic() - cycle_start
        remaining = max(0.0, settings.poll_interval_seconds - elapsed)
        slept = 0.0
        while _running and slept < remaining:
            time.sleep(min(1.0, remaining - slept))
            slept += 1.0

    log.info("Engine stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
