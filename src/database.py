"""SQLite storage shared by the bot (writer) and dashboard (reader).

SQLite is used because it is zero-setup, file-based, and perfectly adequate for
a single-bot workload. The bot records equity snapshots, signals, and trades;
the dashboard reads them back.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL lets the dashboard read while the bot writes without locking.
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts        TEXT NOT NULL,
                    equity    REAL NOT NULL,
                    cash      REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS signals (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts        TEXT NOT NULL,
                    symbol    TEXT NOT NULL,
                    signal    TEXT NOT NULL,   -- BUY / SELL / HOLD
                    price     REAL,
                    detail    TEXT              -- free-form JSON/string
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts        TEXT NOT NULL,
                    symbol    TEXT NOT NULL,
                    side      TEXT NOT NULL,
                    qty       REAL,
                    notional  REAL,
                    price     REAL,
                    order_id  TEXT,
                    status    TEXT
                );

                CREATE TABLE IF NOT EXISTS bot_status (
                    key       TEXT PRIMARY KEY,
                    value     TEXT,
                    updated   TEXT
                );
                """
            )

    # --- writers (used by the bot) ------------------------------------------
    def record_equity(self, equity: float, cash: float) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO equity_snapshots (ts, equity, cash) VALUES (?, ?, ?)",
                (_now_iso(), equity, cash),
            )

    def record_signal(self, symbol: str, signal: str, price: float | None, detail: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO signals (ts, symbol, signal, price, detail) VALUES (?, ?, ?, ?, ?)",
                (_now_iso(), symbol, signal, price, detail),
            )

    def record_trade(
        self,
        symbol: str,
        side: str,
        qty: float | None,
        notional: float | None,
        price: float | None,
        order_id: str | None,
        status: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO trades (ts, symbol, side, qty, notional, price, order_id, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (_now_iso(), symbol, side, qty, notional, price, order_id, status),
            )

    def set_status(self, key: str, value: Any) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO bot_status (key, value, updated) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated""",
                (key, str(value), _now_iso()),
            )

    # --- readers (used by the dashboard) ------------------------------------
    def recent_trades(self, limit: int = 25) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def recent_signals(self, limit: int = 25) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def equity_curve(self, limit: int = 500) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, equity, cash FROM equity_snapshots ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def all_status(self) -> dict[str, str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value, updated FROM bot_status").fetchall()
            return {r["key"]: r["value"] for r in rows}
