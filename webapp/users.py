"""
webapp/users.py

The Admin tab's Users page: who has an account, and adding, resetting,
promoting and removing them. Nobody signs up on their own -- an admin
creates the account with a temporary password, which the new user is asked
to replace at their first sign-in.
"""

import secrets
import sqlite3
from typing import Optional

import Accounts
import Betting


def view(conn: sqlite3.Connection) -> list[dict]:
    """Every user, with what they're betting in and whether they've saved Kalshi keys."""
    rows = []
    for user in Accounts.list_users(conn):
        rows.append({
            **user,
            "mode": Betting.load_config(conn, user["id"])["mode"],
            "keys": [env for env in Accounts.KALSHI_ENVS if Accounts.kalshi_key_info(conn, user["id"], env)],
            "bets": conn.execute("SELECT COUNT(*) FROM bets WHERE user_id = ?", (user["id"],)).fetchone()[0],
        })
    return rows


def temporary_password() -> str:
    """A random password for a new or reset account, shown to the admin once."""
    return secrets.token_urlsafe(9)


def create(conn: sqlite3.Connection, username: str, is_admin: bool) -> tuple[dict, str]:
    """A new account with a temporary password. Returns (user, that password)."""
    password = temporary_password()
    user = Accounts.create_user(conn, username, password, is_admin=is_admin, must_change_password=True)
    return user, password


def reset_password(conn: sqlite3.Connection, user_id: int) -> Optional[str]:
    """A fresh temporary password for a user who's locked out (signs them out everywhere)."""
    if Accounts.get_user(conn, user_id) is None:
        return None
    password = temporary_password()
    Accounts.set_password(conn, user_id, password, must_change=True)
    return password
