"""FastAPI entry point: Telegram bot, watchlist dashboard and JSON API in one process.

In production Telegram pushes updates to /telegram/webhook and FastAPI hands
them to the python-telegram-bot Application. Without WEBHOOK_BASE_URL (local
dev) the same process long-polls Telegram instead. The JSON API and the
dashboard make the market layer reusable outside Telegram.
"""
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from telegram import Update

from app.bot import build_application, register_commands
from app.config import get_settings, setup_logging
from app.market import MarketData, Snapshot, TickerNotFoundError
from app.signals import assess
from app.watchlist import Watchlist

settings = get_settings()
setup_logging(settings)
logger = logging.getLogger("nadav")

DASHBOARD = Path(__file__).parent / "static" / "dashboard.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    webhook_mode = bool(settings.webhook_base_url)
    tg = build_application(settings, webhook_mode=webhook_mode)
    await tg.initialize()
    await tg.start()
    await register_commands(tg)
    if webhook_mode:
        url = f"{settings.webhook_base_url.rstrip('/')}/telegram/webhook"
        await tg.bot.set_webhook(
            url=url,
            secret_token=settings.webhook_secret,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logger.info("Webhook registered at %s", url)
    else:
        await tg.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("WEBHOOK_BASE_URL not set; polling Telegram for updates.")
    app.state.tg = tg
    app.state.market = tg.bot_data["market"]
    app.state.watchlist = Watchlist(settings.watchlist_db_path)
    yield
    if tg.updater and tg.updater.running:
        await tg.updater.stop()
    await tg.stop()
    await tg.shutdown()


app = FastAPI(title="Nadav", description="AI market analysis bot", lifespan=lifespan)


@app.post("/telegram/webhook", include_in_schema=False)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    # Telegram echoes our secret in this header, so random callers are rejected.
    # compare_digest takes the same time whatever the input, so the secret can't be guessed
    # one character at a time from response timing.
    received = (x_telegram_bot_api_secret_token or "").encode()
    if not hmac.compare_digest(received, settings.webhook_secret.encode()):
        raise HTTPException(status_code=403, detail="Invalid secret token")
    tg = request.app.state.tg
    await tg.update_queue.put(Update.de_json(await request.json(), tg.bot))
    return {"ok": True}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


async def _snapshot_or_http_error(market: MarketData, ticker: str) -> Snapshot:
    try:
        return await market.get_snapshot(ticker)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{ticker!r} doesn't look like a ticker") from exc
    except TickerNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"No data for {ticker.upper()}") from exc


@app.get("/api/snapshot/{ticker}")
async def snapshot(ticker: str, request: Request) -> dict:
    """Indicator snapshot as JSON (no LLM call)."""
    return (await _snapshot_or_http_error(request.app.state.market, ticker)).to_dict()


# --- Watchlist dashboard ------------------------------------------------------

class WatchlistAdd(BaseModel):
    ticker: str


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(DASHBOARD)


@app.get("/api/watchlist")
async def get_watchlist(request: Request) -> list[dict]:
    """Every watched ticker with its snapshot, signals, score and stance."""
    tickers = request.app.state.watchlist.tickers()
    results = await request.app.state.market.get_many(tickers)
    rows = []
    for ticker, result in zip(tickers, results, strict=True):
        if isinstance(result, Snapshot):
            rows.append({**result.to_dict(), **assess(result)})
        else:
            logger.warning("Watchlist fetch failed for %s: %r", ticker, result)
            rows.append({"ticker": ticker, "error": "Data unavailable right now"})
    return rows


@app.post("/api/watchlist", status_code=201)
async def add_to_watchlist(body: WatchlistAdd, request: Request) -> dict:
    # Fetch first, so typos and delisted symbols never make it into the list.
    snap = await _snapshot_or_http_error(request.app.state.market, body.ticker)
    request.app.state.watchlist.add(snap.ticker)
    return {**snap.to_dict(), **assess(snap)}


@app.delete("/api/watchlist/{ticker}", status_code=204)
async def remove_from_watchlist(ticker: str, request: Request) -> None:
    try:
        removed = request.app.state.watchlist.remove(ticker)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{ticker!r} doesn't look like a ticker") from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"{ticker.upper()} is not on the watchlist")
