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

# Fixed, known domain -- not queried from the database the way
# list_season_years() queries actual seasons, since every value here is
# always a valid filter choice regardless of what's been logged so far.
SEGMENTS = ["preseason", "regular", "postseason"]


def _season_filter(segment: Optional[str], season_year: Optional[str]) -> tuple[str, list]:
    """SQL `AND ...` clause (or "") plus its params for the segment/season_year pair."""
    conditions = []
    params: list = []
    if segment:
        conditions.append("segment(game_date) = ?")
        params.append(segment)
    if season_year:
        conditions.append("season_year(game_date) = ?")
        params.append(season_year)
    return (" AND " + " AND ".join(conditions)) if conditions else "", params


def _call_correct(prob: Optional[float], outcome: Optional[int]) -> Optional[bool]:
    """
    Did a YES-probability call end up matching the outcome? None (no color,
    not right or wrong) if the game hasn't settled or the call was a flat
    50/50 -- same "exactly 50% doesn't count as calling a side" rule used
    by direction_accuracy in ai_stats().
    """
    if prob is None or outcome is None or prob == 0.5:
        return None
    return (prob > 0.5) == (outcome == 1)


def list_games(
    conn: sqlite3.Connection,
    game_date: Optional[str] = None,
    segment: Optional[str] = None,
    season_year: Optional[str] = None,
) -> list[dict]:
    """
    One row per tracked game, newest first. Pass game_date (ISO, e.g.
    "2026-10-05") to limit to one day, segment ("preseason", "regular",
    "postseason") to limit to one part of the season, and/or season_year
    (e.g. "2026-27") to limit to one year-spanning season -- independently
    of each other, combinable freely.

    DailyLog.py logs only one team's market per game, so grouping by ticker
    is equivalent to grouping by game here.
    """
    conditions = []
    params: list = []
    if game_date:
        conditions.append("game_date = ?")
        params.append(game_date)
    if segment:
        conditions.append("segment(game_date) = ?")
        params.append(segment)
    if season_year:
        conditions.append("season_year(game_date) = ?")
        params.append(season_year)
    where = f"WHERE {' AND '.join(conditions)} " if conditions else ""

    sql = (
        "SELECT ticker, event_ticker, game_date, team, opponent, yes_bid, yes_ask, "
        "       market_prob, outcome, COUNT(*), AVG(predicted_prob), "
        "       segment(game_date), season_year(game_date) "
        "FROM predictions "
        + where
        + "GROUP BY ticker ORDER BY game_date DESC, ticker"
    )
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
            "ai_prob": ai_prob,
            "outcome": outcome,
            "ai_count": ai_count,
            "segment": seg,
            "season_year": year,
            "market_correct": _call_correct(market_prob, outcome),
            "ai_correct": _call_correct(ai_prob, outcome),
        }
        for (ticker, event_ticker, game_date, team, opponent, yes_bid, yes_ask,
             market_prob, outcome, ai_count, ai_prob, seg, year) in rows
    ]


def list_game_dates(conn: sqlite3.Connection) -> list[str]:
    """Every distinct game date tracked so far, newest first."""
    rows = conn.execute(
        "SELECT DISTINCT game_date FROM predictions WHERE game_date IS NOT NULL ORDER BY game_date DESC"
    ).fetchall()
    return [r[0] for r in rows]


def list_season_years(conn: sqlite3.Connection) -> list[str]:
    """Every distinct year-spanning season tracked so far, newest first (e.g. ["2027-28", "2026-27"])."""
    rows = conn.execute(
        "SELECT DISTINCT season_year(game_date) FROM predictions "
        "WHERE game_date IS NOT NULL ORDER BY season_year(game_date) DESC"
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
        "       market_prob, outcome, settled_at, segment(game_date), season_year(game_date) "
        "FROM predictions WHERE ticker = ? LIMIT 1",
        (ticker,),
    ).fetchone()
    if row is None:
        return None
    (ticker, event_ticker, game_date, team, opponent, yes_bid, yes_ask,
     market_prob, outcome, settled_at, seg, year) = row

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
        "segment": seg,
        "season_year": year,
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


def ai_stats(
    conn: sqlite3.Connection,
    llm: str,
    segment: Optional[str] = None,
    season_year: Optional[str] = None,
) -> dict:
    """
    One AI's calibration/accuracy picture, over its settled games. Pass
    segment ("preseason", "regular", "postseason") and/or season_year
    (e.g. "2026-27") to scope this -- see Tracker.brier_scores() for why
    segment matters.

    - avg_edge_on_win / avg_edge_on_loss: the average (predicted - market)
      edge, split by what actually happened. A skilled AI's edge should
      point the right way *in advance* of the outcome: positive on games
      the team went on to win, negative on games it went on to lose. Edge
      numbers that look similar either way mean the edge isn't actually
      predictive, whatever the raw Brier score says.
    - direction_accuracy: how often the AI's >50%/<50% call matched the
      winner, regardless of margin -- market_direction_accuracy is the
      same thing for Kalshi's own price, for comparison. A predicted_prob
      of exactly 0.50 counts as not having called a side.

    Any stat computed from zero rows comes back as None, not 0 -- "no data
    yet" and "exactly zero" are different things here.
    """
    import Tracker

    season_sql, season_params = _season_filter(segment, season_year)

    total_n, settled_n = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END), 0) "
        "FROM predictions WHERE llm = ?" + season_sql,
        [llm, *season_params],
    ).fetchone()

    (avg_edge_on_win, avg_edge_on_loss, direction_accuracy,
     market_direction_accuracy, avg_confidence) = conn.execute(
        "SELECT "
        "  AVG(CASE WHEN outcome = 1 THEN predicted_prob - market_prob END), "
        "  AVG(CASE WHEN outcome = 0 THEN predicted_prob - market_prob END), "
        "  AVG(CASE WHEN (predicted_prob > 0.5 AND outcome = 1) OR (predicted_prob < 0.5 AND outcome = 0) "
        "       THEN 1.0 ELSE 0.0 END), "
        "  AVG(CASE WHEN (market_prob > 0.5 AND outcome = 1) OR (market_prob < 0.5 AND outcome = 0) "
        "       THEN 1.0 ELSE 0.0 END), "
        "  AVG(confidence) "
        "FROM predictions WHERE llm = ? AND outcome IS NOT NULL" + season_sql,
        [llm, *season_params],
    ).fetchone()

    scores = Tracker.brier_scores(conn, segment=segment, season_year=season_year).get(llm)
    weight = Tracker.compute_weights(conn, segment=segment, season_year=season_year).get(llm, 1.0)

    return {
        "llm": llm,
        "total_n": total_n,
        "settled_n": settled_n,
        "pending_n": total_n - settled_n,
        "brier": scores["brier"] if scores else None,
        "market_brier": scores["market_brier"] if scores else None,
        "weight": weight,
        "avg_edge_on_win": avg_edge_on_win,
        "avg_edge_on_loss": avg_edge_on_loss,
        "direction_accuracy": direction_accuracy,
        "market_direction_accuracy": market_direction_accuracy,
        "avg_confidence": avg_confidence,
    }


def ai_predictions(
    conn: sqlite3.Connection,
    llm: str,
    segment: Optional[str] = None,
    season_year: Optional[str] = None,
) -> list[dict]:
    """Every prediction this AI has made, newest game first, with its edge vs the market."""
    season_sql, season_params = _season_filter(segment, season_year)

    rows = conn.execute(
        "SELECT ticker, game_date, team, opponent, model, predicted_prob, confidence, market_prob, "
        "       outcome, segment(game_date), season_year(game_date) "
        "FROM predictions WHERE llm = ?" + season_sql + " ORDER BY game_date DESC, ticker",
        [llm, *season_params],
    ).fetchall()
    return [
        {
            "ticker": ticker,
            "game_date": game_date,
            "team": team,
            "opponent": opponent,
            "model": model,
            "predicted_prob": predicted_prob,
            "confidence": confidence,
            "market_prob": market_prob,
            "outcome": outcome,
            "segment": seg,
            "season_year": year,
            "edge": predicted_prob - market_prob,
        }
        for (ticker, game_date, team, opponent, model, predicted_prob, confidence,
             market_prob, outcome, seg, year) in rows
    ]


def accuracy_summary(
    conn: sqlite3.Connection, segment: Optional[str] = None, season_year: Optional[str] = None
) -> list[dict]:
    """Each AI's Brier score / weight so far, for display alongside the market's own."""
    import Tracker

    scores = Tracker.brier_scores(conn, segment=segment, season_year=season_year)
    weights = Tracker.compute_weights(conn, segment=segment, season_year=season_year)
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
