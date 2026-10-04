import pytest

from app.config import Settings

STRONG = "Zq3x_8vJ-2mK9pL4rT7wY1cN6bH0dF5gA_sE"  # 36 chars


def make(**overrides) -> Settings:
    return Settings(_env_file=None, telegram_bot_token="t", anthropic_api_key="k", **overrides)


@pytest.mark.parametrize("secret", [
    "change-me",                                    # the code default
    "a-long-random-string_only-letters-digits-_-",  # the public .env.example value
    "short-secret",
    "",
])
def test_webhook_mode_rejects_guessable_secrets(secret):
    with pytest.raises(ValueError, match="WEBHOOK_SECRET"):
        make(webhook_base_url="https://nadav.example.com", webhook_secret=secret, dashboard_password=STRONG)


def test_webhook_mode_accepts_random_secrets():
    settings = make(
        webhook_base_url="https://nadav.example.com", webhook_secret=STRONG, dashboard_password=STRONG
    )
    assert settings.production


@pytest.mark.parametrize("password", ["", "short-password"])
def test_webhook_mode_requires_a_dashboard_password(password):
    with pytest.raises(ValueError, match="DASHBOARD_PASSWORD"):
        make(webhook_base_url="https://nadav.example.com", webhook_secret=STRONG, dashboard_password=password)


@pytest.mark.parametrize("url", [None, ""])
def test_polling_mode_does_not_need_a_secret(url):
    assert make(webhook_base_url=url).webhook_secret == "change-me"


def test_public_deployment_requires_password_but_not_webhook_secret():
    with pytest.raises(ValueError, match="DASHBOARD_PASSWORD"):
        make(public_deployment=True)
    settings = make(public_deployment=True, dashboard_password=STRONG)
    assert settings.production and not settings.webhook_mode
