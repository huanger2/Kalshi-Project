"""
webapp/main.py

FastAPI app: browse tracked markets and compare each AI's prediction
against Kalshi's price. See webapp/__init__.py for the bigger picture.

    py -m uvicorn webapp.main:app --reload
    (then open http://127.0.0.1:8000/)
"""

import sqlite3
from pathlib import Path
from typing import Iterator, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import Tracker
from DailyLog import DEFAULT_LLM_NAMES
from webapp import queries

HERE = Path(__file__).parent

app = FastAPI(title="AI vs Kalshi")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))

_STYLE_CSS = HERE / "static" / "style.css"


def _static_version() -> int:
    """
    style.css's own mtime, used as a cache-busting query string (?v=...) on
    its <link> tag -- browsers cache static assets aggressively and we have
    no other signal to tell one to refetch after an edit, so tie the URL
    itself to whether the file actually changed. Read fresh every render
    (not cached at startup) so it's correct without restarting the server.
    """
    return int(_STYLE_CSS.stat().st_mtime)


templates.env.globals["static_version"] = _static_version


def get_db() -> Iterator[sqlite3.Connection]:
    """
    A fresh connection per request, closed when the handler returns --
    sqlite3 connections aren't meant to be shared across threads, and
    uvicorn's default sync handlers each run on their own thread.

    A FastAPI dependency (not a plain helper function) specifically so
    tests can override it via app.dependency_overrides to point at a
    throwaway database instead of the real predictions.db.
    """
    conn = Tracker.connect()
    try:
        yield conn
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
def markets_page(
    request: Request,
    date: Optional[str] = None,
    season: Optional[str] = None,
    segment: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db),
):
    games = queries.list_games(conn, game_date=date, segment=segment, season_year=season)
    dates = queries.list_game_dates(conn)
    seasons = queries.list_season_years(conn)
    accuracy = queries.accuracy_summary(conn, segment=segment, season_year=season)
    return templates.TemplateResponse(
        request, "markets.html",
        {
            "games": games, "dates": dates, "selected_date": date, "accuracy": accuracy,
            "seasons": seasons, "selected_season": season,
            "segments": queries.SEGMENTS, "selected_segment": segment,
            "ai_names": DEFAULT_LLM_NAMES, "active_tab": "markets",
        },
    )


@app.get("/markets/{ticker}", response_class=HTMLResponse)
def market_detail_page(request: Request, ticker: str, conn: sqlite3.Connection = Depends(get_db)):
    game = queries.get_game(conn, ticker)
    if game is None:
        raise HTTPException(status_code=404, detail=f"No tracked predictions for {ticker!r}")
    return templates.TemplateResponse(
        request, "market_detail.html",
        {"game": game, "ai_names": DEFAULT_LLM_NAMES, "active_tab": "markets"},
    )


@app.get("/ai/{name}", response_class=HTMLResponse)
def ai_detail_page(
    request: Request,
    name: str,
    season: Optional[str] = None,
    segment: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db),
):
    # Case-insensitive so /ai/openai works the same as the canonical /ai/OpenAI.
    matched = next((n for n in DEFAULT_LLM_NAMES if n.lower() == name.lower()), None)
    if matched is None:
        raise HTTPException(status_code=404, detail=f"Unknown AI {name!r}")
    stats = queries.ai_stats(conn, matched, segment=segment, season_year=season)
    predictions = queries.ai_predictions(conn, matched, segment=segment, season_year=season)
    seasons = queries.list_season_years(conn)
    return templates.TemplateResponse(
        request, "ai_detail.html",
        {
            "stats": stats, "predictions": predictions,
            "seasons": seasons, "selected_season": season,
            "segments": queries.SEGMENTS, "selected_segment": segment,
            "ai_names": DEFAULT_LLM_NAMES, "active_tab": matched,
        },
    )


# --- JSON, for whatever consumes this next (a JS frontend, a mobile client) ---

@app.get("/api/markets")
def api_markets(
    date: Optional[str] = None,
    season: Optional[str] = None,
    segment: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    return queries.list_games(conn, game_date=date, segment=segment, season_year=season)


@app.get("/api/markets/{ticker}")
def api_market_detail(ticker: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    game = queries.get_game(conn, ticker)
    if game is None:
        raise HTTPException(status_code=404, detail=f"No tracked predictions for {ticker!r}")
    return game


@app.get("/api/accuracy")
def api_accuracy(
    season: Optional[str] = None, segment: Optional[str] = None, conn: sqlite3.Connection = Depends(get_db)
) -> list[dict]:
    return queries.accuracy_summary(conn, segment=segment, season_year=season)


@app.get("/api/ai/{name}")
def api_ai_detail(
    name: str,
    season: Optional[str] = None,
    segment: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    matched = next((n for n in DEFAULT_LLM_NAMES if n.lower() == name.lower()), None)
    if matched is None:
        raise HTTPException(status_code=404, detail=f"Unknown AI {name!r}")
    return {
        "stats": queries.ai_stats(conn, matched, segment=segment, season_year=season),
        "predictions": queries.ai_predictions(conn, matched, segment=segment, season_year=season),
    }
