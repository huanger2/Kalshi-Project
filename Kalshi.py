import base64
import datetime
import os
from typing import Optional

import requests
from dotenv import load_dotenv

BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Kalshi series tickers. KXNBA is the *championship* market; KXNBAGAME is the
# per-game winner market (one binary market per team in each game).
NBA_GAME_SERIES = "KXNBAGAME"
NBA_CHAMPION_SERIES = "KXNBA"


# ---------------------------------------------------------------------------
# Auth (only needed for authenticated endpoints; market data is public)
# ---------------------------------------------------------------------------

def load_private_key(file_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.backends import default_backend

    with open(file_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())


def sign_request(private_key, method, path, api_key=None, key_id=None):
    """Build Kalshi auth headers, using a bearer key if given, else an RSA signature."""
    if api_key:
        headers = {"Authorization": f"Bearer {api_key}"}
        if key_id:
            headers["KALSHI-ACCESS-KEY"] = key_id
        return headers

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


def get_auth_headers(method, path):
    """Load credentials from AIAPIKEYS.env lazily and return auth headers."""
    load_dotenv("AIAPIKEYS.env")
    api_key = os.getenv("kalshi_api_key") or os.getenv("KALSHI_API_KEY")
    key_id = os.getenv("kalshi_api_key_id") or os.getenv("KALSHI_API_KEY_ID")
    private_key = None
    if not api_key:
        private_key = load_private_key(os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi.key"))
    return sign_request(private_key, method, path, api_key=api_key, key_id=key_id)


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

def get_markets(limit=100, cursor=None, status=None, series_ticker=None):
    params = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    if status:
        params["status"] = status  # "open", "closed", "settled"
    if series_ticker:
        params["series_ticker"] = series_ticker

    response = requests.get(f"{BASE}/markets", params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def get_all_series():
    response = requests.get(f"{BASE}/series", timeout=30)
    response.raise_for_status()
    return response.json()


def get_all_markets(market=None, status="open"):
    """Return every market in a series, following pagination cursors."""
    markets = []
    cursor = None

    while True:
        data = get_markets(limit=200, cursor=cursor, status=status, series_ticker=market)
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


if __name__ == "__main__":
    for m in get_all_markets(market=NBA_GAME_SERIES):
        print(m["ticker"], "|", m["title"], "|", f"{implied_yes_prob(m):.0%}")
