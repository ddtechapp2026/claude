"""Trading strategy - THIS IS WHERE YOUR LOGIC GOES.

The bot loop feeds this module a list of recent close prices and asks for a
decision. Return one of the Signal values below. Everything else (fetching
data, placing/closing orders, logging, the dashboard) is already wired up for
you, so you can focus purely on *when to buy and sell* here.

The default implementation is a deliberately inert placeholder that always
returns HOLD, so the full pipeline runs safely without trading until you write
your own rules. A worked SMA-crossover example is included, commented out, to
show the shape of a real strategy.
"""
from __future__ import annotations

from dataclasses import dataclass


# Signal values the bot understands.
BUY = "BUY"    # open / add to a long position
SELL = "SELL"  # close the position
HOLD = "HOLD"  # do nothing


@dataclass
class Decision:
    signal: str          # BUY / SELL / HOLD
    reason: str = ""     # human-readable explanation, shown on the dashboard


def _sma(values: list[float], period: int) -> float | None:
    """Simple moving average of the last `period` values, or None if too few."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def decide(closes: list[float], *, has_position: bool) -> Decision:
    """Decide what to do given recent close prices.

    Args:
        closes:       recent close prices, oldest first, newest last.
        has_position: True if the bot currently holds the traded symbol.

    Returns:
        A Decision with signal BUY, SELL, or HOLD.

    Contract the bot enforces around this function:
        * A BUY is only acted on when you do NOT already hold a position.
        * A SELL is only acted on when you DO hold a position.
        So you can return BUY/SELL freely; redundant ones are ignored.
    """
    if not closes:
        return Decision(HOLD, "no price data yet")

    # ------------------------------------------------------------------
    # >>> PUT YOUR STRATEGY HERE <<<
    #
    # `closes[-1]` is the latest price. Compute whatever indicators you
    # like from `closes` and return Decision(BUY/SELL/HOLD, "why").
    #
    # Example: SMA crossover. Uncomment to enable a real (simple) strategy.
    #
    # fast = _sma(closes, 10)
    # slow = _sma(closes, 30)
    # if fast is None or slow is None:
    #     return Decision(HOLD, "warming up indicators")
    # if fast > slow and not has_position:
    #     return Decision(BUY, f"fast SMA {fast:.2f} > slow SMA {slow:.2f}")
    # if fast < slow and has_position:
    #     return Decision(SELL, f"fast SMA {fast:.2f} < slow SMA {slow:.2f}")
    # return Decision(HOLD, f"fast {fast:.2f} / slow {slow:.2f}, no cross")
    # ------------------------------------------------------------------

    return Decision(HOLD, "placeholder strategy - no rules defined yet")
