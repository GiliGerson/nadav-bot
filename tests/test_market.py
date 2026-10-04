import math

import pandas as pd
import pytest

from app.market import (
    TickerNotFoundError,
    annualized_volatility,
    build_snapshot,
    normalize_ticker,
    rsi,
    sma,
    volume_ratio,
)


@pytest.mark.parametrize("raw, expected", [
    ("aapl", "AAPL"), ("$tsla", "TSLA"), ("^GSPC", "^GSPC"),
    ("teva.ta", "TEVA.TA"), ("btc-usd", "BTC-USD"), ("eurusd=x", "EURUSD=X"),
])
def test_normalize_valid(raw, expected):
    assert normalize_ticker(raw) == expected


@pytest.mark.parametrize("raw", ["", "hello world", "שלום", "A" * 20, "DROP;TABLE"])
def test_normalize_invalid(raw):
    with pytest.raises(ValueError):
        normalize_ticker(raw)


def test_rsi_extremes():
    assert rsi(pd.Series(range(1, 40), dtype=float)) == 100.0
    assert rsi(pd.Series(range(40, 1, -1), dtype=float)) == pytest.approx(0.0, abs=1e-9)


def test_rsi_needs_enough_data():
    assert rsi(pd.Series([1.0, 2.0, 3.0])) is None


def test_sma():
    assert sma(pd.Series([1.0, 2.0, 3.0, 4.0]), 2) == 3.5
    assert sma(pd.Series([1.0]), 2) is None


def test_volatility_zero_for_constant_growth_rate():
    closes = pd.Series([100 * (1.01 ** i) for i in range(60)])
    assert annualized_volatility(closes) == pytest.approx(0.0, abs=1e-9)


def test_volume_ratio_handles_zero_volume_indices():
    assert volume_ratio(pd.Series([0.0] * 30)) is None
    assert volume_ratio(pd.Series([100.0] * 20 + [300.0])) == pytest.approx(3.0)


def test_build_snapshot_uptrend(uptrend):
    snap = build_snapshot("TEST", uptrend, name="Test Co", currency="USD")
    assert snap.price == pytest.approx(200)
    assert snap.high_52w == pytest.approx(200)
    assert snap.from_52w_high_pct == pytest.approx(0)
    assert snap.change_1d_pct > 0
    assert snap.rsi_14 == 100.0
    assert snap.sma_20 > snap.sma_50 > snap.sma_200
    assert snap.as_of == uptrend.index[-1].strftime("%Y-%m-%d")


def test_build_snapshot_short_history(make):
    snap = build_snapshot("NEW", make([10.0, 11.0]))
    assert snap.change_1d_pct == pytest.approx(10.0)
    assert snap.sma_200 is None and snap.rsi_14 is None and snap.change_3m_pct is None


def test_build_snapshot_empty_raises(make):
    with pytest.raises(TickerNotFoundError):
        build_snapshot("NOPE", pd.DataFrame())


def test_to_dict_is_rounded_and_json_safe(uptrend):
    data = build_snapshot("TEST", uptrend).to_dict()
    assert "from_52w_high_pct" in data
    for value in data.values():
        if isinstance(value, float):
            assert not math.isnan(value)
            assert round(value, 2) == value


def test_minor_currency_converted_to_major(uptrend):
    from app.market import to_major_currency

    history, currency = to_major_currency(uptrend, "ILA")  # Tel Aviv quotes in agorot
    assert currency == "ILS"
    assert history["Close"].iloc[-1] == pytest.approx(uptrend["Close"].iloc[-1] / 100)
    assert history["Volume"].equals(uptrend["Volume"])
    assert to_major_currency(uptrend, "USD")[0] is uptrend
