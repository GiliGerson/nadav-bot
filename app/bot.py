"""Telegram layer: command handlers, formatting, and per-user rate limiting."""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from html import escape

from anthropic import APIError
from telegram import BotCommand, Message, Update
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from app.analysis import Analyst
from app.config import Settings
from app.market import MarketData, Snapshot, TickerNotFoundError, normalize_ticker
from app.ratelimit import RateLimiter
from app.usage import DailyAIUsage

logger = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
# Bidi marks and zero-width chars that RTL keyboards (e.g. Hebrew) can prepend to a message.
# They stop Telegram from tagging "/price AAPL" as a command, so we strip them ourselves.
INVISIBLE_CHARS = dict.fromkeys(
    map(ord, "\u200b\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff")
)
DISCLAIMER = "Not financial advice. Data from Yahoo Finance, may be delayed."
AI_LIMIT_MESSAGE = (
    "Daily AI analysis limit reached. Try again tomorrow, or use /price for instant indicators."
)
AI_RESTRICTED_MESSAGE = "AI analysis is limited to a private demo. Try /price or /market."
AI_USER_QUOTA_MESSAGE = (
    "You've used today's free AI analyses. Try again tomorrow, or use /price for instant indicators."
)
# /market itself is open to everyone, so it can't point users back to /market.
AI_RESTRICTED_MARKET_NOTE = "AI commentary is limited to a private demo."
CONCURRENT_UPDATES = 8
MARKET_TICKERS = ["^GSPC", "^IXIC", "^DJI", "^VIX", "^TA125.TA"]

COMMANDS = [
    BotCommand("analyze", "AI analysis of a ticker, e.g. /analyze NVDA"),
    BotCommand("price", "Quick snapshot without AI, e.g. /price TEVA.TA"),
    BotCommand("compare", "Compare 2-4 tickers, e.g. /compare AAPL MSFT"),
    BotCommand("market", "Market pulse across major indices"),
    BotCommand("myid", "Show your Telegram user ID"),
    BotCommand("help", "How to use Nadav"),
]

WELCOME = (
    "👋 <b>Hi, I'm Nadav</b>, your AI market analyst.\n\n"
    "/analyze <code>NVDA</code>: indicators + AI analysis\n"
    "/price <code>TEVA.TA</code>: quick snapshot\n"
    "/compare <code>AAPL MSFT</code>: side-by-side\n"
    "/market: pulse of the major indices\n\n"
    "Tip: just send a ticker like <code>BTC-USD</code> and I'll analyze it.\n"
    "AI analyses are free to try, a few per day.\n\n"
    f"<i>{DISCLAIMER}</i>"
)


# --- Formatting ---------------------------------------------------------------

def fmt_num(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}%"


def format_snapshot(s: Snapshot) -> str:
    dot = "🟢" if s.change_1d_pct >= 0 else "🔴"
    return "\n".join([
        f"<b>{escape(s.name)}</b> (<code>{escape(s.ticker)}</code>)",
        f"{dot} {fmt_num(s.price)} {escape(s.currency)}  ({fmt_pct(s.change_1d_pct)} today)",
        f"1M {fmt_pct(s.change_1m_pct)} · 3M {fmt_pct(s.change_3m_pct)}",
        f"52W {fmt_num(s.low_52w)} – {fmt_num(s.high_52w)} "
        f"({fmt_pct(s.from_52w_high_pct)} from high)",
        f"RSI(14) {fmt_num(s.rsi_14, 1)} · Vol(30d) {fmt_num(s.volatility_30d_pct, 1)}%",
        f"SMA 20/50/200: {fmt_num(s.sma_20)} / {fmt_num(s.sma_50)} / {fmt_num(s.sma_200)}",
        f"<i>As of {escape(s.as_of)}</i>",
    ])


def compact_line(s: Snapshot) -> str:
    dot = "🟢" if s.change_1d_pct >= 0 else "🔴"
    return f"{dot} <b>{escape(s.name)}</b> {fmt_num(s.price)} ({fmt_pct(s.change_1d_pct)})"


def with_analysis(header: str, analysis: str) -> str:
    footer = f"\n\n<i>{DISCLAIMER}</i>"
    room = TELEGRAM_LIMIT - len(header) - len(footer) - 4
    if len(escape(analysis)) <= room:
        return f"{header}\n\n{escape(analysis)}{footer}"
    # Cut the raw text, then escape. Cutting escaped text could split an entity like "&amp;",
    # which Telegram rejects as invalid HTML. Every character escapes to at least one, so
    # dropping the overshoot in raw characters always fits.
    text = analysis
    while len(escape(text)) > room - 1:
        text = text[: len(text) - (len(escape(text)) - (room - 1))]
    return f"{header}\n\n{escape(text)}…{footer}"


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """Parse '/price AAPL' (or '/price@MyBot AAPL') into ('price', ['AAPL'])."""
    parts = text.translate(INVISIBLE_CHARS).strip().split()
    if not parts or not parts[0].startswith("/") or len(parts[0]) < 2:
        return None
    return parts[0][1:].split("@")[0].lower(), parts[1:]


# --- Helpers ------------------------------------------------------------------

def _deps(context: ContextTypes.DEFAULT_TYPE) -> tuple[Settings, MarketData, Analyst]:
    data = context.bot_data
    return data["settings"], data["market"], data["analyst"]


def _on_cooldown(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Per-user gap between analyses, so one user can't burn through the LLM budget."""
    return not context.bot_data["llm_cooldown"].allow(user_id)


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every handler: private chats only, and a per-user rate limit on everything.

    Ignoring groups stops the bot being added to a large chat and spammed from there. The
    rate limit covers commands that never reach Claude too (/price, /market, typos), since
    flooding those can still get our IP throttled or banned by Yahoo Finance.
    """
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None or chat.type != ChatType.PRIVATE:
        raise ApplicationHandlerStop
    limiter: RateLimiter = context.bot_data["rate_limiter"]
    if not limiter.allow(user.id):
        if update.effective_message and limiter.should_warn(user.id):
            await update.effective_message.reply_text("⏳ Too many requests. Try again in a minute.")
        raise ApplicationHandlerStop


async def _typing(update: Update) -> None:
    await update.effective_chat.send_action(ChatAction.TYPING)


async def _fetch_or_reply(update: Update, market: MarketData, raw: str) -> Snapshot | None:
    message = update.effective_message
    try:
        return await market.get_snapshot(raw)
    except ValueError:
        await message.reply_html(f"🤔 <code>{escape(raw)}</code> doesn't look like a ticker.")
    except TickerNotFoundError:
        await message.reply_html(
            f"🔍 No data for <code>{escape(raw.upper())}</code>. "
            "Try the Yahoo Finance symbol (e.g. <code>TEVA.TA</code>, <code>BTC-USD</code>)."
        )
    except Exception:
        logger.exception("Market data fetch failed for %s", raw)
        await message.reply_text("⚠️ Market data is unavailable right now. Try again shortly.")
    return None


async def _reply_with_analysis(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    header: str,
    thinking: str,
    make_analysis: Callable[[], Awaitable[str]],
    restricted_note: str = AI_RESTRICTED_MESSAGE,
) -> None:
    """Reply with the indicator header, then fill in Claude's analysis if the user may have it.

    The owner (allowlisted IDs) draws on their own daily quota. Everyone else shares a
    public one: a few calls per person and a cap for all of them together, kept separate
    so strangers can never use up the owner's budget.
    """
    settings: Settings = context.bot_data["settings"]
    usage: DailyAIUsage = context.bot_data["ai_usage"]
    user = update.effective_user
    public_ai_open = settings.public_ai_per_user_daily > 0 and settings.public_ai_daily_limit > 0
    if user is not None and user.id in settings.ai_allowed_user_ids:
        full = usage.try_acquire(("owner", settings.daily_ai_limit))
    elif user is not None and public_ai_open:
        full = usage.try_acquire(
            (f"user:{user.id}", settings.public_ai_per_user_daily),
            ("public", settings.public_ai_daily_limit),
        )
    else:
        await update.effective_message.reply_html(f"{header}\n\n🔒 <i>{restricted_note}</i>")
        return
    if full is not None:
        logger.info("AI quota %r is full; sending indicators only", full.split(":")[0])
        note = AI_USER_QUOTA_MESSAGE if full.startswith("user:") else AI_LIMIT_MESSAGE
        await update.effective_message.reply_html(f"{header}\n\n⏳ <i>{note}</i>")
        return
    placeholder = await update.effective_message.reply_html(f"{header}\n\n🧠 <i>{thinking}</i>")
    await _finish_with_llm(placeholder, header, make_analysis())


async def _finish_with_llm(placeholder: Message, header: str, coro: Awaitable[str]) -> None:
    try:
        analysis = await coro
        await placeholder.edit_text(with_analysis(header, analysis), parse_mode=ParseMode.HTML)
    except APIError:
        logger.exception("Claude API call failed")
        await placeholder.edit_text(
            f"{header}\n\n⚠️ <i>AI analysis is unavailable right now.</i>",
            parse_mode=ParseMode.HTML,
        )


# --- Handlers -----------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_html(WELCOME)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the sender's Telegram ID, the value AI_ALLOWED_USER_IDS expects."""
    await update.effective_message.reply_html(
        f"Your Telegram user ID: <code>{update.effective_user.id}</code>"
    )


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_html("Usage: /price <code>AAPL</code>")
        return
    _, market, _ = _deps(context)
    await _typing(update)
    snapshot = await _fetch_or_reply(update, market, context.args[0])
    if snapshot:
        await update.effective_message.reply_html(format_snapshot(snapshot))


async def analyze(
    update: Update, context: ContextTypes.DEFAULT_TYPE, ticker: str | None = None
) -> None:
    ticker = ticker or (context.args[0] if context.args else None)
    if not ticker:
        await update.effective_message.reply_html("Usage: /analyze <code>NVDA</code>")
        return
    _, market, analyst = _deps(context)
    if _on_cooldown(context, update.effective_user.id):
        await update.effective_message.reply_text("⏳ One sec, give me a few seconds between analyses.")
        return

    await _typing(update)
    snapshot = await _fetch_or_reply(update, market, ticker)
    if not snapshot:
        return
    header = format_snapshot(snapshot)
    await _reply_with_analysis(update, context, header, "Analyzing…", lambda: analyst.analyze(snapshot))


async def compare(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tickers = list(dict.fromkeys(a.upper() for a in context.args))  # dedupe, keep order
    if not 2 <= len(tickers) <= 4:
        await update.effective_message.reply_html(
            "Usage: /compare <code>AAPL MSFT GOOGL</code> (2-4 tickers)"
        )
        return
    _, market, analyst = _deps(context)
    if _on_cooldown(context, update.effective_user.id):
        await update.effective_message.reply_text("⏳ One sec, give me a few seconds between analyses.")
        return

    await _typing(update)
    results = await market.get_many(tickers)
    failed = [t for t, r in zip(tickers, results, strict=True) if isinstance(r, Exception)]
    if failed:
        await update.effective_message.reply_html(
            "🔍 Couldn't load: " + ", ".join(f"<code>{escape(t)}</code>" for t in failed)
        )
        return

    header = "⚖️ <b>Comparison</b>\n" + "\n".join(compact_line(s) for s in results)
    await _reply_with_analysis(update, context, header, "Comparing…", lambda: analyst.compare(results))


async def market_pulse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, market, analyst = _deps(context)
    if _on_cooldown(context, update.effective_user.id):
        await update.effective_message.reply_text("⏳ One sec, give me a few seconds between analyses.")
        return

    await _typing(update)
    snapshots = [r for r in await market.get_many(MARKET_TICKERS) if isinstance(r, Snapshot)]
    if not snapshots:
        await update.effective_message.reply_text("⚠️ Market data is unavailable right now.")
        return

    header = "🌍 <b>Market pulse</b>\n" + "\n".join(compact_line(s) for s in snapshots)
    await _reply_with_analysis(
        update,
        context,
        header,
        "Reading the room…",
        lambda: analyst.market_pulse(snapshots),
        restricted_note=AI_RESTRICTED_MARKET_NOTE,
    )


async def free_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A bare ticker (e.g. 'nvda' or '$TSLA') is treated as /analyze."""
    text = (update.effective_message.text or "").translate(INVISIBLE_CHARS).strip()
    command = parse_command(text)
    if command and command[0] in COMMAND_HANDLERS:
        # A command Telegram didn't tag as one (see INVISIBLE_CHARS); route it ourselves.
        logger.info("Routing untagged command %r", text)
        context.args = command[1]
        await COMMAND_HANDLERS[command[0]](update, context)
        return
    try:
        ticker = normalize_ticker(text)
    except ValueError:
        await update.effective_message.reply_html(
            "Send me a ticker like <code>AAPL</code>, or /help to see what I can do."
        )
        return
    await analyze(update, context, ticker=ticker)


COMMAND_HANDLERS = {
    "start": start,
    "help": start,
    "price": price,
    "analyze": analyze,
    "compare": compare,
    "market": market_pulse,
    "myid": myid,
}


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error while processing update", exc_info=context.error)


# --- Wiring -------------------------------------------------------------------

async def register_commands(application: Application) -> None:
    await application.bot.set_my_commands(COMMANDS)


async def ensure_no_live_webhook(application: Application) -> None:
    """Stop local polling from silently taking the bot away from a deployment.

    Starting to poll deletes the webhook, so the deployed bot would stop receiving
    messages with no error anywhere. Refuse unless FORCE_POLLING is set.
    """
    if application.bot_data["settings"].force_polling:
        return
    info = await application.bot.get_webhook_info()
    if info.url:
        raise RuntimeError(
            f"This bot is deployed (webhook active at {info.url}). Polling would switch the "
            "deployed bot off. Stop it first, use a separate test bot token, or set "
            "FORCE_POLLING=true to take the bot back on purpose."
        )


async def _post_init_polling(application: Application) -> None:
    await ensure_no_live_webhook(application)
    await register_commands(application)


def build_application(settings: Settings, *, webhook_mode: bool) -> Application:
    builder = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(register_commands if webhook_mode else _post_init_polling)
        # Updates are handled one at a time by default, so a few slow requests (Yahoo, Claude)
        # from strangers would queue up everyone else's, the owner's included.
        .concurrent_updates(CONCURRENT_UPDATES)
    )
    if webhook_mode:
        builder = builder.updater(None)  # FastAPI receives updates instead
    application = builder.build()

    application.bot_data.update(
        settings=settings,
        market=MarketData(ttl_seconds=settings.cache_ttl_seconds),
        analyst=Analyst(settings.anthropic_api_key, settings.claude_model, settings.bot_language),
        ai_usage=DailyAIUsage(settings.watchlist_db_path),
        rate_limiter=RateLimiter(settings.user_requests_per_minute, window_seconds=60),
        llm_cooldown=RateLimiter(1, window_seconds=settings.user_cooldown_seconds),
    )

    # Group -1 runs before the command handlers, and ApplicationHandlerStop ends processing.
    application.add_handler(TypeHandler(Update, gate), group=-1)
    for name, handler in COMMAND_HANDLERS.items():
        application.add_handler(CommandHandler(name, handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))
    application.add_error_handler(on_error)
    return application
