"""Crypto market data via Alpaca.

Only needs historical bars. Alpaca's crypto data endpoint works without API
keys (keys just raise your rate limits), so the whole system can run on market
data alone. Prices are cached per symbol for a short window so ten bots sharing
a symbol trigger one fetch, not ten.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from alpaca.data.historical import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from .config import settings

_TIMEFRAMES = {
    "1Min": TimeFrame(1, TimeFrameUnit.Minute),
    "5Min": TimeFrame(5, TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
    "1Day": TimeFrame(1, TimeFrameUnit.Day),
}

_client = CryptoHistoricalDataClient(
    api_key=settings.alpaca_api_key or None,
    secret_key=settings.alpaca_secret_key or None,
)

_cache: dict[str, tuple[float, list[float]]] = {}
_CACHE_TTL = 30.0  # seconds


def get_closes(symbol: str) -> list[float]:
    """Recent close prices (oldest -> newest) for `symbol`, short-cached."""
    now = time.monotonic()
    hit = _cache.get(symbol)
    if hit and (now - hit[0]) < _CACHE_TTL:
        return hit[1]

    timeframe = _TIMEFRAMES.get(settings.bar_timeframe, _TIMEFRAMES["15Min"])
    start = datetime.now(timezone.utc) - timedelta(hours=settings.lookback_hours)
    request = CryptoBarsRequest(symbol_or_symbols=[symbol], timeframe=timeframe, start=start)
    bars = _client.get_crypto_bars(request)
    closes = [float(b.close) for b in bars.data.get(symbol, [])]
    _cache[symbol] = (now, closes)
    return closes


def get_price(symbol: str) -> float | None:
    closes = get_closes(symbol)
    return closes[-1] if closes else None


TIMEFRAMES = ("1Min", "5Min", "15Min", "1Hour", "1Day")

# Largest window (days) we'll fetch per timeframe, to keep the bar count (and the
# backtest's runtime) sane. 1-minute is the finest Alpaca offers for crypto.
_MAX_DAYS = {"1Min": 5, "5Min": 30, "15Min": 120, "1Hour": 400, "1Day": 3650}


# Auto-pick a timeframe that keeps the bar count reasonable for a given window.
def timeframe_for_days(days: float) -> str:
    if days <= 2:
        return "1Min"
    if days <= 5:
        return "5Min"
    if days <= 30:
        return "15Min"
    if days <= 120:
        return "1Hour"
    return "1Day"


def cap_days(days: float, timeframe: str) -> float:
    """Clamp the window so a fine timeframe can't request an enormous history."""
    return min(days, _MAX_DAYS.get(timeframe, 3650))


def get_bars(symbol: str, days: float, timeframe: str | None = None) -> list[dict]:
    """Historical bars for backtesting: [{'t': iso, 'c': close}, ...] oldest first.

    Not cached (each backtest window differs) and returns timestamps for the
    equity-curve x-axis.
    """
    tf_name = timeframe or timeframe_for_days(days)
    tf = _TIMEFRAMES.get(tf_name, _TIMEFRAMES["1Hour"])
    start = datetime.now(timezone.utc) - timedelta(days=days)
    request = CryptoBarsRequest(symbol_or_symbols=[symbol], timeframe=tf, start=start)
    bars = _client.get_crypto_bars(request)
    series = bars.data.get(symbol, [])
    return [{"t": b.timestamp.isoformat(), "c": float(b.close)} for b in series]
