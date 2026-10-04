"""Rule-based technical signals, a 1-5 score, and a Buy / Hold / Sell stance.

Like the indicators in `market.py`, every signal is a deterministic rule over a
`Snapshot`, so each stance can be traced back to the exact conditions behind it.
This is a mechanical technical read, not investment advice.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from app.market import Snapshot


@dataclass(frozen=True)
class Signal:
    label: str
    bias: int  # +1 bullish, -1 bearish


def signals(s: Snapshot) -> list[Signal]:
    out: list[Signal] = []

    if s.sma_200 is not None:
        if s.price > s.sma_200:
            out.append(Signal("Above SMA 200", +1))
        elif s.price < s.sma_200:
            out.append(Signal("Below SMA 200", -1))

    if s.sma_20 is not None and s.sma_50 is not None and s.sma_200 is not None:
        if s.sma_20 > s.sma_50 > s.sma_200:
            out.append(Signal("Averages stacked up", +1))
        elif s.sma_20 < s.sma_50 < s.sma_200:
            out.append(Signal("Averages stacked down", -1))

    if s.rsi_14 is not None:
        if s.rsi_14 < 30:
            out.append(Signal(f"Oversold (RSI {s.rsi_14:.0f})", +1))
        elif s.rsi_14 > 70:
            out.append(Signal(f"Overbought (RSI {s.rsi_14:.0f})", -1))

    if s.change_3m_pct is not None:
        if s.change_3m_pct >= 10:
            out.append(Signal("Strong 3M momentum", +1))
        elif s.change_3m_pct <= -10:
            out.append(Signal("Weak 3M momentum", -1))

    if s.from_52w_high_pct >= -3:
        out.append(Signal("Near 52-week high", +1))
    elif s.from_52w_high_pct <= -30:
        out.append(Signal("Deep drawdown from high", -1))

    if s.volume_vs_20d_avg is not None and s.volume_vs_20d_avg >= 1.5:
        if s.change_1d_pct >= 0:
            out.append(Signal("Up on heavy volume", +1))
        else:
            out.append(Signal("Down on heavy volume", -1))

    return out


def score(sigs: list[Signal]) -> int:
    """1 (most bearish) to 5 (most bullish); every 2 net signals move one step from neutral 3.

    The trend signals overlap (SMA 200, stacked averages, 52-week high), so one point
    per signal saturated almost every uptrend at 5. Halving keeps the scale graded.
    """
    net = sum(sig.bias for sig in sigs)
    steps = min(2, abs(net) // 2)
    return 3 + steps if net > 0 else 3 - steps


def stance(value: int) -> str:
    if value >= 4:
        return "Buy"
    if value <= 2:
        return "Sell"
    return "Hold"


def assess(s: Snapshot) -> dict:
    sigs = signals(s)
    value = score(sigs)
    return {"signals": [asdict(sig) for sig in sigs], "score": value, "stance": stance(value)}
