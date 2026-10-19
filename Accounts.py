"""
Accounts.py

Users, and the settings and secrets that belong to each one rather than to
the whole site. Everything lives in predictions.db beside the bets it owns:

    users              username, password hash, admin flag
    user_settings      one JSON value per (user, name): "betting" (the
                       auto-betting rules, Betting.load_config()) and
                       "ai_weights" (AIWeights.for_user())
    user_kalshi_keys   each user's Kalshi API key per environment ("demo",
                       "live"), the private key encrypted (see below)
    app_secrets        generated server secrets, e.g. the session-cookie key

Shared, site-wide settings (AI models, pricing, league dates, the default AI
weights a new user starts from) stay in the JSON config files, edited by an
admin.

Accounts are created by an admin; nobody signs up on their own. The very
first admin comes from the USERNAME / PASSWORD environment variables the
single-login version used (see bootstrap()), which also moves that era's
data -- betting.json's rules, the KALSHI_* keys in the environment, and the
bets placed so far -- onto that account.

Passwords are stored as salted PBKDF2-SHA256 hashes (hashlib, no extra
package). Kalshi private keys are encrypted with Fernet (the cryptography
package the Kalshi signing already needs) under a key from SECRETS_KEY, or,
without it, a key generated into secrets.key in the data directory. Either
way the key is never stored in the database, so a copy of predictions.db
alone doesn't expose anyone's Kalshi keys.
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash        TEXT    NOT NULL,
    is_admin             INTEGER NOT NULL DEFAULT 0,
    must_change_password INTEGER NOT NULL DEFAULT 0,  -- set by an admin's reset; cleared when they pick their own
    session_version      INTEGER NOT NULL DEFAULT 1,  -- bumped to sign the user out everywhere
    created_at           TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS user_settings (
    user_id     INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    value       TEXT    NOT NULL,  -- JSON
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (user_id, name)
);

CREATE TABLE IF NOT EXISTS user_kalshi_keys (
    user_id       INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    env           TEXT    NOT NULL,  -- "demo" or "live"
    key_id        TEXT    NOT NULL,
    private_key   BLOB    NOT NULL,  -- the PEM, Fernet-encrypted (see _fernet())
    saved_at      TEXT    NOT NULL,
    PRIMARY KEY (user_id, env)
);

CREATE TABLE IF NOT EXISTS app_secrets (
    name   TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

# The tables that are about people rather than games: carried over when
# predictions.db is replaced by an upload, and left out of its download.
ACCOUNT_TABLES = ["users", "user_settings", "user_kalshi_keys", "app_secrets"]

KALSHI_ENVS = ["demo", "live"]
PBKDF2_ITERATIONS = 390_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD = 8


class AccountError(ValueError):
    """A request the accounts rules refuse; the message is shown to the user."""


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def hash_password(password: str, iterations: Optional[int] = None) -> str:
    iterations = iterations or PBKDF2_ITERATIONS
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, base64.b64decode(digest))


# Checked against when the username doesn't exist, so a wrong username takes
# as long as a wrong password and doesn't reveal which accounts exist.
_DUMMY_HASH = None


def _check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"Passwords need at least {MIN_PASSWORD} characters.")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

_USER_COLUMNS = "id, username, is_admin, must_change_password, session_version, created_at"


def _user(row) -> Optional[dict]:
    if row is None:
        return None
    uid, username, is_admin, must_change, version, created_at = row
    return {"id": uid, "username": username, "is_admin": bool(is_admin),
            "must_change_password": bool(must_change), "session_version": version, "created_at": created_at}


def get_user(conn: sqlite3.Connection, user_id: int) -> Optional[dict]:
    return _user(conn.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?", (user_id,)).fetchone())


def list_users(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(f"SELECT {_USER_COLUMNS} FROM users ORDER BY username COLLATE NOCASE").fetchall()
    return [_user(r) for r in rows]


def count_users(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(conn: sqlite3.Connection, username: str, password: str, is_admin: bool = False,
                must_change_password: bool = False, check_rules: bool = True) -> dict:
    """A new account. Raises AccountError for a bad or taken username, or a too-short password."""
    username = username.strip()
    if check_rules:
        if not USERNAME_RE.match(username):
            raise AccountError("Usernames are 3-32 letters, digits, dots, dashes or underscores.")
        _check_password_rules(password)
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, must_change_password, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (username, hash_password(password), int(is_admin), int(must_change_password), _now()))
    except sqlite3.IntegrityError:
        raise AccountError(f"There's already a user called {username!r}.") from None
    conn.commit()
    return get_user(conn, cur.lastrowid)


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> Optional[dict]:
    """The user these credentials belong to, or None."""
    global _DUMMY_HASH
    row = conn.execute("SELECT id, password_hash FROM users WHERE username = ?", (username.strip(),)).fetchone()
    if row is None:
        _DUMMY_HASH = _DUMMY_HASH or hash_password("not a real password")
        verify_password(password, _DUMMY_HASH)
        return None
    return get_user(conn, row[0]) if verify_password(password, row[1]) else None


def set_password(conn: sqlite3.Connection, user_id: int, password: str, must_change: bool = False) -> None:
    """
    A new password, which also signs the user out of every other browser
    (session_version). `must_change` is for an admin's reset: the user is
    asked to pick their own at their next sign-in.
    """
    _check_password_rules(password)
    conn.execute("UPDATE users SET password_hash = ?, must_change_password = ?, "
                 "session_version = session_version + 1 WHERE id = ?",
                 (hash_password(password), int(must_change), user_id))
    conn.commit()


def change_own_password(conn: sqlite3.Connection, user_id: int, current: str, new: str) -> None:
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or not verify_password(current, row[0]):
        raise AccountError("Your current password isn't right.")
    if current == new:
        raise AccountError("Pick a password different from the current one.")
    set_password(conn, user_id, new)


def _admin_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0]


def set_admin(conn: sqlite3.Connection, user_id: int, is_admin: bool) -> None:
    user = get_user(conn, user_id)
    if user is None:
        raise AccountError("No such user.")
    if user["is_admin"] and not is_admin and _admin_count(conn) == 1:
        raise AccountError("That's the only admin -- make someone else an admin first.")
    conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(is_admin), user_id))
    conn.commit()


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    """Remove an account with its settings and keys. Its bets stay in the history, unowned."""
    user = get_user(conn, user_id)
    if user is None:
        raise AccountError("No such user.")
    if user["is_admin"] and _admin_count(conn) == 1:
        raise AccountError("That's the only admin -- make someone else an admin first.")
    for table in ("user_settings", "user_kalshi_keys"):
        conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Per-user settings (JSON values)
# ---------------------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, user_id: int, name: str) -> Optional[dict]:
    row = conn.execute("SELECT value FROM user_settings WHERE user_id = ? AND name = ?", (user_id, name)).fetchone()
    return json.loads(row[0]) if row else None


def set_setting(conn: sqlite3.Connection, user_id: int, name: str, value: dict) -> None:
    conn.execute(
        "INSERT INTO user_settings (user_id, name, value, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (user_id, name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (user_id, name, json.dumps(value), _now()))
    conn.commit()


def delete_setting(conn: sqlite3.Connection, user_id: int, name: str) -> None:
    conn.execute("DELETE FROM user_settings WHERE user_id = ? AND name = ?", (user_id, name))
    conn.commit()


# ---------------------------------------------------------------------------
# Server secrets
# ---------------------------------------------------------------------------

def app_secret(conn: sqlite3.Connection, name: str) -> str:
    """A random secret generated on first use and kept in the database (e.g. the cookie-signing key)."""
    row = conn.execute("SELECT value FROM app_secrets WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    value = secrets.token_urlsafe(32)
    conn.execute("INSERT OR IGNORE INTO app_secrets (name, value) VALUES (?, ?)", (name, value))
    conn.commit()
    return conn.execute("SELECT value FROM app_secrets WHERE name = ?", (name,)).fetchone()[0]


def secrets_key_path() -> str:
    import DataDir
    return DataDir.data_path("secrets.key")


def _fernet():
    """
    The cipher for Kalshi private keys: from SECRETS_KEY (any string; hashed
    to a key) when set, else from secrets.key in the data directory,
    generated the first time. Losing it makes saved keys unreadable -- users
    would re-enter them -- so on a server, set SECRETS_KEY.
    """
    from cryptography.fernet import Fernet

    configured = os.environ.get("SECRETS_KEY", "").strip()
    if configured:
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(configured.encode()).digest()))
    path = secrets_key_path()
    if not os.path.exists(path):
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        with open(path, "wb") as f:
            f.write(Fernet.generate_key())
    with open(path, "rb") as f:
        return Fernet(f.read().strip())


# ---------------------------------------------------------------------------
# Kalshi keys
# ---------------------------------------------------------------------------

def save_kalshi_key(conn: sqlite3.Connection, user_id: int, env: str, key_id: str, private_key: str) -> None:
    """
    Store a user's Kalshi key for `env`, encrypted. Raises AccountError if
    the key id is blank or the private key can't be read as a PEM key.
    """
    import Kalshi

    if env not in KALSHI_ENVS:
        raise AccountError(f"Unknown Kalshi environment {env!r}.")
    key_id = key_id.strip()
    if not key_id:
        raise AccountError("Enter the key's ID (shown next to it on Kalshi's API keys page).")
    try:
        Kalshi.parse_private_key(private_key)
    except Kalshi.TradingError:
        raise AccountError("That private key couldn't be read. Paste the whole key, including its "
                           "BEGIN and END lines, or upload the .key file Kalshi gave you.") from None
    except ImportError:
        raise AccountError("The server is missing the cryptography package.") from None
    token = _fernet().encrypt(private_key.strip().encode())
    conn.execute(
        "INSERT INTO user_kalshi_keys (user_id, env, key_id, private_key, saved_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT (user_id, env) DO UPDATE SET key_id = excluded.key_id, "
        "private_key = excluded.private_key, saved_at = excluded.saved_at",
        (user_id, env, key_id, token, _now()))
    conn.commit()


def delete_kalshi_key(conn: sqlite3.Connection, user_id: int, env: str) -> None:
    conn.execute("DELETE FROM user_kalshi_keys WHERE user_id = ? AND env = ?", (user_id, env))
    conn.commit()


def kalshi_key_info(conn: sqlite3.Connection, user_id: int, env: str) -> Optional[dict]:
    """What may be shown about a saved key -- its ID and when -- never the private key."""
    row = conn.execute("SELECT key_id, saved_at FROM user_kalshi_keys WHERE user_id = ? AND env = ?",
                       (user_id, env)).fetchone()
    return {"key_id": row[0], "saved_at": row[1]} if row else None


def kalshi_credentials(conn: sqlite3.Connection, user_id: int, env: str):
    """
    The user's Kalshi.Credentials for `env`, decrypted, or None if they
    haven't saved a key. Raises Kalshi.TradingError if a saved key can't be
    decrypted (the server's secrets key changed).
    """
    import Kalshi
    from cryptography.fernet import InvalidToken

    row = conn.execute("SELECT key_id, private_key FROM user_kalshi_keys WHERE user_id = ? AND env = ?",
                       (user_id, env)).fetchone()
    if row is None:
        return None
    try:
        pem = _fernet().decrypt(bytes(row[1])).decode()
    except InvalidToken:
        raise Kalshi.TradingError("the saved Kalshi key can't be decrypted -- the server's SECRETS_KEY changed "
                                  "since it was saved; enter the key again") from None
    return Kalshi.Credentials(env=env, key_id=row[0], private_key=Kalshi.parse_private_key(pem))


# ---------------------------------------------------------------------------
# The first admin, and the single-login era's data
# ---------------------------------------------------------------------------

def first_admin_id(conn: sqlite3.Connection) -> Optional[int]:
    row = conn.execute("SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1").fetchone()
    return row[0] if row else None


def claim_unowned(conn: sqlite3.Connection) -> None:
    """
    Bets and balance snapshots with no owner (placed before accounts
    existed, or brought in by a predictions.db upload) belong to the first
    admin -- the one person who could place bets back then.
    """
    admin = first_admin_id(conn)
    if admin is None:
        return
    for table in ("bets", "balance_history"):
        conn.execute(f"UPDATE {table} SET user_id = ? WHERE user_id IS NULL", (admin,))
    conn.commit()


def bootstrap(conn: sqlite3.Connection) -> Optional[dict]:
    """
    With no users yet and USERNAME / PASSWORD in the environment, create
    the first admin from them, and give that account the single-login era's
    settings: betting.json's rules, the KALSHI_* keys from the environment
    (AIAPIKEYS.env locally) and every bet placed so far. Returns the new
    admin, or None if there was nothing to do. Safe to call any time.
    """
    if count_users(conn):
        claim_unowned(conn)
        return None
    username, password = os.environ.get("USERNAME", "").strip(), os.environ.get("PASSWORD", "")
    if not username or not password:
        return None
    try:
        admin = create_user(conn, username, password, is_admin=True, check_rules=False)
    except AccountError:
        return None  # created by a concurrent request a moment ago

    import Betting
    import ConfigFiles
    import Kalshi

    legacy_rules = ConfigFiles.load(Betting.BETTING_PATH)
    if legacy_rules:
        set_setting(conn, admin["id"], "betting",
                    {k: v for k, v in legacy_rules.items() if k in Betting.DEFAULTS})
    for env in KALSHI_ENVS:
        found = Kalshi.legacy_env_credentials(env)
        if found:
            try:
                save_kalshi_key(conn, admin["id"], env, *found)
            except Exception as e:  # a bad legacy key mustn't stop the admin being created
                print(f"could not move the {env} Kalshi key from the environment onto {username}: {e}")
    claim_unowned(conn)
    print(f"created the first admin, {username}, from USERNAME/PASSWORD")
    return admin
