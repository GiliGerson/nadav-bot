import base64
import hashlib
import re
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import security
from app.security import (
    CSRF_HEADER,
    inline_script_hashes,
    make_dashboard_auth,
    require_csrf_header,
    security_headers,
)

DASHBOARD = Path(__file__).parents[1] / "app" / "static" / "dashboard.html"


@pytest.fixture(autouse=True)
def no_login_delay(monkeypatch):
    monkeypatch.setattr(security, "FAILED_LOGIN_DELAY_SECONDS", 0)


def make_client(password: str) -> TestClient:
    app = FastAPI()
    auth = make_dashboard_auth("nadav", password)

    @app.get("/data", dependencies=[Depends(auth)])
    async def data() -> dict:
        return {"secret": "watchlist"}

    @app.post("/change", dependencies=[Depends(auth), Depends(require_csrf_header)])
    async def change() -> dict:
        return {"ok": True}

    return TestClient(app)


# --- Dashboard password -------------------------------------------------------

def test_no_credentials_is_rejected_with_basic_challenge():
    res = make_client("correct-horse-battery").get("/data")
    assert res.status_code == 401
    assert res.headers["WWW-Authenticate"].startswith("Basic")
    assert "watchlist" not in res.text


@pytest.mark.parametrize("user, password", [
    ("nadav", "wrong-password"),
    ("admin", "correct-horse-battery"),  # right password, wrong user
    ("", ""),
])
def test_wrong_credentials_are_rejected(user, password):
    assert make_client("correct-horse-battery").get("/data", auth=(user, password)).status_code == 401


def test_correct_credentials_are_accepted():
    res = make_client("correct-horse-battery").get("/data", auth=("nadav", "correct-horse-battery"))
    assert res.json() == {"secret": "watchlist"}


def test_no_password_configured_means_open_for_local_dev():
    assert make_client("").get("/data").status_code == 200


# --- CSRF ---------------------------------------------------------------------

def test_state_change_without_dashboard_header_is_rejected():
    client = make_client("correct-horse-battery")
    res = client.post("/change", auth=("nadav", "correct-horse-battery"))
    assert res.status_code == 403


def test_state_change_with_dashboard_header_is_accepted():
    client = make_client("correct-horse-battery")
    res = client.post("/change", auth=("nadav", "correct-horse-battery"), headers={CSRF_HEADER: "1"})
    assert res.status_code == 200


def test_dashboard_sends_the_csrf_header_on_every_api_call():
    html = DASHBOARD.read_text()
    calls = re.findall(r"await fetch\(", html)
    assert len(calls) == 3  # load, add, remove
    assert html.count(f'"{CSRF_HEADER}": "1"') == len(calls)


# --- Security headers ---------------------------------------------------------

def test_csp_allows_exactly_the_dashboard_script():
    html = DASHBOARD.read_text()
    body = re.search(r"<script>(.*?)</script>", html, flags=re.DOTALL).group(1)
    expected = "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"
    assert inline_script_hashes(html) == [expected]

    csp = security_headers([expected], https=False)["Content-Security-Policy"]
    assert f"script-src {expected}" in csp
    assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]
    assert "frame-ancestors 'none'" in csp


def test_hsts_only_over_https():
    assert "Strict-Transport-Security" in security_headers([], https=True)
    assert "Strict-Transport-Security" not in security_headers([], https=False)


def test_no_scripts_means_scripts_blocked():
    assert "script-src 'none'" in security_headers([], https=False)["Content-Security-Policy"]
