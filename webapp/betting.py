"""
webapp/betting.py

The admin Betting tab: the auto-betting rules (betting.json, see Betting.py)
and how the bets have done -- overall, broken down by league / side / edge,
and a what-if backtest that replays the rules over every settled game
already logged, to help pick a threshold.

A bet's P&L is derived at read time from its game's outcome in the
predictions table (Betting.result()), so this module only reads.
"""

import sqlite3
from typing import Mapping, Optional

import Betting
import ConfigFiles
import DataDir
import Leagues
from webapp import queries
from webapp.settings import SettingsError, _number

BACKTEST_EDGES = [0.0, 0.02, 0.04, 0.06, 0.08, 0.10, 0.15, 0.20]
EDGE_BUCKETS = [(0.0, 0.05, "Under 5 pts"), (0.05, 0.10, "5-10 pts"), (0.10, 0.15, "10-15 pts"),
                (0.15, float("inf"), "15+ pts")]


def list_bets(conn: sqlite3.Connection) -> list[dict]:
    """Every bet, newest game first, with its result (status/payout/pnl) filled in."""
    rows = conn.execute(
        "SELECT b.id, b.created_at, b.mode, b.ticker, b.league, b.game_date, b.team, b.opponent, b.side, "
        "       b.price, b.count, b.cost, b.fee, b.ai_prob, b.edge, "
        "       (SELECT MAX(outcome) FROM predictions p WHERE p.ticker = b.ticker), "
        "       (SELECT MAX(tied) FROM predictions p WHERE p.ticker = b.ticker) "
        "FROM bets b ORDER BY b.game_date DESC, b.id DESC"
    ).fetchall()
    keys = ["id", "created_at", "mode", "ticker", "league", "game_date", "team", "opponent", "side",
            "price", "count", "cost", "fee", "ai_prob", "edge"]
    bets = []
    for row in rows:
        bet = dict(zip(keys, row[:-2]))
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


def backtest(conn: sqlite3.Connection, config: dict) -> list[dict]:
    """
    Replay the rules (all but min_edge, which each row varies, and the daily
    cap, which depends on order) over every settled game logged so far, at
    the prices recorded when it was logged: YES at its ask, NO at 1 - yes_bid.
    One summary row per edge threshold, flagging the one in effect.
    """
    games = [g for g in queries.list_games(conn) if g["outcome"] is not None]
    edges = sorted({round(e, 6) for e in BACKTEST_EDGES + [config["min_edge"]]})
    rows = []
    for min_edge in edges:
        rules = {**config, "min_edge": min_edge}
        bets = []
        for g in games:
            no_ask = None if g["yes_bid"] is None else 1 - g["yes_bid"]
            bet, _ = Betting.decide(g["ai_prob"], g["ai_count"], g["yes_ask"], no_ask, g["league"], rules,
                                    segment=g["segment"])
            if bet:
                bets.append({**bet, **Betting.result(bet, g["outcome"], g["tied"])})
        rows.append({"min_edge": min_edge, "current": abs(min_edge - config["min_edge"]) < 1e-9,
                     **summarize(bets)})
    return rows


def view(conn: sqlite3.Connection) -> dict:
    """Everything the Betting page shows."""
    config = Betting.load_config()
    bets = list_bets(conn)
    settled_games = conn.execute(
        "SELECT COUNT(DISTINCT ticker) FROM predictions WHERE outcome IS NOT NULL"
    ).fetchone()[0]
    return {
        "config": config,
        "modes": Betting.MODES,
        "leagues": list(Leagues.LEAGUES),
        "segments": Betting.SEGMENTS,
        "hosted": bool(DataDir.data_dir()),
        "file": {"name": Betting.BETTING_PATH.name, "override": ConfigFiles.has_override(Betting.BETTING_PATH)},
        "bets": bets[:200],
        "bet_total": len(bets),
        "summary": summarize(bets),
        "breakdowns": breakdowns(bets),
        "backtest": backtest(conn, config),
        "settled_games": settled_games,
    }


def save_config(form: Mapping) -> None:
    """
    Validate the rules form and save betting.json; any invalid field raises
    SettingsError, saving nothing. Every field is a string except "leagues"
    and "segments", the lists of checked league codes and season segments.
    """
    errors: list[str] = []
    mode = form.get("mode", "")
    if mode not in Betting.MODES:
        errors.append(f"Mode: {mode!r} isn't one of {', '.join(Betting.MODES)}.")

    def percent(name, label, maximum):
        value = _number(form.get(name, ""), label, errors, maximum=maximum, required=True)
        return None if value is None else round(value / 100, 6)

    min_edge = percent("min_edge", "Minimum edge (points)", 50)
    min_price = percent("min_price", "Lowest price (cents)", 99)
    max_price = percent("max_price", "Highest price (cents)", 99)
    stake = _number(form.get("stake_usd", ""), "Dollars per bet", errors, minimum=0.01, maximum=10000, required=True)
    daily = _number(form.get("max_daily_usd", ""), "Daily cap", errors, maximum=100000, required=True)
    min_ais = _number(form.get("min_ais", ""), "AIs that must answer", errors, minimum=1, maximum=10, required=True)
    if min_ais is not None and min_ais != int(min_ais):
        errors.append("AIs that must answer: must be a whole number.")
    if min_price is not None and max_price is not None and min_price > max_price:
        errors.append("The lowest price can't be above the highest price.")
    leagues = [code for code in Leagues.LEAGUES if code in (form.get("leagues") or [])]
    segments = [seg for seg in Betting.SEGMENTS if seg in (form.get("segments") or [])]
    if errors:
        raise SettingsError(errors)

    config = ConfigFiles.load(Betting.BETTING_PATH)
    config.update({"mode": mode, "min_edge": min_edge, "stake_usd": stake, "max_daily_usd": daily,
                   "min_price": min_price, "max_price": max_price, "min_ais": int(min_ais), "leagues": leagues,
                   "segments": segments})
    ConfigFiles.save(Betting.BETTING_PATH, config)


def reset_config() -> bool:
    """Hosted only: drop the volume copy of betting.json. False if there wasn't one."""
    return ConfigFiles.reset(Betting.BETTING_PATH)


def flash(saved: Optional[str]) -> Optional[str]:
    return {"rules": "Rules saved.", "reset": "Back to the repo's betting.json."}.get(saved or "")
