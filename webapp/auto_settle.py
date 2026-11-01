"""
webapp/auto_settle.py

Records game results as they come in, so bets resolve on their own instead
of waiting for the next daily run or a "Check bets" click.

A background thread in the web service wakes every AUTO_SETTLE_MINUTES
(default 15; 0 turns it off) and looks up, on Kalshi's public market data,
every logged game from today or earlier that has no result yet, recording
the finished ones through Tracker.settle_markets() -- the same path as the
daily run and "Check bets", so a game settled here is never looked up again
by them. It needs no Kalshi key (so it doesn't spend anyone's account rate
limit), spaces its lookups out, and skips a round while a daily run is
going.
"""

import datetime
import os
import threading
import time
from typing import Optional

DEFAULT_MINUTES = 15
SPACING_SECONDS = 0.3  # between market lookups, to be gentle with Kalshi's public API

_status = {"last_run": None, "checked": None, "settled": None, "error": None}
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def interval_minutes() -> float:
    try:
        return max(0.0, float(os.environ.get("AUTO_SETTLE_MINUTES", DEFAULT_MINUTES)))
    except ValueError:
        return float(DEFAULT_MINUTES)


def settle_once(conn, today: Optional[datetime.date] = None, fetch_market=None, sleep=time.sleep) -> dict:
    """
    One round: look up every unsettled game played today or earlier and
    record the finished ones. A market Kalshi can't be reached for is
    skipped (it's tried again next round). Returns {"checked", "settled"}.
    """
    import Kalshi
    import Tracker

    today = today or datetime.date.today()
    tomorrow = (today + datetime.timedelta(days=1)).isoformat()
    tickers = Tracker.unsettled_tickers(conn, before=tomorrow)
    fetch_market = fetch_market or Kalshi.get_market

    def fetch(ticker):
        if tickers and ticker != tickers[0]:
            sleep(SPACING_SECONDS)
        try:
            return fetch_market(ticker)
        except Exception as e:
            print(f"auto-settle: couldn't look up {ticker}: {e}", flush=True)
            return {}  # no result: still pending, tried again next round

    settled = Tracker.settle_markets(conn, tickers, fetch)
    return {"checked": len(tickers), "settled": len(settled)}


def _round() -> None:
    import Tracker
    from webapp import daily_run

    if daily_run.status().get("state") == "running":
        return  # the daily run settles games itself, and is writing to the database
    conn = Tracker.connect()
    try:
        result = settle_once(conn)
        _status.update(last_run=time.time(), error=None, **result)
        if result["settled"]:
            print(f"auto-settle: recorded {result['settled']} result(s) of {result['checked']} game(s) checked",
                  flush=True)
    except Exception as e:  # never let a bad round stop the thread
        _status.update(last_run=time.time(), error=str(e))
        print(f"auto-settle round failed: {e}", flush=True)
    finally:
        conn.close()


def _loop(minutes: float) -> None:
    # A short wait first, so a redeploy's startup isn't slowed; then every `minutes`.
    if _stop.wait(60):
        return
    while not _stop.is_set():
        _round()
        if _stop.wait(minutes * 60):
            return


def start() -> bool:
    """Start the background thread (once per process). False if AUTO_SETTLE_MINUTES turns it off."""
    global _thread
    minutes = interval_minutes()
    if minutes <= 0 or (_thread is not None and _thread.is_alive()):
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(minutes,), name="auto-settle", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    _stop.set()


def summary(now: Optional[float] = None) -> dict:
    """For the Betting tab: whether it's on, how often, and how the last round went."""
    from webapp.daily_run import _ago

    minutes = interval_minutes()
    return {"on": minutes > 0, "minutes": int(minutes) if minutes == int(minutes) else minutes,
            "last_ago": _ago(_status["last_run"], now if now is not None else time.time()),
            "checked": _status["checked"], "settled": _status["settled"], "error": _status["error"]}
