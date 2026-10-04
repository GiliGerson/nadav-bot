"""Daily caps on Claude API calls, persisted in SQLite so restarts don't reset them.

Counts live in named buckets per UTC day, e.g. "owner", "public" (all strangers
together) and "user:<id>" (one stranger). A call reserves a slot in every bucket
that applies, all or nothing, before Claude is contacted.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path


class _BucketFull(Exception):
    def __init__(self, bucket: str) -> None:
        self.bucket = bucket


class DailyAIUsage:
    def __init__(self, path: str, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._path = path
        self._clock = clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ai_calls ("
                "day TEXT NOT NULL, bucket TEXT NOT NULL, calls INTEGER NOT NULL, "
                "PRIMARY KEY (day, bucket))"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # `with conn` commits on success and rolls back if the block raises.
        with closing(sqlite3.connect(self._path)) as conn, conn:
            yield conn

    def _today(self) -> str:
        # Keyed by UTC date, so each new day starts every bucket at zero.
        return self._clock().astimezone(UTC).date().isoformat()

    def try_acquire(self, *limits: tuple[str, int]) -> str | None:
        """Reserve one call in each (bucket, daily limit) pair, all or nothing.

        Returns None on success, or the name of the first bucket that is full. A full
        bucket rolls back the others, so a refused request never uses up anyone's quota.
        """
        day = self._today()
        try:
            with self._connect() as conn:
                # One row per stranger per day; old days are no longer needed.
                conn.execute("DELETE FROM ai_calls WHERE day < ?", (day,))
                for bucket, limit in limits:
                    conn.execute(
                        "INSERT OR IGNORE INTO ai_calls (day, bucket, calls) VALUES (?, ?, 0)", (day, bucket)
                    )
                    # Check and increment in one statement, so concurrent requests can't overshoot.
                    cursor = conn.execute(
                        "UPDATE ai_calls SET calls = calls + 1 WHERE day = ? AND bucket = ? AND calls < ?",
                        (day, bucket, limit),
                    )
                    if cursor.rowcount != 1:
                        raise _BucketFull(bucket)
        except _BucketFull as full:
            return full.bucket
        return None

    def used_today(self, bucket: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT calls FROM ai_calls WHERE day = ? AND bucket = ?", (self._today(), bucket)
            ).fetchone()
        return row[0] if row else 0
