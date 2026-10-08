"""
Tracker.py

Accumulates LLM predictions in a local SQLite file and, once the markets
settle, turns them into a 0-1 accuracy weight per LLM.

This is the single table meant to back everything that queries "AI vs
Kalshi" data -- the webapp/ package queries it directly. DailyLog.py and
Collect.py both write into it (see record_predictions()); DailyLog.py's
daily_logs/*.xlsx stays as a human-friendly export, not a second source of
truth -- migrate_daily_logs.py backfills any days that predate this table.

Accuracy is measured with the Brier score: the mean of (p - outcome)^2 over
settled predictions, where p is the LLM's probability that YES resolves and
outcome is 1 (YES) or 0 (NO). Lower is better. Always answering 50% scores
0.25, so:

    weight = clamp(1 - brier / 0.25, 0, 1)

A perfect forecaster gets 1.0, a coin-flip 0.0, and anything worse than a
coin-flip is floored at 0.0.
"""

import sqlite3
from datetime import datetime, timezone
from typing import Callable, Optional

DEFAULT_DB = "predictions.db"
COIN_FLIP_BRIER = 0.25

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT    NOT NULL,
    ticker         TEXT    NOT NULL,
    event_ticker   TEXT,
    title          TEXT,
    team           TEXT,             -- the side this row's prediction is about
    opponent       TEXT,
    game_date      TEXT,             -- scheduled (US) game date, ISO; may differ from created_at
    yes_bid        REAL,
    yes_ask        REAL,
    llm            TEXT    NOT NULL,
    model          TEXT,             -- which model (ai_settings.json) produced this prediction
    predicted_prob REAL    NOT NULL,
    confidence     REAL    NOT NULL,
    market_prob    REAL    NOT NULL,
    outcome        INTEGER,          -- 1 = YES won, 0 = NO won, NULL = not settled yet
    settled_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_predictions_ticker ON predictions (ticker);
CREATE INDEX IF NOT EXISTS idx_predictions_event ON predictions (event_ticker);
"""

# Columns added after the table first shipped. CREATE TABLE IF NOT EXISTS
# above only applies to a brand-new file; an existing predictions.db from
# before these columns existed needs them added explicitly.
_MIGRATED_COLUMNS = {
    "team": "TEXT", "opponent": "TEXT", "game_date": "TEXT",
    "yes_bid": "REAL", "yes_ask": "REAL", "model": "TEXT",
}


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
    for column, sql_type in _MIGRATED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE predictions ADD COLUMN {column} {sql_type}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    """
    Open (creating if needed) the predictions database.

    Registers two SQL functions on every connection, so queries can filter
    or group by either without storing them as columns -- both are purely
    derived from game_date, so a query always reflects the current
    nba_season.json, never a stale snapshot from whenever a row was inserted:

        segment(game_date)     -- "preseason", "regular", or "postseason"
        season_year(game_date) -- the year-spanning season, e.g. "2026-27"

    (See SeasonType.py.)
    """
    import SeasonType

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.create_function("segment", 1, SeasonType.segment_iso)
    conn.create_function("season_year", 1, SeasonType.season_year_iso)
    return conn


def record_predictions(
    conn: sqlite3.Connection,
    market: dict,
    market_prob: float,
    predictions: list,
    opponent: Optional[str] = None,
    yes_bid: Optional[float] = None,
    yes_ask: Optional[float] = None,
    game_date: Optional[str] = None,
) -> int:
    """
    Store one row per LLM prediction for a market.

    Args:
        market:      Kalshi market dict (needs "ticker"; "event_ticker"/"title"/
                     "yes_sub_title" optional, the last giving `team`).
        market_prob: The market's implied YES probability at the time of the query.
        predictions: LLMPrediction objects (llm_name, predicted_prob, confidence).
        opponent, yes_bid, yes_ask, game_date:
                     Optional context a caller may already have on hand (e.g.
                     DailyLog.py knows the sibling team and today's date; a bare
                     Probability.analyze_market() call may not).

    Returns:
        Number of rows inserted.
    """
    from AISettings import get_model

    def model_for(llm_name: str) -> Optional[str]:
        # Best-effort: an llm_name that isn't a real configured provider (test
        # fixtures use made-up names) shouldn't stop the prediction itself
        # from being recorded.
        try:
            return get_model(llm_name.lower())
        except (KeyError, ValueError):
            return None

    now = _now()
    team = market.get("yes_sub_title")
    rows = [
        (
            now,
            market["ticker"],
            market.get("event_ticker"),
            market.get("title"),
            team,
            opponent,
            game_date,
            yes_bid,
            yes_ask,
            p.llm_name,
            model_for(p.llm_name),
            p.predicted_prob,
            p.confidence,
            market_prob,
        )
        for p in predictions
    ]
    conn.executemany(
        "INSERT INTO predictions "
        "(created_at, ticker, event_ticker, title, team, opponent, game_date, yes_bid, yes_ask, "
        " llm, model, predicted_prob, confidence, market_prob) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)


def settle(conn: sqlite3.Connection, ticker: str, yes_won: bool) -> int:
    """Record the final outcome for every prediction on a ticker. Returns rows updated."""
    cur = conn.execute(
        "UPDATE predictions SET outcome = ?, settled_at = ? WHERE ticker = ? AND outcome IS NULL",
        (1 if yes_won else 0, _now(), ticker),
    )
    conn.commit()
    return cur.rowcount


def unsettled_tickers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT ticker FROM predictions WHERE outcome IS NULL ORDER BY ticker"
    ).fetchall()
    return [r[0] for r in rows]


def settle_from_kalshi(
    conn: sqlite3.Connection,
    fetch_market: Optional[Callable[[str], dict]] = None,
) -> dict[str, bool]:
    """
    Look up every unsettled ticker on Kalshi and record any results.

    Args:
        fetch_market: Function ticker -> market dict. Defaults to Kalshi.get_market;
                      injectable so tests don't touch the network.

    Returns:
        {ticker: yes_won} for the markets that had settled.
    """
    if fetch_market is None:
        from Kalshi import get_market
        fetch_market = get_market
    from Kalshi import get_market_result

    settled = {}
    for ticker in unsettled_tickers(conn):
        result = get_market_result(fetch_market(ticker))
        if result is None:
            continue
        settle(conn, ticker, result)
        settled[ticker] = result
    return settled


def brier_scores(
    conn: sqlite3.Connection,
    segment: Optional[str] = None,
    season_year: Optional[str] = None,
) -> dict[str, dict]:
    """
    Per-LLM accuracy over settled predictions.

    Pass segment ("preseason", "regular", or "postseason") to scope this to
    one part of the season instead of everything settled -- preseason games
    are inherently noisier (limited rotations, no real stakes) regardless
    of an AI's actual skill, so mixing them in with regular-season/playoff
    games muddies the signal either way. Pass season_year (e.g. "2026-27")
    to scope to one year-spanning season instead. The two combine freely.

    Returns {llm: {"n": count, "brier": mean squared error,
                   "market_brier": the market's own score on the same games}}.
    """
    where = "WHERE outcome IS NOT NULL"
    params: list = []
    if segment:
        where += " AND segment(game_date) = ?"
        params.append(segment)
    if season_year:
        where += " AND season_year(game_date) = ?"
        params.append(season_year)
    rows = conn.execute(
        f"SELECT llm, COUNT(*), "
        f"       AVG((predicted_prob - outcome) * (predicted_prob - outcome)), "
        f"       AVG((market_prob - outcome) * (market_prob - outcome)) "
        f"FROM predictions {where} GROUP BY llm",
        params,
    ).fetchall()
    return {llm: {"n": n, "brier": brier, "market_brier": mkt} for llm, n, brier, mkt in rows}


def brier_to_weight(brier: float) -> float:
    """Map a Brier score to a weight in [0, 1] (0.25 coin-flip -> 0, 0.0 perfect -> 1)."""
    return max(0.0, min(1.0, 1.0 - brier / COIN_FLIP_BRIER))


def compute_weights(
    conn: sqlite3.Connection,
    min_samples: int = 20,
    default_weight: float = 1.0,
    segment: Optional[str] = None,
    season_year: Optional[str] = None,
) -> dict[str, float]:
    """
    Accuracy weight in [0, 1] for each LLM seen so far.

    An LLM with fewer than ``min_samples`` settled predictions keeps
    ``default_weight`` because a handful of games says little about skill.

    segment/season_year scope the underlying Brier score the same way
    brier_scores() does -- see its docstring.
    """
    weights = {}
    seen = conn.execute("SELECT DISTINCT llm FROM predictions").fetchall()
    for (llm,) in seen:
        weights[llm] = default_weight

    scores = brier_scores(conn, segment=segment, season_year=season_year)
    for llm, stats in scores.items():
        if stats["n"] >= min_samples:
            weights[llm] = brier_to_weight(stats["brier"])
    return weights
