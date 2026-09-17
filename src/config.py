"""Central configuration, loaded from environment / the .env file.

Single source of truth for the engine and dashboard. Import `settings`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Market data (Alpaca crypto data works without keys; keys raise rate limits)
    alpaca_api_key: str
    alpaca_secret_key: str

    # Engine
    poll_interval_seconds: int
    bar_timeframe: str
    lookback_hours: int
    default_symbol: str
    crypto_universe: tuple  # symbols an "Auto" bot may scan / pick from

    # Storage
    database_path: Path

    # Dashboard
    dashboard_host: str
    dashboard_port: int

    # AI via OpenRouter (OpenAI-compatible API)
    openrouter_api_key: str
    openrouter_model: str
    openrouter_base_url: str

    # Learning loop
    ai_review_min_trades: int   # min closed trades before a review runs
    ai_min_winrate: float       # review triggers when win-rate is below this

    @property
    def ai_enabled(self) -> bool:
        return bool(self.openrouter_api_key) and "your_" not in self.openrouter_api_key


def load_settings() -> Settings:
    db_path = Path(os.getenv("DATABASE_PATH", "data/stonks.db"))
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    return Settings(
        alpaca_api_key=os.getenv("ALPACA_API_KEY", "").strip(),
        alpaca_secret_key=os.getenv("ALPACA_SECRET_KEY", "").strip(),
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        bar_timeframe=os.getenv("BAR_TIMEFRAME", "15Min").strip(),
        lookback_hours=int(os.getenv("LOOKBACK_HOURS", "72")),
        default_symbol=os.getenv("TRADE_SYMBOL", "BTC/USD").strip(),
        crypto_universe=tuple(
            s.strip() for s in os.getenv(
                "CRYPTO_UNIVERSE",
                "BTC/USD,ETH/USD,SOL/USD,LTC/USD,DOGE/USD,AVAX/USD,LINK/USD,BCH/USD,UNI/USD,DOT/USD",
            ).split(",") if s.strip()
        ),
        database_path=db_path,
        dashboard_host=os.getenv("DASHBOARD_HOST", "127.0.0.1").strip(),
        dashboard_port=int(os.getenv("DASHBOARD_PORT", "8000")),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        openrouter_model=os.getenv("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet").strip(),
        openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip(),
        ai_review_min_trades=int(os.getenv("AI_REVIEW_MIN_TRADES", "5")),
        ai_min_winrate=float(os.getenv("AI_MIN_WINRATE", "0.45")),
    )


settings = load_settings()
