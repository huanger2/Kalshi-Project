"""
DataDir.py

Where generated data lives: predictions.db, daily_logs/, api_costs.xlsx,
predictions.xlsx, and (hosted only) the league tracking switches' state.

Unset (the default, and how this runs on a personal machine): everything
sits in the working directory, exactly as before. Set DATA_DIR (e.g.
DATA_DIR=/data on Railway, where /data is a mounted volume) to put all of
it there instead -- a hosted service's own filesystem is wiped on every
deploy, a volume isn't.

PREDICTIONS_DB still overrides just the database's path, for anyone
already using it.
"""

import os
from typing import Optional


def data_dir() -> str:
    """The directory generated data is written to ("" = the working directory)."""
    return os.environ.get("DATA_DIR", "")


def data_path(name: str) -> str:
    """`name` inside the data directory (unchanged when DATA_DIR isn't set)."""
    base = data_dir()
    return os.path.join(base, name) if base else name


def problem(db_path: str) -> Optional[str]:
    """
    Why the database can't be opened, in terms of what to fix -- or None if
    its folder exists and is writable. SQLite itself only ever says "unable
    to open database file", which doesn't say whether the folder is missing
    (on Railway: no volume attached, or mounted at a different path than
    DATA_DIR) or unwritable (a non-root process writing to a volume).
    """
    folder = os.path.dirname(os.path.abspath(db_path))
    setting = f"DATA_DIR is {data_dir()!r}" if data_dir() else f"The database path is {db_path!r}"
    if not os.path.isdir(folder):
        return (f"{setting}, but {folder} doesn't exist. On Railway: attach a volume to this service "
                f"mounted at {folder}, or change DATA_DIR to the volume's actual mount path.")
    if not os.access(folder, os.W_OK):
        uid = getattr(os, "getuid", lambda: "?")()
        return (f"{folder} exists, but this process (user id {uid}) can't write to it. On Railway: set the "
                f"variable RAILWAY_RUN_UID=0 on this service, so the app can write to its volume.")
    return None
