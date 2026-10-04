from dataclasses import replace

import numpy as np

from app.market import build_snapshot
from app.signals import Signal, assess, score, stance


def test_steady_uptrend_is_buy(make):
    result = assess(build_snapshot("UP", make(list(np.linspace(100, 200, 252)))))
    labels = [s["label"] for s in result["signals"]]
    assert "Above SMA 200" in labels
    assert "Averages stacked up" in labels
    assert result["stance"] == "Buy"


def test_steady_downtrend_is_sell(make):
    result = assess(build_snapshot("DOWN", make(list(np.linspace(200, 100, 252)))))
    labels = [s["label"] for s in result["signals"]]
    assert "Below SMA 200" in labels
    assert "Deep drawdown from high" in labels
    assert result["stance"] == "Sell"


def test_rsi_extremes_and_heavy_volume(uptrend):
    snap = build_snapshot("X", uptrend)
    oversold = replace(snap, rsi_14=22.0, volume_vs_20d_avg=2.0, change_1d_pct=-1.5)
    labels = [s["label"] for s in assess(oversold)["signals"]]
    assert "Oversold (RSI 22)" in labels
    assert "Down on heavy volume" in labels


def test_missing_indicators_produce_no_signals(uptrend):
    snap = replace(
        build_snapshot("NEW", uptrend),
        sma_20=None, sma_50=None, sma_200=None, rsi_14=None, change_3m_pct=None,
        volume_vs_20d_avg=None, price=150.0, high_52w=200.0,  # -25% from high: neutral band
    )
    assert assess(snap) == {"signals": [], "score": 3, "stance": "Hold"}


def test_score_is_clamped_and_maps_to_stance():
    bull, bear = Signal("b", +1), Signal("s", -1)
    assert score([bull] * 6) == 5
    assert score([bear] * 6) == 1
    assert score([bull, bear]) == 3
    assert score([bull]) == 3  # a single signal isn't enough to leave neutral
    assert score([bull] * 2) == 4
    assert score([bear] * 3) == 2
    assert [stance(v) for v in range(1, 6)] == ["Sell", "Sell", "Hold", "Buy", "Buy"]
