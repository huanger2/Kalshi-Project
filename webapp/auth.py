"""
webapp/auth.py

Who's viewing: a signed-in user (an admin or not), a guest, or nobody yet
(who gets sent to the login page).

    admin  everything a user can do, plus the Admin tab: shared settings
           (AI models, pricing, league dates, default AI weights), users,
           data, and the league tracking switches
    user   their own Betting tab and Settings (password, AI weights,
           Kalshi keys) on top of browsing
    guest  browsing only, with the default AI weights

Accounts live in predictions.db (see Accounts.py); an admin creates them.

The choice is remembered in one cookie:

    "guest"                                  -- continued as guest; nothing to forge
    "u.<id>.<version>.<issued>.<sig>"        -- signed in as user <id>

<sig> is an HMAC of the rest, keyed by SESSION_SECRET if set, else a secret
generated into the database on first use (Accounts.app_secret()). <version>
must match the user's session_version, which a password change bumps, so
that signs them out everywhere. Signed with the standard library's hmac
rather than Starlette's SessionMiddleware, which would need the extra
itsdangerous package for one cookie.
"""

import hashlib
import hmac
import os
import sqlite3
import time
from typing import Optional

COOKIE = "kalshi_session"
MAX_AGE = 30 * 24 * 3600  # a sign-in lasts 30 days

ADMIN = "admin"
USER = "user"
GUEST = "guest"


def _secret(conn: sqlite3.Connection) -> bytes:
    import Accounts

    explicit = os.environ.get("SESSION_SECRET")
    return (explicit or Accounts.app_secret(conn, "session")).encode()


def _sign(payload: str, secret: bytes) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def user_cookie(conn: sqlite3.Connection, user: dict, now: Optional[float] = None) -> str:
    """The cookie value for a fresh sign-in as `user`."""
    payload = f"u.{user['id']}.{user['session_version']}.{int(now if now is not None else time.time())}"
    return f"{payload}.{_sign(payload, _secret(conn))}"


def is_guest(value: Optional[str]) -> bool:
    return value == GUEST


def signed_in_user(conn: sqlite3.Connection, value: Optional[str], now: Optional[float] = None) -> Optional[dict]:
    """
    The user a cookie value signs in, or None: forged, expired, for a
    deleted user, or from before their password last changed.
    """
    import Accounts

    if not value or not value.startswith("u."):
        return None
    try:
        _, uid, version, issued, sig = value.split(".")
        user_id, issued_at = int(uid), int(issued)
    except ValueError:
        return None
    if not hmac.compare_digest(sig, _sign(f"u.{uid}.{version}.{issued}", _secret(conn))):
        return None
    if (now if now is not None else time.time()) - issued_at > MAX_AGE:
        return None
    user = Accounts.get_user(conn, user_id)
    if user is None or str(user["session_version"]) != version:
        return None
    return user


def role_of(user: Optional[dict], value: Optional[str]) -> Optional[str]:
    """ADMIN, USER, GUEST, or None (no choice made yet)."""
    if user is not None:
        return ADMIN if user["is_admin"] else USER
    return GUEST if is_guest(value) else None


# Failed sign-ins per (username, client address), kept in memory: after
# MAX_FAILURES within FAILURE_WINDOW seconds, that pair is refused until the
# window passes. A restart forgets them, which is fine for slowing guessing.
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60
_failures: dict[tuple[str, str], list[float]] = {}


def _recent(key: tuple[str, str], now: float) -> list[float]:
    times = [t for t in _failures.get(key, []) if now - t < FAILURE_WINDOW]
    if times:
        _failures[key] = times
    else:
        _failures.pop(key, None)
    return times


def throttled(username: str, client: str, now: Optional[float] = None) -> bool:
    return len(_recent((username.strip().lower(), client), now or time.time())) >= MAX_FAILURES


def record_failure(username: str, client: str, now: Optional[float] = None) -> None:
    now = now or time.time()
    key = (username.strip().lower(), client)
    _failures[key] = _recent(key, now) + [now]


def clear_failures(username: str, client: str) -> None:
    _failures.pop((username.strip().lower(), client), None)


def safe_next(target: Optional[str]) -> str:
    """Where to go after signing in: a same-site path only, never another site (//evil.com)."""
    if target and target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return "/"
