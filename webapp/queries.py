"""
webapp/queries.py

Read-only queries against predictions.db for the webapp. Kept separate from
main.py so they're testable without spinning up FastAPI, and separate from
Tracker.py because these are view-shaping queries for the webapp
specifically, not part of the write-side tracking API every other module
depends on.
"""

import sqlite3
from typing import Optional


def list_games(conn: sqlite3.Connection, game_date: Optional[str] = None) -> list[dict]:
    """
    One row per tracked game, newest first. Pass game_date (ISO, e.g.
    "2026-10-05") to limit to one day.

    DailyLog.py logs only one team's market per game, so grouping by ticker
    is equivalent to grouping by game here.
    """
    sql = (
        "SELECT ticker, event_ticker, game_date, team, opponent, yes_bid, yes_ask, "
        "       market_prob, outcome, COUNT(*) "
        "FROM predictions "
        + ("WHERE game_date = ? " if game_date else "")
        + "GROUP BY ticker ORDER BY game_date DESC, ticker"
    )
    params = (game_date,) if game_date else ()
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "ticker": ticker,
            "event_ticker": event_ticker,
            "game_date": game_date,
            "team": team,
            "opponent": opponent,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "market_prob": market_prob,
            "outcome": outcome,
            "ai_count": ai_count,
        }
        for (ticker, event_ticker, game_date, team, opponent, yes_bid, yes_ask,
             market_prob, outcome, ai_count) in rows
    ]


def list_game_dates(conn: sqlite3.Connection) -> list[str]:
    """Every distinct game date tracked so far, newest first."""
    rows = conn.execute(
        "SELECT DISTINCT game_date FROM predictions WHERE game_date IS NOT NULL ORDER BY game_date DESC"
    ).fetchall()
    return [r[0] for r in rows]


def get_game(conn: sqlite3.Connection, ticker: str) -> Optional[dict]:
    """
    Full detail for one game: its Kalshi odds/outcome, plus every AI's
    prediction next to the market's own price (the "AI vs Kalshi" view).

    Returns None if no prediction was ever recorded for this ticker.
    """
    row = conn.execute(
        "SELECT ticker, event_ticker, game_date, team, opponent, yes_bid, yes_ask, "
        "       market_prob, outcome, settled_at "
        "FROM predictions WHERE ticker = ? LIMIT 1",
        (ticker,),
    ).fetchone()
    if row is None:
        return None
    (ticker, event_ticker, game_date, team, opponent, yes_bid, yes_ask,
     market_prob, outcome, settled_at) = row

    predictions = conn.execute(
        "SELECT llm, model, predicted_prob, confidence FROM predictions WHERE ticker = ? ORDER BY llm",
        (ticker,),
    ).fetchall()

    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "game_date": game_date,
        "team": team,
        "opponent": opponent,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "market_prob": market_prob,
        "outcome": outcome,
        "settled_at": settled_at,
        "predictions": [
            {
                "llm": llm,
                "model": model,
                "predicted_prob": predicted_prob,
                "confidence": confidence,
                # Positive: this AI thought YES (the named team) was more
                # likely than the market priced it; negative: less likely.
                "edge": predicted_prob - market_prob,
            }
            for llm, model, predicted_prob, confidence in predictions
        ],
    }


def accuracy_summary(conn: sqlite3.Connection) -> list[dict]:
    """Each AI's Brier score / weight so far, for display alongside the market's own."""
    import Tracker

    scores = Tracker.brier_scores(conn)
    weights = Tracker.compute_weights(conn)
    return [
        {
            "llm": llm,
            "settled_n": stats["n"],
            "brier": stats["brier"],
            "market_brier": stats["market_brier"],
            "weight": weights.get(llm, 1.0),
        }
        for llm, stats in sorted(scores.items())
    ]
