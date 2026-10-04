import json
from types import SimpleNamespace

import pytest

from app.analysis import Analyst
from app.market import build_snapshot


class FakeMessages:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="  analysis text  ")])


@pytest.fixture
def analyst():
    a = Analyst(api_key="test", model="test-model", language="he")
    a.client = SimpleNamespace(messages=FakeMessages())
    return a


async def test_analyze_grounds_prompt_in_snapshot(analyst, uptrend):
    snap = build_snapshot("TEST", uptrend, name="Test Co")
    result = await analyst.analyze(snap)

    assert result == "analysis text"
    call = analyst.client.messages.calls[0]
    assert call["model"] == "test-model"
    assert "Hebrew" in call["system"]
    assert "Never invent news" in call["system"]
    payload = call["messages"][0]["content"].split("Data:\n", 1)[1]
    assert json.loads(payload)[0]["ticker"] == "TEST"


async def test_compare_includes_every_ticker(analyst, uptrend):
    snaps = [build_snapshot(t, uptrend) for t in ("AAA", "BBB", "CCC")]
    await analyst.compare(snaps)
    content = analyst.client.messages.calls[0]["messages"][0]["content"]
    assert all(t in content for t in ("AAA", "BBB", "CCC"))
