"""Telegram layer: command handlers, formatting, and per-user rate limiting."""
from __future__ import annotations

import logging
import time
from html import escape

from anthropic import APIError
from telegram import BotCommand, Message, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.analysis import Analyst
from app.config import Settings
from app.market import MarketData, Snapshot, TickerNotFoundError, normalize_ticker

logger = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
# Bidi marks and zero-width chars that RTL keyboards (e.g. Hebrew) can prepend to a message.
# They stop Telegram from tagging "/price AAPL" as a command, so we strip them ourselves.
INVISIBLE_CHARS = dict.fromkeys(
    map(ord, "\u200b\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff")
)
DISCLAIMER = "Not financial advice. Data from Yahoo Finance, may be delayed."
MARKET_TICKERS = ["^GSPC", "^IXIC", "^DJI", "^VIX", "^TA125.TA"]

COMMANDS = [
    BotCommand("analyze", "AI analysis of a ticker, e.g. /analyze NVDA"),
    BotCommand("price", "Quick snapshot without AI, e.g. /price TEVA.TA"),
    BotCommand("compare", "Compare 2-4 tickers, e.g. /compare AAPL MSFT"),
    BotCommand("market", "Market pulse across major indices"),
    BotCommand("help", "How to use Nadav"),
]

WELCOME = (
    "👋 <b>Hi, I'm Nadav</b>, your AI market analyst.\n\n"
    "/analyze <code>NVDA</code>: indicators + AI analysis\n"
    "/price <code>TEVA.TA</code>: quick snapshot\n"
    "/compare <code>AAPL MSFT</code>: side-by-side\n"
    "/market: pulse of the major indices\n\n"
    "Tip: just send a ticker like <code>BTC-USD</code> and I'll analyze it.\n\n"
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
    body = escape(analysis)
    room = TELEGRAM_LIMIT - len(header) - len(footer) - 4
    if len(body) > room:
        body = body[: room - 1] + "…"
    return f"{header}\n\n{body}{footer}"


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
    """Simple per-user limiter so one user can't burn through the LLM budget."""
    settings = context.bot_data["settings"]
    last_calls: dict[int, float] = context.bot_data.setdefault("last_llm_call", {})
    now = time.monotonic()
    if now - last_calls.get(user_id, float("-inf")) < settings.user_cooldown_seconds:
        return True
    last_calls[user_id] = now
    return False


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


async def _finish_with_llm(placeholder: Message, header: str, coro) -> None:
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
    placeholder = await update.effective_message.reply_html(f"{header}\n\n🧠 <i>Analyzing…</i>")
    await _finish_with_llm(placeholder, header, analyst.analyze(snapshot))


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
    placeholder = await update.effective_message.reply_html(f"{header}\n\n🧠 <i>Comparing…</i>")
    await _finish_with_llm(placeholder, header, analyst.compare(results))


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
    placeholder = await update.effective_message.reply_html(f"{header}\n\n🧠 <i>Reading the room…</i>")
    await _finish_with_llm(placeholder, header, analyst.market_pulse(snapshots))


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
}


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled error while processing update", exc_info=context.error)


# --- Wiring -------------------------------------------------------------------

async def register_commands(application: Application) -> None:
    await application.bot.set_my_commands(COMMANDS)


def build_application(settings: Settings, *, webhook_mode: bool) -> Application:
    builder = Application.builder().token(settings.telegram_bot_token).post_init(register_commands)
    if webhook_mode:
        builder = builder.updater(None)  # FastAPI receives updates instead
    application = builder.build()

    application.bot_data.update(
        settings=settings,
        market=MarketData(ttl_seconds=settings.cache_ttl_seconds),
        analyst=Analyst(settings.anthropic_api_key, settings.claude_model, settings.bot_language),
    )

    for name, handler in COMMAND_HANDLERS.items():
        application.add_handler(CommandHandler(name, handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))
    application.add_error_handler(on_error)
    return application
