"""
webapp/betting.py

The Betting tab, for whoever is signed in: their own auto-betting rules
(see Betting.py) and how their bets have done -- overall, broken down by
league / side / edge, and a what-if backtest that replays their rules
(with their AI weights) over every settled game already logged, to help
pick a threshold. Nobody sees anyone else's bets or rules.

A bet's P&L is derived at read time from its game's outcome in the
predictions table (Betting.result()), so this module only reads -- apart
from noting a demo/live account's balance when the page fetches it, for
the balance chart. Paper, demo and live bets are shown one mode at a time,
never added together.
"""

import sqlite3
from typing import Mapping, Optional

import Accounts
import AIWeights
import Betting
import Kalshi
import Leagues
from webapp import queries
from webapp.settings import MAX_WEIGHT, PROVIDERS, SettingsError, _number

BACKTEST_EDGES = [0.0, 0.02, 0.04, 0.06, 0.08, 0.10, 0.15, 0.20]
EDGE_BUCKETS = [(0.0, 0.05, "Under 5 pts"), (0.05, 0.10, "5-10 pts"), (0.10, 0.15, "10-15 pts"),
                (0.15, float("inf"), "15+ pts")]


def _confidence_from_predictions(conn: sqlite3.Connection, ticker: str, weights: dict) -> Optional[float]:
    """For a bet placed before confidence was recorded: its game's AI confidence with the user's weights now."""
    from EVCalculator import LLMPrediction, consensus_confidence

    rows = conn.execute("SELECT llm, predicted_prob, confidence FROM predictions WHERE ticker = ?", (ticker,)).fetchall()
    return consensus_confidence([LLMPrediction(llm, p, c, weight=AIWeights.lookup(weights, llm)) for llm, p, c in rows])


def list_bets(conn: sqlite3.Connection, user_id: int, mode: Optional[str] = None) -> list[dict]:
    """
    A user's bets (or their `mode` bets), newest game first, with each
    result (status/payout/pnl) filled in, and the AI confidence it was
    placed at (worked out from its predictions for bets from before that
    was recorded).
    """
    rows = conn.execute(
        "SELECT b.id, b.created_at, b.mode, b.ticker, b.league, b.game_date, b.team, b.opponent, b.side, "
        "       b.price, b.count, b.cost, b.fee, b.ai_prob, b.edge, b.ai_confidence, b.model_prob, "
        "       (SELECT MAX(outcome) FROM predictions p WHERE p.ticker = b.ticker), "
        "       (SELECT MAX(tied) FROM predictions p WHERE p.ticker = b.ticker) "
        "FROM bets b WHERE b.user_id = ? AND (? IS NULL OR b.mode = ?) ORDER BY b.game_date DESC, b.id DESC",
        (user_id, mode, mode),
    ).fetchall()
    keys = ["id", "created_at", "mode", "ticker", "league", "game_date", "team", "opponent", "side",
            "price", "count", "cost", "fee", "ai_prob", "edge", "ai_confidence", "model_prob"]
    bets = []
    weights = None
    for row in rows:
        bet = dict(zip(keys, row[:-2]))
        if bet["ai_confidence"] is None:
            weights = weights if weights is not None else AIWeights.for_user(conn, user_id)
            bet["ai_confidence"] = _confidence_from_predictions(conn, bet["ticker"], weights)
        bet.update(Betting.result(bet, row[-2], bool(row[-1])))
        bet["backing"] = bet["team"] if bet["side"] == "yes" else bet["opponent"]
        bets.append(bet)
    return bets


def summarize(bets: list[dict]) -> dict:
    """Totals over bets carrying cost/fee/status/pnl: ROI and win rate count settled bets only."""
    settled = [b for b in bets if b["status"] != "pending"]
    won = sum(b["status"] == "won" for b in settled)
    lost = sum(b["status"] == "lost" for b in settled)
    settled_outlay = sum(b["cost"] + b["fee"] for b in settled)
    pnl = sum(b["pnl"] for b in settled)
    return {
        "bets": len(bets),
        "pending": len(bets) - len(settled),
        "settled": len(settled),
        "won": won,
        "lost": lost,
        "staked": sum(b["cost"] + b["fee"] for b in bets),
        "fees": sum(b["fee"] for b in bets),
        "pnl": pnl,
        "roi": pnl / settled_outlay if settled_outlay else None,
        "win_rate": won / (won + lost) if won + lost else None,
    }


def breakdowns(bets: list[dict]) -> list[dict]:
    """Summaries grouped three ways: by league, by side, and by edge when placed."""
    def group(title, key_fn, order):
        rows = []
        for label in order:
            members = [b for b in bets if key_fn(b) == label]
            if members:
                rows.append({"label": label, **summarize(members)})
        return {"title": title, "rows": rows}

    def bucket(bet):
        return next(label for low, high, label in EDGE_BUCKETS if low <= bet["edge"] < high)

    return [
        group("League", lambda b: b["league"], list(Leagues.LEAGUES)),
        group("Side", lambda b: "YES (listed team)" if b["side"] == "yes" else "NO (its opponent)",
              ["YES (listed team)", "NO (its opponent)"]),
        group("Edge when placed", bucket, [label for _, _, label in EDGE_BUCKETS]),
    ]


def backtest(conn: sqlite3.Connection, config: dict, weights: Optional[dict] = None) -> list[dict]:
    """
    Replay the rules (all but min_edge, which each row varies) over every
    settled game logged so far, on fair odds (walk-forward, see FairOdds) or
    the raw AI odds as the rules say, at the prices recorded when it was logged:
    YES at its ask, NO at 1 - yes_bid. Each day's cap goes to its biggest
    edges, as in the daily run. One summary row per edge threshold, flagging
    the one in effect.
    """
    every = queries.list_games(conn, weights=weights)
    games = [g for g in every if g["outcome"] is not None]
    if config.get("odds_model", "fair") == "fair":
        # Each day priced by a model fitted only on the days before it, as the daily run would have.
        import FairOdds
        fair = FairOdds.cached_walk_forward(every, weights)
        games = [{**g, "ai_prob": fair.get(g["ticker"])} for g in games]
    edges = sorted({round(e, 6) for e in BACKTEST_EDGES + [config["min_edge"]]})
    rows = []
    for min_edge in edges:
        rules = {**config, "min_edge": min_edge}
        bets = []
        for g in games:
            no_ask = None if g["yes_bid"] is None else 1 - g["yes_bid"]
            bet, _ = Betting.decide(g["ai_prob"], g["ai_count"], g["yes_ask"], no_ask, g["league"], rules,
                                    segment=g["segment"], ai_confidence=g["ai_confidence"])
            if bet:
                bets.append({**bet, "game_date": g["game_date"], **Betting.result(bet, g["outcome"], g["tied"])})
        bets, _ = Betting.apply_daily_cap(bets, config["max_daily_usd"])
        rows.append({"min_edge": min_edge, "current": abs(min_edge - config["min_edge"]) < 1e-9,
                     **summarize(bets)})
    return rows


def balance_series(conn: sqlite3.Connection, user_id: int, mode: str, bets: list[dict]) -> dict:
    """
    The balance chart's line for `mode`. Demo/live: the Kalshi account's
    recorded cash balance (balance_history). Paper has no account, so its
    "balance" is the running total of settled P&L by game day, from $0.

    Returns {"kind": "balance"/"pnl", "points": [{"x": ISO date or time, "y": dollars}]}.
    """
    if mode in Betting.TRADING_MODES:
        rows = conn.execute(
            "SELECT recorded_at, balance FROM balance_history WHERE user_id = ? AND mode = ? "
            "ORDER BY recorded_at, id", (user_id, mode)
        ).fetchall()
        return {"kind": "balance", "points": [{"x": at, "y": round(balance, 2)} for at, balance in rows]}

    by_day: dict[str, float] = {}
    for bet in bets:
        if bet["status"] != "pending" and bet["game_date"]:
            by_day[bet["game_date"]] = by_day.get(bet["game_date"], 0.0) + bet["pnl"]
    points, total = [], 0.0
    for day in sorted(by_day):
        total += by_day[day]
        points.append({"x": day, "y": round(total, 2)})
    return {"kind": "pnl", "points": points}


def account(conn: sqlite3.Connection, user_id: int, mode: str) -> dict:
    """The user's Kalshi account a demo/live mode bets from: whether a key is saved, and its cash balance."""
    info = {"mode": mode, "credentials": False, "balance": None, "error": None}
    try:
        creds = Accounts.kalshi_credentials(conn, user_id, mode)
    except Exception as e:
        info["error"] = str(e)
        return info
    info["credentials"] = creds is not None
    if creds is not None:
        try:
            info["balance"] = Kalshi.get_balance(creds)
        except Exception as e:
            info["error"] = str(e)
    return info


def view(conn: sqlite3.Connection, user: dict, shown: Optional[str] = None) -> dict:
    """
    Everything the Betting page shows `user`. `shown` picks which of their
    bets the totals and list cover ("paper", "demo" or "live"); by default
    the mode in effect.
    """
    uid = user["id"]
    config = Betting.load_config(conn, uid)
    weights = AIWeights.for_user(conn, uid)
    with_bets = {row[0] for row in conn.execute("SELECT DISTINCT mode FROM bets WHERE user_id = ?", (uid,))}
    tabs = [m for m in Betting.MODES if m in with_bets or (m == config["mode"] and m != "off")]
    if shown not in tabs:
        shown = config["mode"] if config["mode"] in tabs else (tabs[0] if tabs else "paper")
    bets = list_bets(conn, uid, shown)
    acct = account(conn, uid, config["mode"]) if config["mode"] in Betting.TRADING_MODES else None
    if acct and acct["balance"] is not None:
        Betting.record_balance(conn, uid, acct["mode"], acct["balance"])
    settled_games = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM predictions WHERE outcome IS NOT NULL"
    ).fetchone()[0]
    return {
        "config": config,
        "modes": Betting.MODES,
        "shown": shown,
        "tabs": tabs,
        "account": acct,
        "keys": {env: Accounts.kalshi_key_info(conn, uid, env) is not None for env in Accounts.KALSHI_ENVS},
        "balance_chart": balance_series(conn, uid, shown, bets),
        "leagues": list(Leagues.LEAGUES),
        "segments": Betting.SEGMENTS,
        "bets": bets[:200],
        "bet_total": len(bets),
        "summary": summarize(bets),
        "breakdowns": breakdowns(bets),
        "backtest": backtest(conn, config, weights),
        "settled_games": settled_games,
        "providers": [{"key": key, "name": name, "weight": weights.get(key, 1.0),
                       "default": AIWeights.defaults().get(key, 1.0)} for key, name in PROVIDERS],
        "custom_weights": Accounts.get_setting(conn, uid, "ai_weights") is not None,
        "weights_brier": _brier(conn, config, weights),
        "fair": fair_view(conn, weights),
    }


def _parse(conn: sqlite3.Connection, user_id: int, form: Mapping, check_mode: bool = True) -> tuple[dict, Optional[dict]]:
    """
    The rules form as (rules, weights), or SettingsError listing every
    problem. Every field is a string except "leagues" and "segments", the
    lists of checked league codes and season segments. weights is None when
    the form has no weight fields (e.g. the Analytics tab applying rules).

    With check_mode, switching to live (real money) also needs
    "confirm_live" ticked, and demo/live need the user's Kalshi key for that
    exchange; a preview skips that, since the mode doesn't change a replay.
    """
    errors: list[str] = []
    mode = form.get("mode", "")
    if mode not in Betting.MODES:
        errors.append(f"Mode: {mode!r} isn't one of {', '.join(Betting.MODES)}.")
    elif check_mode and mode in Betting.TRADING_MODES and Accounts.kalshi_key_info(conn, user_id, mode) is None:
        errors.append(f"Mode: {mode} needs your Kalshi {mode} API key -- add it on the Settings tab first.")
    elif (check_mode and mode == "live" and Betting.load_config(conn, user_id)["mode"] != "live"
          and form.get("confirm_live") != "yes"):
        errors.append("Mode: tick the box confirming live bets spend real money.")

    def percent(name, label, maximum):
        value = _number(form.get(name, ""), label, errors, maximum=maximum, required=True)
        return None if value is None else round(value / 100, 6)

    min_edge = percent("min_edge", "Minimum edge (points)", 50)
    min_price = percent("min_price", "Lowest price (cents)", 99)
    max_price = percent("max_price", "Highest price (cents)", 99)
    stake = _number(form.get("stake_usd", ""), "Dollars per bet", errors, minimum=0.01, maximum=10000, required=True)
    daily = _number(form.get("max_daily_usd", ""), "Daily cap", errors, maximum=100000, required=True)
    min_ais = _number(form.get("min_ais", ""), "AIs that must answer", errors, minimum=1, maximum=10, required=True)
    # Optional in the form (older callers don't send it): blank means no minimum.
    min_confidence = (percent("min_confidence", "Minimum AI confidence (%)", 100)
                      if str(form.get("min_confidence", "")).strip() else 0.0)
    if min_ais is not None and min_ais != int(min_ais):
        errors.append("AIs that must answer: must be a whole number.")
    if min_price is not None and max_price is not None and min_price > max_price:
        errors.append("The lowest price can't be above the highest price.")
    leagues = [code for code in Leagues.LEAGUES if code in (form.get("leagues") or [])]
    segments = [seg for seg in Betting.SEGMENTS if seg in (form.get("segments") or [])]
    odds_model = form.get("odds_model") or "fair"
    if odds_model not in Betting.ODDS_MODELS:
        errors.append(f"Odds to bet on: {odds_model!r} isn't one of {', '.join(Betting.ODDS_MODELS)}.")

    weights = None
    if any(f"weight::{key}" in form for key, _ in PROVIDERS):
        weights = {key: _number(form.get(f"weight::{key}", ""), f"{name} weight", errors,
                                maximum=MAX_WEIGHT, required=True) for key, name in PROVIDERS}
        if not errors and not any(weights.values()):
            errors.append("At least one AI needs a weight above 0.")
    if errors:
        raise SettingsError(errors)

    rules = {"mode": mode, "min_edge": min_edge, "stake_usd": stake, "max_daily_usd": daily, "min_price": min_price,
             "max_price": max_price, "min_ais": int(min_ais), "min_confidence": min_confidence or 0.0,
             "odds_model": odds_model, "leagues": leagues, "segments": segments}
    return rules, weights


def fair_view(conn: sqlite3.Connection, weights: dict) -> dict:
    """What the fair-odds model currently makes of the AIs, for the Betting tab."""
    import FairOdds

    model = FairOdds.model_for(conn, weights)
    return {"trusts": model.trusts_ais, "beta": model.beta, "gamma": model.gamma, "k1": model.k1,
            "games": model.games, "held_out": model.held_out, "min_games": FairOdds.MIN_GAMES}


def save_config(conn: sqlite3.Connection, user_id: int, form: Mapping) -> None:
    """
    Validate the rules form and save the user's betting rules, and their AI
    weights if the form has them; any invalid field raises SettingsError,
    saving nothing. Weights are only stored as the user's own when they
    differ from what's in effect, so saving rules doesn't turn the site
    defaults into a frozen personal copy.
    """
    rules, weights = _parse(conn, user_id, form)
    Accounts.set_setting(conn, user_id, "betting", rules)
    if weights is not None and weights != AIWeights.for_user(conn, user_id):
        Accounts.set_setting(conn, user_id, "ai_weights", weights)


def reset_weights(conn: sqlite3.Connection, user_id: int) -> None:
    """Back to following the site-wide default weights."""
    Accounts.delete_setting(conn, user_id, "ai_weights")


def _brier(conn: sqlite3.Connection, rules: dict, weights: dict) -> dict:
    """The AI odds' Brier score with `weights`, on the settled games in the rules' leagues and season parts."""
    import Optimizer

    games = [g for g in Optimizer.load_games(conn, rules["segments"]) if g["league"] in rules["leagues"]]
    return {"brier": Optimizer.brier(games, weights), "games": len(Optimizer._scored(games))}


def preview(conn: sqlite3.Connection, user_id: int, form: Mapping) -> dict:
    """
    The what-if replay and the AI odds' Brier score for the rules form as
    it stands, unsaved -- the Betting tab updates its numbers from this as
    the user types. Raises SettingsError while a field is invalid.
    """
    rules, weights = _parse(conn, user_id, form, check_mode=False)
    effective = {**AIWeights.for_user(conn, user_id), **(weights or {})}
    return {"backtest": backtest(conn, rules, effective), **_brier(conn, rules, effective)}


def check_bets(conn: sqlite3.Connection, user_id: int, fetch_market=None) -> dict:
    """
    "Check bets": look up every one of the user's pending bets' games on
    Kalshi now and record any that have finished -- the same settling the
    daily run does, so it won't look those games up again. A market Kalshi
    can't be reached for is skipped and counted.

    Returns {"checked", "settled", "failed"}.
    """
    import Tracker

    if fetch_market is None:
        fetch_market = Kalshi.get_market
    pending = [row[0] for row in conn.execute(
        "SELECT DISTINCT b.ticker FROM bets b WHERE b.user_id = ? AND NOT EXISTS "
        "(SELECT 1 FROM predictions p WHERE p.ticker = b.ticker AND p.outcome IS NOT NULL) ORDER BY b.ticker",
        (user_id,))]
    failed = []

    def fetch(ticker):
        try:
            return fetch_market(ticker)
        except Exception as e:  # one unreachable market mustn't stop the rest
            print(f"could not check {ticker} on Kalshi: {e}")
            failed.append(ticker)
            return {}  # no result: left pending

    settled = Tracker.settle_markets(conn, pending, fetch)
    return {"checked": len(pending), "settled": len(settled), "failed": len(failed)}


def check_flash(checked: Optional[str], settled: Optional[str], failed: Optional[str]) -> Optional[str]:
    """The message after "Check bets", from its counts (query parameters, so strings)."""
    if checked is None:
        return None
    try:
        checked_n, settled_n, failed_n = int(checked), int(settled or 0), int(failed or 0)
    except ValueError:
        return None
    if not checked_n:
        return "No pending bets to check."
    still = checked_n - settled_n - failed_n
    parts = [f"Checked {checked_n} pending game{'' if checked_n == 1 else 's'} on Kalshi:",
             f"{settled_n} finished" + ("" if not settled_n else " and recorded") + ",",
             f"{still} still in progress or unsettled."]
    if failed_n:
        parts.append(f"{failed_n} couldn't be reached; try again shortly.")
    return " ".join(parts)


def flash(saved: Optional[str]) -> Optional[str]:
    return {"rules": "Rules saved.", "weights": "Back to the site's default AI weights."}.get(saved or "")
