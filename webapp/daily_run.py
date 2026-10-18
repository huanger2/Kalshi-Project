"""
webapp/daily_run.py

Runs DailyLog.run() inside the webapp's own process, in a background
thread, when Railway's cron service asks (see trigger_daily_run.py).

Why here and not in the cron service itself: a Railway volume attaches to
exactly one service, and the webapp's volume is where predictions.db (and
everything else generated, see DataDir.py) lives. The cron service just
makes the request and reports the outcome.

Protected by a shared secret, the DAILY_RUN_TOKEN env var, sent as
"Authorization: Bearer <token>". With no token configured, the endpoint
refuses every request -- failing closed, like the admin login.
"""

import hmac
import os
import threading
import time
import traceback
from typing import Optional

_lock = threading.Lock()  # held for the whole run: one at a time
_status_lock = threading.Lock()
_status = {"run_id": 0, "state": "idle", "started_at": None, "finished_at": None,
           "settled": None, "logged": None, "error": None}


def is_configured() -> bool:
    return bool(os.environ.get("DAILY_RUN_TOKEN"))


def token_ok(authorization: Optional[str]) -> bool:
    """Constant-time check of an Authorization header against DAILY_RUN_TOKEN."""
    token = os.environ.get("DAILY_RUN_TOKEN")
    if not token or not authorization:
        return False
    return hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode())


def status() -> dict:
    with _status_lock:
        return dict(_status)


def _update(**fields) -> None:
    with _status_lock:
        _status.update(fields)


def _run(run) -> None:
    try:
        settled, logged = run()
        _update(state="done", finished_at=time.time(), settled=settled, logged=logged)
    except BaseException as e:  # SystemExit too: DailyLog raises it for a locked spreadsheet
        traceback.print_exc()
        _update(state="failed", finished_at=time.time(), error=f"{type(e).__name__}: {e}")
    finally:
        _lock.release()


def start(run=None) -> Optional[int]:
    """
    Start a daily run in the background. Returns its run id, or None if one
    is already in progress. `run` defaults to DailyLog.run; injectable so
    tests don't call the AIs.
    """
    if not _lock.acquire(blocking=False):
        return None
    if run is None:
        import DailyLog
        run = DailyLog.run
    with _status_lock:
        _status.update(run_id=_status["run_id"] + 1, state="running", started_at=time.time(),
                       finished_at=None, settled=None, logged=None, error=None)
        run_id = _status["run_id"]
    threading.Thread(target=_run, args=(run,), name=f"daily-run-{run_id}", daemon=True).start()
    return run_id
