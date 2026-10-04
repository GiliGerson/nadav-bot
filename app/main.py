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

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware
from telegram import Update

from app.bot import build_application, ensure_no_live_webhook, register_commands
from app.config import get_settings, setup_logging
from app.market import MarketData, Snapshot, TickerNotFoundError
from app.security import (
    inline_script_hashes,
    make_dashboard_auth,
    require_csrf_header,
    security_headers,
)
from app.signals import assess
from app.watchlist import Watchlist

settings = get_settings()
setup_logging(settings)
logger = logging.getLogger("nadav")

DASHBOARD = Path(__file__).parent / "static" / "dashboard.html"
SECURITY_HEADERS = security_headers(
    # Hash the bytes exactly as served: read_text() would normalise CRLF line endings and
    # produce a hash that blocks the page's own script.
    inline_script_hashes(DASHBOARD.read_bytes().decode()), https=settings.production
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    webhook_mode = settings.webhook_mode
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
            # Keep pending updates: on hosts that sleep when idle (Render's free plan), the
            # message that woke the server is still pending here, and dropping it would
            # silently swallow the user's first message.
            drop_pending_updates=False,
        )
        logger.info("Webhook registered at %s", url)
    else:
        await ensure_no_live_webhook(tg)
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


# The interactive API docs map every endpoint for anyone; keep them local-only.
docs = {} if not settings.production else {"docs_url": None, "redoc_url": None, "openapi_url": None}
app = FastAPI(title="Nadav", description="AI market analysis bot", lifespan=lifespan, **docs)

if not settings.production:
    # Locally the dashboard has no password, so only answer requests addressed to this machine.
    # This blocks DNS rebinding, where a malicious site points its own domain at 127.0.0.1
    # and reads the dashboard through the owner's browser.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])

# Everything that exposes or changes the owner's data sits behind the dashboard password.
protected = APIRouter(
    dependencies=[Depends(make_dashboard_auth(settings.dashboard_username, settings.dashboard_password))]
)


MAX_API_BODY_BYTES = 4096  # a watchlist add is ~20 bytes of JSON


@app.middleware("http")
async def limit_api_body_size(request: Request, call_next):
    # FastAPI reads and parses the JSON body before running the auth dependency, so without
    # this an anonymous client could post hundreds of MB and burn memory before getting a 401.
    if request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH"}:
        length = request.headers.get("content-length")
        if length is None:
            return JSONResponse({"detail": "Content-Length required"}, status_code=411)
        if not length.isdigit() or int(length) > MAX_API_BODY_BYTES:
            return JSONResponse({"detail": "Request body too large"}, status_code=413)
    return await call_next(request)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if request.url.path != "/health":
        # Watchlist data is personal: keep it out of browser and proxy caches.
        response.headers["Cache-Control"] = "no-store"
    return response


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


# Only mounted when it's actually used: in polling mode there's no route to forge updates into.
if settings.webhook_mode:
    app.add_api_route("/telegram/webhook", telegram_webhook, methods=["POST"], include_in_schema=False)


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


# GETs need the header too: otherwise any site could make the owner's browser fire
# Yahoo fetches through a local server with <img src="http://localhost:8000/api/...">.
@protected.get("/api/snapshot/{ticker}", dependencies=[Depends(require_csrf_header)])
async def snapshot(ticker: str, request: Request) -> dict:
    """Indicator snapshot as JSON (no LLM call)."""
    return (await _snapshot_or_http_error(request.app.state.market, ticker)).to_dict()


# --- Watchlist dashboard ------------------------------------------------------

class WatchlistAdd(BaseModel):
    ticker: str


@protected.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(DASHBOARD)


@protected.get("/api/watchlist", dependencies=[Depends(require_csrf_header)])
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


@protected.post("/api/watchlist", status_code=201, dependencies=[Depends(require_csrf_header)])
async def add_to_watchlist(body: WatchlistAdd, request: Request) -> dict:
    # Fetch first, so typos and delisted symbols never make it into the list.
    snap = await _snapshot_or_http_error(request.app.state.market, body.ticker)
    request.app.state.watchlist.add(snap.ticker)
    return {**snap.to_dict(), **assess(snap)}


@protected.delete(
    "/api/watchlist/{ticker}", status_code=204, dependencies=[Depends(require_csrf_header)]
)
async def remove_from_watchlist(ticker: str, request: Request) -> None:
    try:
        removed = request.app.state.watchlist.remove(ticker)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{ticker!r} doesn't look like a ticker") from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"{ticker.upper()} is not on the watchlist")


app.include_router(protected)
