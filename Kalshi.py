import base64
import datetime
import math
import os
import re
import textwrap
import time
import uuid
from typing import NamedTuple, Optional
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

# Where the single-login version read each environment's key from: an API
# key id plus its private key (the PEM text, or a path to the .key file).
# Keys now belong to users (Accounts.py); these are only read once, to move
# them onto the first admin (legacy_env_credentials()).
CREDENTIAL_VARS = {
    "live": {"key_id": "KALSHI_API_KEY_ID", "key": "KALSHI_API_KEY", "key_path": "KALSHI_PRIVATE_KEY_PATH"},
    "demo": {"key_id": "KALSHI_DEMO_API_KEY_ID", "key": "KALSHI_DEMO_API_KEY",
             "key_path": "KALSHI_DEMO_PRIVATE_KEY_PATH"},
}


class TradingError(Exception):
    """Kalshi refused an authenticated request, or credentials are missing."""


class Credentials(NamedTuple):
    """One Kalshi account's API key: which exchange it's for, its id, and the loaded private key."""
    env: str  # "demo" or "live" (TRADING_BASES)
    key_id: str
    private_key: object

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
    A private key (Ed25519 or RSA) from its PEM text, however an env var mangled it:
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
    """
    Kalshi auth headers: a signature over timestamp + method + path (no
    query). Kalshi issues both key types: an Ed25519 key signs the message
    as is, an RSA key with RSA-PSS (SHA-256, salt the digest's length).
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding

    timestamp_ms = str(int(datetime.datetime.now().timestamp() * 1000))
    path_no_query = path.split("?")[0]
    msg = (timestamp_ms + method + path_no_query).encode("utf-8")

    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        signature = private_key.sign(msg)
    else:
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


def legacy_env_credentials(env: str) -> Optional[tuple[str, str]]:
    """
    (key id, private key PEM text) from the environment / AIAPIKEYS.env, as
    the single-login version kept them, or None. Only Accounts.bootstrap()
    reads these, to move them onto the first admin's account.
    """
    # override=True: AIAPIKEYS.env must win over a stale OS-level env var of
    # the same name, which load_dotenv() otherwise silently leaves in place.
    load_dotenv("AIAPIKEYS.env", override=True)
    names = CREDENTIAL_VARS[env]
    key_id = _env(names["key_id"])
    if not key_id:
        return None
    key_text = _env(names["key"])
    if not key_text:
        key_path = _env(names["key_path"]) or ("kalshi.key" if env == "live" else None)
        if not key_path or not os.path.isfile(key_path):
            return None
        with open(key_path, encoding="utf-8") as f:
            key_text = f.read()
    return key_id, key_text


def get_auth_headers(method, path, creds: Credentials):
    """Sign one request with `creds`."""
    return sign_request(creds.private_key, method, path, key_id=creds.key_id)


# Kalshi rate-limits the API (HTTP 429 "too_many_requests"). A refused call
# is retried after the wait Kalshi asks for (Retry-After), else after 1, 2,
# 4, 8 seconds -- safe for orders too: a 429 means Kalshi didn't take it, and
# each order carries a client_order_id. _sleep is swapped out in tests.
RATE_LIMIT_RETRIES = 4
_sleep = time.sleep


def _retry_wait(response, attempt: int) -> float:
    try:
        return min(30.0, max(0.5, float(response.headers.get("Retry-After"))))
    except (TypeError, ValueError):
        return float(2 ** attempt)


def _trade_request(method: str, endpoint: str, creds: Credentials, body: Optional[dict] = None) -> dict:
    """
    An authenticated call to the exchange `creds` are for. Raises
    TradingError with Kalshi's message on a refusal; rate-limited calls are
    retried first (see RATE_LIMIT_RETRIES).
    """
    if creds is None:
        raise TradingError("no Kalshi key saved")
    if creds.env not in TRADING_BASES:
        raise TradingError(f"unknown Kalshi environment {creds.env!r}")
    url = TRADING_BASES[creds.env] + endpoint
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        # Signed fresh each try: the signature covers a timestamp. The full path from the root is signed.
        headers = get_auth_headers(method, urlparse(url).path, creds)
        response = requests.request(method, url, headers=headers, json=body, timeout=30)
        if response.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
            break
        _sleep(_retry_wait(response, attempt))
    if response.status_code >= 400:
        try:
            detail = response.json().get("error") or response.json()
        except ValueError:
            detail = response.text[:300]
        raise TradingError(f"Kalshi {creds.env} {method} {endpoint}: HTTP {response.status_code} {detail}")
    return response.json()


def get_balance(creds: Credentials) -> float:
    """Available cash in the account `creds` belong to, in dollars."""
    data = _trade_request("GET", "/portfolio/balance", creds)
    if data.get("balance_dollars") not in (None, ""):
        return float(data["balance_dollars"])
    return data["balance"] / 100.0


# ---------------------------------------------------------------------------
# Exchange shards
# ---------------------------------------------------------------------------
#
# Kalshi runs several matching engines ("exchange shards", numbered by
# exchange_index): sports markets live on their own shard, apart from the
# default shard 0 where deposits land. Each shard checks collateral against
# the cash on *it*, and only Kalshi's own apps move money between shards for
# you -- an API order on a shard with no cash is refused with
# insufficient_shard_balance ("Exchange user not found" if the account has
# never had money there). So before an order, fund_shard() moves what's
# missing onto the market's shard. (docs.kalshi.com: Exchange Sharding.)

SHARD_WAIT_SECONDS = 15  # transfers between shards are asynchronous


_market_shards: dict[str, int] = {}


def market_shard(ticker: str) -> int:
    """The exchange shard a market trades on (0 when Kalshi doesn't say). Remembered: live markets don't move."""
    if ticker not in _market_shards:
        _market_shards[ticker] = int(get_market(ticker).get("exchange_index") or 0)
    return _market_shards[ticker]


def shard_balances(creds: Credentials) -> dict[int, float]:
    """Available cash per exchange shard, in dollars."""
    data = _trade_request("GET", "/portfolio/balance", creds)
    breakdown = data.get("balance_breakdown") or []
    if breakdown:
        return {int(b["exchange_index"]): float(b["balance"]) for b in breakdown}
    total = float(data["balance_dollars"]) if data.get("balance_dollars") not in (None, "") else data["balance"] / 100
    return {0: total}


def transfer_between_shards(creds: Credentials, dollars: float, source: int, destination: int) -> str:
    """Move cash between two of the account's exchange shards. Returns Kalshi's transfer id (it completes later)."""
    body = {
        "source": "event_contract",
        "destination": "event_contract",
        "amount": int(round(dollars * 10000)),  # centicents
        "source_exchange_shard": source,
        "destination_exchange_shard": destination,
        "client_transfer_id": str(uuid.uuid4()),
    }
    return _trade_request("POST", "/portfolio/intra_exchange_instance_transfer", creds, body).get("transfer_id")


def fund_shard(creds: Credentials, shard: int, needed: float, sleep=time.sleep,
               wait_seconds: float = SHARD_WAIT_SECONDS) -> Optional[float]:
    """
    Make sure `shard` has at least `needed` dollars, moving the shortfall
    from whichever other shard has the most cash. Returns the dollars moved
    (None if none were needed). Raises TradingError if the account doesn't
    have that much anywhere, or the transfer hasn't landed in time.
    """
    balances = shard_balances(creds)
    have = balances.get(shard, 0.0)
    if have >= needed:
        return None
    shortfall = math.ceil((needed - have) * 100) / 100
    donors = sorted(((cash, index) for index, cash in balances.items() if index != shard), reverse=True)
    if not donors or donors[0][0] < shortfall:
        elsewhere = sum(cash for cash, _ in donors)
        raise TradingError(f"not enough cash: the order needs ${needed:.2f} on Kalshi's exchange shard {shard}, "
                           f"which has ${have:.2f}, with ${elsewhere:.2f} on the others")
    transfer_between_shards(creds, shortfall, donors[0][1], shard)
    # Check after 1s, then 2s, 3s... later: a handful of balance calls, not one a second.
    waited, pause = 0.0, 1.0
    while waited < wait_seconds:
        pause = min(pause, wait_seconds - waited)
        sleep(pause)
        waited += pause
        if shard_balances(creds).get(shard, 0.0) >= needed:
            return shortfall
        pause += 1.0
    raise TradingError(f"moved ${shortfall:.2f} to Kalshi's exchange shard {shard}, but it hadn't arrived after "
                       f"{int(wait_seconds)}s; the next run will use it")


def buy(ticker: str, side: str, count: int, price: float, creds: Credentials,
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
    data = _trade_request("POST", "/portfolio/events/orders", creds, body)
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
