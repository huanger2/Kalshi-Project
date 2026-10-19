"""
webapp/data_files.py

The admin Data page's file handling: list what the daily run has generated
(predictions.db, daily_logs/*.xlsx, api_costs.xlsx) for download, and accept
uploads -- a predictions.db to replace the server's (a one-time move of
history from another machine), and daily log spreadsheets.

Every path is read at call time from the module that owns it (Tracker,
DailyLog, CostExport), so DATA_DIR (see DataDir.py) applies here exactly as
it does to the daily run itself.

predictions.db also holds the accounts (Accounts.py): users, their
settings and their encrypted Kalshi keys. A download leaves those tables
out (sanitized_db_copy()), and an uploaded replacement keeps the server's
own accounts rather than whatever the upload had.
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


def sanitized_db_copy(path: str) -> None:
    """
    Write predictions.db to `path` without the account tables -- the
    download is for the game history, and shouldn't carry anyone's password
    hash or (even encrypted) Kalshi key.
    """
    import Accounts

    source = sqlite3.connect(db_path())
    target = sqlite3.connect(path)
    try:
        source.backup(target)
        for table in Accounts.ACCOUNT_TABLES:
            target.execute(f"DROP TABLE IF EXISTS {table}")
        target.commit()
        target.execute("VACUUM")
    finally:
        source.close()
        target.close()


def _carry_over_accounts(conn: sqlite3.Connection, current: str) -> None:
    """
    Copy the server's account tables from `current` (the database being
    replaced) into the upload, replacing whatever it had: accounts belong to
    this server, not to the file. Bets in the upload owned by a user id the
    server doesn't have become unowned, and the first admin claims them.
    """
    import Accounts

    conn.execute("ATTACH DATABASE ? AS old", (current,))
    try:
        old_tables = {row[0] for row in conn.execute("SELECT name FROM old.sqlite_master WHERE type = 'table'")}
        for table in Accounts.ACCOUNT_TABLES:
            conn.execute(f"DELETE FROM main.{table}")
            if table in old_tables:
                conn.execute(f"INSERT INTO main.{table} SELECT * FROM old.{table}")
        conn.commit()
    finally:
        conn.execute("DETACH DATABASE old")
    for table in ("bets", "balance_history"):
        conn.execute(f"UPDATE {table} SET user_id = NULL WHERE user_id NOT IN (SELECT id FROM users)")
    conn.commit()
    Accounts.claim_unowned(conn)


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
            if os.path.exists(target):
                _carry_over_accounts(conn, target)
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
