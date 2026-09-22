"""Real Alpaca order execution for bots with live trading enabled.

Only used when a bot's `live_trading` toggle is ON. Places market orders on the
Alpaca account (paper by default). This is a thin, best-effort mirror of a bot's
simulated trades — the virtual wallet stays the dashboard's source of truth, and
any broker error is logged without disrupting the simulation.

IMPORTANT: Alpaca is a SINGLE account. If several bots trade live at once they
share one balance and one position per symbol and WILL interfere with each
other. Prefer one live bot at a time.
"""
from __future__ import annotations

import logging

from .config import settings

log = logging.getLogger("stonks.broker")

_client = None
_init_tried = False


def available() -> bool:
    """True if Alpaca trading keys are configured."""
    return settings.alpaca_trading_enabled


def _get_client():
    global _client, _init_tried
    if _client is not None or _init_tried:
        return _client
    _init_tried = True
    if not available():
        return None
    try:
        from alpaca.trading.client import TradingClient
        _client = TradingClient(
            api_key=settings.alpaca_api_key,
            secret_key=settings.alpaca_secret_key,
            paper=settings.alpaca_paper,
        )
        log.info("Alpaca trading client ready (paper=%s)", settings.alpaca_paper)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not init Alpaca trading client: %s", exc)
        _client = None
    return _client


def submit_buy(symbol: str, notional_usd: float) -> dict:
    """Place a market BUY for a dollar amount. Returns {order_id, status}."""
    client = _get_client()
    if client is None:
        return {"order_id": None, "status": "no_broker"}
    try:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        order = client.submit_order(MarketOrderRequest(
            symbol=symbol, notional=round(notional_usd, 2),
            side=OrderSide.BUY, time_in_force=TimeInForce.GTC))
        return {"order_id": str(order.id), "status": str(order.status)}
    except Exception as exc:  # noqa: BLE001
        log.warning("Alpaca BUY %s failed: %s", symbol, exc)
        return {"order_id": None, "status": f"error: {str(exc)[:120]}"}


def submit_sell(symbol: str, qty: float) -> dict:
    """Place a market SELL for a quantity. Returns {order_id, status}."""
    client = _get_client()
    if client is None:
        return {"order_id": None, "status": "no_broker"}
    try:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        order = client.submit_order(MarketOrderRequest(
            symbol=symbol, qty=round(qty, 9),
            side=OrderSide.SELL, time_in_force=TimeInForce.GTC))
        return {"order_id": str(order.id), "status": str(order.status)}
    except Exception as exc:  # noqa: BLE001
        log.warning("Alpaca SELL %s failed: %s", symbol, exc)
        return {"order_id": None, "status": f"error: {str(exc)[:120]}"}
