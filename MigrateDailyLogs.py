"""
MigrateDailyLogs.py

One-time (but safely re-runnable) backfill: reads every daily_logs/*.xlsx
file and records its rows into predictions.db, so the webapp has the real
history from before Tracker.record_predictions() was wired into
DailyLog.py's daily run.

Idempotent: skips any (ticker, llm) pair already in the database, so
running this again after a new day's file appears only adds that new day.
Each row's *actual* model (the `{ai}_model` column DailyLog.py logs) is
preserved as-is, not re-looked-up against the current ai_settings.json --
an old gpt-4.1-mini row must stay a gpt-4.1-mini row.

    py MigrateDailyLogs.py [folder]
"""

import datetime
import os
import sys

import pandas as pd

import DailyLog
import Leagues
import Tracker

LLM_NAMES = ["OpenAI", "Claude", "Gemini"]


def _value(v):
    """None for a missing/NaN cell, otherwise the plain value."""
    return None if pd.isna(v) else v


def all_days(folder: str) -> list[datetime.date]:
    """Every daily log file's date in `folder`, oldest first."""
    days = []
    for name in os.listdir(folder):
        stem, ext = os.path.splitext(name)
        if ext != ".xlsx":
            continue
        try:
            days.append(datetime.date.fromisoformat(stem))
        except ValueError:
            continue  # not a daily log file (e.g. Excel's ~$ lock file)
    return sorted(days)


def migrate_file(conn, path: str) -> int:
    """Insert every (game, AI) prediction in one day's spreadsheet that isn't in the database yet."""
    df = DailyLog.load(path)
    if df.empty:
        return 0

    existing = {(r[0], r[1]) for r in conn.execute("SELECT ticker, llm FROM predictions")}
    now = Tracker._now()
    rows = []

    for _, row in df.iterrows():
        ticker = row["ticker"]
        outcome = _value(row.get("team_won"))
        tied = 1 if _value(row.get("winner")) == "Tie" else None
        settled_at = _value(row.get("settled_at")) if outcome is not None else None

        for name in LLM_NAMES:
            if (ticker, name) in existing:
                continue
            key = name.lower()
            prob = _value(row.get(f"{key}_prob"))
            if prob is None:
                continue  # that AI failed on this game; nothing to migrate

            rows.append((
                now, ticker, _value(row.get("event_ticker")), Leagues.league_for_ticker(ticker),
                f"{row.get('team')} wins",
                _value(row.get("team")), _value(row.get("opponent")), _value(row.get("game_date")),
                _value(row.get("yes_bid")), _value(row.get("yes_ask")),
                name, _value(row.get(f"{key}_model")),
                float(prob), float(_value(row.get(f"{key}_confidence")) or 0.0),
                float(row["market_prob"]),
                int(outcome) if outcome is not None else None,
                tied,
                settled_at,
            ))

    if rows:
        conn.executemany(
            "INSERT INTO predictions "
            "(created_at, ticker, event_ticker, league, title, team, opponent, game_date, yes_bid, yes_ask, "
            " llm, model, predicted_prob, confidence, market_prob, outcome, tied, settled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    return len(rows)


def migrate_all(folder: str = DailyLog.DEFAULT_DIR, db_path: str = Tracker.DEFAULT_DB) -> int:
    """Backfill every daily_logs/*.xlsx file into predictions.db. Safe to re-run. Returns rows added."""
    conn = Tracker.connect(db_path)
    try:
        return sum(migrate_file(conn, DailyLog.path_for(day, folder)) for day in all_days(folder))
    finally:
        conn.close()


if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else DailyLog.DEFAULT_DIR
    n = migrate_all(folder)
    print(f"migrated {n} prediction row(s)")
