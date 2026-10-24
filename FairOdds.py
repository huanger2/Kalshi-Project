"""
FairOdds.py

"Fair odds": the probability bets are decided on, anchored on Kalshi's
price and moved only as far as the AIs have earned.

Why: the AIs' odds hug 50% -- in log-odds they swing about a tenth as far
as Kalshi's prices. Compared with the ask directly, every favorite then
looks overpriced and every underdog cheap, so raw AI odds bet underdogs
almost every time, whatever the AIs actually know.

The model, for a game Kalshi prices at m with AI odds a and AI confidence c:

    surprise = logit(a) - (k0 + k1 * logit(m))
    fair     = sigmoid( logit(m) + beta * surprise + gamma * surprise * c )

1. Compression (k0, k1): how the AIs usually relate to the market --
   least squares of logit(AI odds) on logit(Kalshi) over every logged game.
   It needs no results, so it uses all of them. "surprise" is how much the
   AIs disagree *more than they usually would* about a game priced like
   this: it's centered, so it doesn't lean toward favorites or underdogs.

2. Skill (beta, gamma >= 0): how far that surprise should move the price,
   fitted on settled games by penalized logistic regression with Kalshi's
   price as the fixed offset -- Kalshi's own calibration is trusted. The
   penalty (lambda) pulls beta and gamma toward 0 (fair odds = Kalshi), and
   its strength is chosen by cross-validation over game days. They can't go
   negative: the AIs' view can be discounted to nothing, never inverted.
   gamma lets confident disagreement count more than unsure disagreement.

Until the AIs show skill on held-out days, beta = gamma = 0: fair odds are
Kalshi's price, so there's no edge and no bet. That's the point.

Replays (the What if? table, the Analytics optimizer) use walk_forward():
each day's fair odds come from a model fitted only on earlier days.

Games here are dicts with market_prob, ai_prob, ai_confidence, outcome,
tied and game_date (Optimizer._with_consensus() and queries.list_games()
both give that shape). Pure Python; fits are cached per weights and data.
"""

import math
from dataclasses import dataclass, field
from typing import Optional

LAMBDAS = [0.001, 0.003, 0.01, 0.03, 0.1, 0.3]
NO_TRUST = float("inf")  # lambda for "the AIs haven't earned anything": beta = gamma = 0
MAX_COEF = 5.0
MIN_GAMES = 20
MIN_DAYS = 4
CV_FOLDS = 5


def logit(p: float) -> float:
    p = min(max(p, 0.01), 0.99)
    return math.log(p / (1 - p))


def sigmoid(z: float) -> float:
    z = max(-30.0, min(30.0, z))
    return 1.0 / (1.0 + math.exp(-z))


@dataclass
class Model:
    k0: float = 0.0          # compression intercept
    k1: float = 0.0          # compression slope: logit(AI) per logit(Kalshi)
    beta: float = 0.0        # how far AI surprise moves the price
    gamma: float = 0.0       # ... extra, per unit of AI confidence
    lam: float = NO_TRUST    # the penalty chosen
    games: int = 0           # settled games it was fitted on
    held_out: dict = field(default_factory=dict)  # {"fair", "market", "ai"}: held-out Brier scores

    @property
    def trusts_ais(self) -> bool:
        return self.beta > 0 or self.gamma > 0

    def surprise(self, market_prob: float, ai_prob: float) -> float:
        return logit(ai_prob) - (self.k0 + self.k1 * logit(market_prob))

    def fair(self, market_prob: Optional[float], ai_prob: Optional[float],
             ai_confidence: Optional[float]) -> Optional[float]:
        """Fair odds for one game; Kalshi's price when the AIs are missing, None without a price."""
        if market_prob is None:
            return None
        if ai_prob is None or not self.trusts_ais:
            return market_prob
        r = self.surprise(market_prob, ai_prob)
        return sigmoid(logit(market_prob) + self.beta * r + self.gamma * r * (ai_confidence or 0.0))


def _usable(g: dict) -> bool:
    return g.get("market_prob") is not None and g.get("ai_prob") is not None


def _settled(games: list[dict]) -> list[dict]:
    return [g for g in games if _usable(g) and g.get("outcome") is not None and not g.get("tied")]


def compression(games: list[dict]) -> tuple[float, float]:
    """(k0, k1) from every game with both prices; (0, 0) with too few to say."""
    pts = [(logit(g["market_prob"]), logit(g["ai_prob"])) for g in games if _usable(g)]
    if len(pts) < 5:
        return 0.0, 0.0
    mx = sum(x for x, _ in pts) / len(pts)
    my = sum(y for _, y in pts) / len(pts)
    var = sum((x - mx) ** 2 for x, _ in pts)
    if var <= 0:
        return my, 0.0
    k1 = sum((x - mx) * (y - my) for x, y in pts) / var
    return my - k1 * mx, k1


def _fit_skill(games: list[dict], k0: float, k1: float, lam: float, start=(0.0, 0.0)) -> tuple[float, float]:
    """beta, gamma in [0, MAX_COEF] minimizing mean log loss + lam * (beta^2 + gamma^2) (damped projected Newton)."""
    if lam == NO_TRUST or not games:
        return 0.0, 0.0
    rows = []
    for g in games:
        lm = logit(g["market_prob"])
        r = logit(g["ai_prob"]) - (k0 + k1 * lm)
        rows.append((lm, r, r * (g.get("ai_confidence") or 0.0), g["outcome"]))
    n = len(rows)
    b0, b1 = start
    for _ in range(100):
        g0 = g1 = h00 = h01 = h11 = 0.0
        for lm, x0, x1, y in rows:
            p = sigmoid(lm + b0 * x0 + b1 * x1)
            w = p * (1 - p)
            g0 += (p - y) * x0
            g1 += (p - y) * x1
            h00 += w * x0 * x0
            h01 += w * x0 * x1
            h11 += w * x1 * x1
        g0, g1 = g0 / n + 2 * lam * b0, g1 / n + 2 * lam * b1
        h00, h01, h11 = h00 / n + 2 * lam, h01 / n, h11 / n + 2 * lam
        det = h00 * h11 - h01 * h01
        if det <= 0:
            break
        s0 = -(h11 * g0 - h01 * g1) / det
        s1 = -(-h01 * g0 + h00 * g1) / det
        new0 = min(MAX_COEF, max(0.0, b0 + 0.7 * s0))
        new1 = min(MAX_COEF, max(0.0, b1 + 0.7 * s1))
        if abs(new0 - b0) < 1e-7 and abs(new1 - b1) < 1e-7:
            b0, b1 = new0, new1
            break
        b0, b1 = new0, new1
    return b0, b1


def _brier(model: Model, games: list[dict]) -> float:
    return sum((model.fair(g["market_prob"], g["ai_prob"], g.get("ai_confidence")) - g["outcome"]) ** 2
               for g in games) / len(games)


def _folds(games: list[dict]) -> list[list[str]]:
    """Game days split into up to CV_FOLDS consecutive blocks."""
    days = sorted({g["game_date"] for g in games})
    size = max(1, math.ceil(len(days) / CV_FOLDS))
    return [days[i:i + size] for i in range(0, len(days), size)]


def fit(all_games: list[dict], settled: Optional[list[dict]] = None, lam: Optional[float] = None) -> Model:
    """
    The fair-odds model: compression from `all_games`, skill from the settled
    ones among them (or `settled`). With `lam` None, the penalty is chosen by
    cross-validation over blocks of game days, and the model records the
    held-out Brier scores of fair odds, Kalshi and the raw AI odds; too
    little data means no trust (fair odds = Kalshi).
    """
    k0, k1 = compression(all_games)
    games = _settled(settled if settled is not None else all_games)
    model = Model(k0=k0, k1=k1, games=len(games))
    if len(games) < MIN_GAMES or len({g["game_date"] for g in games}) < MIN_DAYS:
        return model
    if lam is None:
        folds = _folds(games)
        scores = {}
        for candidate in LAMBDAS + [NO_TRUST]:
            total = 0.0
            for fold in folds:
                train = [g for g in games if g["game_date"] not in fold]
                test = [g for g in games if g["game_date"] in fold]
                if not train or not test:
                    continue
                b, c = _fit_skill(train, k0, k1, candidate)
                total += _brier(Model(k0=k0, k1=k1, beta=b, gamma=c), test) * len(test)
            scores[candidate] = total / len(games)
        best = min(scores.values())
        # The most shrinkage within 0.1% of the best: trust the AIs only as much as clearly helps.
        lam = max((c for c, v in scores.items() if v <= best * 1.001), key=lambda c: c)
        model.held_out = {"fair": scores[lam], "market": scores[NO_TRUST],
                          "ai": sum((g["ai_prob"] - g["outcome"]) ** 2 for g in games) / len(games)}
    model.lam = lam
    model.beta, model.gamma = _fit_skill(games, k0, k1, lam)
    return model


def walk_forward(games: list[dict], lam: Optional[float] = None) -> dict[str, Optional[float]]:
    """
    {ticker: fair odds} where each game day's come from a model fitted only
    on settled games from earlier days (compression and the penalty use
    them all: neither looks at results of the day being priced, beyond the
    penalty's one number). For replaying the rules honestly.
    """
    if lam is None:
        lam = fit(games).lam
    k0, k1 = compression(games)
    settled = _settled(games)
    out = {}
    start = (0.0, 0.0)
    for day in sorted({g["game_date"] for g in games if g.get("game_date")}):
        before = [g for g in settled if g["game_date"] < day]
        enough = len(before) >= MIN_GAMES and len({g["game_date"] for g in before}) >= MIN_DAYS
        start = _fit_skill(before, k0, k1, lam, start) if enough else (0.0, 0.0)
        model = Model(k0=k0, k1=k1, beta=start[0], gamma=start[1])
        for g in games:
            if g.get("game_date") == day:
                out[g["ticker"]] = model.fair(g.get("market_prob"), g.get("ai_prob"), g.get("ai_confidence"))
    return out


def load(conn, weights: Optional[dict] = None) -> list[dict]:
    """Every logged game (settled or not) with its Kalshi price and the AI odds/confidence under `weights`."""
    from webapp import queries
    return queries.list_games(conn, weights=weights)


def model_for(conn, weights: Optional[dict] = None) -> Model:
    """The current fair-odds model for these AI weights (cached)."""
    return cached_fit(load(conn, weights), weights)


# Fits are a few hundred milliseconds at most, but pages and the live preview
# ask often: cache by the weights and a fingerprint of the data.
_cache: dict = {}


def _key(kind: str, weights: Optional[dict], games: list[dict]) -> tuple:
    settled = sum(1 for g in games if g.get("outcome") is not None)
    return (kind, tuple(sorted((weights or {}).items())), len(games), settled,
            max((g.get("game_date") or "" for g in games), default=""))


def cached_fit(games: list[dict], weights: Optional[dict]) -> Model:
    key = _key("fit", weights, games)
    if key not in _cache:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = fit(games)
    return _cache[key]


def cached_walk_forward(games: list[dict], weights: Optional[dict]) -> dict[str, Optional[float]]:
    key = _key("walk", weights, games)
    if key not in _cache:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = walk_forward(games, cached_fit(games, weights).lam)
    return _cache[key]
