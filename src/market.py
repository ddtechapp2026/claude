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
