"""
Optimizer.py

Fits a user's AI weights and betting rules to the settled games logged so
far, for the Analytics tab. Small data is the whole problem here -- a few
dozen games -- so every step is built not to mistake luck for skill:

AI weights
    The consensus is EVCalculator.consensus_probability(): each AI's
    probability weighted by its own confidence times its weight. The fit
    picks weights that minimize the consensus's Brier score on settled
    games, plus a penalty for straying from equal weights (ridge-style
    shrinkage). How strong that penalty is (lambda) is chosen by
    leave-one-day-out cross-validation: fit on every other day, score the
    held-out day, and keep the lambda that predicts held-out days best. With
    little data that's usually a strong pull toward equal weights, which is
    the honest answer. Weights only matter relative to each other, so
    they're kept averaging 1.0.

Betting rules
    A grid search over the rules that decide *which* bets to take
    (min_edge, min_price, max_price, min_ais, min_confidence), each replayed over the
    settled games exactly like the Betting tab's what-if table (the user's
    stake, daily cap, leagues and seasons, which are about how much risk to
    take, stay as they are). Rules are ranked by a lower confidence bound
    of their per-bet return -- mean minus one standard error -- not by raw
    ROI, so a handful of lucky bets can't win; and a candidate needs a
    minimum number of bets to count at all.

    Walk-forward validation then asks whether tuning works at all: for each
    later game day, pick rules using only the days before it, and score them
    on that day. The out-of-sample result next to the user's current rules
    on the same days is the honest estimate; the in-sample one is not.

Pure Python (no numpy/scipy): the data is small and the search is a few
thousand cheap replays.
"""

import math
import random
import sqlite3
from typing import Iterable, Optional

import Betting

MAX_WEIGHT = 10.0
WEIGHT_STEP = 0.05          # the Settings form's step, so suggestions are enterable as-is
LAMBDAS = [0.0, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1]
MIN_GAMES = 20              # below this, the tab doesn't offer to optimize
MIN_DAYS = 4

GRID = {
    "min_edge": [0.0, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20],
    "min_price": [0.05, 0.10, 0.15, 0.20, 0.30],
    "max_price": [0.70, 0.80, 0.85, 0.90, 0.95],
}
Z = 1.0                     # the score is mean return - Z standard errors
BOOTSTRAP_SAMPLES = 2000


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def load_games(conn: sqlite3.Connection, segments: Optional[Iterable[str]] = None) -> list[dict]:
    """
    Every settled game, oldest first, each with its Kalshi prices, outcome
    and every AI's (name, probability, confidence) as "preds". `segments`
    keeps only those parts of the season ("preseason", "regular",
    "postseason"; SeasonType.segment()) -- preseason results are noisy,
    since teams rest players and don't play to win.
    """
    rows = conn.execute(
        "SELECT ticker, game_date, league, segment(game_date, league), yes_bid, yes_ask, market_prob, "
        "       outcome, tied, llm, predicted_prob, confidence "
        "FROM predictions WHERE outcome IS NOT NULL ORDER BY game_date, ticker, llm"
    ).fetchall()
    games: dict[str, dict] = {}
    for (ticker, day, league, segment, yes_bid, yes_ask, market_prob, outcome, tied,
         llm, prob, confidence) in rows:
        game = games.setdefault(ticker, {
            "ticker": ticker, "game_date": day, "league": league, "segment": segment,
            "yes_bid": yes_bid, "yes_ask": yes_ask, "market_prob": market_prob,
            "outcome": outcome, "tied": bool(tied), "preds": [],
        })
        game["preds"].append((llm.lower(), prob, confidence))
    wanted = None if segments is None else set(segments)
    return [g for g in games.values() if wanted is None or g["segment"] in wanted]


def segment_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Settled games in each part of the season, for the filter's labels."""
    counts: dict[str, int] = {}
    for g in load_games(conn):
        counts[g["segment"]] = counts.get(g["segment"], 0) + 1
    return counts


def ai_names(games: list[dict]) -> list[str]:
    return sorted({llm for g in games for llm, _, _ in g["preds"]})


def consensus(preds: list[tuple], weights: dict[str, float]) -> Optional[float]:
    """EVCalculator.consensus_probability() on (llm, p, c) tuples: sum(p*c*w) / sum(c*w)."""
    if not preds:
        return None
    num = den = 0.0
    for llm, p, c in preds:
        factor = c * weights.get(llm, 1.0)
        num += p * factor
        den += factor
    return num / den if den else sum(p for _, p, _ in preds) / len(preds)


def _scored(games: Iterable[dict]) -> list[dict]:
    """Games that can score a probability: settled with a winner (an NFL tie has no right answer)."""
    return [g for g in games if not g["tied"] and g["preds"]]


def brier(games: list[dict], weights: dict[str, float]) -> Optional[float]:
    scored = _scored(games)
    if not scored:
        return None
    return sum((consensus(g["preds"], weights) - g["outcome"]) ** 2 for g in scored) / len(scored)


def market_brier(games: list[dict]) -> Optional[float]:
    scored = [g for g in _scored(games) if g["market_prob"] is not None]
    if not scored:
        return None
    return sum((g["market_prob"] - g["outcome"]) ** 2 for g in scored) / len(scored)


def days_of(games: list[dict]) -> list[str]:
    return sorted({g["game_date"] for g in games})


# ---------------------------------------------------------------------------
# AI weights
# ---------------------------------------------------------------------------

def _normalized(weights: dict[str, float]) -> dict[str, float]:
    """Scaled to average 1.0 (the consensus doesn't care about scale; the shrinkage penalty does)."""
    mean = sum(weights.values()) / len(weights)
    return {k: v / mean for k, v in weights.items()} if mean > 0 else {k: 1.0 for k in weights}


def fit_weights(games: list[dict], llms: list[str], lam: float) -> dict[str, float]:
    """
    Weights (averaging 1.0) minimizing Brier + lam * mean((w - 1)^2), by
    moving weight between pairs of AIs in shrinking steps until no move
    helps. Small, deterministic, and enough for a handful of AIs.
    """
    scored = _scored(games)

    def objective(w):
        if not scored:
            return 0.0
        loss = sum((consensus(g["preds"], w) - g["outcome"]) ** 2 for g in scored) / len(scored)
        return loss + lam * sum((v - 1.0) ** 2 for v in w.values()) / len(w)

    w = {llm: 1.0 for llm in llms}
    best = objective(w)
    for step in (0.5, 0.25, 0.1, 0.05, 0.02, 0.01):
        improved = True
        while improved:
            improved = False
            for i in llms:
                for j in llms:
                    if i == j or w[j] < step - 1e-12 or w[i] + step > MAX_WEIGHT:
                        continue
                    candidate = {**w, i: w[i] + step, j: w[j] - step}
                    value = objective(candidate)
                    if value < best - 1e-12:
                        w, best, improved = candidate, value, True
    return w


def _rounded(weights: dict[str, float]) -> dict[str, float]:
    """To the Settings form's 0.05 step, still averaging about 1.0."""
    return {k: round(round(v / WEIGHT_STEP) * WEIGHT_STEP, 2) for k, v in _normalized(weights).items()}


def cross_validated_brier(games: list[dict], llms: list[str], lam: float) -> Optional[float]:
    """Leave-one-day-out: fit on every other day, score the held-out day's games; pooled over all days."""
    total, count = 0.0, 0
    for day in days_of(games):
        held_out = _scored(g for g in games if g["game_date"] == day)
        if not held_out:
            continue
        w = fit_weights([g for g in games if g["game_date"] != day], llms, lam)
        total += sum((consensus(g["preds"], w) - g["outcome"]) ** 2 for g in held_out)
        count += len(held_out)
    return total / count if count else None


def optimize_weights(games: list[dict], current: dict[str, float]) -> dict:
    """
    The suggested weights, and how they compare with the current and equal
    weights on Brier score -- in-sample, and cross-validated (each day
    predicted by weights fit without it).
    """
    llms = ai_names(games)
    cv = {lam: cross_validated_brier(games, llms, lam) for lam in LAMBDAS}
    # The best held-out score; on a near-tie (within 0.1%) prefer more shrinkage.
    best_cv = min(v for v in cv.values() if v is not None)
    lam = max(l for l, v in cv.items() if v is not None and v <= best_cv * 1.001)
    suggested = _rounded(fit_weights(games, llms, lam))
    equal = {llm: 1.0 for llm in llms}
    current = {llm: current.get(llm, 1.0) for llm in llms}
    return {
        "llms": llms,
        "suggested": suggested,
        "current": current,
        "lambda": lam,
        "games": len(_scored(games)),
        "brier": {"current": brier(games, current), "suggested": brier(games, suggested),
                  "equal": brier(games, equal), "market": market_brier(games)},
        # Fixed weights involve no fitting, so their held-out score is just their score.
        "cv_brier": {"suggested": cv[lam], "equal": brier(games, equal), "current": brier(games, current)},
        "single": {llm: brier(games, {other: (1.0 if other == llm else 0.0) for other in llms}) for llm in llms},
    }


# ---------------------------------------------------------------------------
# Betting rules
# ---------------------------------------------------------------------------

def confidence(preds: list[tuple], weights: dict[str, float]) -> Optional[float]:
    """EVCalculator.consensus_confidence() on (llm, p, c) tuples: sum(c*w) / sum(w)."""
    total = sum(weights.get(llm, 1.0) for llm, _, _ in preds)
    if not preds or total <= 0:
        return None
    return sum(c * weights.get(llm, 1.0) for llm, _, c in preds) / total


def _with_consensus(games: list[dict], weights: dict[str, float]) -> list[dict]:
    return [{**g, "ai_prob": consensus(g["preds"], weights), "ai_confidence": confidence(g["preds"], weights),
             "ai_count": len(g["preds"])} for g in games]


def simulate(games: list[dict], rules: dict) -> list[dict]:
    """
    The bets `rules` would have placed on games carrying "ai_prob" (see
    _with_consensus()), at the prices logged then -- YES at its ask, NO at
    1 - yes_bid -- with each day's cap going to its biggest edges, like the
    daily run. Each bet carries its result (Betting.result()).
    """
    bets = []
    for g in games:
        no_ask = None if g["yes_bid"] is None else 1 - g["yes_bid"]
        bet, _ = Betting.decide(g["ai_prob"], g["ai_count"], g["yes_ask"], no_ask, g["league"], rules,
                                segment=g["segment"], ai_confidence=g.get("ai_confidence"),
                                market_prob=g.get("market_prob"))
        if bet:
            bets.append({**bet, "game_date": g["game_date"], **Betting.result(bet, g["outcome"], g["tied"])})
    kept, _ = Betting.apply_daily_cap(bets, rules["max_daily_usd"])
    return kept


def returns(bets: list[dict]) -> list[float]:
    """Each bet's profit per dollar put in (cost + fee)."""
    return [b["pnl"] / (b["cost"] + b["fee"]) for b in bets if b["cost"] + b["fee"] > 0]


def score(bets: list[dict], min_bets: int) -> Optional[float]:
    """Mean per-bet return minus Z standard errors: high only when it's good *and* not just a few bets."""
    r = returns(bets)
    if len(r) < max(min_bets, 2):
        return None
    mean = sum(r) / len(r)
    sd = math.sqrt(sum((x - mean) ** 2 for x in r) / (len(r) - 1))
    return mean - Z * sd / math.sqrt(len(r))


def summary(bets: list[dict]) -> dict:
    outlay = sum(b["cost"] + b["fee"] for b in bets)
    pnl = sum(b["pnl"] for b in bets)
    won = sum(b["status"] == "won" for b in bets)
    lost = sum(b["status"] == "lost" for b in bets)
    return {"bets": len(bets), "staked": outlay, "pnl": pnl, "roi": pnl / outlay if outlay else None,
            "win_rate": won / (won + lost) if won + lost else None}


def bootstrap_roi(bets: list[dict], samples: int = BOOTSTRAP_SAMPLES, seed: int = 0) -> Optional[tuple]:
    """A 90% interval for ROI, resampling these bets with replacement: how much of it could be luck."""
    if len(bets) < 2:
        return None
    rng = random.Random(seed)
    rois = []
    for _ in range(samples):
        pick = [bets[rng.randrange(len(bets))] for _ in bets]
        outlay = sum(b["cost"] + b["fee"] for b in pick)
        rois.append(sum(b["pnl"] for b in pick) / outlay if outlay else 0.0)
    rois.sort()
    return rois[int(0.05 * samples)], rois[int(0.95 * samples) - 1]


CONFIDENCE_QUANTILES = [0.2, 0.4, 0.6, 0.8]


def confidence_grid(games: list[dict]) -> list[float]:
    """
    Minimum-confidence values worth trying: none (0), plus the levels below
    which 20/40/60/80% of these games' weighted AI confidence falls --
    taken from the data, since how confident the AIs say they are varies a
    lot (a fixed 50/60/70% grid could sit above every game). Whole percents.
    """
    values = sorted(g["ai_confidence"] for g in games if g.get("ai_confidence") is not None)
    grid = {0.0}
    for q in CONFIDENCE_QUANTILES if values else []:
        grid.add(round(values[min(len(values) - 1, int(q * len(values)))], 2))
    return sorted(grid)


def candidates(base: dict, max_ais: int, confidences: Iterable[float] = (0.0,)) -> list[dict]:
    """Every grid combination, the user's other rules (stake, cap, leagues, seasons, mode) kept."""
    found = []
    for min_edge in GRID["min_edge"]:
        for min_price in GRID["min_price"]:
            for max_price in GRID["max_price"]:
                for min_ais in range(1, max_ais + 1):
                    for min_confidence in confidences:
                        found.append({**base, "min_edge": min_edge, "min_price": min_price,
                                      "max_price": max_price, "min_ais": min_ais, "min_confidence": min_confidence})
    return found


def min_bets_for(games: list[dict]) -> int:
    """A rule set has to place at least this many bets to be judged: 10% of the games, and never under 5."""
    return max(5, round(0.1 * len(games)))


def best_rules(games: list[dict], base: dict, max_ais: int) -> Optional[tuple[dict, list[dict], float]]:
    """(rules, their bets, their score) for the best-scoring grid point, or None if none places enough bets."""
    need = min_bets_for(games)
    best = None
    for rules in candidates(base, max_ais, confidence_grid(games)):
        bets = simulate(games, rules)
        s = score(bets, need)
        # Ties go to more bets, then the stricter edge, then the lower confidence bar (fewer rules).
        key = None if s is None else (round(s, 9), len(bets), rules["min_edge"], -rules["min_confidence"])
        if key is not None and (best is None or key > best[0]):
            best = (key, rules, bets, s)
    return None if best is None else (best[1], best[2], best[3])


def walk_forward(games: list[dict], base: dict, max_ais: int, min_train_days: int = 3) -> dict:
    """
    For each game day after the first few: choose rules on the days before
    it only, then see how they did that day. Pooled over every such day,
    next to what the user's current rules did on the same days.
    """
    days = days_of(games)
    tuned, current = [], []
    tested = []
    for k in range(min_train_days, len(days)):
        train = [g for g in games if g["game_date"] < days[k]]
        test = [g for g in games if g["game_date"] == days[k]]
        found = best_rules(train, base, max_ais)
        if found:
            tuned += simulate(test, found[0])
        current += simulate(test, base)
        tested.append(days[k])
    return {"days": tested, "tuned": summary(tuned), "current": summary(current)}


def optimize_rules(games: list[dict], weights: dict[str, float], current_rules: dict,
                   fair: Optional[dict] = None) -> dict:
    """
    The suggested rules (with `weights` making the AI odds), their replayed
    results and a bootstrap interval, the current rules' results on the
    same games, and the walk-forward check.
    """
    with_ai = _with_consensus(games, weights)
    if fair is not None and current_rules.get("odds_model", "fair") == "fair":
        # Bet on walk-forward fair odds ({ticker: prob}, FairOdds.walk_forward()), as the daily run would.
        with_ai = [{**g, "ai_prob": fair.get(g["ticker"])} for g in with_ai]
    max_ais = max((len(g["preds"]) for g in games), default=1)
    found = best_rules(with_ai, current_rules, max_ais)
    current_bets = simulate(with_ai, current_rules)
    result = {
        "min_bets": min_bets_for(games),
        "current": {**summary(current_bets), "interval": bootstrap_roi(current_bets),
                    "score": score(current_bets, 2)},
        "walk_forward": walk_forward(with_ai, current_rules, max_ais),
        "suggested": None,
    }
    if found:
        rules, bets, s = found
        result["suggested"] = {
            "rules": {k: rules[k] for k in ("min_edge", "min_price", "max_price", "min_ais", "min_confidence")},
            **summary(bets), "interval": bootstrap_roi(bets), "score": s,
        }
    return result


# ---------------------------------------------------------------------------
# Always-on diagnostics
# ---------------------------------------------------------------------------

PROB_BUCKETS = [(i / 10, (i + 1) / 10) for i in range(10)]
EDGE_BUCKETS = [(-1.0, 0.0, "Negative"), (0.0, 0.05, "0-5 pts"), (0.05, 0.10, "5-10 pts"),
                (0.10, 0.15, "10-15 pts"), (0.15, 1.0, "15+ pts")]


def calibration(games: list[dict], weights: dict[str, float]) -> list[dict]:
    """
    For each tenth of predicted probability: how often the listed team
    actually won, for the AI consensus and for Kalshi's price. Well
    calibrated means the two numbers in a row are close.
    """
    scored = _scored(games)
    rows = []
    for low, high in PROB_BUCKETS:
        def bucket(prob_of):
            hits = [g for g in scored if prob_of(g) is not None
                    and (low <= prob_of(g) < high or (high == 1.0 and prob_of(g) == 1.0))]
            if not hits:
                return None
            return {"games": len(hits), "predicted": sum(prob_of(g) for g in hits) / len(hits),
                    "actual": sum(g["outcome"] for g in hits) / len(hits)}
        rows.append({"low": low, "high": high,
                     "ai": bucket(lambda g: consensus(g["preds"], weights)),
                     "market": bucket(lambda g: g["market_prob"])})
    return rows


def edge_returns(games: list[dict], weights: dict[str, float]) -> list[dict]:
    """
    Does a bigger edge actually pay? Every settled game's better side, one
    contract at its ask with Kalshi's fee, grouped by the edge the AI odds
    saw -- ignoring every other rule, so it's the raw signal.
    """
    rows = {label: [] for _, _, label in EDGE_BUCKETS}
    for g in games:
        ai = consensus(g["preds"], weights)
        if ai is None:
            continue
        sure = confidence(g["preds"], weights)
        sides = []
        if g["yes_ask"] is not None:
            sides.append(("yes", ai - g["yes_ask"], g["yes_ask"]))
        if g["yes_bid"] is not None:
            sides.append(("no", (1 - ai) - (1 - g["yes_bid"]), 1 - g["yes_bid"]))
        if not sides:
            continue
        side, edge, price = max(sides, key=lambda s: s[1])
        if not 0 < price < 1:
            continue
        fee = Betting.fee(1, price)
        bet = {"side": side, "count": 1, "cost": price, "fee": fee}
        res = Betting.result(bet, g["outcome"], g["tied"])
        label = next(lbl for lo, hi, lbl in EDGE_BUCKETS if lo <= edge < hi or (hi == 1.0 and edge >= hi))
        rows[label].append({**res, "cost": price, "fee": fee, "ai_confidence": sure, "edge": edge})
    return _bucket_rows(rows, [label for _, _, label in EDGE_BUCKETS])


def _bucket_rows(rows: dict[str, list[dict]], order: list[str]) -> list[dict]:
    """A summary() per non-empty bucket, plus its average AI confidence and edge."""
    out = []
    for label in order:
        bets = rows[label]
        if bets:
            sure = [b["ai_confidence"] for b in bets if b["ai_confidence"] is not None]
            out.append({"label": label, **summary(bets),
                        "ai_confidence": sum(sure) / len(sure) if sure else None,
                        "edge": sum(b["edge"] for b in bets) / len(bets)})
    return out


CONFIDENCE_BUCKETS = [(0.0, 0.3, "Under 30%"), (0.3, 0.4, "30-40%"), (0.4, 0.5, "40-50%"),
                      (0.5, 0.6, "50-60%"), (0.6, 1.01, "60%+")]


def confidence_returns(games: list[dict], weights: dict[str, float]) -> list[dict]:
    """
    Does confidence pay? The same one-contract replay as edge_returns(),
    grouped by the AIs' weighted confidence instead -- if the AIs know when
    they know, ROI should rise with it.
    """
    by_edge = {}
    for g in games:
        ai = consensus(g["preds"], weights)
        sure = confidence(g["preds"], weights)
        if ai is None or sure is None:
            continue
        sides = []
        if g["yes_ask"] is not None:
            sides.append(("yes", ai - g["yes_ask"], g["yes_ask"]))
        if g["yes_bid"] is not None:
            sides.append(("no", (1 - ai) - (1 - g["yes_bid"]), 1 - g["yes_bid"]))
        if not sides:
            continue
        side, edge, price = max(sides, key=lambda s: s[1])
        if not 0 < price < 1:
            continue
        fee = Betting.fee(1, price)
        res = Betting.result({"side": side, "count": 1, "cost": price, "fee": fee}, g["outcome"], g["tied"])
        label = next(lbl for lo, hi, lbl in CONFIDENCE_BUCKETS if lo <= sure < hi)
        by_edge.setdefault(label, []).append({**res, "cost": price, "fee": fee, "ai_confidence": sure, "edge": edge})
    order = [label for _, _, label in CONFIDENCE_BUCKETS]
    return _bucket_rows({label: by_edge.get(label, []) for label in order}, order)


def source_scores(games: list[dict], weights: dict[str, float]) -> list[dict]:
    """Brier score of each AI alone, the weighted consensus and Kalshi's price, on the same games."""
    llms = ai_names(games)
    rows = [{"label": f"{llm.capitalize() if llm != 'openai' else 'OpenAI'} alone",
             "brier": brier(games, {o: (1.0 if o == llm else 0.0) for o in llms})} for llm in llms]
    rows.append({"label": "AI odds (your weights)", "brier": brier(games, weights)})
    rows.append({"label": "Kalshi price", "brier": market_brier(games)})
    return rows
