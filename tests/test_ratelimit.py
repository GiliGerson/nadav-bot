from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.ext import ApplicationHandlerStop

from app.bot import gate
from app.market import MarketData, build_snapshot
from app.ratelimit import RateLimiter


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


# --- RateLimiter --------------------------------------------------------------

def test_allows_up_to_limit_then_blocks(clock):
    limiter = RateLimiter(3, window_seconds=60, clock=clock)
    assert [limiter.allow("u") for _ in range(5)] == [True, True, True, False, False]


def test_window_slides(clock):
    limiter = RateLimiter(2, window_seconds=60, clock=clock)
    assert limiter.allow("u") and limiter.allow("u")
    clock.now += 30
    assert not limiter.allow("u")
    clock.now += 30  # the first two events are now a full window old
    assert limiter.allow("u")


def test_users_are_limited_independently(clock):
    limiter = RateLimiter(1, window_seconds=60, clock=clock)
    assert limiter.allow("alice")
    assert limiter.allow("bob")
    assert not limiter.allow("alice")


def test_memory_is_bounded(clock):
    limiter = RateLimiter(1, window_seconds=60, max_keys=100, clock=clock)
    for user in range(10_000):
        limiter.allow(user)
    assert len(limiter._events) == 100
    assert limiter.allow(0)  # forgotten, so treated as new


def test_warns_once_per_window(clock):
    limiter = RateLimiter(1, window_seconds=60, clock=clock)
    assert [limiter.should_warn("u") for _ in range(3)] == [True, False, False]
    clock.now += 60
    assert limiter.should_warn("u")


def test_zero_cooldown_never_blocks(clock):
    limiter = RateLimiter(1, window_seconds=0, clock=clock)
    assert all(limiter.allow("u") for _ in range(5))


# --- Telegram gate ------------------------------------------------------------

def make_update(chat_type: str = "private", user_id: int | None = 1):
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(
        effective_chat=SimpleNamespace(type=chat_type),
        effective_user=None if user_id is None else SimpleNamespace(id=user_id),
        effective_message=message,
    ), message


def make_context(limit: int = 2):
    return SimpleNamespace(bot_data={"rate_limiter": RateLimiter(limit, window_seconds=60)})


async def test_gate_lets_private_messages_through():
    update, _ = make_update()
    await gate(update, make_context())  # no ApplicationHandlerStop


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_gate_ignores_groups_and_channels(chat_type):
    update, message = make_update(chat_type)
    with pytest.raises(ApplicationHandlerStop):
        await gate(update, make_context())
    message.reply_text.assert_not_called()


async def test_gate_ignores_updates_without_a_user():
    update, _ = make_update(user_id=None)
    with pytest.raises(ApplicationHandlerStop):
        await gate(update, make_context())


async def test_gate_rate_limits_and_warns_once():
    context = make_context(limit=2)
    update, message = make_update()
    await gate(update, context)
    await gate(update, context)
    for _ in range(3):
        with pytest.raises(ApplicationHandlerStop):
            await gate(update, context)
    message.reply_text.assert_awaited_once()
    assert "Too many requests" in message.reply_text.await_args.args[0]


# --- Market cache bound -------------------------------------------------------

async def test_market_cache_is_bounded(monkeypatch, uptrend):
    market = MarketData(max_entries=3)
    monkeypatch.setattr(MarketData, "_fetch", staticmethod(lambda t: build_snapshot(t, uptrend)))
    for ticker in ["AAA", "BBB", "CCC", "DDD"]:
        await market.get_snapshot(ticker)
    assert list(market._cache) == ["BBB", "CCC", "DDD"]


async def test_unknown_tickers_are_cached_as_missing(monkeypatch):
    from app.market import TickerNotFoundError

    calls = []

    def fetch(ticker):
        calls.append(ticker)
        raise TickerNotFoundError(ticker)

    monkeypatch.setattr(MarketData, "_fetch", staticmethod(fetch))
    market = MarketData()
    for _ in range(5):
        with pytest.raises(TickerNotFoundError):
            await market.get_snapshot("ZZZZQX")
    assert calls == ["ZZZZQX"]


async def test_parallel_yahoo_fetches_are_capped(monkeypatch, uptrend):
    import asyncio
    import threading

    active, peak, lock = 0, 0, threading.Lock()

    def fetch(ticker):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        threading.Event().wait(0.05)
        with lock:
            active -= 1
        return build_snapshot(ticker, uptrend)

    monkeypatch.setattr(MarketData, "_fetch", staticmethod(fetch))
    market = MarketData(max_concurrent=2)
    await asyncio.gather(*(market.get_snapshot(f"T{i}") for i in range(8)))
    assert peak == 2
