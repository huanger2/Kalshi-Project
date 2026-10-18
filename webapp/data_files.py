"""
webapp/data_files.py

The admin Data page's file handling: list what the daily run has generated
(predictions.db, daily_logs/*.xlsx, api_costs.xlsx) for download, and accept
uploads -- a predictions.db to replace the server's (a one-time move of
history from another machine), and daily log spreadsheets.

Every path is read at call time from the module that owns it (Tracker,
DailyLog, CostExport), so DATA_DIR (see DataDir.py) applies here exactly as
it does to the daily run itself.
"""

import datetime
import os
import re
import sqlite3
from typing import Optional

SQLITE_MAGIC = b"SQLite format 3\x00"
XLSX_MAGIC = b"PK\x03\x04"  # an .xlsx is a zip archive
_DAY_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.xlsx$")


def db_path() -> str:
    import Tracker
    return Tracker.DEFAULT_DB


def logs_dir() -> str:
    import DailyLog
    return DailyLog.DEFAULT_DIR


def costs_path() -> str:
    import CostExport
    return CostExport.DEFAULT_OUT


def _entry(name: str, path: str, label: str) -> dict:
    stat = os.stat(path)
    return {"name": name, "path": path, "label": label, "size": stat.st_size,
            "modified": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="minutes")}


def listing() -> list[dict]:
    """Every downloadable file that exists right now: the database, the cost sheet, then day files newest first."""
    files = []
    if os.path.exists(db_path()):
        files.append(_entry("predictions.db", db_path(), "Prediction database"))
    if os.path.exists(costs_path()):
        files.append(_entry("api_costs.xlsx", costs_path(), "API spend"))
    if os.path.isdir(logs_dir()):
        for name in sorted(os.listdir(logs_dir()), reverse=True):
            if _DAY_FILE.match(name):
                files.append(_entry(f"daily_logs/{name}", os.path.join(logs_dir(), name), "Daily log"))
    return files


def resolve_download(name: str) -> Optional[str]:
    """The path for a listed file's name, or None -- only names listing() produced, so no ../ tricks."""
    return next((f["path"] for f in listing() if f["name"] == name), None)


class UploadError(ValueError):
    """An upload that was rejected; the message is shown to the admin."""


def replace_database(data: bytes, now: Optional[datetime.datetime] = None) -> dict:
    """
    Swap in an uploaded predictions.db. It must be a SQLite file with a
    predictions table; it's migrated to the current schema (league/tied
    columns, see Tracker._migrate()) before going live. The database it
    replaces is kept beside it as predictions.db.bak-<timestamp>, never
    deleted. Returns {"rows": prediction rows, "backup": backup file name or None}.
    """
    import Tracker

    if not data.startswith(SQLITE_MAGIC):
        raise UploadError("That isn't a SQLite database file.")
    target = db_path()
    folder = os.path.dirname(target) or "."
    os.makedirs(folder, exist_ok=True)
    tmp = os.path.join(folder, ".predictions-upload.db")
    with open(tmp, "wb") as f:
        f.write(data)
    try:
        check = sqlite3.connect(tmp)
        try:
            has_table = check.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'predictions'"
            ).fetchone()
        finally:
            check.close()
        if not has_table:
            raise UploadError("That database has no predictions table.")
        conn = Tracker.connect(tmp)  # applies any pending schema migrations
        try:
            rows = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        os.remove(tmp)
        raise UploadError(f"That database couldn't be read: {e}") from None
    except UploadError:
        os.remove(tmp)
        raise

    backup = None
    if os.path.exists(target):
        stamp = (now or datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
        backup = f"{os.path.basename(target)}.bak-{stamp}"
        os.replace(target, os.path.join(folder, backup))
    os.replace(tmp, target)
    return {"rows": rows, "backup": backup}


def add_daily_logs(files: list[tuple[str, bytes]]) -> dict:
    """
    Save uploaded day files (named like 2026-10-04.xlsx) into daily_logs/.
    A day that already has a file on the server is skipped, not
    overwritten -- the server's copy may have results filled in since.
    Returns {"added": [...], "skipped": [...], "rejected": [...]} by file name.
    """
    import DailyLog

    folder = logs_dir()
    os.makedirs(folder, exist_ok=True)
    added, skipped, rejected = [], [], []
    for name, data in files:
        name = os.path.basename(name or "")
        if not _DAY_FILE.match(name) or not data.startswith(XLSX_MAGIC):
            rejected.append(name or "(unnamed)")
            continue
        target = os.path.join(folder, name)
        if os.path.exists(target):
            skipped.append(name)
            continue
        # Still .xlsx (pandas picks its reader by extension), but not a name
        # DailyLog.earlier_days() would mistake for a real day file.
        tmp = target[: -len(".xlsx")] + ".upload.xlsx"
        with open(tmp, "wb") as f:
            f.write(data)
        try:
            DailyLog.load(tmp)  # must be a readable daily log, not just any spreadsheet
        except Exception:
            os.remove(tmp)
            rejected.append(name)
            continue
        os.replace(tmp, target)
        added.append(name)
    return {"added": added, "skipped": skipped, "rejected": rejected}
