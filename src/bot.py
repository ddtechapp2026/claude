"""The trading bot main loop.

Run with:  python -m src.bot

Each cycle it:
  1. pulls recent prices from Alpaca,
  2. asks strategy.decide() what to do,
  3. places or closes a paper order if the decision says so,
  4. records equity, the signal, and any trade in SQLite,
  5. sleeps until the next cycle.

Everything it writes shows up on the dashboard. It never trades real money
while ALPACA_PAPER=true (the default).
"""
from __future__ import annotations

import logging
import signal as os_signal
import sys
import time
from datetime import datetime, timezone

from . import strategy
from .alpaca_client import AlpacaClient
from .config import settings
from .database import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
log = logging.getLogger("stonks.bot")

_running = True


def _handle_stop(signum, frame):
    global _running
    log.info("Received signal %s, shutting down after this cycle...", signum)
    _running = False


def run_once(client: AlpacaClient, db: Database) -> None:
    """Execute a single decision cycle."""
    symbol = settings.trade_symbol

    # 1. Account snapshot (also proves our credentials work).
    account = client.get_account()
    db.record_equity(account["equity"], account["cash"])

    # 2. Market data + current position.
    closes = client.get_recent_closes(symbol)
    price = closes[-1] if closes else None
    has_position = client.get_position_qty(symbol) > 0

    # 3. Ask the strategy.
    decision = strategy.decide(closes, has_position=has_position)
    db.record_signal(symbol, decision.signal, price, decision.reason)
    log.info(
        "%s @ %s -> %s (%s) | equity=$%.2f position=%s",
        symbol,
        f"{price:.2f}" if price else "n/a",
        decision.signal,
        decision.reason,
        account["equity"],
        has_position,
    )

    # 4. Act on the decision (respecting the position contract).
    if decision.signal == strategy.BUY and not has_position:
        _do_buy(client, db, symbol, price)
    elif decision.signal == strategy.SELL and has_position:
        _do_sell(client, db, symbol, price)

    # 5. Publish status for the dashboard.
    db.set_status("last_cycle", datetime.now(timezone.utc).isoformat())
    db.set_status("last_signal", decision.signal)
    db.set_status("last_reason", decision.reason)
    db.set_status("last_price", f"{price:.2f}" if price else "n/a")
    db.set_status("symbol", symbol)
    db.set_status("dry_run", settings.dry_run)
    db.set_status("paper", settings.paper)


def _do_buy(client: AlpacaClient, db: Database, symbol: str, price: float | None) -> None:
    notional = settings.order_notional_usd
    if settings.dry_run:
        log.info("[DRY RUN] would BUY $%.2f of %s", notional, symbol)
        db.record_trade(symbol, "BUY", None, notional, price, None, "dry_run")
        return
    try:
        result = client.submit_notional_buy(symbol, notional)
        log.info("BUY submitted: $%.2f of %s (order %s, %s)",
                 notional, symbol, result["order_id"], result["status"])
        db.record_trade(symbol, "BUY", None, notional, price,
                        result["order_id"], result["status"])
    except Exception as exc:  # noqa: BLE001 - log and keep the bot alive
        log.exception("BUY failed: %s", exc)
        db.record_trade(symbol, "BUY", None, notional, price, None, f"error: {exc}")


def _do_sell(client: AlpacaClient, db: Database, symbol: str, price: float | None) -> None:
    if settings.dry_run:
        log.info("[DRY RUN] would SELL entire %s position", symbol)
        db.record_trade(symbol, "SELL", None, None, price, None, "dry_run")
        return
    try:
        result = client.submit_close_position(symbol)
        log.info("SELL submitted: %s (order %s, %s)",
                 symbol, result["order_id"], result["status"])
        db.record_trade(symbol, "SELL", result.get("qty"), None, price,
                        result["order_id"], result["status"])
    except Exception as exc:  # noqa: BLE001
        log.exception("SELL failed: %s", exc)
        db.record_trade(symbol, "SELL", None, None, price, None, f"error: {exc}")


def main() -> int:
    if not settings.has_credentials:
        log.error(
            "Alpaca credentials are missing. Copy .env.example to .env and set "
            "ALPACA_API_KEY / ALPACA_SECRET_KEY (use PAPER keys)."
        )
        return 1

    os_signal.signal(os_signal.SIGINT, _handle_stop)
    os_signal.signal(os_signal.SIGTERM, _handle_stop)

    db = Database(settings.database_path)
    client = AlpacaClient(settings)

    mode = "PAPER" if settings.paper else "LIVE"
    log.info("Stonks bot starting | mode=%s | symbol=%s | interval=%ss | dry_run=%s",
             mode, settings.trade_symbol, settings.poll_interval_seconds, settings.dry_run)
    db.set_status("bot_state", "running")

    while _running:
        cycle_start = time.monotonic()
        try:
            run_once(client, db)
        except Exception as exc:  # noqa: BLE001 - never let one bad cycle kill the bot
            log.exception("Cycle error (continuing): %s", exc)
            db.set_status("last_error", str(exc))

        # Sleep the remainder of the interval, staying responsive to shutdown.
        elapsed = time.monotonic() - cycle_start
        remaining = max(0.0, settings.poll_interval_seconds - elapsed)
        slept = 0.0
        while _running and slept < remaining:
            time.sleep(min(1.0, remaining - slept))
            slept += 1.0

    db.set_status("bot_state", "stopped")
    log.info("Stonks bot stopped cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
