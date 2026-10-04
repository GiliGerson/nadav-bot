"""Persistent watchlist backed by SQLite (single user, so one small table is enough)."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.market import normalize_ticker


class Watchlist:
    def __init__(self, path: str) -> None:
        self._path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS watchlist (ticker TEXT PRIMARY KEY, added_at TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # `with conn` commits (or rolls back); closing() releases the file handle.
        with closing(sqlite3.connect(self._path)) as conn, conn:
            yield conn

    def tickers(self) -> list[str]:
        with self._connect() as conn:
            return [row[0] for row in conn.execute("SELECT ticker FROM watchlist ORDER BY added_at")]

    def add(self, raw_ticker: str) -> str:
        ticker = normalize_ticker(raw_ticker)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO watchlist (ticker, added_at) VALUES (?, ?)",
                (ticker, datetime.now(UTC).isoformat()),
            )
        return ticker

    def remove(self, raw_ticker: str) -> bool:
        ticker = normalize_ticker(raw_ticker)
        with self._connect() as conn:
            return conn.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker,)).rowcount > 0
