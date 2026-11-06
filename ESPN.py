"""
ESPN.py

ESPN's own win probability for a game -- the "Matchup Predictor" on its
game pages, from its BPI (NBA) and FPI (NFL) models -- logged as one more
source next to the AIs, under the name "ESPN". It's free and needs no key:
it reads ESPN's public site API (the JSON behind espn.com, undocumented, so
it could change without notice).

  scoreboard  site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard?dates=YYYYMMDD
              each game's ESPN id, start time and teams
  summary     .../summary?event={id}  -> "predictor": each team's gameProjection (win %)
              until the game starts; after that, "winprobability" -- ESPN's in-game
              chart, whose first point (the opening play) carries its pre-game number

A Kalshi market is matched to ESPN's game by its teams: the event ticker
(KXNBAGAME-26NOV09SASPHX) names the day and both teams, the market ticker's
last part (-SAS) the team the market is about. ESPN has no predictor for
NHL or MLB, so those games just have no ESPN prediction.

ESPN doesn't rate its confidence the way the AIs are asked to, so every
ESPN prediction is logged at CONFIDENCE. The predictions are combined
confidence-weighted (EVCalculator.consensus_probability()), so that fixes
how much ESPN counts next to the AIs at equal weights; each user's ESPN
weight is the real dial.

Start times come from the scoreboard too (start_time()), for betting a set
time before each game (Betting.py, webapp/pregame.py).

Games logged before ESPN was added (or when ESPN didn't answer) get their
ESPN prediction afterwards from backfill(), which the daily run calls --
also by hand: py ESPN.py backfill. A finished game's predictor is gone, so
its number is the win-probability chart's opening point. That tracks the
pre-game number closely (an NFL check: 56.8% at the opening play vs. 56.6%
projected; over 123 NBA regular-season games it correlates 0.945 with
Kalshi's closing price) but is taken at tip-off, a few hours later than a
morning daily run would see it. Preseason games are skipped: ESPN doesn't
rate preseason matchups (every one opens at the same 50.9%).
"""

import datetime
import sqlite3
import sys
import time
from typing import Callable, Optional

import requests

import Leagues

BASE = "https://site.api.espn.com/apis/site/v2/sports"
NAME = "ESPN"
MODEL = "espn-matchup-predictor"  # what the "model" column records (AISettings.get_model("espn"))
CONFIDENCE = 0.5

SPORTS = {"NBA": "basketball/nba", "NFL": "football/nfl", "NHL": "hockey/nhl", "MLB": "baseball/mlb"}
# ESPN's abbreviation -> Kalshi's, where they differ.
ALIASES = {
    "NBA": {"NY": "NYK", "SA": "SAS", "GS": "GSW", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS"},
    "NFL": {"WSH": "WAS", "JAX": "JAC"},
    "NHL": {},
    "MLB": {},
}
# Leagues whose games have a Matchup Predictor; elsewhere ESPN isn't asked.
PREDICTOR_LEAGUES = {"NBA", "NFL"}

SCOREBOARD_TTL = 3600.0  # seconds a day's scoreboard is reused (start times rarely move)
_scoreboards: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_MONTHS = {m: i for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                                        "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


class NoPrediction(Exception):
    """ESPN has nothing for this market (no predictor in its league, or the game wasn't found)."""


def _get(url: str, params: dict) -> dict:
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def parse_market(market: dict) -> Optional[dict]:
    """
    {"league", "day", "team", "opponent", "team_is_home"} from a Kalshi game
    market's tickers (Kalshi lists the away team first), or None if they
    don't look like a game.
    """
    ticker, event = market.get("ticker") or "", market.get("event_ticker") or ""
    league = Leagues.league_for_ticker(ticker)
    parts = event.split("-")
    if league not in SPORTS or len(parts) < 2 or len(parts[1]) < 9:
        return None
    code = parts[1]
    try:
        day = datetime.date(2000 + int(code[:2]), _MONTHS[code[2:5]], int(code[5:7]))
    except (KeyError, ValueError):
        return None
    pair, team = code[7:], ticker.split("-")[-1]
    if pair.startswith(team) and len(pair) > len(team):
        return {"league": league, "day": day, "team": team, "opponent": pair[len(team):], "team_is_home": False}
    if pair.endswith(team) and len(pair) > len(team):
        return {"league": league, "day": day, "team": team, "opponent": pair[:-len(team)], "team_is_home": True}
    return None


def scoreboard(league: str, day: datetime.date, fetch: Callable = _get, now: Optional[float] = None) -> list[dict]:
    """
    ESPN's games for a league on a day: [{"id", "start" (aware UTC datetime),
    "home", "away" (Kalshi team codes)}], cached for SCOREBOARD_TTL.
    """
    now = time.time() if now is None else now
    key = (league, day.isoformat())
    cached = _scoreboards.get(key)
    if cached and now - cached[0] < SCOREBOARD_TTL:
        return cached[1]
    data = fetch(f"{BASE}/{SPORTS[league]}/scoreboard", {"dates": day.strftime("%Y%m%d")})
    aliases = ALIASES.get(league, {})
    games = []
    for event in data.get("events", []):
        competition = (event.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): aliases.get(c["team"]["abbreviation"], c["team"]["abbreviation"])
                 for c in competition.get("competitors", []) if c.get("team")}
        start = event.get("date")
        games.append({"id": event.get("id"), "home": teams.get("home"), "away": teams.get("away"),
                      "start": datetime.datetime.fromisoformat(start.replace("Z", "+00:00")) if start else None})
    _scoreboards[key] = (now, games)
    return games


def find_game(market: dict, fetch: Callable = _get) -> Optional[tuple[dict, dict]]:
    """(parse_market(market), ESPN's game) for a Kalshi game market, or None if ESPN doesn't list it."""
    parsed = parse_market(market)
    if parsed is None:
        return None
    home, away = ((parsed["team"], parsed["opponent"]) if parsed["team_is_home"]
                  else (parsed["opponent"], parsed["team"]))
    for game in scoreboard(parsed["league"], parsed["day"], fetch):
        if game["home"] == home and game["away"] == away:
            return parsed, game
    return None


def start_time(market: dict, fetch: Callable = _get) -> Optional[datetime.datetime]:
    """When the market's game starts (aware, UTC), per ESPN; None if ESPN can't be reached or doesn't list it."""
    try:
        found = find_game(market, fetch)
    except Exception as e:
        print(f"ESPN: couldn't look up {market.get('ticker')}'s start time: {e}")
        return None
    return found[1]["start"] if found else None


def win_probability(market: dict, fetch: Callable = _get) -> float:
    """
    ESPN's chance (0-1) that the market's team (its YES side) wins. Raises
    NoPrediction when ESPN has none for it, and requests' errors when ESPN
    can't be reached.
    """
    parsed = parse_market(market)
    if parsed is None:
        raise NoPrediction(f"{market.get('ticker')} doesn't look like a game market")
    if parsed["league"] not in PREDICTOR_LEAGUES:
        raise NoPrediction(f"ESPN has no matchup predictor for {parsed['league']}")
    found = find_game(market, fetch)
    if found is None:
        raise NoPrediction(f"ESPN doesn't list {market.get('event_ticker')}")
    summary = fetch(f"{BASE}/{SPORTS[parsed['league']]}/summary", {"event": found[1]["id"]})
    predictor = summary.get("predictor") or {}
    side = predictor.get("homeTeam" if parsed["team_is_home"] else "awayTeam") or {}
    try:
        return max(0.0, min(1.0, float(side["gameProjection"]) / 100))
    except (KeyError, TypeError, ValueError):
        pass
    # Started or finished: the predictor is gone, but the win-probability chart opens on ESPN's number.
    chart = summary.get("winprobability") or []
    try:
        home = float(chart[0]["homeWinPercentage"])
        tie = float(chart[0].get("tiePercentage") or 0.0)
    except (IndexError, KeyError, TypeError, ValueError):
        raise NoPrediction(f"ESPN has no matchup prediction for {market.get('event_ticker')} yet") from None
    return max(0.0, min(1.0, home if parsed["team_is_home"] else 1 - home - tie))


def ask_espn(market: dict) -> tuple[float, float]:
    """
    The (probability, confidence) source for the daily run, like an AI's
    parsed answer (see Probability.query_llms(), which calls sources marked
    takes_market with the market instead of a prompt).
    """
    return win_probability(market), CONFIDENCE


ask_espn.takes_market = True

SPACING_SECONDS = 0.1  # between ESPN lookups in backfill()


def backfill(conn: sqlite3.Connection, fetch: Callable = _get, sleep=time.sleep) -> dict:
    """
    Add an ESPN prediction to every logged game in a predicted league (NBA,
    NFL) that has none, preseason aside, copying the game's other columns
    (logged time, prices, outcome) from its rows already there, so it sits
    with the AIs' as if logged with them. Games ESPN can't place are left
    for the next try. Returns {"added", "missing", "preseason"}.
    """
    import SeasonType

    rows = conn.execute(
        "SELECT ticker, MIN(rowid) FROM predictions GROUP BY ticker "
        "HAVING SUM(llm = ?) = 0 ORDER BY MIN(game_date), ticker", (NAME,)).fetchall()
    added = missing = preseason = 0
    first = True
    for ticker, rowid in rows:
        (created_at, event_ticker, league, title, team, opponent, game_date, yes_bid, yes_ask, market_prob,
         outcome, tied, settled_at) = conn.execute(
            "SELECT created_at, event_ticker, league, title, team, opponent, game_date, yes_bid, yes_ask, "
            "market_prob, outcome, tied, settled_at FROM predictions WHERE rowid = ?", (rowid,)).fetchone()
        league = league or Leagues.league_for_ticker(ticker)
        if league not in PREDICTOR_LEAGUES:
            continue
        if SeasonType.segment_iso(game_date, league) == "preseason":
            preseason += 1
            continue
        if not first:
            sleep(SPACING_SECONDS)
        first = False
        market = {"ticker": ticker, "event_ticker": event_ticker or ticker.rsplit("-", 1)[0]}
        try:
            prob = win_probability(market, fetch)
        except Exception as e:  # NoPrediction, or ESPN unreachable: try again next time
            print(f"ESPN backfill: nothing for {ticker}: {e}", flush=True)
            missing += 1
            continue
        conn.execute(
            "INSERT INTO predictions (created_at, ticker, event_ticker, league, title, team, opponent, game_date, "
            " yes_bid, yes_ask, llm, model, predicted_prob, confidence, market_prob, outcome, tied, settled_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (created_at, ticker, event_ticker, league, title, team, opponent, game_date, yes_bid, yes_ask,
             NAME, MODEL, prob, CONFIDENCE, market_prob, outcome, tied, settled_at))
        conn.commit()
        added += 1
    return {"added": added, "missing": missing, "preseason": preseason}


if __name__ == "__main__":
    if sys.argv[1:2] != ["backfill"]:
        sys.exit("usage: py ESPN.py backfill [path/to/predictions.db]")
    import Tracker

    db = Tracker.connect(*sys.argv[2:3])
    try:
        print(backfill(db))
    finally:
        db.close()
