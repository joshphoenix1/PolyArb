"""Portfolio tracker - balances, positions, P&L tracking with SQLite persistence."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

from data.models import TradeRecord

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "portfolio.db"


class PortfolioTracker:
    """Tracks positions, balances, and P&L with SQLite persistence."""

    def __init__(self, db_path: Path = DB_PATH):
        self._db_path = db_path
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_db()
        self._daily_pnl: float = 0.0
        self._daily_pnl_date: str = ""

    def _init_db(self):
        cursor = self._conn.cursor()
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                event_title TEXT,
                num_outcomes INTEGER,
                intended_sets REAL,
                filled_sets REAL,
                total_cost REAL,
                expected_profit REAL,
                realized_profit REAL,
                status TEXT,
                order_ids TEXT,
                fill_details TEXT,
                timestamp REAL
            );

            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                event_title TEXT,
                token_id TEXT NOT NULL,
                side TEXT DEFAULT 'YES',
                size REAL DEFAULT 0,
                avg_price REAL DEFAULT 0,
                opened_at REAL,
                UNIQUE(event_id, token_id)
            );

            CREATE INDEX IF NOT EXISTS idx_trades_event ON trades(event_id);
            CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_positions_event ON positions(event_id);
            """
        )
        self._conn.commit()

    def record_trade(self, trade: TradeRecord):
        """Record a completed trade."""
        cursor = self._conn.cursor()
        cursor.execute(
            """
            INSERT INTO trades (event_id, event_title, num_outcomes, intended_sets,
                filled_sets, total_cost, expected_profit, realized_profit,
                status, order_ids, fill_details, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trade.opportunity_event_id,
                trade.event_title,
                trade.num_outcomes,
                trade.intended_sets,
                trade.filled_sets,
                trade.total_cost,
                trade.expected_profit,
                trade.realized_profit,
                trade.status,
                json.dumps(trade.order_ids),
                json.dumps(trade.fill_details),
                trade.timestamp,
            ),
        )
        self._conn.commit()

        # Update daily P&L
        today = time.strftime("%Y-%m-%d")
        if self._daily_pnl_date != today:
            self._daily_pnl = 0.0
            self._daily_pnl_date = today
        self._daily_pnl += trade.realized_profit

        logger.info(
            f"Trade recorded: {trade.event_title} | "
            f"status={trade.status} | "
            f"cost=${trade.total_cost:.4f} | "
            f"profit=${trade.realized_profit:.4f}"
        )

    def update_position(
        self,
        event_id: str,
        event_title: str,
        token_id: str,
        size_delta: float,
        price: float,
    ):
        """Update or create a position."""
        cursor = self._conn.cursor()
        cursor.execute(
            "SELECT size, avg_price FROM positions WHERE event_id = ? AND token_id = ?",
            (event_id, token_id),
        )
        row = cursor.fetchone()

        if row:
            old_size = row["size"]
            old_avg = row["avg_price"]
            new_size = old_size + size_delta
            if new_size > 0:
                new_avg = (old_size * old_avg + size_delta * price) / new_size
            else:
                new_avg = 0.0
            cursor.execute(
                "UPDATE positions SET size = ?, avg_price = ? WHERE event_id = ? AND token_id = ?",
                (new_size, new_avg, event_id, token_id),
            )
        else:
            cursor.execute(
                """
                INSERT INTO positions (event_id, event_title, token_id, side, size, avg_price, opened_at)
                VALUES (?, ?, ?, 'YES', ?, ?, ?)
                """,
                (event_id, event_title, token_id, size_delta, price, time.time()),
            )
        self._conn.commit()

    def get_event_exposure(self, event_id: str) -> float:
        """Get total USDC exposure for a specific event."""
        cursor = self._conn.cursor()
        cursor.execute(
            "SELECT SUM(size * avg_price) as exposure FROM positions WHERE event_id = ? AND size > 0",
            (event_id,),
        )
        row = cursor.fetchone()
        return float(row["exposure"] or 0)

    def get_total_exposure(self) -> float:
        """Get total USDC exposure across all positions."""
        cursor = self._conn.cursor()
        cursor.execute(
            "SELECT SUM(size * avg_price) as exposure FROM positions WHERE size > 0"
        )
        row = cursor.fetchone()
        return float(row["exposure"] or 0)

    def get_daily_pnl(self) -> float:
        """Get today's realized P&L."""
        today = time.strftime("%Y-%m-%d")
        if self._daily_pnl_date != today:
            # Recompute from DB
            cursor = self._conn.cursor()
            start_ts = time.mktime(time.strptime(today, "%Y-%m-%d"))
            cursor.execute(
                "SELECT SUM(realized_profit) as pnl FROM trades WHERE timestamp >= ?",
                (start_ts,),
            )
            row = cursor.fetchone()
            self._daily_pnl = float(row["pnl"] or 0)
            self._daily_pnl_date = today
        return self._daily_pnl

    def get_cumulative_pnl(self) -> float:
        """Get all-time realized P&L."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT SUM(realized_profit) as pnl FROM trades")
        row = cursor.fetchone()
        return float(row["pnl"] or 0)

    def get_recent_trades(self, limit: int = 50) -> list[dict]:
        """Get recent trades."""
        cursor = self._conn.cursor()
        cursor.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def get_open_positions(self) -> list[dict]:
        """Get all open positions."""
        cursor = self._conn.cursor()
        cursor.execute("SELECT * FROM positions WHERE size > 0 ORDER BY opened_at DESC")
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def get_trade_count(self) -> int:
        cursor = self._conn.cursor()
        cursor.execute("SELECT COUNT(*) as cnt FROM trades")
        row = cursor.fetchone()
        return int(row["cnt"])

    def close(self):
        self._conn.close()
