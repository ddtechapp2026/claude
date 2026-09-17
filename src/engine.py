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

from . import indicators, llm, market, strategy_engine
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


def _snapshot(closes: list[float]) -> dict:
    """Indicator snapshot recorded with every decision, for the learning dataset."""
    def r(v):
        return round(v, 4) if isinstance(v, (int, float)) else v
    return {
        "price": r(closes[-1]) if closes else None,
        "bars": len(closes),
        "rsi14": r(indicators.rsi(closes, 14)),
        "sma10": r(indicators.sma(closes, 10)),
        "sma30": r(indicators.sma(closes, 30)),
        "ema9": r(indicators.ema(closes, 9)),
        "ema21": r(indicators.ema(closes, 21)),
        "pct_change4": r(indicators.pct_change(closes, 4)),
        "high20": r(indicators.highest(closes, 20)),
        "low20": r(indicators.lowest(closes, 20)),
    }


def _candidate_symbols(bot: dict) -> list[str]:
    """Symbols to consider when the bot is flat. 'AUTO' scans the universe."""
    if (bot["symbol"] or "").upper() == "AUTO":
        return list(settings.crypto_universe)
    return [bot["symbol"]]


def _sell(db: Database, bot: dict, wallet: dict, symbol: str, price: float, reason: str) -> float:
    qty = wallet["position_qty"]
    entry = wallet["entry_price"] or price
    fee_pct = bot.get("fee_pct", 0.0) or 0.0
    proceeds = qty * price
    buy_notional = qty * entry
    sell_fee = proceeds * fee_pct
    buy_fee = buy_notional * fee_pct          # fee paid when this position was opened
    gross = proceeds - buy_notional           # price-only round-trip P&L
    round_fee = buy_fee + sell_fee
    net = gross - round_fee                    # after both fees
    db.update_wallet(bot["id"], cash=wallet["cash"] + proceeds - sell_fee, position_qty=0.0,
                     position_symbol=None, entry_price=None,
                     realized_pnl=wallet["realized_pnl"] + net,
                     gross_realized=wallet["gross_realized"] + gross,
                     equity=wallet["cash"] + proceeds - sell_fee)
    db.record_trade(bot["id"], symbol, "SELL", qty, price, proceeds, net, reason,
                    fee=round_fee, gross_pnl=gross)
    log.info("[%s] SELL %s %.6f @ %.2f  gross=%+.2f fee=%.2f net=%+.2f (%s)",
             bot["name"], symbol, qty, price, gross, round_fee, net, reason)
    return net


def _buy(db: Database, bot: dict, wallet: dict, symbol: str, price: float,
         size_fraction: float, reason: str) -> bool:
    fee_pct = bot.get("fee_pct", 0.0) or 0.0
    # Reserve room for the buy fee so we never overspend the wallet.
    notional = min(bot["max_trade_usd"], wallet["cash"] / (1 + fee_pct) * size_fraction)
    fee = notional * fee_pct
    if notional < _MIN_NOTIONAL or (notional + fee) > wallet["cash"]:
        return False
    qty = notional / price
    db.update_wallet(bot["id"], cash=wallet["cash"] - notional - fee, position_qty=qty,
                     position_symbol=symbol, entry_price=price,
                     equity=wallet["cash"] - fee)  # equity drops by the fee only
    db.record_trade(bot["id"], symbol, "BUY", qty, price, notional, None, reason, fee=fee)
    log.info("[%s] BUY  %s %.6f @ %.2f  ($%.2f, fee %.2f) (%s)",
             bot["name"], symbol, qty, price, notional, fee, reason)
    return True


def run_bot(db: Database, bot: dict) -> None:
    wallet = db.get_wallet(bot["id"])
    if wallet is None:
        return

    try:
        spec = strategy_engine.validate_spec(json.loads(bot["strategy_spec"] or "{}"))
    except Exception as exc:  # noqa: BLE001
        db.update_bot(bot["id"], status="bad strategy", last_reason=str(exc), last_cycle=_now())
        return

    qty = wallet["position_qty"]
    has_position = qty > 0
    closed = False

    if has_position:
        # Manage the held position (whatever symbol it's in).
        symbol = wallet["position_symbol"] or bot["symbol"]
        closes = market.get_closes(symbol)
        price = closes[-1] if closes else None
        if price is None:
            db.update_bot(bot["id"], status="no data for %s" % symbol, last_cycle=_now())
            return
        equity = wallet["cash"] + qty * price
        db.update_wallet(bot["id"], equity=equity)
        db.record_equity(bot["id"], equity)

        entry = wallet["entry_price"] or price
        if _expired(bot["run_until"]):
            _sell(db, bot, wallet, symbol, price, "run ended")
            db.update_bot(bot["id"], enabled=0, status="finished",
                          last_reason="run window ended", last_cycle=_now())
            return
        if bot["stop_loss_pct"] > 0 and price <= entry * (1 - bot["stop_loss_pct"]):
            decision = strategy_engine.Decision(strategy_engine.SELL, "stop-loss")
        elif bot["take_profit_pct"] > 0 and price >= entry * (1 + bot["take_profit_pct"]):
            decision = strategy_engine.Decision(strategy_engine.SELL, "take-profit")
        else:
            decision = strategy_engine.evaluate(spec, closes, has_position=True, entry_price=entry)

        db.record_signal(bot["id"], symbol, decision.signal, price, decision.reason, _snapshot(closes))
        if decision.signal == strategy_engine.SELL:
            _sell(db, bot, wallet, symbol, price, decision.reason)
            closed = True
        db.update_bot(bot["id"], status="running", last_reason=f"{symbol}: {decision.reason}",
                      last_cycle=_now())
    else:
        # Flat: mark cash equity, then scan candidate symbols for an entry.
        db.update_wallet(bot["id"], equity=wallet["cash"])
        db.record_equity(bot["id"], wallet["cash"])
        if _expired(bot["run_until"]):
            db.update_bot(bot["id"], enabled=0, status="finished",
                          last_reason="run window ended", last_cycle=_now())
            return

        chosen = None
        scanned = {}
        for symbol in _candidate_symbols(bot):
            closes = market.get_closes(symbol)
            if not closes:
                continue
            decision = strategy_engine.evaluate(spec, closes, has_position=False)
            scanned[symbol] = decision.signal
            if decision.signal == strategy_engine.BUY:
                chosen = (symbol, closes, decision)
                break

        if chosen:
            symbol, closes, decision = chosen
            price = closes[-1]
            context = _snapshot(closes)
            context["scanned"] = scanned
            db.record_signal(bot["id"], symbol, "BUY", price, decision.reason, context)
            _buy(db, bot, wallet, symbol, price, spec.get("size_fraction", 1.0), decision.reason)
            db.update_bot(bot["id"], status="running", last_reason=f"{symbol}: {decision.reason}",
                          last_cycle=_now())
        else:
            # No entry anywhere — still log the move (HOLD) with what we saw.
            first = _candidate_symbols(bot)[0]
            closes = market.get_closes(first)
            price = closes[-1] if closes else None
            context = _snapshot(closes)
            context["scanned"] = scanned
            reason = "no entry across universe" if len(scanned) > 1 else "entry rule not met"
            db.record_signal(bot["id"], first, "HOLD", price, reason, context)
            db.update_bot(bot["id"], status="running", last_reason=reason, last_cycle=_now())

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
