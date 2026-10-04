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
    for path in ["/", "/api/watchlist", "/api/snapshot/AAPL"]:
        assert client.get(path, headers={"Host": "localhost"}).status_code == 401


def test_local_mode_rejects_other_hosts(load_app):
    client = TestClient(load_app(), base_url="http://localhost")
    assert client.get("/health").status_code == 200
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400
