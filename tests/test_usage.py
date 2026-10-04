from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import AI_LIMIT_MESSAGE, _reply_with_analysis
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
def db(tmp_path):
    return str(tmp_path / "data" / "nadav.db")


def test_increments_per_call(db, clock):
    usage = DailyAIUsage(db, limit=5, clock=clock)
    assert usage.used_today() == 0
    assert usage.try_acquire() is True
    assert usage.try_acquire() is True
    assert usage.used_today() == 2


def test_blocks_once_limit_reached(db, clock):
    usage = DailyAIUsage(db, limit=2, clock=clock)
    assert [usage.try_acquire() for _ in range(4)] == [True, True, False, False]
    assert usage.used_today() == 2  # blocked attempts don't count


def test_resets_at_midnight_utc(db, clock):
    usage = DailyAIUsage(db, limit=1, clock=clock)
    clock.now = datetime(2026, 10, 4, 23, 59, 59, tzinfo=UTC)
    assert usage.try_acquire() is True
    assert usage.try_acquire() is False
    clock.now += timedelta(seconds=1)  # 00:00:00 UTC on the 5th
    assert usage.used_today() == 0
    assert usage.try_acquire() is True


def test_day_boundary_is_utc_not_local_time(db, clock):
    usage = DailyAIUsage(db, limit=1, clock=clock)
    israel = timezone(timedelta(hours=3))
    clock.now = datetime(2026, 10, 5, 1, 0, tzinfo=israel)  # still Oct 4 in UTC
    assert usage.try_acquire() is True
    clock.now = datetime(2026, 10, 4, 23, 0, tzinfo=UTC)
    assert usage.try_acquire() is False


def test_count_survives_restart(db, clock):
    DailyAIUsage(db, limit=2, clock=clock).try_acquire()
    restarted = DailyAIUsage(db, limit=2, clock=clock)
    assert restarted.used_today() == 1
    assert restarted.try_acquire() is True
    assert restarted.try_acquire() is False


def test_zero_limit_disables_ai(db, clock):
    assert DailyAIUsage(db, limit=0, clock=clock).try_acquire() is False


async def test_limit_reached_sends_indicators_without_calling_claude(db, clock):
    usage = DailyAIUsage(db, limit=0, clock=clock)
    message = SimpleNamespace(reply_html=AsyncMock())
    update = SimpleNamespace(effective_message=message)
    context = SimpleNamespace(bot_data={"ai_usage": usage})
    make_analysis = AsyncMock()

    await _reply_with_analysis(update, context, "<b>AAPL</b>", "Analyzing…", make_analysis)

    make_analysis.assert_not_called()
    sent = message.reply_html.await_args.args[0]
    assert sent.startswith("<b>AAPL</b>")
    assert AI_LIMIT_MESSAGE in sent
