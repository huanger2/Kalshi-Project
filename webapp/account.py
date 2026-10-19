"""
webapp/account.py

A signed-in user's own Settings page: their password and their Kalshi API
keys. (Their AI weights sit with their betting rules on the Betting tab.)
Everything here belongs to one user (Accounts.py); the site-wide settings
are on the Admin tab (webapp/settings.py).
"""

import sqlite3
from typing import Mapping, Optional

import Accounts
from webapp.settings import SettingsError

KALSHI_ENV_NAMES = {"demo": "Kalshi demo (play money)", "live": "Live Kalshi (real money)"}


def view(conn: sqlite3.Connection, user: dict) -> dict:
    """Everything the Settings page shows for `user`."""
    import Betting

    mode = Betting.load_config(conn, user["id"])["mode"]
    return {
        "user": user,
        "kalshi": [{"env": env, "name": KALSHI_ENV_NAMES[env], "key": Accounts.kalshi_key_info(conn, user["id"], env),
                    "in_use": mode == env} for env in Accounts.KALSHI_ENVS],
    }


def change_password(conn: sqlite3.Connection, user: dict, form: Mapping[str, str]) -> None:
    new, confirm = form.get("new_password", ""), form.get("confirm_password", "")
    if new != confirm:
        raise SettingsError(["The new passwords don't match."])
    try:
        Accounts.change_own_password(conn, user["id"], form.get("current_password", ""), new)
    except Accounts.AccountError as e:
        raise SettingsError([str(e)]) from None


def save_kalshi_key(conn: sqlite3.Connection, user_id: int, env: str, key_id: str, private_key: str) -> None:
    if env not in Accounts.KALSHI_ENVS:
        raise SettingsError([f"Unknown Kalshi environment {env!r}."])
    try:
        Accounts.save_kalshi_key(conn, user_id, env, key_id, private_key)
    except Accounts.AccountError as e:
        raise SettingsError([str(e)]) from None


def check_kalshi_key(conn: sqlite3.Connection, user_id: int, env: str) -> tuple[Optional[float], Optional[str]]:
    """(balance, None) if Kalshi accepts the saved key, else (None, why not)."""
    import Kalshi

    try:
        creds = Accounts.kalshi_credentials(conn, user_id, env)
        if creds is None:
            return None, "No key saved."
        return Kalshi.get_balance(creds), None
    except Exception as e:
        return None, str(e)
