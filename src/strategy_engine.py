"""Safe strategy interpreter.

A strategy is stored as a JSON "spec" (never executable code). The engine
evaluates it against recent prices to decide BUY / SELL / HOLD. This is what the
plain-English -> AI translation produces, and what the rule-based fallback
produces too, so both paths run through the same safe evaluator.

Spec shape:
{
  "entry": <group>,          # when flat, BUY if this is true
  "exit":  <group>,          # when holding, SELL if this is true
  "size_fraction": 0.5       # fraction of the bot's cash to deploy per buy
}

A <group> is either:
  {"all": [<condition|group>, ...]}   # AND
  {"any": [<condition|group>, ...]}   # OR
A <condition> is:
  {"left": <term>, "op": "<|>|<=|>=|==", "right": <term>}

A <term> is a number, or one of:
  "price", "pct_from_entry",
  "sma(N)", "ema(N)", "rsi(N)", "pct_change(N)", "high(N)", "low(N)"
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import indicators

BUY, SELL, HOLD = "BUY", "SELL", "HOLD"

_OPS = {
    "<": lambda a, b: a < b,
    ">": lambda a, b: a > b,
    "<=": lambda a, b: a <= b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
}
_FUNCS = {"sma", "ema", "rsi", "pct_change", "high", "low"}
_TERM_RE = re.compile(r"^(\w+)\((\d+)\)$")


@dataclass
class Decision:
    signal: str
    reason: str = ""
    size_fraction: float = 1.0


class SpecError(ValueError):
    """Raised when a strategy spec is malformed or references unknown terms."""


# --------------------------------------------------------------------------
# Validation - reject anything the evaluator can't safely handle.
# --------------------------------------------------------------------------
def validate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict):
        raise SpecError("spec must be an object")
    out: dict = {}
    for key in ("entry", "exit"):
        node = spec.get(key)
        if node is None:
            out[key] = {"all": []}  # empty group == never true
        else:
            _validate_group(node)
            out[key] = node
    size = spec.get("size_fraction", 1.0)
    try:
        size = float(size)
    except (TypeError, ValueError):
        raise SpecError("size_fraction must be a number")
    out["size_fraction"] = min(1.0, max(0.05, size))
    return out


def _validate_group(node) -> None:
    if not isinstance(node, dict):
        raise SpecError("condition group must be an object")
    if "all" in node or "any" in node:
        key = "all" if "all" in node else "any"
        items = node[key]
        if not isinstance(items, list):
            raise SpecError(f"'{key}' must be a list")
        for item in items:
            _validate_group(item)
        return
    # leaf condition
    if not all(k in node for k in ("left", "op", "right")):
        raise SpecError("condition needs left, op, right")
    if node["op"] not in _OPS:
        raise SpecError(f"unknown operator: {node['op']!r}")
    _validate_term(node["left"])
    _validate_term(node["right"])


def _validate_term(term) -> None:
    if isinstance(term, (int, float)):
        return
    if not isinstance(term, str):
        raise SpecError(f"bad term: {term!r}")
    s = term.strip()
    if s in ("price", "pct_from_entry"):
        return
    try:
        float(s)
        return
    except ValueError:
        pass
    m = _TERM_RE.match(s)
    if m and m.group(1) in _FUNCS:
        return
    raise SpecError(f"unknown term: {term!r}")


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------
def _resolve(term, closes: list[float], ctx: dict) -> float | None:
    if isinstance(term, (int, float)):
        return float(term)
    s = str(term).strip()
    try:
        return float(s)
    except ValueError:
        pass
    if s == "price":
        return closes[-1] if closes else None
    if s == "pct_from_entry":
        entry = ctx.get("entry_price")
        if not entry or not closes:
            return None
        return (closes[-1] - entry) / entry
    m = _TERM_RE.match(s)
    if not m:
        return None
    fn, n = m.group(1), int(m.group(2))
    if fn == "sma":
        return indicators.sma(closes, n)
    if fn == "ema":
        return indicators.ema(closes, n)
    if fn == "rsi":
        return indicators.rsi(closes, n)
    if fn == "pct_change":
        return indicators.pct_change(closes, n)
    if fn == "high":
        return indicators.highest(closes, n)
    if fn == "low":
        return indicators.lowest(closes, n)
    return None


def _eval_group(node: dict, closes: list[float], ctx: dict) -> bool:
    if "all" in node:
        items = node["all"]
        return bool(items) and all(_eval_group(i, closes, ctx) for i in items)
    if "any" in node:
        return any(_eval_group(i, closes, ctx) for i in node["any"])
    left = _resolve(node["left"], closes, ctx)
    right = _resolve(node["right"], closes, ctx)
    if left is None or right is None:  # not enough data -> not satisfied
        return False
    return _OPS[node["op"]](left, right)


def evaluate(spec: dict, closes: list[float], *, has_position: bool,
             entry_price: float | None = None) -> Decision:
    """Decide BUY / SELL / HOLD from a validated spec and recent prices."""
    if not closes:
        return Decision(HOLD, "no price data")
    ctx = {"entry_price": entry_price}
    size = float(spec.get("size_fraction", 1.0))
    if has_position:
        if _eval_group(spec.get("exit", {"all": []}), closes, ctx):
            return Decision(SELL, "exit rule met", size)
        return Decision(HOLD, "holding; exit rule not met", size)
    if _eval_group(spec.get("entry", {"all": []}), closes, ctx):
        return Decision(BUY, "entry rule met", size)
    return Decision(HOLD, "flat; entry rule not met", size)


def describe(spec: dict) -> str:
    """Short human-readable summary of a spec, for the dashboard."""
    def term(t):
        return str(t)

    def grp(node):
        if not isinstance(node, dict):
            return "?"
        if "all" in node:
            return " AND ".join(grp(i) for i in node["all"]) or "never"
        if "any" in node:
            return " OR ".join(grp(i) for i in node["any"]) or "never"
        return f"{term(node.get('left'))} {node.get('op')} {term(node.get('right'))}"

    entry = grp(spec.get("entry", {"all": []}))
    exit_ = grp(spec.get("exit", {"all": []}))
    size = spec.get("size_fraction", 1.0)
    return f"BUY when [{entry}]; SELL when [{exit_}]; size {int(float(size) * 100)}% of cash"
