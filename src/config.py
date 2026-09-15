"""Central configuration, loaded from environment / the .env file.

Everything the bot and dashboard need to know is read here in one place so
there is a single source of truth. Import `settings` from this module.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load variables from a .env file in the project root (if present). Real
# environment variables always win over the file, which is what we want on the
# server where systemd may inject them.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Alpaca
    api_key: str
    secret_key: str
    paper: bool

    # Bot
    trade_symbol: str
    poll_interval_seconds: int
    order_notional_usd: float
    bar_timeframe: str
    lookback_hours: int
    dry_run: bool

    # Storage
    database_path: Path

    # Dashboard
    dashboard_host: str
    dashboard_port: int

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key) and bool(self.secret_key) and "your_" not in self.api_key


def load_settings() -> Settings:
    db_path = os.getenv("DATABASE_PATH", "data/stonks.db")
    # Resolve relative DB paths against the project root so the bot and the
    # dashboard always agree on the same file no matter their working dir.
    db_path_resolved = Path(db_path)
    if not db_path_resolved.is_absolute():
        db_path_resolved = PROJECT_ROOT / db_path_resolved

    return Settings(
        api_key=os.getenv("ALPACA_API_KEY", "").strip(),
        secret_key=os.getenv("ALPACA_SECRET_KEY", "").strip(),
        paper=_get_bool("ALPACA_PAPER", True),
        trade_symbol=os.getenv("TRADE_SYMBOL", "BTC/USD").strip(),
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        order_notional_usd=float(os.getenv("ORDER_NOTIONAL_USD", "100")),
        bar_timeframe=os.getenv("BAR_TIMEFRAME", "15Min").strip(),
        lookback_hours=int(os.getenv("LOOKBACK_HOURS", "48")),
        dry_run=_get_bool("DRY_RUN", False),
        database_path=db_path_resolved,
        dashboard_host=os.getenv("DASHBOARD_HOST", "127.0.0.1").strip(),
        dashboard_port=int(os.getenv("DASHBOARD_PORT", "8000")),
    )


settings = load_settings()
