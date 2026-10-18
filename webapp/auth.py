"""
webapp/auth.py

Who's viewing: a signed-in admin, a guest, or nobody yet (who gets sent to
the login page). Only an admin may see or use the league tracking switch.

There's a single admin login for now, checked against the USERNAME and
PASSWORD environment variables. With either one unset, nobody can sign in
(guests can still browse) -- failing closed, never open.

The choice is remembered in one cookie:

    "guest"                   -- continued as guest; nothing to forge
    "admin.<issued>.<sig>"    -- signed in; <sig> is an HMAC of "admin.<issued>"

The HMAC key comes from SESSION_SECRET if set, else is derived from
PASSWORD, so changing the password signs everyone out. Signed with the
standard library's hmac rather than Starlette's SessionMiddleware, which
would need the extra itsdangerous package for one cookie.
"""

import hashlib
import hmac
import os
import time
from typing import Optional

from fastapi import Request

COOKIE = "kalshi_session"
MAX_AGE = 30 * 24 * 3600  # an admin sign-in lasts 30 days

ADMIN = "admin"
GUEST = "guest"


def _secret() -> Optional[bytes]:
    explicit = os.environ.get("SESSION_SECRET")
    if explicit:
        return explicit.encode()
    password = os.environ.get("PASSWORD")
    if not password:
        return None
    return hashlib.sha256(b"kalshi-session:" + password.encode()).digest()


def _sign(payload: str, secret: bytes) -> str:
    return hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()


def is_configured() -> bool:
    """Whether an admin login exists at all (USERNAME and PASSWORD both set)."""
    return bool(os.environ.get("USERNAME") and os.environ.get("PASSWORD"))


def credentials_match(username: str, password: str) -> bool:
    """Constant-time comparison against USERNAME/PASSWORD; always False if either is unset."""
    if not is_configured():
        return False
    user_ok = hmac.compare_digest(username.encode(), os.environ["USERNAME"].encode())
    pass_ok = hmac.compare_digest(password.encode(), os.environ["PASSWORD"].encode())
    return user_ok and pass_ok


def admin_cookie(now: Optional[float] = None) -> str:
    """The cookie value for a fresh admin sign-in. Only call after credentials_match()."""
    secret = _secret()
    if secret is None:
        raise RuntimeError("No PASSWORD/SESSION_SECRET to sign the session with")
    payload = f"{ADMIN}.{int(now if now is not None else time.time())}"
    return f"{payload}.{_sign(payload, secret)}"


def role(request: Request, now: Optional[float] = None) -> Optional[str]:
    """ADMIN, GUEST, or None (no choice made yet, or a forged/expired admin cookie)."""
    value = request.cookies.get(COOKIE)
    if value == GUEST:
        return GUEST
    if not value or not value.startswith(ADMIN + "."):
        return None
    try:
        _, issued, sig = value.split(".")
        issued_at = int(issued)
    except ValueError:
        return None
    secret = _secret()
    if secret is None or not hmac.compare_digest(sig, _sign(f"{ADMIN}.{issued}", secret)):
        return None
    if (now if now is not None else time.time()) - issued_at > MAX_AGE:
        return None
    return ADMIN


def safe_next(target: Optional[str]) -> str:
    """Where to go after signing in: a same-site path only, never another site (//evil.com)."""
    if target and target.startswith("/") and not target.startswith(("//", "/\\")):
        return target
    return "/"
