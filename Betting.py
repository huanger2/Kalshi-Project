"""
Betting.py

Automatic betting on the AI odds. When the daily run logs a game, the AIs'
consensus probability (EVCalculator.consensus_probability(), weighted by
ai_weights.json exactly like the webapp's "AI odds" column) is compared with
what a contract actually costs right now -- the ask, not the midpoint -- on
each side of the market:

    edge(YES) = AI odds - yes_ask           (backing the logged team)
    edge(NO)  = (1 - AI odds) - no_ask      (backing its opponent)

The side with the bigger edge gets a bet if it clears betting.json's rules
(see decide()). A bet buys as many whole contracts as stake_usd covers.

Only paper betting exists so far: a bet is recorded in predictions.db's
bets table at the price it would have been bought at, plus Kalshi's fee,
and nothing is sent to Kalshi. A bet's result isn't stored -- it follows
from its game's outcome in the predictions table (see result()), which the
daily run already settles, so bets never need a settle step of their own.

betting.json is read fresh on every call, like the other config files, and
goes through ConfigFiles, so the Betting tab's saves land on the volume
when hosted.
"""

import datetime
import math
import sqlite3
from pathlib import Path
from typing import Optional

import ConfigFiles
import Leagues

BETTING_PATH = Path(__file__).with_name("betting.json")
MODES = ["off", "paper"]
SEGMENTS = ["preseason", "regular", "postseason"]  # see SeasonType.segment()

DEFAULTS = {
    "mode": "off",
    "min_edge": 0.05,
    "stake_usd": 5.0,
    "max_daily_usd": 50.0,
    "min_price": 0.10,
    "max_price": 0.90,
    "min_ais": 2,
    "leagues": list(Leagues.LEAGUES),
    "segments": list(SEGMENTS),
}

# Kalshi's trading fee on most markets: 7% of C * P * (1 - P), rounded up to
# the next cent per order (C contracts at price P in dollars). It peaks at
# 1.75 cents per contract at 50 cents and shrinks toward the extremes.
FEE_RATE = 0.07


def load_config() -> dict:
    """betting.json's rules, with DEFAULTS filling in anything it leaves out (mode "off" if there's no file)."""
    config = ConfigFiles.load(BETTING_PATH)
    return {key: config.get(key, default) for key, default in DEFAULTS.items()}


def fee(count: int, price: float) -> float:
    """Kalshi's fee in dollars for buying `count` contracts at `price` (0-1)."""
    # The small epsilon keeps float noise (0.0700000001) from adding a cent.
    return math.ceil(FEE_RATE * count * price * (1 - price) * 100 - 1e-9) / 100


def decide(
    ai_prob: Optional[float],
    ai_count: int,
    yes_ask: Optional[float],
    no_ask: Optional[float],
    league: Optional[str],
    config: dict,
    segment: Optional[str] = None,
) -> tuple[Optional[dict], str]:
    """
    The bet the rules call for on one market, or None, plus a one-line reason.

    `segment` is the game's part of the season ("preseason", "regular",
    "postseason"; SeasonType.segment()); None (no game date) skips that rule.

    Leaves out the daily cap and anything else that depends on bets already
    placed (see consider()), so the what-if backtest can reuse it as is.

    Returns ({"side", "price", "count", "cost", "fee", "edge", "ai_prob"}, reason)
    or (None, why not).
    """
    if league not in config["leagues"]:
        return None, f"not betting on {league or 'unknown league'} games"
    if segment is not None and segment not in config["segments"]:
        return None, f"not betting on {segment} games"
    if ai_prob is None or ai_count < config["min_ais"]:
        return None, f"{ai_count} AI(s) answered, need {config['min_ais']}"

    sides = []
    for side, prob, price in (("yes", ai_prob, yes_ask), ("no", 1 - ai_prob, no_ask)):
        if price is not None and config["min_price"] <= price <= config["max_price"]:
            sides.append({"side": side, "price": price, "edge": prob - price})
    if not sides:
        return None, "no side priced within the allowed range"
    best = max(sides, key=lambda s: s["edge"])
    if best["edge"] < config["min_edge"]:
        return None, f"best edge {best['edge']:+.1%} ({best['side'].upper()}) is under {config['min_edge']:.1%}"

    count = math.floor(config["stake_usd"] / best["price"] + 1e-9)
    if count < 1:
        return None, f"${config['stake_usd']:.2f} doesn't cover one contract at {best['price']:.2f}"
    bet = {
        **best,
        "count": count,
        "cost": round(count * best["price"], 4),
        "fee": fee(count, best["price"]),
        "ai_prob": ai_prob,
    }
    return bet, f"{best['side'].upper()} x{count} at {best['price']:.2f}, edge {best['edge']:+.1%}"


def result(bet: dict, outcome: Optional[int], tied: bool = False) -> dict:
    """
    How a bet did, from its market's YES outcome (1/0, None while pending).
    A tie (NFL) settles both sides at $0.50 a contract -- see Kalshi.is_tie().

    Returns {"status": "won"/"lost"/"tie"/"pending", "payout", "pnl"} (None while pending).
    """
    if outcome is None:
        return {"status": "pending", "payout": None, "pnl": None}
    if tied:
        status, per_contract = "tie", 0.5
    else:
        won = (outcome == 1) == (bet["side"] == "yes")
        status, per_contract = ("won", 1.0) if won else ("lost", 0.0)
    payout = per_contract * bet["count"]
    return {"status": status, "payout": payout, "pnl": round(payout - bet["cost"] - bet["fee"], 4)}


def spent_on(conn: sqlite3.Connection, game_date: str, mode: str) -> float:
    """Cost + fees of the `mode` bets already placed on one game day."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost + fee), 0) FROM bets WHERE game_date = ? AND mode = ?", (game_date, mode)
    ).fetchone()
    return row[0]


def consider(
    conn: sqlite3.Connection,
    market: dict,
    predictions: list,
    prices: dict,
    game_date: str,
    opponent: Optional[str] = None,
    config: Optional[dict] = None,
) -> Optional[dict]:
    """
    Apply the rules to a market the daily run just logged, recording a bet in
    the bets table if they call for one. Returns the bet, or None.

    Args:
        market:      Kalshi market dict (the one whose predictions were logged).
        predictions: That market's LLMPrediction objects.
        prices:      Kalshi.get_prices(market) -- yes_ask/no_ask are what a bet pays.
        game_date:   ISO date the game is on (the daily cap is per game day).
    """
    import AIWeights
    import SeasonType
    from EVCalculator import consensus_probability

    config = config or load_config()
    mode = config["mode"]
    if mode not in MODES or mode == "off":
        return None
    ticker = market["ticker"]
    if conn.execute("SELECT 1 FROM bets WHERE ticker = ? AND mode = ?", (ticker, mode)).fetchone():
        return None  # already bet on this market

    league = Leagues.league_for_ticker(ticker)
    ai_prob = consensus_probability(predictions, {p.llm_name: AIWeights.get_weight(p.llm_name) for p in predictions})
    bet, reason = decide(ai_prob, len(predictions), prices.get("yes_ask"), prices.get("no_ask"), league, config,
                         segment=SeasonType.segment_iso(game_date, league))
    if bet is None:
        print(f"no bet on {ticker}: {reason}")
        return None
    spent = spent_on(conn, game_date, mode)
    if spent + bet["cost"] + bet["fee"] > config["max_daily_usd"] + 1e-9:
        print(f"no bet on {ticker}: today's ${config['max_daily_usd']:.2f} cap is used up (${spent:.2f} so far)")
        return None

    conn.execute(
        "INSERT INTO bets (created_at, mode, ticker, event_ticker, league, game_date, team, opponent, "
        " side, price, count, cost, fee, ai_prob, edge) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.datetime.now().isoformat(timespec="seconds"), mode, ticker, market.get("event_ticker"), league,
         game_date, market.get("yes_sub_title"), opponent, bet["side"], bet["price"], bet["count"], bet["cost"],
         bet["fee"], ai_prob, bet["edge"]),
    )
    conn.commit()
    print(f"{mode} bet on {ticker}: {reason}")
    return bet
