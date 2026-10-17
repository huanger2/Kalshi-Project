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

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import Leagues
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
# For the nav's per-league tabs on every page. tracked_leagues is called
# fresh each render, so a flipped switch shows up without a restart.
templates.env.globals["league_codes"] = queries.LEAGUES
templates.env.globals["tracked_leagues"] = Leagues.enabled_leagues


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


def _league_or_404(code: str) -> str:
    """The canonical league code for a URL segment (/league/nfl works too), or a 404."""
    if code.upper() not in Leagues.LEAGUES:
        raise HTTPException(status_code=404, detail=f"Unknown league {code!r}")
    return code.upper()


def _render_markets(
    request: Request,
    conn: sqlite3.Connection,
    league: Optional[str],
    date: Optional[str],
    season: Optional[str],
    segment: Optional[str],
):
    """The markets table: every league's games (league=None) or one league's tab."""
    games = queries.list_games(conn, game_date=date, segment=segment, season_year=season, league=league)
    dates = queries.list_game_dates(conn, segment=segment, season_year=season, league=league)
    seasons = queries.list_season_years(conn, league=league)
    accuracy = queries.accuracy_summary(conn, segment=segment, season_year=season, league=league)
    return templates.TemplateResponse(
        request, "markets.html",
        {
            "games": games, "dates": dates, "selected_date": date, "accuracy": accuracy,
            "selected_league": league,
            "seasons": seasons, "selected_season": season,
            "segments": queries.SEGMENTS, "selected_segment": segment,
            "ai_names": DEFAULT_LLM_NAMES, "active_tab": league or "markets",
        },
    )


@app.get("/", response_class=HTMLResponse)
def markets_page(
    request: Request,
    date: Optional[str] = None,
    season: Optional[str] = None,
    segment: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db),
):
    return _render_markets(request, conn, None, date, season, segment)


@app.get("/league/{code}", response_class=HTMLResponse)
def league_markets_page(
    request: Request,
    code: str,
    date: Optional[str] = None,
    season: Optional[str] = None,
    segment: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db),
):
    return _render_markets(request, conn, _league_or_404(code), date, season, segment)


@app.post("/league/{code}/tracking")
def set_league_tracking(code: str, enabled: str = Form(...)):
    """
    The league tab's tracking switch: adds/removes the league from
    leagues.json's "enabled" list, which DailyLog.py reads on its next run.
    A plain form POST + redirect back, so it works without any JavaScript.
    """
    league = _league_or_404(code)
    Leagues.set_enabled(league, enabled == "1")
    return RedirectResponse(f"/league/{league}", status_code=303)


@app.get("/markets/{ticker}", response_class=HTMLResponse)
def market_detail_page(request: Request, ticker: str, conn: sqlite3.Connection = Depends(get_db)):
    game = queries.get_game(conn, ticker)
    if game is None:
        raise HTTPException(status_code=404, detail=f"No tracked predictions for {ticker!r}")
    return templates.TemplateResponse(
        request, "market_detail.html",
        {"game": game, "ai_names": DEFAULT_LLM_NAMES, "active_tab": game["league"] or "markets"},
    )


@app.get("/ai/{name}", response_class=HTMLResponse)
def ai_detail_page(
    request: Request,
    name: str,
    season: Optional[str] = None,
    segment: Optional[str] = None,
    league: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db),
):
    # Case-insensitive so /ai/openai works the same as the canonical /ai/OpenAI.
    matched = next((n for n in DEFAULT_LLM_NAMES if n.lower() == name.lower()), None)
    if matched is None:
        raise HTTPException(status_code=404, detail=f"Unknown AI {name!r}")
    stats = queries.ai_stats(conn, matched, segment=segment, season_year=season, league=league)
    predictions = queries.ai_predictions(conn, matched, segment=segment, season_year=season, league=league)
    seasons = queries.list_season_years(conn, league=league)
    return templates.TemplateResponse(
        request, "ai_detail.html",
        {
            "stats": stats, "predictions": predictions,
            "leagues": queries.LEAGUES, "selected_league": league,
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
    league: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    return queries.list_games(conn, game_date=date, segment=segment, season_year=season, league=league)


@app.get("/api/markets/{ticker}")
def api_market_detail(ticker: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    game = queries.get_game(conn, ticker)
    if game is None:
        raise HTTPException(status_code=404, detail=f"No tracked predictions for {ticker!r}")
    return game


@app.get("/api/accuracy")
def api_accuracy(
    season: Optional[str] = None,
    segment: Optional[str] = None,
    league: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> list[dict]:
    return queries.accuracy_summary(conn, segment=segment, season_year=season, league=league)


@app.get("/api/ai/{name}")
def api_ai_detail(
    name: str,
    season: Optional[str] = None,
    segment: Optional[str] = None,
    league: Optional[str] = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    matched = next((n for n in DEFAULT_LLM_NAMES if n.lower() == name.lower()), None)
    if matched is None:
        raise HTTPException(status_code=404, detail=f"Unknown AI {name!r}")
    return {
        "stats": queries.ai_stats(conn, matched, segment=segment, season_year=season, league=league),
        "predictions": queries.ai_predictions(conn, matched, segment=segment, season_year=season, league=league),
    }
