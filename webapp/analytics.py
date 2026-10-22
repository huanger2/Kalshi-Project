"""
webapp/analytics.py

The Analytics tab, for whoever is signed in: how well the AI odds (with
their own weights) have been calibrated, whether a bigger edge has actually
paid, and an optimizer (Optimizer.py) that suggests AI weights and betting
rules fitted to the settled games so far -- with honest checks next to the
suggestion, and buttons to apply it.

Everything on the tab -- diagnostics and the optimizer -- covers the parts
of the season picked in its filter (preseason, regular season,
postseason), by default the ones the user's betting rules bet on:
preseason results are noisy, since teams rest players and don't play to
win, so tuning on them can mislead.

A run's results are kept as the user's "analytics_run" setting (with the
season parts it used), so they survive the redirect after running or
applying, and the page can say when newer settled games, or a different
filter, have made a run stale.
"""

import datetime
import sqlite3
from typing import Optional

import Accounts
import AIWeights
import Betting
import Optimizer
from webapp import betting
from webapp.settings import MAX_WEIGHT, SettingsError

RUN_SETTING = "analytics_run"
RULE_KEYS = ["min_edge", "min_price", "max_price", "min_ais", "min_confidence"]
# A suggestion has to beat the current weights' held-out Brier by this much to be recommended.
BRIER_MARGIN = 0.001


def _verdicts(run: dict) -> dict:
    """Plain-language recommendations from a run's honest checks."""
    w = run["weights"]
    cv = w["cv_brier"]
    if w["suggested"] == w["current"]:
        weights = ("same", "The best fit is the weights you already have.")
    elif cv["suggested"] is not None and cv["current"] is not None and cv["suggested"] < cv["current"] - BRIER_MARGIN:
        weights = ("better", "These predicted held-out days better than your current weights did.")
    else:
        weights = ("unclear", "These didn't predict held-out days meaningfully better than your current weights. "
                              "There isn't enough data yet to tell the AIs apart.")

    r = run["rules"]
    wf = r["walk_forward"]
    if r["suggested"] is None:
        rules = ("none", f"No rule set placed the {r['min_bets']} bets needed to judge it.")
    # .get: runs saved before min_confidence existed don't have it (0 then meant no minimum).
    elif ({k: r["suggested"]["rules"].get(k, 0.0) for k in RULE_KEYS}
          == {k: r["current_rules"].get(k, 0.0) for k in RULE_KEYS}):
        rules = ("same", "The best rules are the ones you already have.")
    elif not wf["days"] or not wf["tuned"]["bets"]:
        rules = ("unclear", "Too few game days to check tuned rules on unseen days yet.")
    elif wf["tuned"]["pnl"] > wf["current"]["pnl"]:
        rules = ("better", "On days they weren't tuned on, rules chosen this way beat your current rules.")
    else:
        rules = ("worse", "On days they weren't tuned on, rules chosen this way did worse than your current "
                          "rules -- the better replay above is likely fitting luck. Keeping your rules is safer.")
    return {"weights": weights, "rules": rules}


def segments_for(conn: sqlite3.Connection, user: dict, picked: Optional[list[str]]) -> list[str]:
    """The season parts to analyze: those picked (in the usual order), else the ones the user bets on."""
    if picked:
        return [s for s in Betting.SEGMENTS if s in picked]
    return list(Betting.load_config(conn, user["id"])["segments"]) or list(Betting.SEGMENTS)


def view(conn: sqlite3.Connection, user: dict, segments: Optional[list[str]] = None) -> dict:
    """Everything the Analytics page shows `user`, for the `segments` parts of the season (see segments_for())."""
    uid = user["id"]
    segments = segments_for(conn, user, segments)
    weights = AIWeights.for_user(conn, uid)
    games = Optimizer.load_games(conn, segments)
    days = Optimizer.days_of(games)
    run = Accounts.get_setting(conn, uid, RUN_SETTING)
    run_segments = (run or {}).get("segments") or list(Betting.SEGMENTS)
    can_run = len(games) >= Optimizer.MIN_GAMES and len(days) >= Optimizer.MIN_DAYS
    counts = Optimizer.segment_counts(conn)
    return {
        "segments": segments,
        "segment_options": [{"key": s, "games": counts.get(s, 0), "checked": s in segments} for s in Betting.SEGMENTS],
        "run_segments": run_segments,
        "other_filter": bool(run) and run_segments != segments,
        "games": len(games),
        "days": len(days),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "can_run": can_run,
        "min_games": Optimizer.MIN_GAMES,
        "min_days": Optimizer.MIN_DAYS,
        "weights": weights,
        "sources": Optimizer.source_scores(games, weights),
        "calibration": Optimizer.calibration(games, weights),
        "edges": Optimizer.edge_returns(games, weights),
        "run": run,
        "stale": bool(run) and run_segments == segments and run.get("games") != len(games),
        "verdicts": _verdicts(run) if run else None,
    }


def run(conn: sqlite3.Connection, user: dict, segments: Optional[list[str]] = None) -> dict:
    """
    Optimize the user's weights, then their rules (using the suggested
    weights), on the `segments` parts of the season; keep and return the result.
    """
    uid = user["id"]
    segments = segments_for(conn, user, segments)
    games = Optimizer.load_games(conn, segments)
    if len(games) < Optimizer.MIN_GAMES or len(Optimizer.days_of(games)) < Optimizer.MIN_DAYS:
        raise SettingsError([f"Optimizing needs at least {Optimizer.MIN_GAMES} settled games over "
                             f"{Optimizer.MIN_DAYS} game days in the parts of the season picked; there are "
                             f"{len(games)} over {len(Optimizer.days_of(games))}."])
    rules = Betting.load_config(conn, uid)
    weights = Optimizer.optimize_weights(games, AIWeights.for_user(conn, uid))
    result = {
        "ran_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "segments": segments,
        "games": len(games),
        "days": len(Optimizer.days_of(games)),
        "weights": weights,
        "rules": {**Optimizer.optimize_rules(games, weights["suggested"], rules),
                  "current_rules": {k: rules[k] for k in RULE_KEYS + ["stake_usd", "max_daily_usd"]}},
    }
    Accounts.set_setting(conn, uid, RUN_SETTING, result)
    return result


def apply(conn: sqlite3.Connection, user: dict, which: str) -> list[str]:
    """
    Apply the last run's suggested weights, rules or both ("weights",
    "rules", "both") through the same validation as the Settings and
    Betting forms. Returns what was applied.
    """
    uid = user["id"]
    last = Accounts.get_setting(conn, uid, RUN_SETTING)
    if not last:
        raise SettingsError(["Run the optimizer first."])
    applied = []
    if which in ("weights", "both"):
        suggested = last["weights"]["suggested"]
        if any(not 0 <= v <= MAX_WEIGHT for v in suggested.values()):
            raise SettingsError(["The suggested weights are out of range; run the optimizer again."])
        Accounts.set_setting(conn, uid, "ai_weights", {**AIWeights.for_user(conn, uid), **suggested})
        applied.append("AI weights")
    if which in ("rules", "both"):
        suggested = (last["rules"].get("suggested") or {}).get("rules")
        if not suggested:
            raise SettingsError(["The last run had no suggested rules to apply."])
        current = Betting.load_config(conn, uid)
        merged = {**current, **suggested}
        betting.save_config(conn, uid, {
            "mode": merged["mode"],
            "min_edge": repr(merged["min_edge"] * 100), "min_price": repr(merged["min_price"] * 100),
            "max_price": repr(merged["max_price"] * 100), "stake_usd": repr(merged["stake_usd"]),
            "max_daily_usd": repr(merged["max_daily_usd"]), "min_ais": str(merged["min_ais"]),
            "min_confidence": repr(merged["min_confidence"] * 100),
            "leagues": merged["leagues"], "segments": merged["segments"],
            # Applying rules never changes the mode, so a live user isn't asked to re-confirm.
            "confirm_live": "yes" if current["mode"] == "live" else "",
        })
        applied.append("betting rules")
    if not applied:
        raise SettingsError([f"Nothing to apply for {which!r}."])
    return applied


def run_segments(conn: sqlite3.Connection, user: dict) -> list[str]:
    """The season parts the last run used (for returning to the same view after applying it)."""
    last = Accounts.get_setting(conn, user["id"], RUN_SETTING) or {}
    return last.get("segments") or list(Betting.SEGMENTS)


def flash(applied: Optional[str], ran: Optional[str]) -> Optional[str]:
    if applied:
        return f"Applied the suggested {applied}."
    if ran:
        return "Optimization finished."
    return None
