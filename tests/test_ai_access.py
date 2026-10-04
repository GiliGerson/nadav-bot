"""Who gets a Claude call: the user allowlist is checked first, then the daily cap."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot import (
    AI_LIMIT_MESSAGE,
    AI_RESTRICTED_MARKET_NOTE,
    AI_RESTRICTED_MESSAGE,
    _reply_with_analysis,
    myid,
)
from app.config import Settings
from app.usage import DailyAIUsage

ALLOWED, STRANGER = 111, 999


@pytest.fixture
def usage(tmp_path):
    clock = lambda: datetime(2026, 10, 4, 12, 0, tzinfo=UTC)  # noqa: E731
    return DailyAIUsage(str(tmp_path / "nadav.db"), limit=2, clock=clock)


def make_settings(allowed: str) -> Settings:
    return Settings(
        _env_file=None, telegram_bot_token="t", anthropic_api_key="k", ai_allowed_user_ids=allowed
    )


def make_chat(user_id: int | None, settings: Settings, usage: DailyAIUsage):
    placeholder = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(reply_html=AsyncMock(return_value=placeholder))
    user = None if user_id is None else SimpleNamespace(id=user_id)
    update = SimpleNamespace(effective_message=message, effective_user=user)
    context = SimpleNamespace(bot_data={"settings": settings, "ai_usage": usage})
    return update, context, message, placeholder


async def ask(update, context, **kwargs):
    make_analysis = AsyncMock(return_value="analysis text")
    await _reply_with_analysis(update, context, "<b>AAPL</b>", "Analyzing…", make_analysis, **kwargs)
    return make_analysis


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


def test_allowlist_defaults_to_nobody():
    settings = Settings(_env_file=None, telegram_bot_token="t", anthropic_api_key="k")
    assert settings.ai_allowed_user_ids == set()


# --- Gating -------------------------------------------------------------------

async def test_allowed_user_gets_analysis_and_uses_cap(usage):
    update, context, message, placeholder = make_chat(ALLOWED, make_settings(str(ALLOWED)), usage)
    make_analysis = await ask(update, context)
    make_analysis.assert_called_once()
    assert "analysis text" in placeholder.edit_text.await_args.args[0]
    assert usage.used_today() == 1


async def test_stranger_gets_card_and_restricted_message(usage):
    update, context, message, _ = make_chat(STRANGER, make_settings(str(ALLOWED)), usage)
    make_analysis = await ask(update, context)
    make_analysis.assert_not_called()
    sent = message.reply_html.await_args.args[0]
    assert sent.startswith("<b>AAPL</b>")
    assert AI_RESTRICTED_MESSAGE in sent


async def test_empty_allowlist_blocks_everyone(usage):
    update, context, message, _ = make_chat(ALLOWED, make_settings(""), usage)
    (await ask(update, context)).assert_not_called()
    assert AI_RESTRICTED_MESSAGE in message.reply_html.await_args.args[0]


async def test_update_without_user_is_restricted(usage):
    update, context, _, _ = make_chat(None, make_settings(str(ALLOWED)), usage)
    (await ask(update, context)).assert_not_called()


async def test_market_uses_its_own_restricted_note(usage):
    update, context, message, _ = make_chat(STRANGER, make_settings(str(ALLOWED)), usage)
    await ask(update, context, restricted_note=AI_RESTRICTED_MARKET_NOTE)
    sent = message.reply_html.await_args.args[0]
    assert AI_RESTRICTED_MARKET_NOTE in sent
    assert "/market" not in sent  # don't send /market users back to /market


# --- Interaction with the daily cap -------------------------------------------

async def test_strangers_do_not_consume_daily_cap(usage):
    settings = make_settings(str(ALLOWED))
    for _ in range(5):
        update, context, _, _ = make_chat(STRANGER, settings, usage)
        await ask(update, context)
    assert usage.used_today() == 0

    update, context, _, _ = make_chat(ALLOWED, settings, usage)
    (await ask(update, context)).assert_called_once()


async def test_allowed_user_still_hits_daily_cap(usage):
    settings = make_settings(str(ALLOWED))
    calls = []
    for _ in range(3):  # cap is 2
        update, context, message, _ = make_chat(ALLOWED, settings, usage)
        calls.append(await ask(update, context))
    assert [c.called for c in calls] == [True, True, False]
    assert AI_LIMIT_MESSAGE in message.reply_html.await_args.args[0]


async def test_stranger_sees_restricted_message_even_when_cap_reached(usage):
    settings = make_settings(str(ALLOWED))
    while usage.try_acquire():
        pass
    update, context, message, _ = make_chat(STRANGER, settings, usage)
    await ask(update, context)
    sent = message.reply_html.await_args.args[0]
    assert AI_RESTRICTED_MESSAGE in sent
    assert AI_LIMIT_MESSAGE not in sent


async def test_cap_zero_blocks_allowed_user_without_calling_claude(tmp_path):
    usage = DailyAIUsage(str(tmp_path / "nadav.db"), limit=0)
    update, context, message, _ = make_chat(ALLOWED, make_settings(str(ALLOWED)), usage)
    (await ask(update, context)).assert_not_called()
    assert AI_LIMIT_MESSAGE in message.reply_html.await_args.args[0]


# --- /myid --------------------------------------------------------------------

async def test_myid_replies_with_user_id():
    message = SimpleNamespace(reply_html=AsyncMock())
    update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=424242))
    await myid(update, SimpleNamespace())
    assert "<code>424242</code>" in message.reply_html.await_args.args[0]
