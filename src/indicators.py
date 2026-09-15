"""Pure-Python technical indicators computed from a list of close prices.

Each returns None when there isn't enough data yet (so the strategy engine can
treat "still warming up" as a non-signal). Kept dependency-free and simple.
"""
from __future__ import annotations


def sma(closes: list[float], period: int) -> float | None:
    if period <= 0 or len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def ema(closes: list[float], period: int) -> float | None:
    if period <= 0 or len(closes) < period:
        return None
    k = 2 / (period + 1)
    # Seed with the SMA of the first `period` values, then walk forward.
    e = sum(closes[:period]) / period
    for price in closes[period:]:
        e = price * k + e * (1 - k)
    return e


def rsi(closes: list[float], period: int = 14) -> float | None:
    if period <= 0 or len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        change = closes[i] - closes[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def pct_change(closes: list[float], period: int) -> float | None:
    """Fractional return over the last `period` bars (0.03 == +3%)."""
    if period <= 0 or len(closes) < period + 1:
        return None
    past = closes[-period - 1]
    if past == 0:
        return None
    return (closes[-1] - past) / past


def highest(closes: list[float], period: int) -> float | None:
    if period <= 0 or len(closes) < period:
        return None
    return max(closes[-period:])


def lowest(closes: list[float], period: int) -> float | None:
    if period <= 0 or len(closes) < period:
        return None
    return min(closes[-period:])
