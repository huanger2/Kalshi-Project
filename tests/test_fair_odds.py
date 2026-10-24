"""The fair-odds model (FairOdds.py) and betting on it."""

import math
import random
import unittest
from unittest import mock

import Betting
import FairOdds
import Optimizer


def logit(p):
    return math.log(p / (1 - p))


def sigmoid(z):
    return 1 / (1 + math.exp(-z))


def synthetic(skill: float, days: int = 30, per_day: int = 8, seed: int = 7, compress: float = 0.1) -> list[dict]:
    """
    Games whose AIs hug 50% (log-odds `compress` x Kalshi's), plus a private
    signal s. The true chance of the listed team is sigmoid(logit(m) + skill*s):
    skill 0 means the AIs know nothing Kalshi doesn't; skill > 0, they do.
    """
    rng = random.Random(seed)
    games = []
    for d in range(days):
        for i in range(per_day):
            m = rng.uniform(0.15, 0.85)
            s = rng.gauss(0, 1)
            ai = sigmoid(compress * logit(m) + 0.4 * s)
            outcome = int(rng.random() < sigmoid(logit(m) + skill * s))
            games.append({"ticker": f"T{d:02d}-{i}", "game_date": f"2026-11-{d + 1:02d}", "league": "NBA",
                          "segment": "regular", "market_prob": m, "yes_bid": m - 0.01, "yes_ask": m + 0.01,
                          "ai_prob": ai, "ai_confidence": 0.5, "ai_count": 3, "outcome": outcome, "tied": False})
    return games


RULES = {**Betting.DEFAULTS, "mode": "paper", "segments": ["regular"], "min_edge": 0.03}


class CompressionTests(unittest.TestCase):
    def test_it_measures_how_much_the_ais_hug_50(self):
        k0, k1 = FairOdds.compression(synthetic(skill=0.0))
        self.assertAlmostEqual(k1, 0.1, delta=0.05)

    def test_surprise_is_not_a_bet_on_underdogs(self):
        # For a heavy favorite where the AIs say what they usually say, there's no surprise.
        model = FairOdds.Model(k0=0.0, k1=0.1, beta=1.0)
        usual_ai = sigmoid(0.1 * logit(0.8))
        self.assertAlmostEqual(model.surprise(0.8, usual_ai), 0.0)
        self.assertAlmostEqual(model.fair(0.8, usual_ai, 0.5), 0.8)


class FitTests(unittest.TestCase):
    def test_no_skill_means_fair_odds_are_kalshis_price(self):
        model = FairOdds.fit(synthetic(skill=0.0))
        self.assertFalse(model.trusts_ais)
        self.assertEqual(model.fair(0.7, 0.55, 0.9), 0.7)

    def test_real_skill_is_learned(self):
        model = FairOdds.fit(synthetic(skill=1.5))
        self.assertTrue(model.trusts_ais)
        self.assertLess(model.held_out["fair"], model.held_out["market"])  # it earned the trust on unseen days
        # AIs more bullish than usual on a 50/50 game: fair odds move up, toward them.
        self.assertGreater(model.fair(0.5, 0.65, 0.5), 0.5)

    def test_the_ais_view_is_never_inverted(self):
        model = FairOdds.fit(synthetic(skill=-1.5))  # the AIs are reliably wrong
        self.assertEqual((model.beta, model.gamma), (0.0, 0.0))

    def test_too_few_games_means_no_trust(self):
        model = FairOdds.fit(synthetic(skill=3.0, days=2))
        self.assertFalse(model.trusts_ais)
        self.assertEqual(model.games, 16)


class BettingOnFairOddsTests(unittest.TestCase):
    def test_compressed_ais_without_skill_stop_the_underdog_bets(self):
        games = synthetic(skill=0.0)
        raw = Optimizer.simulate(games, RULES)
        underdogs = sum(1 for b in raw if b["price"] < 0.5)
        self.assertGreater(len(raw), 50)
        self.assertGreater(underdogs / len(raw), 0.8)  # the old behaviour: mostly underdogs
        fair = FairOdds.walk_forward(games)
        self.assertEqual(Optimizer.simulate([{**g, "ai_prob": fair[g["ticker"]]} for g in games], RULES), [])

    def test_with_skill_it_bets_both_ways_and_makes_money(self):
        games = synthetic(skill=1.5, days=40)
        fair = FairOdds.walk_forward(games)
        bets = Optimizer.simulate([{**g, "ai_prob": fair[g["ticker"]]} for g in games], RULES)
        self.assertGreater(len(bets), 10)
        sides = {b["price"] < 0.5 for b in bets}
        self.assertEqual(sides, {True, False})  # favorites and underdogs both
        self.assertGreater(Optimizer.summary(bets)["pnl"], 0)

    def test_walk_forward_never_uses_a_days_own_results(self):
        games = synthetic(skill=3.0)
        fair = FairOdds.walk_forward(games)
        first_days = [g for g in games if g["game_date"] <= "2026-11-03"]  # under MIN_GAMES settled before them
        self.assertTrue(all(fair[g["ticker"]] == g["market_prob"] for g in first_days))
        # Flipping the results of the last day changes nothing about that day's fair odds.
        last = max(g["game_date"] for g in games)
        flipped = [{**g, "outcome": 1 - g["outcome"]} if g["game_date"] == last else g for g in games]
        lam = FairOdds.fit(games).lam
        again = FairOdds.walk_forward(flipped, lam)
        before = FairOdds.walk_forward(games, lam)
        self.assertEqual({t: before[t] for t in before if t.startswith("T29")},
                         {t: again[t] for t in again if t.startswith("T29")})


class CandidateTests(unittest.TestCase):
    """Betting.candidate() decides on fair odds by default, and records both probabilities."""

    def bettor(self, model, odds_model="fair"):
        return {"user_id": 1, "username": "u", "weights": None, "creds": None, "fair_model": model,
                "config": {**RULES, "segments": ["preseason", "regular", "postseason"], "odds_model": odds_model}}

    def candidate(self, bettor):
        from EVCalculator import LLMPrediction
        conn = mock.Mock()
        conn.execute.return_value.fetchone.return_value = None  # not bet on yet
        market = {"ticker": "KXNBAGAME-26NOV01AAABBB-AAA", "event_ticker": "KXNBAGAME-26NOV01AAABBB",
                  "yes_sub_title": "Home"}
        preds = [LLMPrediction("OpenAI", 0.62, 0.8), LLMPrediction("Claude", 0.60, 0.8)]
        with mock.patch("builtins.print"):
            return Betting.candidate(conn, bettor, market, preds, {"yes_bid": 0.30, "yes_ask": 0.32, "no_ask": 0.70},
                                     "2026-11-01")

    def test_an_untrained_model_means_no_bet_where_raw_ai_odds_would_bet(self):
        self.assertIsNotNone(self.candidate(self.bettor(FairOdds.Model(), odds_model="ai")))  # 61% vs a 32c ask
        self.assertIsNone(self.candidate(self.bettor(FairOdds.Model())))

    def test_a_trusting_model_bets_and_records_both_odds(self):
        model = FairOdds.Model(k0=0.0, k1=0.1, beta=2.0)
        bet = self.candidate(self.bettor(model))
        self.assertIsNotNone(bet)
        self.assertAlmostEqual(bet["ai_prob"], 0.61)                 # what the AIs said
        self.assertAlmostEqual(bet["model_prob"], model.fair(0.31, 0.61, 0.8))  # what it was decided on
        self.assertAlmostEqual(bet["edge"], bet["model_prob"] - 0.32)


if __name__ == "__main__":
    unittest.main()
