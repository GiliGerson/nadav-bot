"""LLM analysis layer.

Claude receives *only* the structured snapshot JSON computed in `market.py`.
Grounding the model in numbers we calculated ourselves keeps the analysis
factual and stops it from inventing news, earnings or price targets.
"""
from __future__ import annotations

import json

from anthropic import AsyncAnthropic

from app.market import Snapshot

LANGUAGES = {"en": "English", "he": "Hebrew"}

SYSTEM_PROMPT = """You are Nadav, a sharp and concise market analyst living inside a Telegram bot.

Rules:
- Base every statement ONLY on the JSON market data provided. Never invent news, earnings, \
events, or price targets.
- A null field means the data is unavailable. Do not guess it.
- Output plain text for Telegram: no Markdown, no asterisks, no # headings. Use short lines. \
Emojis are fine as section markers.
- Never tell the user to buy, sell, or hold. Describe the technical setup; do not prescribe trades.
- Stay under {max_words} words.
- Write in {language}. Keep tickers and numbers in Latin characters."""

ANALYZE_PROMPT = """Analyze this instrument using this structure:
📈 Trend: price vs. SMA 20/50/200 and the 1M/3M moves
⚡ Momentum: RSI(14) and volume vs. its 20-day average
🌊 Risk: 30-day volatility and distance from the 52-week high/low
🧭 Bottom line: one sentence on what the data shows and one level or signal worth watching

Data:
{data}"""

COMPARE_PROMPT = """Compare these instruments side by side on trend, momentum, and risk.
Finish with one line on which shows the stronger technical setup right now and why \
(an observation, not a recommendation).

Data:
{data}"""

MARKET_PROMPT = """Give a brief market pulse from these index snapshots:
overall tone, any notable divergence between indices, and what the VIX level implies \
about sentiment (if present).

Data:
{data}"""


class Analyst:
    def __init__(self, api_key: str, model: str, language: str = "en") -> None:
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = model
        self.language = LANGUAGES.get(language, "English")

    async def analyze(self, snapshot: Snapshot) -> str:
        return await self._complete(ANALYZE_PROMPT.format(data=_dump([snapshot])), 180)

    async def compare(self, snapshots: list[Snapshot]) -> str:
        return await self._complete(COMPARE_PROMPT.format(data=_dump(snapshots)), 220)

    async def market_pulse(self, snapshots: list[Snapshot]) -> str:
        return await self._complete(MARKET_PROMPT.format(data=_dump(snapshots)), 150)

    async def _complete(self, prompt: str, max_words: int) -> str:
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=800,
            system=SYSTEM_PROMPT.format(max_words=max_words, language=self.language),
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in response.content if b.type == "text").strip()


def _dump(snapshots: list[Snapshot]) -> str:
    return json.dumps([s.to_dict() for s in snapshots], ensure_ascii=False, indent=2)
