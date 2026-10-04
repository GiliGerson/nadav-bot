"""Global daily cap on Claude API calls, persisted in SQLite so restarts don't reset it."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path


class DailyAIUsage:
    """Counts Claude calls across all users; the count resets at midnight UTC."""

    def __init__(
        self, path: str, limit: int, clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    ) -> None:
        self._path = path
        self.limit = limit
        self._clock = clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS ai_usage (day TEXT PRIMARY KEY, calls INTEGER NOT NULL)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path)) as conn, conn:
            yield conn

    def _today(self) -> str:
        # Keyed by UTC date, so a new day starts a fresh row at zero.
        return self._clock().astimezone(UTC).date().isoformat()

    def try_acquire(self) -> bool:
        """Reserve one call for today. Returns False once the daily limit is reached."""
        day = self._today()
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO ai_usage (day, calls) VALUES (?, 0)", (day,))
            # Check and increment in one statement, so concurrent requests can't overshoot.
            cursor = conn.execute(
                "UPDATE ai_usage SET calls = calls + 1 WHERE day = ? AND calls < ?", (day, self.limit)
            )
            return cursor.rowcount == 1

    def used_today(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT calls FROM ai_usage WHERE day = ?", (self._today(),)).fetchone()
        return row[0] if row else 0
