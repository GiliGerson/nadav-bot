"""Web hardening for the dashboard and JSON API: auth, CSRF guard and security headers.

Threat model: once deployed, the server has a public URL. Without these, anyone
who finds it could read the watchlist (which reveals what the owner tracks),
edit it, or use the API to hammer Yahoo Finance from our IP.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import re

from fastapi import HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

logger = logging.getLogger(__name__)

# Sent by the dashboard's own fetch() calls. Browsers can't add a custom header to a
# cross-site form post, and a cross-site fetch() with one needs a CORS preflight that
# we never approve, so requiring it blocks CSRF against the logged-in owner.
CSRF_HEADER = "X-Nadav-Dashboard"
FAILED_LOGIN_DELAY_SECONDS = 1.0

_basic = HTTPBasic(auto_error=False, realm="Nadav dashboard")


def credentials_match(credentials: HTTPBasicCredentials | None, username: str, password: str) -> bool:
    if credentials is None:
        return False
    # Compare both fields, always, in constant time, so timing leaks neither which was wrong.
    user_ok = hmac.compare_digest(credentials.username.encode(), username.encode())
    pass_ok = hmac.compare_digest(credentials.password.encode(), password.encode())
    return user_ok and pass_ok


def make_dashboard_auth(username: str, password: str):
    """FastAPI dependency enforcing HTTP Basic auth. No password configured = open (local dev)."""

    async def require_auth(request: Request) -> None:
        if not password:
            return
        credentials = await _basic(request)
        if credentials_match(credentials, username, password):
            return
        if credentials is not None:
            client = request.client.host if request.client else "unknown"
            logger.warning("Failed dashboard login from %s", client)
            # A fixed delay slows online guessing without a lockout an attacker could abuse
            # to lock the owner out. The 16+ char password requirement does the real work.
            await asyncio.sleep(FAILED_LOGIN_DELAY_SECONDS)
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="Nadav dashboard"'},
        )

    return require_auth


async def require_csrf_header(request: Request) -> None:
    if request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(status_code=403, detail="Missing dashboard request header")


def inline_script_hashes(html: str) -> list[str]:
    """CSP hashes for each inline <script>, so the page runs without 'unsafe-inline' scripts."""
    return [
        "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"
        for body in re.findall(r"<script>(.*?)</script>", html, flags=re.DOTALL)
    ]


def security_headers(script_hashes: list[str], https: bool) -> dict[str, str]:
    script_src = " ".join(script_hashes) or "'none'"
    csp = "; ".join([
        "default-src 'none'",
        f"script-src {script_src}",
        # Inline style attributes are low risk (no script execution) and the page uses a few.
        "style-src 'self' 'unsafe-inline'",
        "connect-src 'self'",
        "img-src 'self' data:",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])
    headers = {
        "Content-Security-Policy": csp,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
    }
    if https:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return headers
