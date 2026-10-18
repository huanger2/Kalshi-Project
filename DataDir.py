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


def data_dir() -> str:
    """The directory generated data is written to ("" = the working directory)."""
    return os.environ.get("DATA_DIR", "")


def data_path(name: str) -> str:
    """`name` inside the data directory (unchanged when DATA_DIR isn't set)."""
    base = data_dir()
    return os.path.join(base, name) if base else name
