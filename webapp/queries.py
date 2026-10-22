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

import Leagues

# Fixed, known domains -- not queried from the database the way
# list_season_years() queries actual seasons, since every value here is
# always a valid filter choice regardless of what's been logged so far.
SEGMENTS = ["preseason", "regular", "postseason"]
LEAGUES = list(Leagues.LEAGUES)


def _conditions(
    segment: Optional[str], season_year: Optional[str], league: Optional[str] = None
) -> tuple[list[str], list]:
    """SQL conditions plus their params for the segment/season_year/league filters that are set."""
    conditions = []
    params: list = []
    if segment:
        conditions.append("segment(game_date, league) = ?")
        params.append(segment)
    if season_year:
        conditions.append("season_year(game_date, league) = ?")
        params.append(season_year)
    if league:
        conditions.append("league = ?")
        params.append(league)
    return conditions, params


def _season_filter(
    segment: Optional[str], season_year: Optional[str], league: Optional[str] = None
) -> tuple[str, list]:
    """SQL `AND ...` clause (or "") plus its params for the segment/season_year/league filters."""
    conditions, params = _conditions(segment, season_year, league)
    return (" AND " + " AND ".join(conditions)) if conditions else "", params


def _call_correct(prob: Optional[float], outcome: Optional[int], tied: bool = False) -> Optional[bool]:
    """
    Did a YES-probability call end up matching the outcome? None (no color,
    not right or wrong) if the game hasn't settled, ended in a tie (NFL
    only -- with no winner, a pick for either side is neither right nor
    wrong), or the call was a flat 50/50 -- same "exactly 50% doesn't count
    as calling a side" rule used by direction_accuracy in ai_stats().
    """
    if prob is None or outcome is None or tied or prob == 0.5:
        return None
    return (prob > 0.5) == (outcome == 1)


def _ai_beat_market(
    ai_prob: Optional[float], market_prob: Optional[float], outcome: Optional[int]
) -> Optional[bool]:
    """
    Whose odds ended up closer to what actually happened: the AIs' average,
    or Kalshi's own price? True/False if one of them was strictly closer to
    the outcome; None if either side is missing, the game hasn't settled,
    or they were equally close (nothing to call either way).
    """
    if ai_prob is None or market_prob is None or outcome is None:
        return None
    ai_error = abs(ai_prob - outcome)
    market_error = abs(market_prob - outcome)
    if ai_error == market_error:
        return None
    return ai_error < market_error


def list_games(
    conn: sqlite3.Connection,
    game_date: Optional[str] = None,
    segment: Optional[str] = None,
    season_year: Optional[str] = None,
    league: Optional[str] = None,
    weights: Optional[dict[str, float]] = None,
) -> list[dict]:
    """
    One row per tracked game, newest first. Pass game_date (ISO, e.g.
    "2026-10-05") to limit to one day, segment ("preseason", "regular",
    "postseason") to limit to one part of the season, season_year (e.g.
    "2026-27", or "2026" for NFL/MLB) to limit to one season, and/or league
    ("NBA", "NFL", "NHL", "MLB") to limit to one league -- independently of
    each other, combinable freely.

    DailyLog.py logs only one team's market per game, so grouping by ticker
    is equivalent to grouping by game here. "AI odds" is each game's
    consensus_probability() across its predictions -- weighted by each
    prediction's own confidence and its AI's manually-configured weight
    -- `weights`, the viewer's own (AIWeights.for_user()), else the
    site-wide defaults (1.0 for every AI unless an admin changed them) --
    not a bare average.
    """
    import AIWeights
    from EVCalculator import LLMPrediction, consensus_confidence, consensus_probability

    conditions, params = _conditions(segment, season_year, league)
    if game_date:
        conditions.append("game_date = ?")
        params.append(game_date)
    where = f"WHERE {' AND '.join(conditions)} " if conditions else ""

    sql = (
        "SELECT ticker, event_ticker, league, game_date, team, opponent, yes_bid, yes_ask, "
        "       market_prob, outcome, tied, COUNT(*), "
        "       segment(game_date, league), season_year(game_date, league) "
        "FROM predictions "
        + where
        + "GROUP BY ticker ORDER BY game_date DESC, ticker"
    )
    rows = conn.execute(sql, params).fetchall()

    predictions_by_ticker: dict[str, list[LLMPrediction]] = {}
    pred_sql = "SELECT ticker, llm, predicted_prob, confidence FROM predictions " + where
    for ticker, llm, predicted_prob, confidence in conn.execute(pred_sql, params).fetchall():
        predictions_by_ticker.setdefault(ticker, []).append(
            LLMPrediction(llm, predicted_prob, confidence, weight=AIWeights.lookup(weights, llm))
        )

    games = []
    for (ticker, event_ticker, game_league, game_date, team, opponent, yes_bid, yes_ask,
         market_prob, outcome, tied, ai_count, seg, year) in rows:
        ai_prob = consensus_probability(predictions_by_ticker.get(ticker, []))
        ai_confidence = consensus_confidence(predictions_by_ticker.get(ticker, []))
        tied = bool(tied)
        games.append({
            "ticker": ticker,
            "event_ticker": event_ticker,
            "league": game_league,
            "game_date": game_date,
            "team": team,
            "opponent": opponent,
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "market_prob": market_prob,
            "ai_prob": ai_prob,
            "ai_confidence": ai_confidence,
            "outcome": outcome,
            "tied": tied,
            "ai_count": ai_count,
            "segment": seg,
            "season_year": year,
            "market_correct": _call_correct(market_prob, outcome, tied),
            "ai_correct": _call_correct(ai_prob, outcome, tied),
            "ai_beat_market": _ai_beat_market(ai_prob, market_prob, outcome),
        })
    return games


def list_game_dates(
    conn: sqlite3.Connection,
    segment: Optional[str] = None,
    season_year: Optional[str] = None,
    league: Optional[str] = None,
) -> list[str]:
    """
    Every distinct game date tracked so far, newest first. Pass segment,
    season_year and/or league to limit the dropdown to dates that actually
    fall in the currently selected part of the season/season/league --
    otherwise picking one of the other filters could leave dates
    selectable that have no games to show.
    """
    season_sql, season_params = _season_filter(segment, season_year, league)
    rows = conn.execute(
        "SELECT DISTINCT game_date FROM predictions WHERE game_date IS NOT NULL"
        + season_sql
        + " ORDER BY game_date DESC",
        season_params,
    ).fetchall()
    return [r[0] for r in rows]


def list_season_years(conn: sqlite3.Connection, league: Optional[str] = None) -> list[str]:
    """
    Every distinct season tracked so far, newest first (e.g. ["2027-28",
    "2026-27"]). NBA/NHL seasons are labeled "2026-27" and NFL/MLB "2026",
    so across every league the list mixes both; pass league to limit it to
    one league's seasons.
    """
    league_sql, league_params = _season_filter(None, None, league)
    rows = conn.execute(
        "SELECT DISTINCT season_year(game_date, league) AS season FROM predictions "
        "WHERE game_date IS NOT NULL" + league_sql + " ORDER BY season DESC",
        league_params,
    ).fetchall()
    return [r[0] for r in rows]


def get_game(conn: sqlite3.Connection, ticker: str) -> Optional[dict]:
    """
    Full detail for one game: its Kalshi odds/outcome, plus every AI's
    prediction next to the market's own price (the "AI vs Kalshi" view).

    Returns None if no prediction was ever recorded for this ticker.
    """
    row = conn.execute(
        "SELECT ticker, event_ticker, league, game_date, team, opponent, yes_bid, yes_ask, "
        "       market_prob, outcome, tied, settled_at, segment(game_date, league), season_year(game_date, league) "
        "FROM predictions WHERE ticker = ? LIMIT 1",
        (ticker,),
    ).fetchone()
    if row is None:
        return None
    (ticker, event_ticker, league, game_date, team, opponent, yes_bid, yes_ask,
     market_prob, outcome, tied, settled_at, seg, year) = row

    predictions = conn.execute(
        "SELECT llm, model, predicted_prob, confidence FROM predictions WHERE ticker = ? ORDER BY llm",
        (ticker,),
    ).fetchall()

    return {
        "ticker": ticker,
        "event_ticker": event_ticker,
        "league": league,
        "game_date": game_date,
        "team": team,
        "opponent": opponent,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "market_prob": market_prob,
        "outcome": outcome,
        "tied": bool(tied),
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
    league: Optional[str] = None,
) -> dict:
    """
    One AI's calibration/accuracy picture, over its settled games. Pass
    segment ("preseason", "regular", "postseason"), season_year (e.g.
    "2026-27") and/or league ("NFL") to scope this -- see
    Tracker.brier_scores() for why segment and league matter.

    - avg_edge_on_win / avg_edge_on_loss: the average (predicted - market)
      edge, split by what actually happened. A skilled AI's edge should
      point the right way *in advance* of the outcome: positive on games
      the team went on to win, negative on games it went on to lose. Edge
      numbers that look similar either way mean the edge isn't actually
      predictive, whatever the raw Brier score says.
    - direction_accuracy: how often the AI's >50%/<50% call matched the
      winner, regardless of margin -- market_direction_accuracy is the
      same thing for Kalshi's own price, for comparison. A predicted_prob
      of exactly 0.50 counts as not having called a side. Ties (NFL only)
      are left out of these and of the won/lost edge averages: with no
      winner, a pick for either side was neither right nor wrong. They
      still count toward the Brier score, as outcome 0 (the team didn't win).

    Any stat computed from zero rows comes back as None, not 0 -- "no data
    yet" and "exactly zero" are different things here.
    """
    import Tracker

    season_sql, season_params = _season_filter(segment, season_year, league)

    total_n, settled_n = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END), 0) "
        "FROM predictions WHERE llm = ?" + season_sql,
        [llm, *season_params],
    ).fetchone()

    (avg_edge_on_win, avg_edge_on_loss, direction_accuracy,
     market_direction_accuracy, avg_confidence) = conn.execute(
        "SELECT "
        "  AVG(CASE WHEN outcome = 1 THEN predicted_prob - market_prob END), "
        "  AVG(CASE WHEN outcome = 0 AND tied IS NOT 1 THEN predicted_prob - market_prob END), "
        "  AVG(CASE WHEN tied = 1 THEN NULL "
        "           WHEN (predicted_prob > 0.5 AND outcome = 1) OR (predicted_prob < 0.5 AND outcome = 0) "
        "       THEN 1.0 ELSE 0.0 END), "
        "  AVG(CASE WHEN tied = 1 THEN NULL "
        "           WHEN (market_prob > 0.5 AND outcome = 1) OR (market_prob < 0.5 AND outcome = 0) "
        "       THEN 1.0 ELSE 0.0 END), "
        "  AVG(confidence) "
        "FROM predictions WHERE llm = ? AND outcome IS NOT NULL" + season_sql,
        [llm, *season_params],
    ).fetchone()

    scores = Tracker.brier_scores(conn, segment=segment, season_year=season_year, league=league).get(llm)
    weight = Tracker.compute_weights(
        conn, segment=segment, season_year=season_year, league=league
    ).get(llm, 1.0)

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
    league: Optional[str] = None,
) -> list[dict]:
    """Every prediction this AI has made, newest game first, with its edge vs the market."""
    season_sql, season_params = _season_filter(segment, season_year, league)

    rows = conn.execute(
        "SELECT ticker, league, game_date, team, opponent, model, predicted_prob, confidence, market_prob, "
        "       outcome, tied, segment(game_date, league), season_year(game_date, league) "
        "FROM predictions WHERE llm = ?" + season_sql + " ORDER BY game_date DESC, ticker",
        [llm, *season_params],
    ).fetchall()
    return [
        {
            "ticker": ticker,
            "league": game_league,
            "game_date": game_date,
            "team": team,
            "opponent": opponent,
            "model": model,
            "predicted_prob": predicted_prob,
            "confidence": confidence,
            "market_prob": market_prob,
            "outcome": outcome,
            "tied": bool(tied),
            "segment": seg,
            "season_year": year,
            "edge": predicted_prob - market_prob,
        }
        for (ticker, game_league, game_date, team, opponent, model, predicted_prob, confidence,
             market_prob, outcome, tied, seg, year) in rows
    ]


def accuracy_summary(
    conn: sqlite3.Connection,
    segment: Optional[str] = None,
    season_year: Optional[str] = None,
    league: Optional[str] = None,
) -> list[dict]:
    """Each AI's Brier score / weight so far, for display alongside the market's own."""
    import Tracker

    scores = Tracker.brier_scores(conn, segment=segment, season_year=season_year, league=league)
    weights = Tracker.compute_weights(conn, segment=segment, season_year=season_year, league=league)
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
