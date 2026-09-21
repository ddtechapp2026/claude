"""AI layer: plain-English -> strategy spec, and learning from losing trades.

Uses OpenRouter's OpenAI-compatible chat API when OPENROUTER_API_KEY is set.
Always falls back to a deterministic rule-based parser so the system keeps
working without a key (or if a request fails).
"""
from __future__ import annotations

import json
import logging
import re

import requests

from .config import settings
from .strategy_engine import SpecError, describe, validate_spec

log = logging.getLogger("stonks.llm")

_SPEC_DOC = """
You translate a plain-English crypto trading idea into a STRICT JSON strategy
spec. Output JSON ONLY, no prose, no code fences.

Schema:
{
  "entry": <group>,   // when flat, BUY if true
  "exit":  <group>,   // when holding, SELL if true
  "size_fraction": <number 0.05..1.0>  // fraction of cash to deploy per buy
}
<group> = {"all":[...]} (AND) or {"any":[...]} (OR); items are groups or conditions.
<condition> = {"left": <term>, "op": "<|>|<=|>=|==", "right": <term>}
<term> = a number, or one of:
  "price", "pct_from_entry",
  "sma(N)", "ema(N)", "rsi(N)", "pct_change(N)", "high(N)", "low(N)"
Notes: pct_change(N) and pct_from_entry are FRACTIONS (0.03 == 3%). N is a bar
count. Do not invent other terms or functions. Keep it simple and valid.
"""


def _chat(system: str, user: str, max_tokens: int = 700) -> str | None:
    if not settings.ai_enabled:
        return None
    try:
        resp = requests.post(
            f"{settings.openrouter_base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
                # Optional OpenRouter attribution headers:
                "HTTP-Referer": "https://github.com/ddtechapp2026/claude",
                "X-Title": "Stonks",
            },
            json={
                "model": settings.effective_model,  # forced to ":free" when free-only
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            },
            timeout=45,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001 - fall back to rules
        log.warning("OpenRouter call failed (%s); using rule-based fallback", exc)
        return None


def _extract_json(text: str) -> dict:
    text = text.strip()
    # tolerate ```json ... ``` fences
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        brace = re.search(r"\{.*\}", text, re.DOTALL)
        if brace:
            text = brace.group(0)
    return json.loads(text)


# --------------------------------------------------------------------------
# Plain-English -> spec
# --------------------------------------------------------------------------
def translate_strategy(text: str) -> tuple[dict, str, str]:
    """Return (validated_spec, explanation, source) for a plain-English idea."""
    content = _chat(_SPEC_DOC, f"Trading idea:\n{text}")
    if content:
        try:
            spec = validate_spec(_extract_json(content))
            return spec, describe(spec), "ai"
        except (SpecError, json.JSONDecodeError, KeyError) as exc:
            log.warning("AI spec invalid (%s); using rule-based fallback", exc)
    spec = rule_based_translate(text)
    return spec, describe(spec), "rules"


def _pct(text: str, default: float) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    return (float(m.group(1)) / 100.0) if m else default


def rule_based_translate(text: str) -> dict:
    """Deterministic parser for common phrasings; always returns a valid spec."""
    t = text.lower()
    entry_terms: list[dict] = []
    exit_terms: list[dict] = []

    if "rsi" in t:
        lo = re.search(r"(?:below|under|oversold)\D*(\d{1,2})", t)
        hi = re.search(r"(?:above|over|overbought)\D*(\d{1,2})", t)
        entry_terms.append({"left": "rsi(14)", "op": "<", "right": int(lo.group(1)) if lo else 30})
        exit_terms.append({"left": "rsi(14)", "op": ">", "right": int(hi.group(1)) if hi else 70})

    if "ema" in t:
        entry_terms.append({"left": "ema(9)", "op": ">", "right": "ema(21)"})
        exit_terms.append({"left": "ema(9)", "op": "<", "right": "ema(21)"})
    elif ("moving average" in t or "sma" in t or "crossover" in t or "cross over" in t):
        entry_terms.append({"left": "sma(10)", "op": ">", "right": "sma(30)"})
        exit_terms.append({"left": "sma(10)", "op": "<", "right": "sma(30)"})

    if "breakout" in t or "momentum" in t or "highest" in t:
        entry_terms.append({"left": "price", "op": ">", "right": "high(20)"})
        exit_terms.append({"left": "price", "op": "<", "right": "sma(10)"})

    # Only treat it as a "buy the dip" rule when a percentage is attached to the
    # dip/drop word (so "RSI drops below 25" and "40% of cash" don't trigger it).
    dip_pct = re.search(r"(?:dip|drop\w*|fall\w*|pull\s?back|pullback)\D{0,12}(\d+(?:\.\d+)?)\s*%", t)
    if dip_pct:
        entry_terms.append({"left": "pct_change(4)", "op": "<=", "right": -abs(float(dip_pct.group(1)) / 100)})
    elif "buy the dip" in t or "the dip" in t:
        entry_terms.append({"left": "pct_change(4)", "op": "<=", "right": -0.03})

    # profit/loss exits phrased against the entry price
    up = re.search(r"up\s+(\d+(?:\.\d+)?)\s*%", t)
    if up:
        exit_terms.append({"left": "pct_from_entry", "op": ">=", "right": float(up.group(1)) / 100})
    down = re.search(r"(?:down|loss|lose)\s+(\d+(?:\.\d+)?)\s*%", t)
    if down:
        exit_terms.append({"left": "pct_from_entry", "op": "<=", "right": -float(down.group(1)) / 100})

    if not entry_terms:  # nothing recognised -> a safe, sensible default
        entry_terms = [{"left": "sma(10)", "op": ">", "right": "sma(30)"}]
        exit_terms = exit_terms or [{"left": "sma(10)", "op": "<", "right": "sma(30)"}]

    size = 1.0
    size_m = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:of\s+)?(?:cash|balance|capital|portfolio)", t)
    if size_m:
        size = float(size_m.group(1)) / 100

    spec = {
        "entry": {"all": entry_terms},
        "exit": {"any": exit_terms} if exit_terms else {"all": []},
        "size_fraction": size,
    }
    return validate_spec(spec)


# --------------------------------------------------------------------------
# Learn from mistakes -> parameter adjustments (engine clamps to guardrails)
# --------------------------------------------------------------------------
def review_trades(bot: dict, closed_trades: list[dict]) -> tuple[dict, str, str]:
    """Suggest numeric adjustments after losing trades.

    Returns (changes, summary, source). `changes` may include:
      stop_loss_pct, take_profit_pct, size_fraction  (all fractions)
    The engine validates/clamps them before applying.
    """
    wins = sum(1 for x in closed_trades if (x.get("pnl") or 0) > 0)
    total = len(closed_trades)
    winrate = wins / total if total else 0.0
    pnl = sum((x.get("pnl") or 0) for x in closed_trades)

    if settings.ai_enabled:
        system = (
            "You are a risk manager tuning a crypto bot. Given its recent closed "
            "trades and current risk settings, return STRICT JSON only with any of: "
            "stop_loss_pct, take_profit_pct, size_fraction (all decimals, e.g. 0.04), "
            'and a short "summary". Be conservative; only adjust what the data '
            "supports. JSON only."
        )
        user = json.dumps({
            "current": {
                "stop_loss_pct": bot["stop_loss_pct"],
                "take_profit_pct": bot["take_profit_pct"],
                "win_rate": round(winrate, 2),
                "total_pnl": round(pnl, 2),
            },
            "recent_trades": [
                {"pnl": round(x.get("pnl") or 0, 2), "reason": x.get("reason")}
                for x in closed_trades[:20]
            ],
        })
        content = _chat(system, user, max_tokens=400)
        if content:
            try:
                data = _extract_json(content)
                changes = {k: float(data[k]) for k in
                           ("stop_loss_pct", "take_profit_pct", "size_fraction") if k in data}
                summary = str(data.get("summary", "AI review"))
                if changes:
                    return changes, summary, "ai"
            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                log.warning("AI review invalid (%s); using heuristic", exc)

    # Heuristic fallback: if it's losing, cut size and tighten the stop a touch.
    changes: dict = {}
    if winrate < settings.ai_min_winrate or pnl < 0:
        changes["size_fraction"] = None  # engine reduces current by 15%
        changes["stop_loss_pct"] = None  # engine tightens current by 20%
        summary = (f"Win rate {winrate:.0%}, PnL {pnl:+.2f}: reducing size and "
                   "tightening stop-loss.")
        return changes, summary, "rules"
    return {}, f"Win rate {winrate:.0%}, PnL {pnl:+.2f}: no change.", "rules"
