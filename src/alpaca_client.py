"""Thin wrapper around the Alpaca SDK.

Keeps all Alpaca-specific details in one file so the rest of the bot deals with
plain numbers and simple method calls. Handles both trading (account, orders,
positions) and crypto market data (price bars).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from .config import Settings

# Map the human-friendly BAR_TIMEFRAME setting to Alpaca's TimeFrame objects.
_TIMEFRAMES = {
    "1Min": TimeFrame(1, TimeFrameUnit.Minute),
    "5Min": TimeFrame(5, TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
    "1Day": TimeFrame(1, TimeFrameUnit.Day),
}


class AlpacaClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.trading = TradingClient(
            api_key=settings.api_key,
            secret_key=settings.secret_key,
            paper=settings.paper,
        )
        # Crypto market data does not require authentication, but passing keys
        # is fine and raises your rate limits.
        self.data = CryptoHistoricalDataClient(
            api_key=settings.api_key or None,
            secret_key=settings.secret_key or None,
        )

    # --- account / positions -------------------------------------------------
    def get_account(self) -> dict:
        a = self.trading.get_account()
        return {
            "equity": float(a.equity),
            "cash": float(a.cash),
            "buying_power": float(a.buying_power),
            "portfolio_value": float(a.portfolio_value),
            "currency": a.currency,
            "status": str(a.status),
        }

    def get_positions(self) -> list[dict]:
        positions = self.trading.get_all_positions()
        result = []
        for p in positions:
            result.append(
                {
                    "symbol": p.symbol,
                    "qty": float(p.qty),
                    "avg_entry_price": float(p.avg_entry_price),
                    "market_value": float(p.market_value),
                    "current_price": float(p.current_price) if p.current_price else None,
                    "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl else 0.0,
                    "unrealized_plpc": float(p.unrealized_plpc) * 100 if p.unrealized_plpc else 0.0,
                }
            )
        return result

    def get_position_qty(self, symbol: str) -> float:
        """Return the quantity currently held for `symbol` (0 if none)."""
        for p in self.get_positions():
            # Alpaca reports crypto positions without the slash, e.g. "BTCUSD".
            if p["symbol"].replace("/", "") == symbol.replace("/", ""):
                return p["qty"]
        return 0.0

    # --- market data ---------------------------------------------------------
    def get_recent_closes(self, symbol: str) -> list[float]:
        """Return recent close prices (oldest -> newest) for the symbol."""
        timeframe = _TIMEFRAMES.get(self.settings.bar_timeframe, _TIMEFRAMES["15Min"])
        start = datetime.now(timezone.utc) - timedelta(hours=self.settings.lookback_hours)
        request = CryptoBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=timeframe,
            start=start,
        )
        bars = self.data.get_crypto_bars(request)
        series = bars.data.get(symbol, [])
        return [float(bar.close) for bar in series]

    def get_latest_price(self, symbol: str) -> float | None:
        closes = self.get_recent_closes(symbol)
        return closes[-1] if closes else None

    # --- orders --------------------------------------------------------------
    def submit_notional_buy(self, symbol: str, notional_usd: float) -> dict:
        order = self.trading.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                notional=round(notional_usd, 2),
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,  # crypto uses GTC/IOC
            )
        )
        return {"order_id": str(order.id), "status": str(order.status)}

    def submit_close_position(self, symbol: str) -> dict:
        """Sell the entire position for `symbol`."""
        qty = self.get_position_qty(symbol)
        if qty <= 0:
            return {"order_id": None, "status": "no_position", "qty": 0.0}
        order = self.trading.submit_order(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
            )
        )
        return {"order_id": str(order.id), "status": str(order.status), "qty": qty}
