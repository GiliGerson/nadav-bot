from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.usage import DailyAIUsage


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 4, 12, 0, tzinfo=UTC))


@pytest.fixture
def usage(tmp_path, clock):
    return DailyAIUsage(str(tmp_path / "data" / "nadav.db"), clock=clock)


def test_increments_per_call(usage):
    assert usage.used_today("owner") == 0
    assert usage.try_acquire(("owner", 5)) is None
    assert usage.try_acquire(("owner", 5)) is None
    assert usage.used_today("owner") == 2


def test_blocks_once_limit_reached(usage):
    results = [usage.try_acquire(("owner", 2)) for _ in range(4)]
    assert results == [None, None, "owner", "owner"]
    assert usage.used_today("owner") == 2  # blocked attempts don't count


def test_buckets_are_independent(usage):
    assert usage.try_acquire(("public", 1)) is None
    assert usage.try_acquire(("public", 1)) == "public"
    assert usage.try_acquire(("owner", 1)) is None


def test_multi_bucket_reservation_is_all_or_nothing(usage):
    assert usage.try_acquire(("public", 1)) is None  # public now full
    assert usage.try_acquire(("user:7", 3), ("public", 1)) == "public"
    assert usage.used_today("user:7") == 0  # rolled back, quota not wasted


def test_reports_first_full_bucket(usage):
    for _ in range(3):
        assert usage.try_acquire(("user:7", 3), ("public", 30)) is None
    assert usage.try_acquire(("user:7", 3), ("public", 30)) == "user:7"
    assert usage.used_today("public") == 3


def test_resets_at_midnight_utc(usage, clock):
    clock.now = datetime(2026, 10, 4, 23, 59, 59, tzinfo=UTC)
    assert usage.try_acquire(("owner", 1)) is None
    assert usage.try_acquire(("owner", 1)) == "owner"
    clock.now += timedelta(seconds=1)  # 00:00:00 UTC on the 5th
    assert usage.used_today("owner") == 0
    assert usage.try_acquire(("owner", 1)) is None


def test_day_boundary_is_utc_not_local_time(usage, clock):
    israel = timezone(timedelta(hours=3))
    clock.now = datetime(2026, 10, 5, 1, 0, tzinfo=israel)  # still Oct 4 in UTC
    assert usage.try_acquire(("owner", 1)) is None
    clock.now = datetime(2026, 10, 4, 23, 0, tzinfo=UTC)
    assert usage.try_acquire(("owner", 1)) == "owner"


def test_count_survives_restart(tmp_path, clock):
    path = str(tmp_path / "nadav.db")
    DailyAIUsage(path, clock=clock).try_acquire(("owner", 2))
    restarted = DailyAIUsage(path, clock=clock)
    assert restarted.used_today("owner") == 1
    assert restarted.try_acquire(("owner", 2)) is None
    assert restarted.try_acquire(("owner", 2)) == "owner"


def test_zero_limit_blocks(usage):
    assert usage.try_acquire(("owner", 0)) == "owner"


def test_old_days_are_cleaned_up(usage, clock):
    for user in range(50):
        usage.try_acquire((f"user:{user}", 3))
    clock.now += timedelta(days=1)
    usage.try_acquire(("owner", 5))
    with usage._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM ai_calls").fetchone()[0] == 1
