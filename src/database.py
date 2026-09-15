"""SQLite storage shared by the engine (writer) and dashboard (reader/writer).

Holds the bot registry, each bot's virtual wallet, its trades, per-bot equity
history, signals, and AI review log. WAL mode lets the dashboard read (and write
config) while the engine runs.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def now_iso() -> str:
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
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS bots (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    name           TEXT NOT NULL,
                    enabled        INTEGER NOT NULL DEFAULT 0,
                    symbol         TEXT NOT NULL DEFAULT 'BTC/USD',
                    strategy_text  TEXT NOT NULL DEFAULT '',
                    strategy_spec  TEXT NOT NULL DEFAULT '{}',
                    starting_cash  REAL NOT NULL DEFAULT 10000,
                    max_trade_usd  REAL NOT NULL DEFAULT 1000,
                    stop_loss_pct  REAL NOT NULL DEFAULT 0.05,
                    take_profit_pct REAL NOT NULL DEFAULT 0.10,
                    run_until      TEXT,               -- ISO ts or NULL (forever)
                    auto_adjust    INTEGER NOT NULL DEFAULT 1,
                    status         TEXT NOT NULL DEFAULT 'idle',
                    last_reason    TEXT NOT NULL DEFAULT '',
                    last_cycle     TEXT,
                    created        TEXT NOT NULL,
                    updated        TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wallets (
                    bot_id         INTEGER PRIMARY KEY REFERENCES bots(id) ON DELETE CASCADE,
                    cash           REAL NOT NULL,
                    position_qty   REAL NOT NULL DEFAULT 0,
                    entry_price    REAL,
                    equity         REAL NOT NULL,
                    realized_pnl   REAL NOT NULL DEFAULT 0,
                    updated        TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id         INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
                    ts             TEXT NOT NULL,
                    side           TEXT NOT NULL,       -- BUY / SELL
                    qty            REAL,
                    price          REAL,
                    notional       REAL,
                    pnl            REAL,                -- realized on SELL
                    reason         TEXT
                );

                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id         INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
                    ts             TEXT NOT NULL,
                    equity         REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS signals (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id         INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
                    ts             TEXT NOT NULL,
                    signal         TEXT NOT NULL,
                    price          REAL,
                    detail         TEXT
                );

                CREATE TABLE IF NOT EXISTS ai_reviews (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id         INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
                    ts             TEXT NOT NULL,
                    summary        TEXT,
                    changes        TEXT,               -- JSON of applied changes
                    source         TEXT                -- 'ai' or 'rules'
                );

                CREATE INDEX IF NOT EXISTS idx_trades_bot ON trades(bot_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_equity_bot ON equity_snapshots(bot_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_signals_bot ON signals(bot_id, id DESC);
                """
            )

    # --- bots ----------------------------------------------------------------
    def create_bot(self, **fields) -> int:
        cols = {
            "name": "Bot", "enabled": 0, "symbol": "BTC/USD",
            "strategy_text": "", "strategy_spec": "{}",
            "starting_cash": 10000.0, "max_trade_usd": 1000.0,
            "stop_loss_pct": 0.05, "take_profit_pct": 0.10,
            "run_until": None, "auto_adjust": 1, "status": "idle",
            "last_reason": "", "last_cycle": None,
        }
        cols.update(fields)
        ts = now_iso()
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO bots
                   (name,enabled,symbol,strategy_text,strategy_spec,starting_cash,
                    max_trade_usd,stop_loss_pct,take_profit_pct,run_until,auto_adjust,
                    status,last_reason,last_cycle,created,updated)
                   VALUES (:name,:enabled,:symbol,:strategy_text,:strategy_spec,
                    :starting_cash,:max_trade_usd,:stop_loss_pct,:take_profit_pct,
                    :run_until,:auto_adjust,:status,:last_reason,:last_cycle,:created,:updated)""",
                {**cols, "created": ts, "updated": ts},
            )
            bot_id = cur.lastrowid
            conn.execute(
                """INSERT INTO wallets (bot_id,cash,position_qty,entry_price,equity,realized_pnl,updated)
                   VALUES (?,?,0,NULL,?,0,?)""",
                (bot_id, cols["starting_cash"], cols["starting_cash"], ts),
            )
        return bot_id

    def count_bots(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) AS c FROM bots").fetchone()["c"]

    def list_bots(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM bots ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def get_bot(self, bot_id: int) -> dict | None:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
            return dict(r) if r else None

    def update_bot(self, bot_id: int, **fields) -> None:
        if not fields:
            return
        fields["updated"] = now_iso()
        sets = ", ".join(f"{k}=:{k}" for k in fields)
        with self._conn() as conn:
            conn.execute(f"UPDATE bots SET {sets} WHERE id=:id", {**fields, "id": bot_id})

    # --- wallets -------------------------------------------------------------
    def get_wallet(self, bot_id: int) -> dict | None:
        with self._conn() as conn:
            r = conn.execute("SELECT * FROM wallets WHERE bot_id=?", (bot_id,)).fetchone()
            return dict(r) if r else None

    def update_wallet(self, bot_id: int, **fields) -> None:
        fields["updated"] = now_iso()
        sets = ", ".join(f"{k}=:{k}" for k in fields)
        with self._conn() as conn:
            conn.execute(f"UPDATE wallets SET {sets} WHERE bot_id=:id", {**fields, "id": bot_id})

    def reset_wallet(self, bot_id: int, starting_cash: float) -> None:
        ts = now_iso()
        with self._conn() as conn:
            conn.execute(
                """UPDATE wallets SET cash=?, position_qty=0, entry_price=NULL,
                   equity=?, realized_pnl=0, updated=? WHERE bot_id=?""",
                (starting_cash, starting_cash, ts, bot_id),
            )
            conn.execute("DELETE FROM trades WHERE bot_id=?", (bot_id,))
            conn.execute("DELETE FROM equity_snapshots WHERE bot_id=?", (bot_id,))
            conn.execute("DELETE FROM signals WHERE bot_id=?", (bot_id,))

    # --- events --------------------------------------------------------------
    def record_trade(self, bot_id, side, qty, price, notional, pnl, reason) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO trades (bot_id,ts,side,qty,price,notional,pnl,reason)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (bot_id, now_iso(), side, qty, price, notional, pnl, reason),
            )

    def record_equity(self, bot_id, equity) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO equity_snapshots (bot_id,ts,equity) VALUES (?,?,?)",
                (bot_id, now_iso(), equity),
            )

    def record_signal(self, bot_id, signal, price, detail="") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO signals (bot_id,ts,signal,price,detail) VALUES (?,?,?,?,?)",
                (bot_id, now_iso(), signal, price, detail),
            )

    def record_review(self, bot_id, summary, changes: dict, source: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO ai_reviews (bot_id,ts,summary,changes,source) VALUES (?,?,?,?,?)",
                (bot_id, now_iso(), summary, json.dumps(changes), source),
            )

    # --- reads for dashboard -------------------------------------------------
    def recent_trades(self, bot_id, limit=25) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE bot_id=? ORDER BY id DESC LIMIT ?", (bot_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]

    def closed_trades(self, bot_id, limit=50) -> list[dict]:
        """SELL trades carry realized pnl; used by the learning loop."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM trades WHERE bot_id=? AND side='SELL' ORDER BY id DESC LIMIT ?",
                (bot_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def recent_signals(self, bot_id, limit=15) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM signals WHERE bot_id=? ORDER BY id DESC LIMIT ?", (bot_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]

    def equity_curve(self, bot_id, limit=500) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts,equity FROM equity_snapshots WHERE bot_id=? ORDER BY id DESC LIMIT ?",
                (bot_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def recent_reviews(self, bot_id, limit=10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM ai_reviews WHERE bot_id=? ORDER BY id DESC LIMIT ?", (bot_id, limit)
            ).fetchall()
            return [dict(r) for r in rows]
