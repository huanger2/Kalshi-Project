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
import json
import os
import threading
import time
import traceback
from typing import Optional

_lock = threading.Lock()  # held for the whole run: one at a time
_status_lock = threading.Lock()
_status = {"run_id": 0, "state": "idle", "started_at": None, "finished_at": None,
           "settled": None, "logged": None, "error": None, "leagues": None}

STATUS_NAME = "daily_run_status.json"


def _status_path() -> Optional[str]:
    """
    Hosted (DATA_DIR set), the last run's status is kept on the volume, so a
    redeploy doesn't wipe the record of whether this morning's run happened.
    Locally it stays in memory only.
    """
    import DataDir
    return DataDir.data_path(STATUS_NAME) if DataDir.data_dir() else None


def _save() -> None:
    path = _status_path()
    if path is None:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_status, f)
        os.replace(tmp, path)
    except OSError as e:  # best-effort: never let bookkeeping fail a run
        print(f"could not save daily run status: {e}", flush=True)


def _load() -> None:
    path = _status_path()
    if path is None or not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return
    _status.update({k: saved.get(k) for k in _status})
    if _status["state"] == "running":  # the process stopped mid-run (e.g. a redeploy)
        _status.update(state="failed", error="Interrupted: the web service restarted during the run.")


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


def _ago(then: Optional[float], now: float) -> Optional[str]:
    """'just now', '12 min ago', '3 h ago', '2 days ago' -- relative, so it reads right in any time zone."""
    if then is None:
        return None
    seconds = max(0, now - then)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    days = int(seconds // 86400)
    return f"{days} day{'' if days == 1 else 's'} ago"


def summary(now: Optional[float] = None) -> dict:
    """status() plus when it started/finished in words, for the admin's run button."""
    now = now if now is not None else time.time()
    current = status()
    return {**current, "started_ago": _ago(current["started_at"], now),
            "finished_ago": _ago(current["finished_at"], now)}


def _update(**fields) -> None:
    with _status_lock:
        _status.update(fields)
        _save()


def _run(run) -> None:
    try:
        settled, logged = run()
        _update(state="done", finished_at=time.time(), settled=settled, logged=logged)
    except BaseException as e:  # SystemExit too: DailyLog raises it for a locked spreadsheet
        traceback.print_exc()
        _update(state="failed", finished_at=time.time(), error=f"{type(e).__name__}: {e}")
    finally:
        _lock.release()


def start(run=None, leagues: Optional[list[str]] = None) -> Optional[int]:
    """
    Start a daily run in the background. Returns its run id, or None if one
    is already in progress. `leagues` limits it to those leagues (an admin's
    "run NBA now"); None is every league tracked in leagues.json, like the
    cron's. `run` defaults to DailyLog.run; injectable so tests don't call
    the AIs.
    """
    if not _lock.acquire(blocking=False):
        return None
    if run is None:
        import DailyLog
        run = (lambda: DailyLog.run(leagues=leagues)) if leagues else DailyLog.run
    with _status_lock:
        _status.update(run_id=_status["run_id"] + 1, state="running", started_at=time.time(),
                       finished_at=None, settled=None, logged=None, error=None, leagues=leagues)
        run_id = _status["run_id"]
        _save()
    threading.Thread(target=_run, args=(run,), name=f"daily-run-{run_id}", daemon=True).start()
    return run_id


_load()
