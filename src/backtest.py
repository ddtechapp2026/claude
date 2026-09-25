"""Backtest a strategy over historical bars.

Pure, in-memory replay — no DB, no orders. It walks the price history one bar at
a time, only ever letting the strategy see *past* bars (no look-ahead), and
applies the exact same logic a live bot uses: entry/exit from the strategy spec,
fixed stop-loss/take-profit, and (when AI Control is on) a trailing stop that
tightens to break-even. Returns trades, an equity curve, and summary stats.
"""
from __future__ import annotations

from . import strategy_engine

_WARMUP = 50      # bars to let indicators form before trading
_MAX_BARS = 12000  # keep the O(n) replay fast; trims to the most recent bars


def run_backtest(bars: list[dict], *, spec: dict, starting_cash: float, max_trade_usd: float,
                 stop_pct: float, tp_pct: float, fee_pct: float, size_fraction: float,
                 ai_control: bool) -> dict:
    if len(bars) > _MAX_BARS:
        bars = bars[-_MAX_BARS:]   # keep the most recent bars
    closes = [float(b["c"]) for b in bars]
    times = [b["t"] for b in bars]
    n = len(closes)
    if n < 5:
        return {"error": "not enough historical data for this window/symbol"}

    cash = starting_cash
    qty = 0.0
    entry = 0.0
    peak = 0.0
    gross_realized = fees_paid = realized = 0.0
    trades: list[dict] = []
    equity_curve: list[dict] = []

    start_i = min(_WARMUP, n - 1)
    for i in range(start_i, n):
        window = closes[: i + 1]
        price = window[-1]
        t = times[i]

        if qty > 0:
            peak = max(peak, price)
            hard = entry * (1 - stop_pct) if stop_pct > 0 else 0.0
            if ai_control and stop_pct > 0:
                trail = peak * (1 - stop_pct)
                be = entry if peak >= entry * (1 + stop_pct) else 0.0
                eff = max(hard, trail, be)
                stop_reason = "stop-loss" if eff <= hard + 1e-12 else (
                    "break-even stop" if be >= trail and be == eff else "trailing stop")
            else:
                eff, stop_reason = hard, "stop-loss"

            sell_reason = None
            if stop_pct > 0 and price <= eff:
                sell_reason = stop_reason
            elif tp_pct > 0 and price >= entry * (1 + tp_pct):
                sell_reason = "take-profit"
            else:
                d = strategy_engine.evaluate(spec, window, has_position=True, entry_price=entry)
                if d.signal == strategy_engine.SELL:
                    sell_reason = d.reason

            if sell_reason:
                proceeds = qty * price
                buy_notional = qty * entry
                sell_fee = proceeds * fee_pct
                buy_fee = buy_notional * fee_pct
                gross = proceeds - buy_notional
                round_fee = buy_fee + sell_fee
                net = gross - round_fee
                cash += proceeds - sell_fee
                gross_realized += gross
                fees_paid += round_fee
                realized += net
                trades.append({"t": t, "side": "SELL", "price": round(price, 2),
                               "qty": qty, "gross_pnl": round(gross, 2),
                               "fee": round(round_fee, 2), "pnl": round(net, 2),
                               "reason": sell_reason})
                qty = entry = peak = 0.0
        else:
            d = strategy_engine.evaluate(spec, window, has_position=False)
            if d.signal == strategy_engine.BUY:
                notional = min(max_trade_usd, cash / (1 + fee_pct) * size_fraction)
                fee = notional * fee_pct
                if notional >= 1.0 and (notional + fee) <= cash:
                    qty = notional / price
                    cash -= notional + fee
                    entry = price
                    peak = price
                    fees_paid += fee
                    trades.append({"t": t, "side": "BUY", "price": round(price, 2),
                                   "qty": qty, "notional": round(notional, 2),
                                   "fee": round(fee, 2), "reason": d.reason})

        equity_curve.append({"t": t, "equity": round(cash + qty * price, 2)})

    final_equity = cash + qty * closes[-1]
    # Max drawdown across the equity curve.
    peak_eq = -1e18
    max_dd = 0.0
    for pt in equity_curve:
        peak_eq = max(peak_eq, pt["equity"])
        if peak_eq > 0:
            max_dd = max(max_dd, (peak_eq - pt["equity"]) / peak_eq)
    sells = [t for t in trades if t["side"] == "SELL"]
    wins = sum(1 for t in sells if t["pnl"] > 0)
    buy_hold = ((closes[-1] - closes[start_i]) / closes[start_i] * 100) if closes[start_i] else 0.0

    return {
        "stats": {
            "starting_cash": round(starting_cash, 2),
            "final_equity": round(final_equity, 2),
            "net_pnl": round(final_equity - starting_cash, 2),
            "return_pct": round((final_equity - starting_cash) / starting_cash * 100, 2) if starting_cash else 0.0,
            "gross_realized": round(gross_realized, 2),
            "fees_paid": round(fees_paid, 2),
            "num_trades": len(sells),
            "wins": wins,
            "win_rate": round(wins / len(sells) * 100, 1) if sells else 0.0,
            "max_drawdown_pct": round(max_dd * 100, 2),
            "buy_hold_return_pct": round(buy_hold, 2),
            "bars": n,
            "from": times[start_i],
            "to": times[-1],
        },
        "equity_curve": equity_curve,
        "trades": list(reversed(trades))[:200],
    }
