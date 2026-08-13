"""
Shared-password gate for the web UI and API.

Threat model
------------
This is a single-purpose internal tool that accepts confidential documents.
The goal is simply to stop anyone who finds the URL from uploading to it or
reading the page -- not to support multiple users, roles, or accounts. So a
single shared password is the right weight of solution.

What it does do carefully:
  - reads the password from the environment, never from source;
  - compares with `secrets.compare_digest`, so a wrong guess takes the same
    time as a right one (no character-by-character timing leak);
  - issues an opaque random session token rather than echoing the password
    back to the browser, so the password itself is sent exactly once;
  - rate-limits attempts per client IP, making online guessing impractical.

Tokens live in memory, so a restart logs everyone out -- acceptable (and
arguably desirable) for a tool that holds no other state. Anything
multi-user would want real accounts and a signed, expiring token instead.
"""
from __future__ import annotations

import os
import secrets
import time
from collections import defaultdict
from typing import Dict, List

from fastapi import Header, HTTPException, Request

#: Environment variable holding the shared password.
PASSWORD_ENV_VAR = "PII_APP_PASSWORD"

#: How long a successful login stays valid.
SESSION_TTL_SECONDS = 12 * 60 * 60

#: Failed logins allowed per IP inside the window before lockout.
MAX_ATTEMPTS = 8
ATTEMPT_WINDOW_SECONDS = 5 * 60

# token -> expiry timestamp
_sessions: Dict[str, float] = {}
# client ip -> timestamps of recent failed attempts
_failed_attempts: Dict[str, List[float]] = defaultdict(list)


def auth_configured() -> bool:
    """False when no password is set, which disables the gate entirely."""
    return bool(os.environ.get(PASSWORD_ENV_VAR, "").strip())


def _purge_expired(now: float) -> None:
    for token, expiry in list(_sessions.items()):
        if expiry <= now:
            _sessions.pop(token, None)


def _recent_failures(client_ip: str, now: float) -> int:
    attempts = [t for t in _failed_attempts[client_ip] if now - t < ATTEMPT_WINDOW_SECONDS]
    _failed_attempts[client_ip] = attempts
    return len(attempts)


def login(password: str, client_ip: str) -> str:
    """Validates the password and returns a fresh session token."""
    now = time.time()
    if _recent_failures(client_ip, now) >= MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail="Too many failed attempts. Try again in a few minutes.",
        )

    expected = os.environ.get(PASSWORD_ENV_VAR, "")
    # compare_digest needs equal-length byte strings to be constant time;
    # encoding both sides keeps non-ASCII passwords working.
    if not expected or not secrets.compare_digest(password.encode(), expected.encode()):
        _failed_attempts[client_ip].append(now)
        raise HTTPException(status_code=401, detail="Incorrect password.")

    _failed_attempts.pop(client_ip, None)
    _purge_expired(now)
    token = secrets.token_urlsafe(32)
    _sessions[token] = now + SESSION_TTL_SECONDS
    return token


def is_valid_token(token: str) -> bool:
    if not token:
        return False
    now = time.time()
    expiry = _sessions.get(token)
    if expiry is None:
        return False
    if expiry <= now:
        _sessions.pop(token, None)
        return False
    return True


def client_ip(request: Request) -> str:
    """
    Best-effort client identity for rate limiting. Behind a platform proxy
    (Render, Railway) the socket address is the proxy's, so the first hop in
    X-Forwarded-For is used when present.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def require_auth(authorization: str = Header(default="")) -> None:
    """
    FastAPI dependency guarding protected routes.

    No-ops when no password is configured, so local development and the CLI
    path stay frictionless; set PII_APP_PASSWORD to turn the gate on.
    """
    if not auth_configured():
        return
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not is_valid_token(token):
        raise HTTPException(status_code=401, detail="Authentication required.")
