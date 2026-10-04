"""The real FastAPI app, loaded in each deployment mode (no lifespan, so no Telegram calls)."""
import importlib

import pytest
from fastapi.testclient import TestClient

from app import config

PASSWORD = "Zq3x_8vJ-2mK9pL4rT7wY1cN6bH0dF5gA_sE"
SECRET = "Wk2n_7Hq-4pR8tV1xZ5cB9mL3dF6gJ0sA_yE"


@pytest.fixture
def load_app(monkeypatch):
    def load(**env):
        base = {
            "TELEGRAM_BOT_TOKEN": "123:test",
            "ANTHROPIC_API_KEY": "test",
            "WEBHOOK_BASE_URL": "",
            "PUBLIC_DEPLOYMENT": "false",
            "DASHBOARD_PASSWORD": "",
        }
        for key, value in {**base, **env}.items():
            monkeypatch.setenv(key, value)
        config.get_settings.cache_clear()
        import app.main
        return importlib.reload(app.main).app

    yield load
    config.get_settings.cache_clear()


def webhook_status(client: TestClient) -> int:
    headers = {"X-Telegram-Bot-Api-Secret-Token": "change-me"}
    return client.post("/telegram/webhook", json={}, headers=headers).status_code


def test_polling_mode_has_no_webhook_route(load_app):
    client = TestClient(load_app(), base_url="http://localhost")
    assert webhook_status(client) == 404


def test_webhook_mode_mounts_webhook_and_hides_docs(load_app):
    app = load_app(WEBHOOK_BASE_URL="https://nadav.example.com", WEBHOOK_SECRET=SECRET,
                   DASHBOARD_PASSWORD=PASSWORD)
    client = TestClient(app)
    assert webhook_status(client) == 403  # mounted, and the placeholder secret is refused
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_public_deployment_requires_dashboard_password(load_app):
    with pytest.raises(ValueError, match="DASHBOARD_PASSWORD"):
        load_app(PUBLIC_DEPLOYMENT="true")


def test_public_polling_does_not_need_webhook_secret(load_app):
    client = TestClient(load_app(PUBLIC_DEPLOYMENT="true", DASHBOARD_PASSWORD=PASSWORD))
    assert webhook_status(client) == 404


def test_spoofed_localhost_host_header_does_not_bypass_auth_when_public(load_app):
    client = TestClient(load_app(PUBLIC_DEPLOYMENT="true", DASHBOARD_PASSWORD=PASSWORD))
    headers = {"Host": "localhost", "X-Nadav-Dashboard": "1"}
    assert client.get("/api/snapshot/AAPL", headers=headers).status_code == 401
    assert client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers).status_code == 401
    assert client.delete("/api/watchlist/AAPL", headers=headers).status_code == 401


def test_dashboard_page_is_public_but_edits_need_the_password(load_app):
    client = TestClient(load_app(PUBLIC_DEPLOYMENT="true", DASHBOARD_PASSWORD=PASSWORD))
    assert client.get("/").status_code == 200
    headers = {"X-Nadav-Dashboard": "1"}
    assert client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers).status_code == 401
    assert client.delete("/api/watchlist/AAPL", headers=headers).status_code == 401


def test_local_mode_rejects_other_hosts(load_app):
    client = TestClient(load_app(), base_url="http://localhost")
    assert client.get("/health").status_code == 200
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400


def test_oversized_api_body_is_rejected_before_auth(load_app):
    client = TestClient(load_app(PUBLIC_DEPLOYMENT="true", DASHBOARD_PASSWORD=PASSWORD))
    res = client.post("/api/watchlist", content=b'{"ticker": "' + b"A" * 10_000 + b'"}',
                      headers={"Content-Type": "application/json"})
    assert res.status_code == 413


def test_chunked_api_body_is_rejected(load_app):
    client = TestClient(load_app(PUBLIC_DEPLOYMENT="true", DASHBOARD_PASSWORD=PASSWORD))
    res = client.post("/api/watchlist", content=iter([b'{"ticker": "AAPL"}']),
                      headers={"Content-Type": "application/json"})
    assert res.status_code == 411


def test_small_api_body_reaches_auth(load_app):
    client = TestClient(load_app(PUBLIC_DEPLOYMENT="true", DASHBOARD_PASSWORD=PASSWORD))
    assert client.post("/api/watchlist", json={"ticker": "AAPL"}).status_code == 401


def test_api_reads_need_the_dashboard_header(load_app):
    # Blocks <img src="http://localhost:8000/api/..."> from making the owner's browser hit Yahoo.
    client = TestClient(load_app(), base_url="http://localhost")
    assert client.get("/api/snapshot/AAPL").status_code == 403
    assert client.get("/api/watchlist").status_code == 403


# --- Local polling must not take the bot away from a deployment ---------------

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

from app.bot import ensure_no_live_webhook  # noqa: E402


def fake_application(webhook_url: str, force: bool = False):
    bot = SimpleNamespace(get_webhook_info=AsyncMock(return_value=SimpleNamespace(url=webhook_url)))
    return SimpleNamespace(bot=bot, bot_data={"settings": SimpleNamespace(force_polling=force)})


async def test_polling_refuses_while_a_webhook_is_live():
    with pytest.raises(RuntimeError, match="deployed"):
        await ensure_no_live_webhook(fake_application("https://nadav-bot.onrender.com/telegram/webhook"))


async def test_polling_allowed_without_webhook():
    await ensure_no_live_webhook(fake_application(""))


async def test_force_polling_skips_the_check():
    app = fake_application("https://nadav-bot.onrender.com/telegram/webhook", force=True)
    await ensure_no_live_webhook(app)
    app.bot.get_webhook_info.assert_not_called()
