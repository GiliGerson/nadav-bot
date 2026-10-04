"""Market data layer.

Fetches daily OHLCV history from Yahoo Finance (via yfinance) and derives a
compact, LLM-friendly snapshot of technical indicators. The indicator math is
kept in pure functions so it can be unit-tested without network access.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# yfinance initialises its on-disk SQLite tz cache lazily and without a lock, so
# parallel first fetches (e.g. /market on a cold container) can hit "database is locked".
_yf_cache_lock = threading.Lock()
_yf_cache_ready = False


def _init_yf_cache() -> None:
    global _yf_cache_ready
    with _yf_cache_lock:
        if not _yf_cache_ready:
            yf.cache.get_tz_cache().lookup("^GSPC")
            _yf_cache_ready = True

# Supports stocks (AAPL), indices (^GSPC), crypto (BTC-USD), FX (EURUSD=X),
# and exchange suffixes such as Tel Aviv (TEVA.TA).
TICKER_RE = re.compile(r"^\^?[A-Z0-9][A-Z0-9.\-=]{0,14}$")


class TickerNotFoundError(Exception):
    """Raised when Yahoo Finance returns no price history for a symbol."""


def normalize_ticker(raw: str) -> str:
    ticker = raw.strip().upper().lstrip("$")
    if not TICKER_RE.match(ticker):
        raise ValueError(f"Invalid ticker: {raw!r}")
    return ticker


@dataclass(frozen=True)
class Snapshot:
    ticker: str
    name: str
    currency: str
    price: float
    change_1d_pct: float
    change_1m_pct: float | None
    change_3m_pct: float | None
    high_52w: float
    low_52w: float
    sma_20: float | None
    sma_50: float | None
    sma_200: float | None
    rsi_14: float | None
    volatility_30d_pct: float | None
    volume_vs_20d_avg: float | None
    as_of: str

    @property
    def from_52w_high_pct(self) -> float:
        return (self.price / self.high_52w - 1) * 100

    def to_dict(self) -> dict:
        """Rounded dict used as the LLM's only source of truth."""
        data = asdict(self)
        data["from_52w_high_pct"] = self.from_52w_high_pct
        return {k: round(v, 2) if isinstance(v, float) else v for k, v in data.items()}


# --- Pure indicator functions -------------------------------------------------

def pct_change(new: float, old: float | None) -> float | None:
    if old is None or old == 0 or math.isnan(old):
        return None
    return (new / old - 1) * 100


def change_over(close: pd.Series, trading_days: int) -> float | None:
    if len(close) <= trading_days:
        return None
    return pct_change(float(close.iloc[-1]), float(close.iloc[-1 - trading_days]))


def sma(close: pd.Series, window: int) -> float | None:
    if len(close) < window:
        return None
    return float(close.tail(window).mean())


def rsi(close: pd.Series, period: int = 14) -> float | None:
    """Relative Strength Index using Wilder's smoothing."""
    if len(close) < period + 1:
        return None
    delta = close.diff().dropna()
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    avg_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().iloc[-1]
    avg_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean().iloc[-1]
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return float(100 - 100 / (1 + rs))


def annualized_volatility(close: pd.Series, window: int = 30) -> float | None:
    """Annualized std-dev of daily log returns over the last `window` days, in %."""
    if len(close) < window + 1:
        return None
    log_returns = np.log(close / close.shift(1)).dropna().tail(window)
    return float(log_returns.std(ddof=1) * math.sqrt(252) * 100)


def volume_ratio(volume: pd.Series, window: int = 20) -> float | None:
    """Latest volume relative to the previous `window`-day average."""
    if len(volume) < window + 1:
        return None
    baseline = volume.iloc[-window - 1:-1].mean()
    if not baseline or math.isnan(baseline):
        return None  # e.g. indices, which report zero volume
    return float(volume.iloc[-1] / baseline)


# Yahoo quotes some exchanges in minor units: Tel Aviv stocks in agorot ("ILA"),
# London in pence ("GBp"). Convert so "TEVA.TA 12,160 ILA" reads "121.60 ILS".
MINOR_CURRENCIES = {"ILA": "ILS", "GBp": "GBP", "ZAc": "ZAR"}


def to_major_currency(history: pd.DataFrame, currency: str) -> tuple[pd.DataFrame, str]:
    if currency not in MINOR_CURRENCIES:
        return history, currency
    history = history.copy()
    prices = [c for c in ("Open", "High", "Low", "Close") if c in history]
    history[prices] = history[prices] / 100
    return history, MINOR_CURRENCIES[currency]


def build_snapshot(
    ticker: str,
    history: pd.DataFrame,
    name: str | None = None,
    currency: str | None = None,
) -> Snapshot:
    if history is None or history.empty or "Close" not in history:
        raise TickerNotFoundError(ticker)
    close = history["Close"].dropna()
    if close.empty:
        raise TickerNotFoundError(ticker)

    price = float(close.iloc[-1])
    previous = float(close.iloc[-2]) if len(close) > 1 else price
    volume = history["Volume"].dropna() if "Volume" in history else pd.Series(dtype=float)
    last_date = close.index[-1]

    return Snapshot(
        ticker=ticker,
        name=name or ticker,
        currency=currency or "",
        price=price,
        change_1d_pct=pct_change(price, previous) or 0.0,
        change_1m_pct=change_over(close, 21),
        change_3m_pct=change_over(close, 63),
        high_52w=float(close.max()),
        low_52w=float(close.min()),
        sma_20=sma(close, 20),
        sma_50=sma(close, 50),
        sma_200=sma(close, 200),
        rsi_14=rsi(close),
        volatility_30d_pct=annualized_volatility(close),
        volume_vs_20d_avg=volume_ratio(volume),
        as_of=last_date.strftime("%Y-%m-%d") if hasattr(last_date, "strftime") else str(last_date),
    )


# --- Network + cache ----------------------------------------------------------

class MarketData:
    """Async facade over yfinance with a small in-memory TTL cache."""

    def __init__(self, ttl_seconds: int = 300, max_entries: int = 1_000) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._cache: dict[str, tuple[float, Snapshot]] = {}

    async def get_snapshot(self, raw_ticker: str) -> Snapshot:
        ticker = normalize_ticker(raw_ticker)
        cached = self._cache.get(ticker)
        if cached and time.monotonic() - cached[0] < self._ttl:
            return cached[1]
        # yfinance is blocking, so keep it off the event loop.
        snapshot = await asyncio.to_thread(self._fetch, ticker)
        self._cache.pop(ticker, None)
        self._cache[ticker] = (time.monotonic(), snapshot)
        if len(self._cache) > self._max_entries:
            # Dicts keep insertion order, so the first key is the oldest fetch.
            del self._cache[next(iter(self._cache))]
        return snapshot

    async def get_many(self, tickers: list[str]) -> list[Snapshot | Exception]:
        return await asyncio.gather(
            *(self.get_snapshot(t) for t in tickers), return_exceptions=True
        )

    @staticmethod
    def _fetch(ticker: str) -> Snapshot:
        _init_yf_cache()
        yf_ticker = yf.Ticker(ticker)
        history = yf_ticker.history(period="1y", interval="1d", auto_adjust=True)
        if history.empty:
            raise TickerNotFoundError(ticker)

        name, currency = ticker, ""
        try:
            currency = getattr(yf_ticker.fast_info, "currency", "") or ""
        except Exception:  # noqa: BLE001 - metadata is best-effort
            logger.debug("fast_info unavailable for %s", ticker)
        try:
            info = yf_ticker.get_info()
            name = info.get("shortName") or info.get("longName") or ticker
            currency = currency or info.get("currency", "")
        except Exception:  # noqa: BLE001
            logger.debug("info unavailable for %s", ticker)

        history, currency = to_major_currency(history, currency)
        return build_snapshot(ticker, history, name=name, currency=currency)
