"""Who gets a Claude call: the owner's quota, and a small public quota for everyone else."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import (
    AI_LIMIT_MESSAGE,
    AI_RESTRICTED_MARKET_NOTE,
    AI_RESTRICTED_MESSAGE,
    AI_USER_QUOTA_MESSAGE,
    _reply_with_analysis,
    myid,
)
from app.config import Settings
from app.usage import DailyAIUsage

OWNER, STRANGER, OTHER_STRANGER = 111, 999, 888


@pytest.fixture
def usage(tmp_path):
    clock = lambda: datetime(2026, 10, 4, 12, 0, tzinfo=UTC)  # noqa: E731
    return DailyAIUsage(str(tmp_path / "nadav.db"), clock=clock)


def make_settings(allowed: str = str(OWNER), **overrides) -> Settings:
    limits = {"daily_ai_limit": 2, "public_ai_per_user_daily": 2, "public_ai_daily_limit": 3}
    return Settings(
        _env_file=None,
        telegram_bot_token="t",
        anthropic_api_key="k",
        ai_allowed_user_ids=allowed,
        **{**limits, **overrides},
    )


def make_chat(user_id: int | None, settings: Settings, usage: DailyAIUsage):
    placeholder = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(reply_html=AsyncMock(return_value=placeholder))
    user = None if user_id is None else SimpleNamespace(id=user_id)
    update = SimpleNamespace(effective_message=message, effective_user=user)
    context = SimpleNamespace(bot_data={"settings": settings, "ai_usage": usage})
    return update, context, message, placeholder


async def ask(user_id, settings, usage, **kwargs):
    """Run one AI request; returns (claude_was_called, text_sent_to_user)."""
    update, context, message, placeholder = make_chat(user_id, settings, usage)
    make_analysis = AsyncMock(return_value="analysis text")
    await _reply_with_analysis(update, context, "<b>AAPL</b>", "Analyzing…", make_analysis, **kwargs)
    if make_analysis.called:
        return True, placeholder.edit_text.await_args.args[0]
    return False, message.reply_html.await_args.args[0]


# --- Settings parsing ---------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("", set()),
    ("123", {123}),
    ("123,456", {123, 456}),
    (" 123 , 456, ", {123, 456}),
])
def test_allowlist_parses_comma_separated_ids(raw, expected):
    assert make_settings(raw).ai_allowed_user_ids == expected


def test_allowlist_rejects_non_numeric_ids():
    with pytest.raises(ValueError):
        make_settings("123,@gili")


def test_public_quota_defaults():
    settings = Settings(_env_file=None, telegram_bot_token="t", anthropic_api_key="k")
    assert settings.ai_allowed_user_ids == set()
    assert (settings.public_ai_per_user_daily, settings.public_ai_daily_limit) == (3, 30)


# --- Owner --------------------------------------------------------------------

async def test_owner_gets_analysis_from_own_quota(usage):
    called, text = await ask(OWNER, make_settings(), usage)
    assert called and "analysis text" in text
    assert usage.used_today("owner") == 1
    assert usage.used_today("public") == 0


async def test_owner_hits_own_daily_cap(usage):
    settings = make_settings()
    results = [await ask(OWNER, settings, usage) for _ in range(3)]  # daily_ai_limit is 2
    assert [called for called, _ in results] == [True, True, False]
    assert AI_LIMIT_MESSAGE in results[-1][1]


# --- Public quota -------------------------------------------------------------

async def test_stranger_gets_analysis_from_public_quota(usage):
    called, text = await ask(STRANGER, make_settings(), usage)
    assert called and "analysis text" in text
    assert usage.used_today(f"user:{STRANGER}") == 1
    assert usage.used_today("public") == 1
    assert usage.used_today("owner") == 0


async def test_stranger_hits_per_user_quota(usage):
    settings = make_settings()
    results = [await ask(STRANGER, settings, usage) for _ in range(3)]  # 2 per user
    assert [called for called, _ in results] == [True, True, False]
    assert AI_USER_QUOTA_MESSAGE in results[-1][1]
    assert results[-1][1].startswith("<b>AAPL</b>")  # still gets the indicator card


async def test_strangers_share_the_public_cap(usage):
    settings = make_settings()  # public cap 3, 2 per user
    for _ in range(2):
        await ask(STRANGER, settings, usage)
    assert (await ask(OTHER_STRANGER, settings, usage))[0] is True  # 3rd public call
    called, text = await ask(OTHER_STRANGER, settings, usage)
    assert not called and AI_LIMIT_MESSAGE in text
    assert usage.used_today(f"user:{OTHER_STRANGER}") == 1  # refused call didn't use quota


async def test_strangers_cannot_use_up_the_owners_quota(usage):
    settings = make_settings()
    for stranger in range(20):
        await ask(1000 + stranger, settings, usage)
    assert usage.used_today("public") == 3
    assert (await ask(OWNER, settings, usage))[0] is True


@pytest.mark.parametrize("overrides", [{"public_ai_daily_limit": 0}, {"public_ai_per_user_daily": 0}])
async def test_zero_public_quota_makes_ai_owner_only(usage, overrides):
    settings = make_settings(**overrides)
    called, text = await ask(STRANGER, settings, usage)
    assert not called and AI_RESTRICTED_MESSAGE in text
    assert (await ask(OWNER, settings, usage))[0] is True


async def test_market_uses_its_own_note_when_ai_is_owner_only(usage):
    settings = make_settings(public_ai_daily_limit=0)
    _, text = await ask(STRANGER, settings, usage, restricted_note=AI_RESTRICTED_MARKET_NOTE)
    assert AI_RESTRICTED_MARKET_NOTE in text
    assert "/market" not in text  # don't send /market users back to /market


async def test_update_without_user_gets_no_ai(usage):
    called, text = await ask(None, make_settings(), usage)
    assert not called and AI_RESTRICTED_MESSAGE in text


# --- /myid --------------------------------------------------------------------

async def test_myid_replies_with_user_id():
    message = SimpleNamespace(reply_html=AsyncMock())
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=424242))
    await myid(update, SimpleNamespace())
    assert "<code>424242</code>" in message.reply_html.await_args.args[0]
