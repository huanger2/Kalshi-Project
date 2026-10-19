"""
webapp/main.py

FastAPI app: browse tracked markets and compare each AI's prediction
against Kalshi's price. See webapp/__init__.py for the bigger picture.

    py -m uvicorn webapp.main:app --reload
    (then open http://127.0.0.1:8000/)
"""

import html
import os
import sqlite3
import sys
import tempfile
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import quote

from starlette.background import BackgroundTask

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import Accounts
import AIWeights
import CostTracker
import Leagues
import Tracker
from DailyLog import DEFAULT_LLM_NAMES
from webapp import account, analytics, auth, betting, daily_run, data_files, queries, settings, suggestions, users

HERE = Path(__file__).parent

app = FastAPI(title="AI vs Kalshi")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))


def _storage_problem() -> Optional[str]:
    import DataDir
    return DataDir.problem(Tracker.DEFAULT_DB)


# Say so at startup, in terms of what to fix, rather than leaving only
# SQLite's bare "unable to open database file" on every request.
if _storage_problem():
    print(f"ERROR: the prediction database can't be opened. {_storage_problem()}", file=sys.stderr, flush=True)


@app.exception_handler(sqlite3.OperationalError)
async def database_unavailable(request: Request, exc: sqlite3.OperationalError):
    problem = _storage_problem()
    if problem is None:  # some other database error: a plain 500, with the traceback in the logs
        traceback.print_exception(exc)
        return PlainTextResponse("Internal Server Error", status_code=500)
    message = f"The prediction database can't be opened. {problem}"
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": message}, status_code=503)
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>Database unavailable - AI vs Kalshi</title>"
        "<body style='font-family: system-ui, sans-serif; max-width: 40rem; margin: 3rem auto; padding: 0 1rem'>"
        f"<h1>Database unavailable</h1><p>{html.escape(message)}</p></body>",
        status_code=503,
    )

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

# Reachable before choosing to sign in or continue as guest. /api/ stays
# open too: it's read-only JSON, and Railway's health check polls it.
# /internal/ is for the cron service, which has its own token instead.
_OPEN_PATHS = {"/login", "/guest", "/logout"}
_OPEN_PREFIXES = ("/static/", "/api/", "/internal/")


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


@contextmanager
def _db():
    """A database connection outside a route (the middleware), from get_db -- or the tests' override of it."""
    factory = app.dependency_overrides.get(get_db, get_db)
    gen = factory()
    try:
        yield next(gen)
    finally:
        gen.close()


# Where a user who was given a temporary password is sent until they pick their own.
_PASSWORD_PAGE = "/settings"


@app.middleware("http")
async def require_sign_in_or_guest(request: Request, call_next):
    """
    Records who's viewing for routes and templates -- request.state.user
    (the signed-in user's dict, or None) and request.state.role ("admin",
    "user", "guest" or None) -- and sends a first-time visitor's page
    request to the login page, which remembers where they were headed.
    """
    value = request.cookies.get(auth.COOKIE)
    user = None
    if value and not auth.is_guest(value):
        try:
            with _db() as conn:
                user = auth.signed_in_user(conn, value)
        except sqlite3.OperationalError as exc:
            return await database_unavailable(request, exc)
    request.state.user = user
    request.state.role = auth.role_of(user, value)

    path = request.url.path
    is_open = path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES)
    if request.state.role is None and not is_open and request.method == "GET":
        target = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
    if (user and user["must_change_password"] and request.method == "GET" and not is_open
            and path != _PASSWORD_PAGE):
        return RedirectResponse(f"{_PASSWORD_PAGE}#account", status_code=303)
    return await call_next(request)


def _set_session(response: RedirectResponse, request: Request, value: str, max_age: Optional[int]) -> None:
    # Secure whenever the site is reached over HTTPS -- Railway terminates TLS
    # at its proxy, so the app itself sees plain HTTP plus this header.
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(auth.COOKIE, value, max_age=max_age, httponly=True, samesite="lax", secure=https)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: Optional[str] = None):
    if request.state.user is not None:
        return RedirectResponse(auth.safe_next(next), status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next": auth.safe_next(next), "error": None, "username": ""}
    )


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
    conn: sqlite3.Connection = Depends(get_db),
):
    def refuse(error: str, status_code: int):
        return templates.TemplateResponse(
            request, "login.html", {"next": auth.safe_next(next), "error": error, "username": username},
            status_code=status_code)

    client = request.client.host if request.client else "?"
    if auth.throttled(username, client):
        return refuse("Too many failed sign-ins. Wait a few minutes and try again.", 429)
    Accounts.bootstrap(conn)  # the very first sign-in creates the admin from USERNAME/PASSWORD
    user = Accounts.authenticate(conn, username, password)
    if user is None:
        auth.record_failure(username, client)
        if not Accounts.count_users(conn):
            return refuse("There are no accounts on this server yet. Set USERNAME and PASSWORD on the server "
                          "to create the first admin. You can still continue as a guest.", 401)
        return refuse("Incorrect username or password.", 401)
    auth.clear_failures(username, client)
    response = RedirectResponse(auth.safe_next(next), status_code=303)
    _set_session(response, request, auth.user_cookie(conn, user), auth.MAX_AGE)
    return response


@app.post("/guest")
def continue_as_guest(request: Request, next: str = Form("/")):
    response = RedirectResponse(auth.safe_next(next), status_code=303)
    _set_session(response, request, auth.GUEST, None)  # a browser-session cookie
    return response


@app.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(auth.COOKIE)
    return response


def _require_admin(request: Request) -> dict:
    if request.state.role != auth.ADMIN:
        raise HTTPException(status_code=403, detail="Only an admin can do that")
    return request.state.user


def _require_user(request: Request) -> dict:
    """The signed-in user (admin or not); guests get a 403."""
    if request.state.user is None:
        raise HTTPException(status_code=403, detail="Sign in to do that")
    return request.state.user


def _viewer_weights(request: Request, conn: sqlite3.Connection) -> dict:
    """The AI weights behind the "AI odds" this viewer sees: their own, or the site defaults for a guest."""
    user = request.state.user
    return AIWeights.for_user(conn, user["id"] if user else None)


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
    games = queries.list_games(conn, game_date=date, segment=segment, season_year=season, league=league,
                               weights=_viewer_weights(request, conn))
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
def set_league_tracking(request: Request, code: str, enabled: str = Form(...)):
    """
    The league tab's tracking switch: adds/removes the league from
    leagues.json's "enabled" list, which DailyLog.py reads on its next run.
    A plain form POST + redirect back, so it works without any JavaScript.
    Admins only -- guests never see the switch, and can't post to it either.
    """
    if request.state.role != auth.ADMIN:
        raise HTTPException(status_code=403, detail="Sign in to change which leagues are tracked")
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


# --- Settings: a user's own (password, AI weights, Kalshi keys; see webapp/account.py) ---

def _render_account(request: Request, conn: sqlite3.Connection, saved: Optional[str] = None,
                    errors: Optional[dict] = None, checked: Optional[dict] = None, status_code: int = 200):
    user = request.state.user
    return templates.TemplateResponse(
        request, "account.html",
        {"a": account.view(conn, user), "saved": saved, "errors": errors or {}, "checked": checked,
         "ai_names": DEFAULT_LLM_NAMES, "active_tab": "settings"},
        status_code=status_code,
    )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: Optional[str] = None, conn: sqlite3.Connection = Depends(get_db)):
    _require_user(request)
    return _render_account(request, conn, saved=saved)


# These read the whole form, so they're async -- and so open their own
# connection (_db()) on the event loop's thread: a Depends(get_db)
# connection is made on a worker thread, and SQLite won't share it.

@app.post("/settings/password", response_class=HTMLResponse)
async def change_password(request: Request):
    user = _require_user(request)
    form = dict(await request.form())
    with _db() as conn:
        try:
            account.change_password(conn, user, form)
        except settings.SettingsError as e:
            return _render_account(request, conn, errors={"account": e.errors}, status_code=400)
        # The change signed out every session, this one included: sign it back in.
        response = RedirectResponse("/settings?saved=account#account", status_code=303)
        _set_session(response, request, auth.user_cookie(conn, Accounts.get_user(conn, user["id"])), auth.MAX_AGE)
    return response


@app.post("/settings/weights", response_class=HTMLResponse)
async def save_own_weights(request: Request):
    user = _require_user(request)
    form = dict(await request.form())
    with _db() as conn:
        try:
            account.save_weights(conn, user["id"], form)
        except settings.SettingsError as e:
            return _render_account(request, conn, errors={"weights": e.errors}, status_code=400)
    return RedirectResponse("/settings?saved=weights#weights", status_code=303)


@app.post("/settings/weights/reset")
def reset_own_weights(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    user = _require_user(request)
    account.reset_weights(conn, user["id"])
    return RedirectResponse("/settings?saved=weights#weights", status_code=303)


@app.post("/settings/kalshi/{env}", response_class=HTMLResponse)
async def save_kalshi_key(
    request: Request,
    env: str,
    key_id: str = Form(""),
    private_key: str = Form(""),
    key_file: Optional[UploadFile] = File(None),
):
    """Save a Kalshi key: the private key pasted, or its .key file uploaded."""
    user = _require_user(request)
    if key_file is not None and key_file.filename:
        private_key = (await key_file.read()).decode("utf-8", errors="replace")
    with _db() as conn:
        try:
            account.save_kalshi_key(conn, user["id"], env, key_id, private_key)
        except settings.SettingsError as e:
            return _render_account(request, conn, errors={f"kalshi-{env}": e.errors}, status_code=400)
    return RedirectResponse(f"/settings?saved=kalshi-{env}#kalshi", status_code=303)


@app.post("/settings/kalshi/{env}/check", response_class=HTMLResponse)
def check_kalshi_key(request: Request, env: str, conn: sqlite3.Connection = Depends(get_db)):
    """Ask Kalshi for the account's balance with the saved key: proof it works."""
    user = _require_user(request)
    balance, error = account.check_kalshi_key(conn, user["id"], env)
    return _render_account(request, conn, checked={"env": env, "balance": balance, "error": error})


@app.post("/settings/kalshi/{env}/delete")
def delete_kalshi_key(request: Request, env: str, conn: sqlite3.Connection = Depends(get_db)):
    user = _require_user(request)
    Accounts.delete_kalshi_key(conn, user["id"], env)
    return RedirectResponse(f"/settings?saved=kalshi-{env}-removed#kalshi", status_code=303)


# --- Admin: site-wide settings (the JSON config files; see webapp/settings.py) ---

def _render_settings(request: Request, saved: Optional[str] = None, errors: Optional[dict] = None,
                     status_code: int = 200, suggested: Optional[dict] = None,
                     suggest_error: Optional[str] = None, applied: Optional[str] = None):
    return templates.TemplateResponse(
        request, "settings.html",
        {"s": settings.view(), "saved": saved, "errors": errors or {},
         "suggested": suggested, "suggest_error": suggest_error, "applied": applied,
         "ai_names": DEFAULT_LLM_NAMES, "active_tab": "admin", "admin_tab": "settings"},
        status_code=status_code,
    )


@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings_page(request: Request, saved: Optional[str] = None, applied: Optional[str] = None):
    _require_admin(request)
    return _render_settings(request, saved=saved if saved in settings.SAVERS else None,
                            applied=applied[:200] if applied else None)


@app.post("/admin/settings/suggestions", response_class=HTMLResponse)
def ask_for_suggestions(request: Request):
    """One Gemini call (with Google Search) for proposed updates, shown for review -- nothing is saved."""
    _require_admin(request)
    try:
        result = suggestions.fetch()
    except Exception as e:  # a bad reply or an API failure: tell the admin, don't 500
        return _render_settings(request, suggest_error=f"Couldn't get suggestions from Gemini: {e}", status_code=502)
    return _render_settings(request, suggested=result)


@app.post("/admin/settings/suggestions/apply", response_class=HTMLResponse)
async def apply_suggestions(request: Request):
    _require_admin(request)
    saved, errors = suggestions.apply(dict(await request.form()))
    if errors:
        return _render_settings(request, suggest_error="Some suggestions weren't applied: " + "; ".join(errors),
                                applied=", ".join(saved) or None, status_code=400)
    return RedirectResponse(f"/admin/settings?applied={quote(', '.join(saved) or 'nothing')}", status_code=303)


@app.post("/admin/settings/{section}", response_class=HTMLResponse)
async def save_settings(request: Request, section: str):
    _require_admin(request)
    saver = settings.SAVERS.get(section)
    if saver is None:
        raise HTTPException(status_code=404, detail=f"No settings section {section!r}")
    try:
        saver(dict(await request.form()))
    except settings.SettingsError as e:
        return _render_settings(request, errors={section: e.errors}, status_code=400)
    return RedirectResponse(f"/admin/settings?saved={section}#{section}", status_code=303)


@app.post("/admin/settings/{section}/reset")
def reset_settings(request: Request, section: str):
    """Hosted only: drop the volume copy so the deployed file is in effect again."""
    _require_admin(request)
    path = settings.paths().get(section)
    if path is None:
        raise HTTPException(status_code=404, detail=f"No settings section {section!r}")
    import ConfigFiles
    ConfigFiles.reset(path)
    return RedirectResponse(f"/admin/settings?saved={section}#{section}", status_code=303)


# --- Admin: users (see webapp/users.py) ---

def _render_users(request: Request, conn: sqlite3.Connection, message: Optional[str] = None,
                  error: Optional[str] = None, new_password: Optional[dict] = None, status_code: int = 200):
    return templates.TemplateResponse(
        request, "admin_users.html",
        {"users": users.view(conn), "message": message, "error": error, "new_password": new_password,
         "ai_names": DEFAULT_LLM_NAMES, "active_tab": "admin", "admin_tab": "users"},
        status_code=status_code,
    )


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    _require_admin(request)
    return _render_users(request, conn)


@app.post("/admin/users", response_class=HTMLResponse)
def add_user(request: Request, username: str = Form(""), is_admin: str = Form(""),
             conn: sqlite3.Connection = Depends(get_db)):
    _require_admin(request)
    try:
        user, password = users.create(conn, username, is_admin == "yes")
    except Accounts.AccountError as e:
        return _render_users(request, conn, error=str(e), status_code=400)
    return _render_users(request, conn, new_password={"username": user["username"], "password": password,
                                                      "created": True})


@app.post("/admin/users/{user_id}/password", response_class=HTMLResponse)
def reset_user_password(request: Request, user_id: int, conn: sqlite3.Connection = Depends(get_db)):
    _require_admin(request)
    password = users.reset_password(conn, user_id)
    if password is None:
        raise HTTPException(status_code=404, detail="No such user")
    return _render_users(request, conn, new_password={"username": Accounts.get_user(conn, user_id)["username"],
                                                      "password": password, "created": False})


@app.post("/admin/users/{user_id}/admin", response_class=HTMLResponse)
def set_user_admin(request: Request, user_id: int, is_admin: str = Form(""),
                   conn: sqlite3.Connection = Depends(get_db)):
    _require_admin(request)
    try:
        Accounts.set_admin(conn, user_id, is_admin == "yes")
    except Accounts.AccountError as e:
        return _render_users(request, conn, error=str(e), status_code=400)
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{user_id}/delete", response_class=HTMLResponse)
def remove_user(request: Request, user_id: int, conn: sqlite3.Connection = Depends(get_db)):
    me = _require_admin(request)
    if user_id == me["id"]:
        return _render_users(request, conn, error="You can't remove your own account.", status_code=400)
    try:
        Accounts.delete_user(conn, user_id)
    except Accounts.AccountError as e:
        return _render_users(request, conn, error=str(e), status_code=400)
    return RedirectResponse("/admin/users", status_code=303)


# --- Betting tab: the signed-in user's own rules and bets (see webapp/betting.py) ---

def _render_betting(request: Request, conn: sqlite3.Connection, saved: Optional[str] = None,
                    errors: Optional[list] = None, status_code: int = 200, shown: Optional[str] = None):
    return templates.TemplateResponse(
        request, "betting.html",
        {"b": betting.view(conn, request.state.user, shown), "message": betting.flash(saved),
         "errors": errors or [], "ai_names": DEFAULT_LLM_NAMES, "active_tab": "betting"},
        status_code=status_code,
    )


@app.get("/betting", response_class=HTMLResponse)
def betting_page(request: Request, saved: Optional[str] = None, bets: Optional[str] = None,
                 conn: sqlite3.Connection = Depends(get_db)):
    """`bets` picks which of the user's bets the page totals: paper, demo or live (default: the mode in effect)."""
    _require_user(request)
    return _render_betting(request, conn, saved=saved, shown=bets)


@app.post("/betting/rules", response_class=HTMLResponse)
def save_betting_rules(
    request: Request,
    mode: str = Form(""),
    min_edge: str = Form(""),
    stake_usd: str = Form(""),
    max_daily_usd: str = Form(""),
    min_price: str = Form(""),
    max_price: str = Form(""),
    min_ais: str = Form(""),
    leagues: list[str] = Form([]),
    segments: list[str] = Form([]),
    confirm_live: str = Form(""),
    conn: sqlite3.Connection = Depends(get_db),
):
    user = _require_user(request)
    try:
        betting.save_config(conn, user["id"], {
            "mode": mode, "min_edge": min_edge, "stake_usd": stake_usd, "max_daily_usd": max_daily_usd,
            "min_price": min_price, "max_price": max_price, "min_ais": min_ais, "leagues": leagues,
            "segments": segments, "confirm_live": confirm_live})
    except settings.SettingsError as e:
        return _render_betting(request, conn, errors=e.errors, status_code=400)
    return RedirectResponse("/betting?saved=rules#rules", status_code=303)


# --- Analytics tab: calibration, edge vs returns, and the optimizer (see webapp/analytics.py) ---

def _render_analytics(request: Request, conn: sqlite3.Connection, message: Optional[str] = None,
                      errors: Optional[list] = None, status_code: int = 200, segments: Optional[list] = None):
    return templates.TemplateResponse(
        request, "analytics.html",
        {"a": analytics.view(conn, request.state.user, segments), "message": message, "errors": errors or [],
         "ai_names": DEFAULT_LLM_NAMES, "active_tab": "analytics"},
        status_code=status_code,
    )


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, applied: Optional[str] = None, ran: Optional[str] = None,
                   segment: list[str] = Query([]), conn: sqlite3.Connection = Depends(get_db)):
    """`segment` (repeatable) picks the parts of the season analyzed; none: the ones the user bets on."""
    _require_user(request)
    return _render_analytics(request, conn, message=analytics.flash(applied and applied[:100], ran),
                             segments=segment)


def _analytics_url(segments: list[str], **params) -> str:
    query = "&".join([f"segment={quote(s)}" for s in segments] + [f"{k}={quote(v)}" for k, v in params.items()])
    return f"/analytics?{query}#optimizer"


@app.post("/analytics/run", response_class=HTMLResponse)
def run_optimizer(request: Request, segment: list[str] = Form([]), conn: sqlite3.Connection = Depends(get_db)):
    user = _require_user(request)
    try:
        result = analytics.run(conn, user, segment)
    except settings.SettingsError as e:
        return _render_analytics(request, conn, errors=e.errors, status_code=400, segments=segment)
    return RedirectResponse(_analytics_url(result["segments"], ran="1"), status_code=303)


@app.post("/analytics/apply", response_class=HTMLResponse)
def apply_optimized(request: Request, which: str = Form(""), conn: sqlite3.Connection = Depends(get_db)):
    user = _require_user(request)
    try:
        applied = analytics.apply(conn, user, which)
    except settings.SettingsError as e:
        return _render_analytics(request, conn, errors=e.errors, status_code=400)
    return RedirectResponse(_analytics_url(analytics.run_segments(conn, user), applied=" and ".join(applied)),
                            status_code=303)


# --- Admin: data -- move history in, get generated spreadsheets out ---

def _render_data_page(request: Request, message: Optional[str] = None, error: Optional[str] = None,
                      status_code: int = 200):
    files = data_files.listing()
    day_logs = [f for f in files if f["name"].startswith("daily_logs/")]  # newest first, one dropdown
    spend = CostTracker.usage_summary(data_files.db_path())  # api_costs.xlsx's provider_summary sheet
    return templates.TemplateResponse(
        request, "admin_data.html",
        {"files": [f for f in files if f not in day_logs], "day_logs": day_logs, "spend": spend,
         "run": daily_run.status(), "message": message, "error": error,
         "ai_names": DEFAULT_LLM_NAMES, "active_tab": "admin", "admin_tab": "data"},
        status_code=status_code,
    )


@app.get("/admin/data", response_class=HTMLResponse)
def admin_data_page(request: Request):
    _require_admin(request)
    return _render_data_page(request)


@app.get("/admin/data/download/predictions.xlsx")
def download_predictions_export(request: Request):
    """Every prediction as a spreadsheet (Export.py), built fresh from predictions.db for each download."""
    _require_admin(request)
    import Export

    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    Export.export_to_excel(db_path=data_files.db_path(), out_path=path)
    return FileResponse(path, filename="predictions.xlsx", background=BackgroundTask(os.remove, path))


@app.get("/admin/data/download")
def download_data_file_by_query(request: Request, name: str):
    """The daily log dropdown's form submits here (?name=daily_logs/2026-10-04.xlsx)."""
    return download_data_file(request, name)


@app.get("/admin/data/download/{name:path}")
def download_data_file(request: Request, name: str):
    _require_admin(request)
    path = data_files.resolve_download(name)
    if path is None:
        raise HTTPException(status_code=404, detail=f"No data file {name!r}")
    if name == "predictions.db":
        # The game history only: accounts, password hashes and Kalshi keys stay on the server.
        fd, copy = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        data_files.sanitized_db_copy(copy)
        return FileResponse(copy, filename="predictions.db", background=BackgroundTask(os.remove, copy))
    return FileResponse(path, filename=os.path.basename(path))


@app.post("/admin/data/predictions-db", response_class=HTMLResponse)
async def upload_predictions_db(request: Request, file: UploadFile = File(...)):
    _require_admin(request)
    if daily_run.status()["state"] == "running":
        return _render_data_page(request, error="A daily run is writing to the database right now; "
                                                "try again once it finishes.", status_code=409)
    try:
        result = data_files.replace_database(await file.read())
    except data_files.UploadError as e:
        return _render_data_page(request, error=str(e), status_code=400)
    backup = f" The previous database was kept as {result['backup']}." if result["backup"] else ""
    return _render_data_page(request, message=f"Database replaced: {result['rows']} prediction rows.{backup}")


@app.post("/admin/data/daily-logs", response_class=HTMLResponse)
async def upload_daily_logs(request: Request, files: list[UploadFile] = File(...)):
    _require_admin(request)
    result = data_files.add_daily_logs([(f.filename, await f.read()) for f in files])
    parts = []
    if result["added"]:
        parts.append(f"Added {len(result['added'])} day file(s).")
    if result["skipped"]:
        parts.append(f"Skipped {', '.join(result['skipped'])}: already on the server.")
    error = (f"Rejected {', '.join(result['rejected'])}: not a daily log named like 2026-10-04.xlsx."
             if result["rejected"] else None)
    return _render_data_page(request, message=" ".join(parts) or None, error=error)


# --- For Railway's cron service (trigger_daily_run.py); see webapp/daily_run.py ---

def _require_daily_run_token(request: Request) -> None:
    # Every refusal is logged, so the web service's logs show a misconfigured
    # cron service's attempts instead of nothing.
    if not daily_run.is_configured():
        print(f"refused {request.method} /internal/daily-run: DAILY_RUN_TOKEN isn't set on this service", flush=True)
        raise HTTPException(status_code=503, detail="DAILY_RUN_TOKEN isn't set on this server")
    if not daily_run.token_ok(request.headers.get("authorization")):
        sent = "no Authorization header" if not request.headers.get("authorization") else "a token that doesn't match"
        print(f"refused {request.method} /internal/daily-run: sent {sent} DAILY_RUN_TOKEN", flush=True)
        raise HTTPException(status_code=401, detail="Bad or missing daily-run token")


@app.post("/internal/daily-run", status_code=202)
def start_daily_run(request: Request):
    """Kick off DailyLog.run() in the background; 409 if one is already going."""
    _require_daily_run_token(request)
    run_id = daily_run.start()
    print(f"daily run requested: {'started run #' + str(run_id) if run_id else 'one is already in progress'}",
          flush=True)
    if run_id is None:
        return JSONResponse({"detail": "A daily run is already in progress", **daily_run.status()}, status_code=409)
    return daily_run.status()


@app.get("/internal/daily-run")
def daily_run_status(request: Request):
    """How the latest daily run went (the cron service polls this until it finishes)."""
    _require_daily_run_token(request)
    return daily_run.status()
