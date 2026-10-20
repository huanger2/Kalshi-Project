"""
Production entrypoint: `python -m webapp`.

Binds to all interfaces on $PORT (Railway sets it; 8000 locally) -- read
here rather than in the start command, so it works whether or not the
host runs that command through a shell that expands $PORT.

With DAILY_TRIGGER set (1/true/yes), it instead runs trigger_daily_run.py
once and exits with its result. That's for the Railway cron service: it's
built from the same repo, so it gets the same start command as the website
(railway.json, which Railway applies to every service and which overrides
the dashboard's start command), and Railway no longer lets a service opt
into a config file of its own. A cron service that started the website
would never exit -- "running" forever, never triggering a run.
"""

import os
import sys

import uvicorn


def daily_trigger_mode() -> bool:
    return os.environ.get("DAILY_TRIGGER", "").strip().lower() in ("1", "true", "yes")


def main() -> None:
    if daily_trigger_mode():
        import trigger_daily_run
        sys.exit(trigger_daily_run.main())
    uvicorn.run("webapp.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
