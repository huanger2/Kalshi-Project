"""
trigger_daily_run.py

The start command of the Railway cron service that replaces the Windows
scheduled task (schedule_task.ps1). It doesn't run the daily job itself --
it asks the webapp service to (POST /internal/daily-run), because only the
webapp's volume has predictions.db; see webapp/daily_run.py. Then it waits
for the run to finish and exits 0 if it succeeded, 1 if not, so Railway's
cron logs show each morning's outcome.

Standard library only. Needs two env vars on the cron service:

    DAILY_RUN_URL    the webapp's base URL, e.g. https://your-app.up.railway.app
    DAILY_RUN_TOKEN  the same secret as the webapp's DAILY_RUN_TOKEN

    python trigger_daily_run.py
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

POLL_SECONDS = 15
TIMEOUT_SECONDS = 60 * 60  # a full slate is a few hundred AI calls; give it an hour


def _call(method: str, url: str, token: str) -> tuple[int, dict]:
    request = urllib.request.Request(url, method=method, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
        except ValueError:
            body = {}
        return e.code, body


def main(sleep=time.sleep, now=time.monotonic) -> int:
    base = os.environ.get("DAILY_RUN_URL", "").rstrip("/")
    token = os.environ.get("DAILY_RUN_TOKEN", "")
    if not base or not token:
        print("DAILY_RUN_URL and DAILY_RUN_TOKEN must both be set on this service")
        return 1
    endpoint = f"{base}/internal/daily-run"

    code, body = _call("POST", endpoint, token)
    if code == 409:
        print("a daily run was already in progress; waiting on that one")
    elif code != 202:
        print(f"could not start the daily run: HTTP {code} {body.get('detail', body)}")
        return 1
    run_id = body.get("run_id")
    print(f"started daily run #{run_id}")

    deadline = now() + TIMEOUT_SECONDS
    while now() < deadline:
        sleep(POLL_SECONDS)
        try:
            code, status = _call("GET", endpoint, token)
        except OSError as e:  # e.g. the webapp mid-redeploy; keep trying until the deadline
            print(f"status check failed ({e}); retrying")
            continue
        if code != 200:
            print(f"status check failed: HTTP {code}; retrying")
            continue
        if status.get("run_id") != run_id:
            print("the webapp restarted mid-run, so this run was lost; the next one will catch up")
            return 1
        if status["state"] == "done":
            print(f"daily run #{run_id} done: settled {status['settled']} game(s), logged {status['logged']}")
            return 0
        if status["state"] == "failed":
            print(f"daily run #{run_id} failed: {status['error']} (details are in the webapp's logs)")
            return 1
    print(f"gave up waiting after {TIMEOUT_SECONDS // 60} minutes; the run may still finish")
    return 1


if __name__ == "__main__":
    sys.exit(main())
