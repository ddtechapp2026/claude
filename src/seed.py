"""Seed 10 starter bots, each with a distinct strategy, if the DB is empty.

Specs are built directly (no AI needed at seed time). You can rewrite any bot's
strategy in plain English from the dashboard later.
"""
from __future__ import annotations

import json

from .config import settings
from .database import Database
from .strategy_engine import validate_spec

# (name, plain-English description, spec, stop_loss, take_profit, max_trade)
_BOTS = [
    ("SMA Crossover", "Buy when the 10-period average crosses above the 30, sell when it crosses back below.",
     {"entry": {"all": [{"left": "sma(10)", "op": ">", "right": "sma(30)"}]},
      "exit": {"any": [{"left": "sma(10)", "op": "<", "right": "sma(30)"}]}, "size_fraction": 0.5},
     0.05, 0.10, 1000),
    ("RSI Reversion", "Buy when RSI is oversold below 30, sell when overbought above 70.",
     {"entry": {"all": [{"left": "rsi(14)", "op": "<", "right": 30}]},
      "exit": {"any": [{"left": "rsi(14)", "op": ">", "right": 70}]}, "size_fraction": 0.5},
     0.05, 0.10, 1000),
    ("Momentum Breakout", "Buy on a breakout above the 20-bar high, exit if price falls below the 10 average.",
     {"entry": {"all": [{"left": "price", "op": ">", "right": "high(20)"}]},
      "exit": {"any": [{"left": "price", "op": "<", "right": "sma(10)"}]}, "size_fraction": 0.5},
     0.04, 0.12, 1000),
    ("Buy The Dip", "Buy after a 3% drop over the last 4 bars, take profit at +3%.",
     {"entry": {"all": [{"left": "pct_change(4)", "op": "<=", "right": -0.03}]},
      "exit": {"any": [{"left": "pct_from_entry", "op": ">=", "right": 0.03}]}, "size_fraction": 0.4},
     0.03, 0.06, 800),
    ("EMA Trend", "Follow the trend: buy when EMA9 is above EMA21, sell when it drops below.",
     {"entry": {"all": [{"left": "ema(9)", "op": ">", "right": "ema(21)"}]},
      "exit": {"any": [{"left": "ema(9)", "op": "<", "right": "ema(21)"}]}, "size_fraction": 0.6},
     0.05, 0.10, 1200),
    ("Slow SMA", "Longer-term: buy when SMA20 is above SMA50, sell when below.",
     {"entry": {"all": [{"left": "sma(20)", "op": ">", "right": "sma(50)"}]},
      "exit": {"any": [{"left": "sma(20)", "op": "<", "right": "sma(50)"}]}, "size_fraction": 0.7},
     0.06, 0.15, 1500),
    ("RSI Trend", "Buy when RSI shows strength above 55, sell when it weakens below 45.",
     {"entry": {"all": [{"left": "rsi(14)", "op": ">", "right": 55}]},
      "exit": {"any": [{"left": "rsi(14)", "op": "<", "right": 45}]}, "size_fraction": 0.5},
     0.05, 0.10, 1000),
    ("Deep Dip", "Buy after a sharp 5% drop over 8 bars, tight 2% stop and 4% target.",
     {"entry": {"all": [{"left": "pct_change(8)", "op": "<=", "right": -0.05}]},
      "exit": {"any": [{"left": "pct_from_entry", "op": ">=", "right": 0.04}]}, "size_fraction": 0.3},
     0.02, 0.04, 600),
    ("Pullback Buyer", "Buy dips below the 20 average while the longer trend is up (SMA20>SMA50).",
     {"entry": {"all": [{"left": "price", "op": "<", "right": "sma(20)"},
                        {"left": "sma(20)", "op": ">", "right": "sma(50)"}]},
      "exit": {"any": [{"left": "pct_from_entry", "op": ">=", "right": 0.05}]}, "size_fraction": 0.4},
     0.04, 0.08, 900),
    ("Fast Scalper", "Quick momentum on the 5 vs 15 average, small size and tight risk.",
     {"entry": {"all": [{"left": "ema(5)", "op": ">", "right": "ema(15)"}]},
      "exit": {"any": [{"left": "ema(5)", "op": "<", "right": "ema(15)"}]}, "size_fraction": 0.25},
     0.02, 0.05, 500),
]


def seed_if_empty(db: Database) -> int:
    if db.count_bots() > 0:
        return 0
    universe = list(settings.crypto_universe) or [settings.default_symbol]
    for i, (name, text, spec, stop, tp, max_trade) in enumerate(_BOTS):
        # Spread the starter bots across different cryptos, and let the last two
        # run in "AUTO" mode (they scan the whole universe and pick what fires).
        symbol = "AUTO" if i >= len(_BOTS) - 2 else universe[i % len(universe)]
        db.create_bot(
            name=name,
            enabled=0,  # start OFF; you turn them on from the dashboard
            symbol=symbol,
            strategy_text=text,
            strategy_spec=json.dumps(validate_spec(spec)),
            starting_cash=10000.0,
            max_trade_usd=float(max_trade),
            stop_loss_pct=stop,
            take_profit_pct=tp,
            run_until=None,
            auto_adjust=1,
        )
    return len(_BOTS)
