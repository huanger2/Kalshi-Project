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
def markets_page(request: Request, date: Optional[str] = None, conn: sqlite3.Connection = Depends(get_db)):
    games = queries.list_games(conn, game_date=date)
    dates = queries.list_game_dates(conn)
    accuracy = queries.accuracy_summary(conn)
    return templates.TemplateResponse(
        request, "markets.html",
        {
            "games": games, "dates": dates, "selected_date": date, "accuracy": accuracy,
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
def ai_detail_page(request: Request, name: str, conn: sqlite3.Connection = Depends(get_db)):
    # Case-insensitive so /ai/openai works the same as the canonical /ai/OpenAI.
    matched = next((n for n in DEFAULT_LLM_NAMES if n.lower() == name.lower()), None)
    if matched is None:
        raise HTTPException(status_code=404, detail=f"Unknown AI {name!r}")
    stats = queries.ai_stats(conn, matched)
    predictions = queries.ai_predictions(conn, matched)
    return templates.TemplateResponse(
        request, "ai_detail.html",
        {
            "stats": stats, "predictions": predictions,
            "ai_names": DEFAULT_LLM_NAMES, "active_tab": matched,
        },
    )


# --- JSON, for whatever consumes this next (a JS frontend, a mobile client) ---

@app.get("/api/markets")
def api_markets(date: Optional[str] = None, conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return queries.list_games(conn, game_date=date)


@app.get("/api/markets/{ticker}")
def api_market_detail(ticker: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    game = queries.get_game(conn, ticker)
    if game is None:
        raise HTTPException(status_code=404, detail=f"No tracked predictions for {ticker!r}")
    return game


@app.get("/api/accuracy")
def api_accuracy(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    return queries.accuracy_summary(conn)


@app.get("/api/ai/{name}")
def api_ai_detail(name: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    matched = next((n for n in DEFAULT_LLM_NAMES if n.lower() == name.lower()), None)
    if matched is None:
        raise HTTPException(status_code=404, detail=f"Unknown AI {name!r}")
    return {"stats": queries.ai_stats(conn, matched), "predictions": queries.ai_predictions(conn, matched)}
