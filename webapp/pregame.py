"""
webapp/pregame.py

Places bets shortly before each game starts, for every user whose "When to
bet" rule (bet_lead_minutes) says so, instead of at the morning's daily run.

A background thread in the web service wakes every PREGAME_CHECK_MINUTES
(default 5; 0 turns it off) and, for each such user, runs
Betting.rerun_today() over today's logged games: games starting within
their lead time are priced fresh from Kalshi and bet under their rules,
exactly as "Save and apply to today's games" would; games further off wait
for a later round, and games already bet are left alone, so each game is
bet at most once. A round is skipped while a daily run is going (it's
logging today's games and writing to the database).

Why: Kalshi's price is a little sharper and cheaper close to tip-off, once
the day's injury news is in, and tip-offs run from noon to late evening,
so no single daily run time suits every game (see README, "When to bet").
"""

import datetime
import os
import threading
import time
from typing import Optional

DEFAULT_MINUTES = 5

_status = {"last_run": None, "placed": None, "error": None}
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def interval_minutes() -> float:
    try:
        return max(0.0, float(os.environ.get("PREGAME_CHECK_MINUTES", DEFAULT_MINUTES)))
    except ValueError:
        return float(DEFAULT_MINUTES)


def bet_once(conn, now: Optional[datetime.datetime] = None, fetch_market=None, tip_for=None,
             today: Optional[datetime.date] = None) -> dict:
    """
    One round: every user betting before tip-off gets Betting.rerun_today()
    on today's games. Each Kalshi market is fetched at most once per round,
    however many users want it. Returns {"users", "placed"}.
    """
    import Accounts
    import Betting
    import Kalshi

    fetch_market = fetch_market or Kalshi.get_market
    fetched: dict[str, dict] = {}

    def fetch(ticker):
        if ticker not in fetched:
            fetched[ticker] = fetch_market(ticker)
        return fetched[ticker]

    users = placed = 0
    for user in Accounts.list_users(conn):
        config = Betting.load_config(conn, user["id"])
        if config["mode"] == "off" or not config.get("bet_lead_minutes"):
            continue
        users += 1
        try:
            result = Betting.rerun_today(conn, user, fetch_market=fetch, now=now, today=today,
                                         **({"tip_for": tip_for} if tip_for else {}))
        except Exception as e:  # one user's problem mustn't stop the others' bets
            print(f"pre-game betting failed for {user['username']}: {e}", flush=True)
            continue
        placed += result["placed"]
        if result["placed"]:
            print(f"pre-game: placed {result['placed']} bet(s) for {user['username']}", flush=True)
            _record_balance(conn, user, config)
    return {"users": users, "placed": placed}


def _record_balance(conn, user: dict, config: dict) -> None:
    """A demo/live account's balance after its bets, for the Betting tab's chart (as the daily run does)."""
    import Accounts
    import Betting
    import Kalshi

    if config["mode"] not in Betting.TRADING_MODES:
        return
    try:
        creds = Accounts.kalshi_credentials(conn, user["id"], config["mode"])
        if creds is not None:
            Betting.record_balance(conn, user["id"], config["mode"], Kalshi.get_balance(creds), force=True)
    except Exception as e:
        print(f"could not record {user['username']}'s Kalshi {config['mode']} balance: {e}", flush=True)


def _round() -> None:
    import Tracker
    from webapp import daily_run

    if daily_run.status().get("state") == "running":
        return  # today's games are still being logged
    conn = Tracker.connect()
    try:
        result = bet_once(conn)
        _status.update(last_run=time.time(), placed=result["placed"], error=None)
    except Exception as e:  # never let a bad round stop the thread
        _status.update(last_run=time.time(), error=str(e))
        print(f"pre-game betting round failed: {e}", flush=True)
    finally:
        conn.close()


def _loop(minutes: float) -> None:
    # A short wait first, so a redeploy's startup isn't slowed; then every `minutes`.
    if _stop.wait(30):
        return
    while not _stop.is_set():
        _round()
        if _stop.wait(minutes * 60):
            return


def start() -> bool:
    """Start the background thread (once per process). False if PREGAME_CHECK_MINUTES turns it off."""
    global _thread
    minutes = interval_minutes()
    if minutes <= 0 or (_thread is not None and _thread.is_alive()):
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(minutes,), name="pregame-betting", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    _stop.set()
