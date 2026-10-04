"""
Tracker.py

Accumulates LLM predictions in a local SQLite file and, once the markets
settle, turns them into a 0-1 accuracy weight per LLM.

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
    llm            TEXT    NOT NULL,
    predicted_prob REAL    NOT NULL,
    confidence     REAL    NOT NULL,
    market_prob    REAL    NOT NULL,
    outcome        INTEGER,          -- 1 = YES won, 0 = NO won, NULL = not settled yet
    settled_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_predictions_ticker ON predictions (ticker);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    """Open (creating if needed) the predictions database."""
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def record_predictions(
    conn: sqlite3.Connection,
    market: dict,
    market_prob: float,
    predictions: list,
) -> int:
    """
    Store one row per LLM prediction for a market.

    Args:
        market:      Kalshi market dict (needs "ticker"; "event_ticker"/"title" optional).
        market_prob: The market's implied YES probability at the time of the query.
        predictions: LLMPrediction objects (llm_name, predicted_prob, confidence).

    Returns:
        Number of rows inserted.
    """
    now = _now()
    rows = [
        (
            now,
            market["ticker"],
            market.get("event_ticker"),
            market.get("title"),
            p.llm_name,
            p.predicted_prob,
            p.confidence,
            market_prob,
        )
        for p in predictions
    ]
    conn.executemany(
        "INSERT INTO predictions "
        "(created_at, ticker, event_ticker, title, llm, predicted_prob, confidence, market_prob) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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


def brier_scores(conn: sqlite3.Connection) -> dict[str, dict]:
    """
    Per-LLM accuracy over settled predictions.

    Returns {llm: {"n": count, "brier": mean squared error,
                   "market_brier": the market's own score on the same games}}.
    """
    rows = conn.execute(
        "SELECT llm, COUNT(*), "
        "       AVG((predicted_prob - outcome) * (predicted_prob - outcome)), "
        "       AVG((market_prob - outcome) * (market_prob - outcome)) "
        "FROM predictions WHERE outcome IS NOT NULL GROUP BY llm"
    ).fetchall()
    return {llm: {"n": n, "brier": brier, "market_brier": mkt} for llm, n, brier, mkt in rows}


def brier_to_weight(brier: float) -> float:
    """Map a Brier score to a weight in [0, 1] (0.25 coin-flip -> 0, 0.0 perfect -> 1)."""
    return max(0.0, min(1.0, 1.0 - brier / COIN_FLIP_BRIER))


def compute_weights(
    conn: sqlite3.Connection,
    min_samples: int = 20,
    default_weight: float = 1.0,
) -> dict[str, float]:
    """
    Accuracy weight in [0, 1] for each LLM seen so far.

    An LLM with fewer than ``min_samples`` settled predictions keeps
    ``default_weight`` because a handful of games says little about skill.
    """
    weights = {}
    seen = conn.execute("SELECT DISTINCT llm FROM predictions").fetchall()
    for (llm,) in seen:
        weights[llm] = default_weight

    for llm, stats in brier_scores(conn).items():
        if stats["n"] >= min_samples:
            weights[llm] = brier_to_weight(stats["brier"])
    return weights
