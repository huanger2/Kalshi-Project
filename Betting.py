"""
Betting.py

Automatic betting on the AI odds, for every user who has turned it on.
When the daily run logs a game, that user's AI consensus probability
(EVCalculator.consensus_probability(), weighted by their own AI weights --
AIWeights.for_user() -- exactly like the "AI odds" column they see) is
compared with what a contract actually costs right now -- the ask, not the
midpoint -- on each side of the market:

    edge(YES) = AI odds - yes_ask           (backing the logged team)
    edge(NO)  = (1 - AI odds) - no_ask      (backing its opponent)

The side with the bigger edge gets a bet if it clears that user's rules
(see decide()). A bet buys as many whole contracts as stake_usd covers.
When more games qualify in a day than max_daily_usd covers, the biggest
edges get the money (see apply_daily_cap()).

Each user's rules are their own (their "betting" setting, Accounts.py),
and so is the mode, which says where their bets go:

    paper  recorded in predictions.db's bets table at the price it would
           have been bought at, plus Kalshi's fee; nothing is sent to Kalshi
    demo   sent to Kalshi's test exchange (play money) with the user's demo key
    live   sent to the real exchange (real money) with the user's live key

A demo or live bet is an immediate-or-cancel limit order at the price the
rules were checked against (Kalshi.buy()), so it fills at that price or
better, or partly, or not at all; what's recorded is what actually filled,
at Kalshi's reported price and fee. Each user's bets, and each of their
modes' bets, are kept apart (the daily cap and the one-bet-per-market rule
are per user and mode).

A bet's result isn't stored -- it follows from its game's outcome in the
predictions table (see result()), which the daily run already settles, so
bets never need a settle step of their own. Kalshi pays out real positions
itself at settlement.

betting.json is no longer read for betting: it only seeds the first admin's
rules when accounts are first set up (Accounts.bootstrap()).
"""

import datetime
import math
import sqlite3
from pathlib import Path
from typing import Callable, Optional

import Leagues

BETTING_PATH = Path(__file__).with_name("betting.json")
MODES = ["off", "paper", "demo", "live"]
# How a game's side is chosen:
#   "fair"      the edge of fair odds (FairOdds.py: Kalshi's price, moved only as far as the AIs' surprise
#               has earned) over the ask
#   "ai"        the edge of the raw AI odds over the ask (they hug 50%, so this mostly backs underdogs)
#   "favorite"  no edge at all, and no AI rules: back whichever side Kalshi's price favors
ODDS_MODELS = ["fair", "ai", "favorite"]
TRADING_MODES = ["demo", "live"]  # the modes that send orders to Kalshi (Kalshi.TRADING_BASES)
SEGMENTS = ["preseason", "regular", "postseason"]  # see SeasonType.segment()

DEFAULTS = {
    "mode": "off",
    "min_edge": 0.05,
    "stake_usd": 5.0,
    "max_daily_usd": 50.0,
    "min_price": 0.10,
    "max_price": 0.90,
    "min_ais": 2,
    "min_confidence": 0.0,  # the AIs' weighted average confidence (EVCalculator.consensus_confidence()), 0-1
    "odds_model": "fair",  # see ODDS_MODELS
    "leagues": list(Leagues.LEAGUES),
    "segments": list(SEGMENTS),
}

# Kalshi's trading fee on most markets: 7% of C * P * (1 - P), rounded up to
# the next cent per order (C contracts at price P in dollars). It peaks at
# 1.75 cents per contract at 50 cents and shrinks toward the extremes.
FEE_RATE = 0.07


def load_config(conn: sqlite3.Connection, user_id: int) -> dict:
    """A user's betting rules, with DEFAULTS filling in anything unset (mode "off" for a new user)."""
    import Accounts

    config = Accounts.get_setting(conn, user_id, "betting") or {}
    return {key: config.get(key, default) for key, default in DEFAULTS.items()}


def bettors(conn: sqlite3.Connection) -> list[dict]:
    """
    Everyone the daily run bets for: each user whose mode isn't off, with
    their rules, AI weights and -- demo/live -- their Kalshi key. A user
    in demo/live mode without a usable key is left out, with a message.

    Each is {"user_id", "username", "config", "weights", "creds"}.
    """
    import Accounts

    found = []
    for user in Accounts.list_users(conn):
        bettor, why = bettor_for(conn, user)
        if bettor is not None:
            found.append(bettor)
        elif why:
            print(f"no bets for {user['username']}: {why}")
    return found


def bettor_for(conn: sqlite3.Connection, user: dict) -> tuple[Optional[dict], Optional[str]]:
    """
    One user as a bettor (see bettors()), or (None, why not): None for
    "betting is off", else what's missing.
    """
    import Accounts
    import AIWeights

    config = load_config(conn, user["id"])
    mode = config["mode"]
    if mode not in MODES or mode == "off":
        return None, None
    creds = None
    if mode in TRADING_MODES:
        try:
            creds = Accounts.kalshi_credentials(conn, user["id"], mode)
        except Exception as e:
            return None, str(e)
        if creds is None:
            return None, f"{mode} mode, but no Kalshi {mode} key saved"
    weights = AIWeights.for_user(conn, user["id"])
    fair_model = None
    if config.get("odds_model", "fair") == "fair":
        import FairOdds
        fair_model = FairOdds.model_for(conn, weights)
    return {"user_id": user["id"], "username": user["username"], "config": config,
            "weights": weights, "creds": creds, "fair_model": fair_model}, None


# Kalshi market statuses that still take orders.
OPEN_STATUSES = {"open", "active", "initialized"}


def latest_games(conn: sqlite3.Connection, today: Optional[datetime.date] = None) -> tuple[Optional[str], list[dict]]:
    """
    The latest logged slate still unsettled -- today's games, normally --
    as (game day, [{"ticker", "event_ticker", "team", "opponent",
    "predictions"}]). A slate older than yesterday counts as none: those
    games are over. (Yesterday counts because the server's date can run
    ahead of the games' -- UTC on Railway.)
    """
    from EVCalculator import LLMPrediction

    today = today or datetime.date.today()
    day = conn.execute("SELECT MAX(game_date) FROM predictions WHERE outcome IS NULL").fetchone()[0]
    if not day or day < (today - datetime.timedelta(days=1)).isoformat():
        return None, []
    games: dict[str, dict] = {}
    for ticker, event, team, opponent, llm, prob, confidence in conn.execute(
            "SELECT ticker, event_ticker, team, opponent, llm, predicted_prob, confidence FROM predictions "
            "WHERE game_date = ? AND outcome IS NULL ORDER BY ticker, llm", (day,)):
        game = games.setdefault(ticker, {"ticker": ticker, "event_ticker": event, "team": team,
                                         "opponent": opponent, "predictions": []})
        game["predictions"].append(LLMPrediction(llm, prob, confidence))
    return day, list(games.values())


def rerun_today(conn: sqlite3.Connection, user: dict, fetch_market=None,
                today: Optional[datetime.date] = None) -> dict:
    """
    Apply a user's current rules to today's already-logged games now,
    instead of waiting for tomorrow's run: each game's market is fetched
    fresh from Kalshi (the price may have moved since it was logged), games
    no longer trading are skipped, and bets go through candidate()/place()
    exactly as in the daily run -- so a game already bet in this mode is
    left alone, and the daily cap counts what's already been spent today.
    The AIs aren't asked again.

    Returns {"day", "games", "placed", "already", "closed", "failed", "why",
    "odds_model", "trusts_ais", "outcomes"}: "why" is set when nothing could
    run (e.g. betting is off); "outcomes" is [{"game", "ticker", "outcome"}]
    -- what happened to each game, bet or not, in words.
    """
    import Kalshi

    result = {"day": None, "games": 0, "placed": 0, "already": 0, "closed": 0, "failed": 0, "why": None,
              "odds_model": None, "trusts_ais": None, "outcomes": []}
    bettor, why = bettor_for(conn, user)
    if bettor is None:
        result["why"] = why or "betting is off"
        return result
    day, games = latest_games(conn, today)
    odds_model = bettor["config"].get("odds_model", "fair")
    result.update(day=day, games=len(games), odds_model=odds_model,
                  trusts_ais=bettor["fair_model"].trusts_ais if bettor.get("fair_model") else None)
    if not games:
        result["why"] = "no unsettled games logged today"
        return result
    fetch_market = fetch_market or Kalshi.get_market
    mode = bettor["config"]["mode"]
    candidates = []
    outcomes: dict[str, str] = {}
    for game in games:
        ticker = game["ticker"]
        if conn.execute("SELECT 1 FROM bets WHERE ticker = ? AND mode = ? AND user_id = ?",
                        (ticker, mode, bettor["user_id"])).fetchone():
            result["already"] += 1
            outcomes[ticker] = f"already bet in {mode} mode (left as it is)"
            continue
        try:
            market = fetch_market(ticker)
        except Exception as e:
            print(f"could not fetch {ticker} from Kalshi: {e}")
            result["failed"] += 1
            outcomes[ticker] = f"couldn't reach Kalshi: {e}"
            continue
        status = (market.get("status") or "open").lower()
        if status not in OPEN_STATUSES:
            result["closed"] += 1
            outcomes[ticker] = f"market no longer trading ({status})"
            continue
        why: list = []
        bet = candidate(conn, bettor, {"ticker": ticker, "event_ticker": game["event_ticker"],
                                       "yes_sub_title": game["team"]},
                        game["predictions"], Kalshi.get_prices(market), day, opponent=game["opponent"], why=why)
        if bet:
            candidates.append(bet)
        else:
            outcomes[ticker] = "no bet: " + (why[0] if why else "the rules didn't call for one")
    if candidates:
        result["placed"] = len(place(conn, bettor, candidates, outcomes))
    result["outcomes"] = [{"ticker": g["ticker"], "game": f"{g['team']} vs {g['opponent'] or '?'}",
                           "outcome": outcomes.get(g["ticker"], "")} for g in games]
    return result


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
    ai_confidence: Optional[float] = None,
    market_prob: Optional[float] = None,
) -> tuple[Optional[dict], str]:
    """
    The bet the rules call for on one market, or None, plus a one-line reason.

    `segment` is the game's part of the season ("preseason", "regular",
    "postseason"; SeasonType.segment()); None (no game date) skips that rule.
    `ai_confidence` is the AIs' weighted average confidence
    (EVCalculator.consensus_confidence()); under the rules' min_confidence
    there's no bet, however big the edge (None counts as under any minimum
    above 0).

    With the rules' odds_model "favorite", the side is simply the one
    Kalshi's price (`market_prob`, the YES midpoint) favors, at its ask; the
    minimum edge doesn't apply (the favorite's ask is almost never below its
    price), and the bet's "priority" for the daily cap is how strongly it's
    favored. The AIs play no part: the minimum edge, the AIs that must answer
    and the minimum AI confidence are all ignored.

    Leaves out the daily cap and anything else that depends on bets already
    placed (see consider()), so the what-if backtest can reuse it as is.

    Returns ({"side", "price", "count", "cost", "fee", "edge", "ai_prob"}, reason)
    or (None, why not).
    """
    if league not in config["leagues"]:
        return None, f"not betting on {league or 'unknown league'} games"
    if segment is not None and segment not in config["segments"]:
        return None, f"not betting on {segment} games"
    if config.get("odds_model") == "favorite":
        # Following the market: no AI rule applies.
        return _back_the_favorite(market_prob, yes_ask, no_ask, ai_prob, config)
    if ai_prob is None or ai_count < config["min_ais"]:
        return None, f"{ai_count} AI(s) answered, need {config['min_ais']}"
    min_confidence = config.get("min_confidence") or 0.0
    if min_confidence > 0 and (ai_confidence is None or ai_confidence < min_confidence):
        shown = "unknown" if ai_confidence is None else f"{ai_confidence:.0%}"
        return None, f"AI confidence {shown} is under {min_confidence:.0%}"

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


def _back_the_favorite(market_prob: Optional[float], yes_ask: Optional[float], no_ask: Optional[float],
                       ai_prob: Optional[float], config: dict) -> tuple[Optional[dict], str]:
    """decide() for odds_model "favorite": the side Kalshi's price favors, if it's priced within the range."""
    if market_prob is None:
        return None, "no Kalshi price to find the favorite from"
    if market_prob == 0.5:
        return None, "Kalshi has no favorite (50/50)"
    side, prob, price = ("yes", market_prob, yes_ask) if market_prob > 0.5 else ("no", 1 - market_prob, no_ask)
    if price is None:
        return None, f"no ask for the favorite ({side.upper()})"
    if not config["min_price"] <= price <= config["max_price"]:
        return None, f"the favorite ({side.upper()}) is priced {price:.2f}, outside the allowed range"
    count = math.floor(config["stake_usd"] / price + 1e-9)
    if count < 1:
        return None, f"${config['stake_usd']:.2f} doesn't cover one contract at {price:.2f}"
    bet = {"side": side, "price": price, "edge": prob - price, "priority": prob, "count": count,
           "cost": round(count * price, 4), "fee": fee(count, price),
           "ai_prob": ai_prob if ai_prob is not None else market_prob}
    return bet, f"{side.upper()} x{count} at {price:.2f}, Kalshi's favorite at {prob:.0%}"


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


def spent_on(conn: sqlite3.Connection, user_id: int, game_date: str, mode: str) -> float:
    """Cost + fees of a user's `mode` bets already placed on one game day."""
    row = conn.execute(
        "SELECT COALESCE(SUM(cost + fee), 0) FROM bets WHERE user_id = ? AND game_date = ? AND mode = ?",
        (user_id, game_date, mode)
    ).fetchone()
    return row[0]


def apply_daily_cap(
    bets: list[dict], cap: float, spent_for: Callable[[str], float] = lambda game_date: 0.0
) -> tuple[list[dict], list[dict]]:
    """
    Which bets fit under each game day's cap (cost + fee), biggest edge
    first: a bet that doesn't fit what's left is skipped, and smaller ones
    after it can still fill the gap. `spent_for(game_date)` is what that day
    already used (bets placed by an earlier run). Returns (kept, skipped).
    """
    kept, skipped = [], []
    by_day: dict[str, list[dict]] = {}
    for bet in bets:
        by_day.setdefault(bet["game_date"], []).append(bet)
    for game_date, day_bets in by_day.items():
        spent = spent_for(game_date)
        # "priority" when the rules set one (the favorite's strength), else the edge.
        for bet in sorted(day_bets, key=lambda b: b.get("priority", b["edge"]), reverse=True):
            outlay = bet["cost"] + bet["fee"]
            if spent + outlay > cap + 1e-9:
                skipped.append(bet)
            else:
                kept.append(bet)
                spent += outlay
    return kept, skipped


def candidate(
    conn: sqlite3.Connection,
    bettor: dict,
    market: dict,
    predictions: list,
    prices: dict,
    game_date: str,
    opponent: Optional[str] = None,
    why: Optional[list] = None,
) -> Optional[dict]:
    """
    The bet one user's rules call for on a market the daily run just
    logged, or None (and, given a `why` list, the reason appended to it). Not placed yet: place() decides which candidates fit
    that user's daily cap.

    Args:
        bettor:      One of bettors(): whose rules, weights and key.
        market:      Kalshi market dict (the one whose predictions were logged).
        predictions: That market's LLMPrediction objects.
        prices:      Kalshi.get_prices(market) -- yes_ask/no_ask are what a bet pays.
        game_date:   ISO date the game is on (the daily cap is per game day).

    Returns decide()'s bet plus what place() records: user_id, mode, ticker,
    event_ticker, league, game_date, team, opponent, reason.
    """
    import AIWeights
    import SeasonType
    from EVCalculator import consensus_confidence, consensus_probability

    config = bettor["config"]
    mode = config["mode"]
    if mode not in MODES or mode == "off":
        return None
    ticker = market["ticker"]
    if conn.execute("SELECT 1 FROM bets WHERE ticker = ? AND mode = ? AND user_id = ?",
                    (ticker, mode, bettor["user_id"])).fetchone():
        if why is not None:
            why.append(f"already bet in {mode} mode")
        return None

    league = Leagues.league_for_ticker(ticker)
    weights = {p.llm_name: AIWeights.lookup(bettor["weights"], p.llm_name) for p in predictions}
    ai_prob = consensus_probability(predictions, weights)
    ai_confidence = consensus_confidence(predictions, weights)
    bid, ask = prices.get("yes_bid"), prices.get("yes_ask")
    market_prob = (bid + ask) / 2 if bid is not None and ask is not None else (ask if ask is not None else bid)
    model_prob = ai_prob
    if config.get("odds_model", "fair") == "fair":
        import FairOdds
        model = bettor.get("fair_model") or FairOdds.Model()  # no model: no trust, fair odds = Kalshi
        model_prob = model.fair(market_prob, ai_prob, ai_confidence)
    elif config.get("odds_model") == "favorite":
        model_prob = market_prob
    bet, reason = decide(model_prob, len(predictions), prices.get("yes_ask"), prices.get("no_ask"), league, config,
                         segment=SeasonType.segment_iso(game_date, league), ai_confidence=ai_confidence,
                         market_prob=market_prob)
    if bet is None:
        print(f"no bet for {bettor['username']} on {ticker}: {reason}")
        if why is not None:
            why.append(reason)
        return None
    # The bet keeps both: the raw AI odds, and the probability it was decided on.
    bet = {**bet, "ai_prob": ai_prob, "model_prob": model_prob}
    return {**bet, "ai_confidence": ai_confidence, "user_id": bettor["user_id"], "mode": mode, "ticker": ticker,
            "event_ticker": market.get("event_ticker"), "league": league, "game_date": game_date,
            "team": market.get("yes_sub_title"), "opponent": opponent, "reason": reason}


def place(conn: sqlite3.Connection, bettor: dict, candidates: list[dict],
          outcomes: Optional[dict] = None) -> list[dict]:
    """
    Place one user's candidates that fit under each day's cap, biggest edge
    first (see apply_daily_cap()), counting bets earlier runs already placed
    that day. Paper bets are just recorded; demo/live ones are sent to
    Kalshi with the user's key first (see send()), and only what fills is
    recorded. Returns the bets placed; given an `outcomes` dict, also
    records {ticker: what happened} for every candidate.
    """
    outcomes = outcomes if outcomes is not None else {}
    config = bettor["config"]
    kept, skipped = apply_daily_cap(
        candidates, config["max_daily_usd"],
        lambda game_date: spent_on(conn, bettor["user_id"], game_date, config["mode"])
    )
    placed = []
    for bet in kept:
        if bet["mode"] in TRADING_MODES:
            sent, problem = send(bet, bettor["creds"])
            if sent is None:
                outcomes[bet["ticker"]] = problem
                continue
            bet = sent
        conn.execute(
            "INSERT INTO bets (created_at, user_id, mode, ticker, event_ticker, league, game_date, team, opponent, "
            " side, price, count, cost, fee, ai_prob, edge, order_id, ai_confidence, model_prob) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (datetime.datetime.now().isoformat(timespec="seconds"), bet["user_id"], bet["mode"], bet["ticker"],
             bet["event_ticker"], bet["league"], bet["game_date"], bet["team"], bet["opponent"], bet["side"],
             bet["price"], bet["count"], bet["cost"], bet["fee"], bet["ai_prob"], bet["edge"], bet.get("order_id"),
             bet.get("ai_confidence"), bet.get("model_prob")),
        )
        # One commit per bet: a real order already went through, so a crash
        # on a later one mustn't lose the record of this one.
        conn.commit()
        placed.append(bet)
        outcomes[bet["ticker"]] = f"bet placed: {bet['reason']}"
        print(f"{bet['mode']} bet for {bettor['username']} on {bet['ticker']}: {bet['reason']}")
    for bet in skipped:
        outcomes[bet["ticker"]] = f"qualified, but the ${config['max_daily_usd']:.2f} daily cap went to bigger edges"
        print(f"no bet for {bettor['username']} on {bet['ticker']}: {bet['reason']}, but the "
              f"${config['max_daily_usd']:.2f} daily cap went to bigger edges")
    return placed


def record_balance(conn: sqlite3.Connection, user_id: int, mode: str, balance: float, force: bool = False) -> bool:
    """
    Note a user's demo/live account cash balance for the Betting tab's
    chart. Unless `force` (the daily run's once-a-day snapshot), a value
    equal to the last one recorded today is skipped, so page views don't
    pile up duplicate points. Returns whether a row was added.
    """
    now = datetime.datetime.now().isoformat(timespec="seconds")
    if not force:
        last = conn.execute(
            "SELECT recorded_at, balance FROM balance_history WHERE user_id = ? AND mode = ? "
            "ORDER BY recorded_at DESC, id DESC LIMIT 1", (user_id, mode)
        ).fetchone()
        if last and last[0][:10] == now[:10] and abs(last[1] - balance) < 0.005:
            return False
    conn.execute("INSERT INTO balance_history (recorded_at, user_id, mode, balance) VALUES (?, ?, ?, ?)",
                 (now, user_id, mode, round(balance, 2)))
    conn.commit()
    return True


def send(bet: dict, creds) -> tuple[Optional[dict], Optional[str]]:
    """
    Send a demo/live bet to Kalshi with `creds` (the user's key) as an
    immediate-or-cancel buy at its price or better. Returns (the bet as
    filled -- count, price, cost, fee and edge from the fill, plus order_id
    -- None), or (None, why) if nothing filled or Kalshi refused; either way
    the run goes on to the next bet.
    """
    import Kalshi

    try:
        fill = Kalshi.buy(bet["ticker"], bet["side"], bet["count"], bet["price"], creds)
    except Exception as e:
        print(f"{bet['mode']} order on {bet['ticker']} failed: {e}")
        return None, f"Kalshi refused the {bet['mode']} order: {e}"
    if not fill["count"]:
        why = f"nothing on offer at {bet['price']:.2f} or better any more, so the order didn't fill"
        print(f"no {bet['mode']} bet on {bet['ticker']}: {why}")
        return None, why
    decided_on = bet.get("model_prob", bet["ai_prob"])
    side_prob = decided_on if bet["side"] == "yes" else 1 - decided_on
    reason = (f"{bet['side'].upper()} x{fill['count']} at {fill['price']:.2f}, edge {side_prob - fill['price']:+.1%}"
              f" (order {fill['order_id']})")
    return {**bet, **fill, "edge": side_prob - fill["price"], "reason": reason}, None


def consider(conn: sqlite3.Connection, bettor: dict, market: dict, predictions: list, prices: dict,
             game_date: str, opponent: Optional[str] = None) -> Optional[dict]:
    """candidate() and place() for a single market: the bet placed, or None."""
    bet = candidate(conn, bettor, market, predictions, prices, game_date, opponent=opponent)
    placed = place(conn, bettor, [bet]) if bet else []
    return placed[0] if placed else None
