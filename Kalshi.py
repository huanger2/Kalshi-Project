import base64
import datetime
import math
import os
import re
import textwrap
import uuid
from typing import Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

import Leagues

BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Where orders go. "demo" is Kalshi's test exchange (demo.kalshi.co): play
# money, its own account and its own API keys. "live" is the real exchange.
TRADING_BASES = {
    "live": BASE,
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}

# Each environment's credentials: an API key id plus its RSA private key,
# given either as the PEM text itself or as a path to the .key file.
CREDENTIAL_VARS = {
    "live": {"key_id": "KALSHI_API_KEY_ID", "key": "KALSHI_API_KEY", "key_path": "KALSHI_PRIVATE_KEY_PATH"},
    "demo": {"key_id": "KALSHI_DEMO_API_KEY_ID", "key": "KALSHI_DEMO_API_KEY",
             "key_path": "KALSHI_DEMO_PRIVATE_KEY_PATH"},
}


class TradingError(Exception):
    """Kalshi refused an authenticated request, or credentials are missing."""

# Each league's per-game winner series (one binary market per team in each
# game, e.g. KXNBAGAME) lives in Leagues.py. Not to be confused with the
# championship series (KXNBA), which isn't tracked.


# ---------------------------------------------------------------------------
# Auth (only needed for authenticated endpoints; market data is public)
# ---------------------------------------------------------------------------

def load_private_key(file_path):
    with open(file_path, "rb") as f:
        return parse_private_key(f.read().decode("utf-8"))


def parse_private_key(text: str):
    """
    An RSA private key from its PEM text, however an env var mangled it:
    surrounding quotes, literal "\\n"s, or the newlines flattened to spaces
    (pasting a key into a one-line .env value or Railway's variable editor).
    A bare base64 body with no BEGIN/END lines is read as PKCS#8, then PKCS#1.
    """
    from cryptography.hazmat.primitives import serialization

    text = text.strip().strip('"').strip("'").replace("\\n", "\n")
    match = re.search(r"-----BEGIN ([A-Z ]+)-----(.*?)-----END \1-----", text, re.S)
    labels, body = ([match.group(1)], match.group(2)) if match else (["PRIVATE KEY", "RSA PRIVATE KEY"], text)
    body = "\n".join(textwrap.wrap(re.sub(r"\s+", "", body), 64))
    for label in labels:
        pem = f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n".encode()
        try:
            return serialization.load_pem_private_key(pem, password=None)
        except ValueError:
            continue
    raise TradingError("the Kalshi private key isn't a readable PEM key")


def sign_request(private_key, method, path, key_id=None):
    """Kalshi auth headers: an RSA-PSS signature over timestamp + method + path (no query)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    timestamp_ms = str(int(datetime.datetime.now().timestamp() * 1000))
    path_no_query = path.split("?")[0]
    msg = (timestamp_ms + method + path_no_query).encode("utf-8")

    signature = private_key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }


def _env(name: str) -> Optional[str]:
    # .strip(): trailing whitespace/newlines (a stray OS env var, or a
    # copy-paste artifact in the .env file) break the auth header outright.
    value = os.getenv(name) or os.getenv(name.lower())
    return value.strip() if value and value.strip() else None


def _load_env_files() -> None:
    # override=True: AIAPIKEYS.env must win over a stale OS-level env var of
    # the same name, which load_dotenv() otherwise silently leaves in place.
    load_dotenv("AIAPIKEYS.env", override=True)


def has_credentials(env: str = "live") -> bool:
    """Whether `env`'s key id and private key are configured (not whether Kalshi accepts them)."""
    _load_env_files()
    names = CREDENTIAL_VARS[env]
    if not _env(names["key_id"]):
        return False
    return bool(_env(names["key"])) or os.path.isfile(_env(names["key_path"]) or ("kalshi.key" if env == "live" else ""))


def get_auth_headers(method, path, env: str = "live"):
    """Load `env`'s credentials from AIAPIKEYS.env / the environment and sign one request."""
    _load_env_files()
    names = CREDENTIAL_VARS[env]
    key_id = _env(names["key_id"])
    if not key_id:
        raise TradingError(f"{names['key_id']} isn't set")
    key_text = _env(names["key"])
    if key_text:
        private_key = parse_private_key(key_text)
    else:
        key_path = _env(names["key_path"]) or ("kalshi.key" if env == "live" else None)
        if not key_path or not os.path.isfile(key_path):
            raise TradingError(f"set {names['key']} (the private key's text) or {names['key_path']}")
        private_key = load_private_key(key_path)
    return sign_request(private_key, method, path, key_id=key_id)


def _trade_request(method: str, endpoint: str, env: str, body: Optional[dict] = None) -> dict:
    """An authenticated call to `env`'s exchange. Raises TradingError with Kalshi's message on a refusal."""
    if env not in TRADING_BASES:
        raise TradingError(f"unknown Kalshi environment {env!r}")
    url = TRADING_BASES[env] + endpoint
    # Kalshi signs the full path from the root: /trade-api/v2/portfolio/...
    headers = get_auth_headers(method, urlparse(url).path, env=env)
    response = requests.request(method, url, headers=headers, json=body, timeout=30)
    if response.status_code >= 400:
        try:
            detail = response.json().get("error") or response.json()
        except ValueError:
            detail = response.text[:300]
        raise TradingError(f"Kalshi {env} {method} {endpoint}: HTTP {response.status_code} {detail}")
    return response.json()


def get_balance(env: str = "live") -> float:
    """Available cash in `env`'s account, in dollars."""
    data = _trade_request("GET", "/portfolio/balance", env)
    if data.get("balance_dollars") not in (None, ""):
        return float(data["balance_dollars"])
    return data["balance"] / 100.0


def buy(ticker: str, side: str, count: int, price: float, env: str,
        client_order_id: Optional[str] = None) -> dict:
    """
    Buy up to `count` contracts on `side` ("yes"/"no") at `price` (0-1) or
    better, filling what the book has right now and cancelling the rest
    (immediate-or-cancel), so a price that moved since it was quoted never
    buys at a worse one.

    Kalshi's order API prices everything on the YES leg: buying YES is a bid
    at the YES price, buying NO an ask at 1 - the NO price, and fills come
    back on that scale too.

    Returns {"order_id", "count" (contracts filled, 0 if none), "price"
    (average paid per contract, on `side`'s scale), "cost", "fee"}.
    """
    if side not in ("yes", "no"):
        raise ValueError(f"side must be 'yes' or 'no', not {side!r}")
    yes_leg = price if side == "yes" else 1 - price
    body = {
        "ticker": ticker,
        "side": "bid" if side == "yes" else "ask",
        "count": str(int(count)),
        "price": f"{yes_leg:.4f}",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "client_order_id": client_order_id or str(uuid.uuid4()),
    }
    data = _trade_request("POST", "/portfolio/events/orders", env, body)
    filled = float(data.get("fill_count") or 0)
    filled = int(filled) if filled.is_integer() else filled
    if not filled:
        return {"order_id": data.get("order_id"), "count": 0, "price": price, "cost": 0.0, "fee": 0.0}
    avg = float(data.get("average_fill_price") or yes_leg)
    paid = avg if side == "yes" else 1 - avg
    fee = float(data.get("average_fee_paid") or 0) * filled
    return {
        "order_id": data.get("order_id"),
        "count": filled,
        "price": round(paid, 4),
        "cost": round(paid * filled, 4),
        "fee": math.ceil(fee * 100 - 1e-9) / 100,
    }


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def _to_prob(value) -> Optional[float]:
    """Convert a Kalshi price to a probability in [0, 1], or None if missing."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price(market: dict, field: str) -> Optional[float]:
    """
    Read a price field as a probability in [0, 1].

    Kalshi returns dollar strings (``yes_ask_dollars: "0.5500"``); older
    responses used integer cents (``yes_ask: 55``). Both are handled.
    """
    dollars = _to_prob(market.get(f"{field}_dollars"))
    if dollars is not None:
        return dollars
    cents = _to_prob(market.get(field))
    if cents is not None:
        return cents / 100.0
    return None


def get_prices(market: dict) -> dict:
    """
    Extract the prices for a binary market as probabilities.

    Because a YES + NO contract always pays exactly $1, each side is priced off
    the opposite side's book when it is missing: no_ask = 1 - yes_bid and
    no_bid = 1 - yes_ask.

    Returns a dict with keys yes_bid, yes_ask, no_bid, no_ask, last (any may be None).
    """
    yes_bid = _price(market, "yes_bid")
    yes_ask = _price(market, "yes_ask")
    no_bid = _price(market, "no_bid")
    no_ask = _price(market, "no_ask")

    if no_ask is None and yes_bid is not None:
        no_ask = 1.0 - yes_bid
    if no_bid is None and yes_ask is not None:
        no_bid = 1.0 - yes_ask
    if yes_ask is None and no_bid is not None:
        yes_ask = 1.0 - no_bid
    if yes_bid is None and no_ask is not None:
        yes_bid = 1.0 - no_ask

    return {
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "last": _price(market, "last_price"),
    }


def implied_yes_prob(market: dict) -> float:
    """
    Best estimate of the market's implied probability that YES resolves.

    Uses the midpoint of the YES bid/ask when both exist (the last trade can be
    stale), then falls back to the last price, then whichever side exists.
    """
    p = get_prices(market)
    if p["yes_bid"] is not None and p["yes_ask"] is not None:
        return (p["yes_bid"] + p["yes_ask"]) / 2.0
    for key in ("last", "yes_ask", "yes_bid"):
        if p[key] is not None:
            return p[key]
    raise ValueError(f"Market {market.get('ticker')} has no usable price data.")


# ---------------------------------------------------------------------------
# API calls (nothing here runs at import time)
# ---------------------------------------------------------------------------

def get_markets(limit=100, cursor=None, status=None, series_ticker=None, min_close_ts=None):
    params = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    if status:
        params["status"] = status  # "open", "closed", "settled"
    if series_ticker:
        params["series_ticker"] = series_ticker
    if min_close_ts:
        params["min_close_ts"] = min_close_ts  # unix seconds

    response = requests.get(f"{BASE}/markets", params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def get_all_series():
    response = requests.get(f"{BASE}/series", timeout=30)
    response.raise_for_status()
    return response.json()


def get_all_markets(market=None, status="open", min_close_ts=None):
    """Return every market in a series, following pagination cursors."""
    markets = []
    cursor = None

    while True:
        data = get_markets(
            limit=200, cursor=cursor, status=status, series_ticker=market, min_close_ts=min_close_ts
        )
        markets.extend(data["markets"])

        cursor = data.get("cursor")
        if not cursor:
            break

    return markets


def get_market(ticker: str) -> dict:
    """Fetch a single market by ticker."""
    response = requests.get(f"{BASE}/markets/{ticker}", timeout=30)
    response.raise_for_status()
    return response.json()["market"]


def get_market_result(market: dict) -> Optional[bool]:
    """
    True if the market settled YES, False if it settled NO, None if unsettled.
    """
    result = (market.get("result") or "").strip().lower()
    if result == "yes":
        return True
    if result == "no":
        return False
    return None


def is_tie(market: dict) -> bool:
    """
    True if a game market settled as a tie (NFL only). Kalshi settles both
    teams' markets as result "scalar" at $0.50 each (checked against the
    live API) rather than yes/no; this project treats that as neither team
    winning -- see Tracker.settle().
    """
    return (market.get("result") or "").strip().lower() == "scalar"


# ---------------------------------------------------------------------------
# Games by day
# ---------------------------------------------------------------------------

_TICKER_DATE = re.compile(r"^[A-Z]+-(\d{2})([A-Z]{3})(\d{2})")
_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def game_date(market: dict) -> Optional[datetime.date]:
    """
    The scheduled (US) date of the game, read from the ticker.

    KXNBAGAME-26OCT03MIATOR is the Oct 3, 2026 game. MLB tickers also carry
    the start time, to tell a doubleheader's two games apart
    (KXMLBGAME-26OCT172000TBCLE), which this ignores. occurrence_datetime is
    not used because it is UTC, which puts evening games on the following day.
    """
    match = _TICKER_DATE.match(market.get("event_ticker") or market.get("ticker") or "")
    if not match or match.group(2) not in _MONTHS:
        return None
    yy, mon, dd = match.groups()
    return datetime.date(2000 + int(yy), _MONTHS[mon], int(dd))


def get_games_on(day: datetime.date, league: str = Leagues.DEFAULT_LEAGUE, status=None) -> list[dict]:
    """Every per-game market (one per team) for `league`'s games scheduled on `day`."""
    # A game's market closes after the game ends, so nothing from `day` closes
    # before it; the extra day of slack covers time zones. This keeps the
    # query from paging through every past game.
    since = datetime.datetime.combine(day - datetime.timedelta(days=1), datetime.time())
    markets = get_all_markets(
        market=Leagues.get(league).series, status=status, min_close_ts=int(since.timestamp())
    )
    return [m for m in markets if game_date(m) == day]


def get_todays_games(league: str = Leagues.DEFAULT_LEAGUE, today: Optional[datetime.date] = None) -> list[dict]:
    """Markets for `league`'s games today (open, live or already finished)."""
    return get_games_on(today or datetime.date.today(), league)


def get_current_odds(ticker: str) -> dict:
    """
    Fetch a market fresh from Kalshi and return its current odds:
    ticker, team, yes_bid, yes_ask, no_bid, no_ask, last, implied_prob.
    """
    market = get_market(ticker)
    return {
        "ticker": market["ticker"],
        "team": market.get("yes_sub_title"),
        **get_prices(market),
        "implied_prob": implied_yes_prob(market),
    }


def get_results(day: datetime.date, league: str = Leagues.DEFAULT_LEAGUE) -> list[dict]:
    """
    Final results for `league`'s games on `day`, one dict per game:
    event_ticker, winner, loser (team names) and tie. A tied game (NFL only)
    has tie=True and no winner/loser. Unfinished games are left out.
    """
    games: dict[str, dict] = {}
    for market in get_games_on(day, league):
        won = get_market_result(market)
        tie = is_tie(market)
        if won is None and not tie:
            continue
        game = games.setdefault(
            market["event_ticker"],
            {"event_ticker": market["event_ticker"], "winner": None, "loser": None, "tie": False},
        )
        if tie:
            game["tie"] = True
        elif won:
            game["winner"] = market.get("yes_sub_title")
        else:
            game["loser"] = market.get("yes_sub_title")
    return [g for g in games.values() if g["winner"] or g["tie"]]


def get_yesterdays_results(league: str = Leagues.DEFAULT_LEAGUE, today: Optional[datetime.date] = None) -> list[dict]:
    """Final results for `league`'s games yesterday."""
    return get_results((today or datetime.date.today()) - datetime.timedelta(days=1), league)
