import unittest

import Tracker
from EVCalculator import LLMPrediction


def market(ticker, event=None):
    return {"ticker": ticker, "event_ticker": event or ticker + "-EVT", "title": ticker}


def pred(llm, prob, conf=0.8):
    return LLMPrediction(llm, prob, conf)


class TrackerTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = Tracker.connect(":memory:")

    def add(self, ticker, llm, prob, market_prob=0.5):
        Tracker.record_predictions(self.conn, market(ticker), market_prob, [pred(llm, prob)])


class RecordAndSettleTests(TrackerTestCase):
    def test_record_stores_one_row_per_llm(self):
        n = Tracker.record_predictions(
            self.conn, market("G1"), 0.54, [pred("A", 0.7), pred("B", 0.4)]
        )
        self.assertEqual(n, 2)
        rows = self.conn.execute(
            "SELECT llm, predicted_prob, confidence, market_prob, outcome FROM predictions ORDER BY llm"
        ).fetchall()
        self.assertEqual(rows, [("A", 0.7, 0.8, 0.54, None), ("B", 0.4, 0.8, 0.54, None)])

    def test_settle_sets_outcome_for_every_llm_on_that_ticker_only(self):
        self.add("G1", "A", 0.7)
        self.add("G1", "B", 0.6)
        self.add("G2", "A", 0.5)
        updated = Tracker.settle(self.conn, "G1", yes_won=True)
        self.assertEqual(updated, 2)
        outcomes = dict(self.conn.execute("SELECT ticker || llm, outcome FROM predictions").fetchall())
        self.assertEqual(outcomes, {"G1A": 1, "G1B": 1, "G2A": None})

    def test_settle_records_a_no_result_as_zero(self):
        self.add("G1", "A", 0.7)
        Tracker.settle(self.conn, "G1", yes_won=False)
        self.assertEqual(self.conn.execute("SELECT outcome FROM predictions").fetchone()[0], 0)

    def test_settling_twice_does_not_overwrite(self):
        self.add("G1", "A", 0.7)
        Tracker.settle(self.conn, "G1", yes_won=True)
        self.assertEqual(Tracker.settle(self.conn, "G1", yes_won=False), 0)
        self.assertEqual(self.conn.execute("SELECT outcome FROM predictions").fetchone()[0], 1)

    def test_unsettled_tickers(self):
        self.add("G1", "A", 0.7)
        self.add("G2", "A", 0.7)
        Tracker.settle(self.conn, "G1", yes_won=True)
        self.assertEqual(Tracker.unsettled_tickers(self.conn), ["G2"])


class SettleFromKalshiTests(TrackerTestCase):
    def test_settles_only_markets_with_a_result(self):
        for t in ("YESGAME", "NOGAME", "LIVEGAME"):
            self.add(t, "A", 0.6)
        results = {"YESGAME": "yes", "NOGAME": "no", "LIVEGAME": ""}

        settled = Tracker.settle_from_kalshi(
            self.conn, fetch_market=lambda t: {"ticker": t, "result": results[t]}
        )

        self.assertEqual(settled, {"YESGAME": True, "NOGAME": False})
        self.assertEqual(Tracker.unsettled_tickers(self.conn), ["LIVEGAME"])


class BrierTests(TrackerTestCase):
    def test_brier_score_is_mean_squared_error_against_outcome(self):
        self.add("G1", "A", 0.9)  # YES -> (0.9-1)^2 = 0.01
        self.add("G2", "A", 0.9)  # NO  -> (0.9-0)^2 = 0.81
        Tracker.settle(self.conn, "G1", True)
        Tracker.settle(self.conn, "G2", False)
        stats = Tracker.brier_scores(self.conn)["A"]
        self.assertEqual(stats["n"], 2)
        self.assertAlmostEqual(stats["brier"], (0.01 + 0.81) / 2)

    def test_market_brier_is_tracked_on_the_same_games(self):
        self.add("G1", "A", 0.9, market_prob=0.6)  # (0.6-1)^2 = 0.16
        Tracker.settle(self.conn, "G1", True)
        self.assertAlmostEqual(Tracker.brier_scores(self.conn)["A"]["market_brier"], 0.16)

    def test_unsettled_predictions_are_excluded(self):
        self.add("G1", "A", 0.9)
        self.assertEqual(Tracker.brier_scores(self.conn), {})

    def test_llms_are_scored_independently(self):
        self.add("G1", "Sharp", 1.0)
        self.add("G1", "Dull", 0.0)
        Tracker.settle(self.conn, "G1", True)
        scores = Tracker.brier_scores(self.conn)
        self.assertAlmostEqual(scores["Sharp"]["brier"], 0.0)
        self.assertAlmostEqual(scores["Dull"]["brier"], 1.0)


class WeightTests(TrackerTestCase):
    def test_brier_to_weight_mapping(self):
        self.assertEqual(Tracker.brier_to_weight(0.0), 1.0)      # perfect
        self.assertEqual(Tracker.brier_to_weight(0.125), 0.5)
        self.assertEqual(Tracker.brier_to_weight(0.25), 0.0)     # coin flip
        self.assertEqual(Tracker.brier_to_weight(0.6), 0.0)      # worse than a coin flip
        self.assertEqual(Tracker.brier_to_weight(-0.1), 1.0)     # never above 1

    def settle_games(self, llm, probs_and_outcomes):
        for i, (prob, yes_won) in enumerate(probs_and_outcomes):
            ticker = f"{llm}-G{i}"
            self.add(ticker, llm, prob)
            Tracker.settle(self.conn, ticker, yes_won)

    def test_llm_below_min_samples_keeps_the_default_weight(self):
        self.settle_games("A", [(1.0, True)] * 5)
        self.assertEqual(Tracker.compute_weights(self.conn, min_samples=20), {"A": 1.0})

    def test_default_weight_is_configurable(self):
        self.settle_games("A", [(1.0, True)] * 2)
        self.assertEqual(Tracker.compute_weights(self.conn, min_samples=20, default_weight=0.5), {"A": 0.5})

    def test_accurate_llm_gets_more_weight_than_inaccurate_llm(self):
        self.settle_games("Sharp", [(0.9, True)] * 10 + [(0.1, False)] * 10)
        self.settle_games("Coin", [(0.5, True)] * 10 + [(0.5, False)] * 10)
        weights = Tracker.compute_weights(self.conn, min_samples=20)
        self.assertAlmostEqual(weights["Sharp"], 1 - 0.01 / 0.25)
        self.assertAlmostEqual(weights["Coin"], 0.0)

    def test_weights_always_within_zero_and_one(self):
        self.settle_games("Wrong", [(1.0, False)] * 25)
        weights = Tracker.compute_weights(self.conn, min_samples=20)
        self.assertEqual(weights["Wrong"], 0.0)

    def test_unsettled_llm_appears_with_the_default(self):
        self.add("G1", "New", 0.7)
        self.assertEqual(Tracker.compute_weights(self.conn), {"New": 1.0})

    def test_weights_feed_straight_into_the_ev_calculator(self):
        from EVCalculator import EVCalculator

        self.settle_games("Sharp", [(0.9, True)] * 10 + [(0.1, False)] * 10)
        weights = Tracker.compute_weights(self.conn, min_samples=20)
        result = EVCalculator(weights=weights).calculate(0.5, [pred("Sharp", 0.8, 1.0)])
        self.assertAlmostEqual(result.ev, 0.3 * weights["Sharp"])


if __name__ == "__main__":
    unittest.main()
