import contextlib
import io
import unittest

import Probability
import Tracker
from Probability import (
    analyze_market,
    build_prompt,
    entry_price,
    one_market_per_event,
    parse_response,
)

MARKET = {
    "ticker": "KXNBAGAME-26OCT20OKCSAS-SAS",
    "event_ticker": "KXNBAGAME-26OCT20OKCSAS",
    "title": "San Antonio wins",
    "rules_primary": "If San Antonio wins the Oklahoma City vs San Antonio game, then the market resolves to Yes.",
    "yes_bid_dollars": "0.5300",
    "yes_ask_dollars": "0.5500",
    "no_bid_dollars": "0.4500",
    "no_ask_dollars": "0.4700",
}


class ParseResponseTests(unittest.TestCase):
    def test_two_line_format(self):
        self.assertEqual(parse_response("Probability: 72%\nConfidence: 80%", "x"), (0.72, 0.80))

    def test_single_line_format(self):
        self.assertEqual(parse_response("Probability: 61%, Confidence: 40%", "x"), (0.61, 0.40))

    def test_decimals_and_case_insensitive(self):
        prob, conf = parse_response("probability: 72.5 %\nCONFIDENCE: 66.5%", "x")
        self.assertAlmostEqual(prob, 0.725)
        self.assertAlmostEqual(conf, 0.665)

    def test_surrounding_chatter_is_ignored(self):
        text = "Sure! Here you go.\nProbability: 30%\nConfidence: 55%\nHope that helps."
        self.assertEqual(parse_response(text, "x"), (0.30, 0.55))

    def test_values_over_100_are_clamped(self):
        self.assertEqual(parse_response("Probability: 101%\nConfidence: 150%", "x"), (1.0, 1.0))

    def test_missing_percent_sign_is_rejected_not_misread(self):
        with self.assertRaises(ValueError):
            parse_response("Probability: 0.72\nConfidence: 80%", "x")

    def test_missing_field_raises_and_names_the_llm(self):
        with self.assertRaisesRegex(ValueError, "Claude"):
            parse_response("Probability: 70%", "Claude")

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_response("I cannot predict sports.", "x")


class BuildPromptTests(unittest.TestCase):
    def test_includes_title_and_game_rules(self):
        prompt = build_prompt(MARKET)
        self.assertIn("San Antonio wins", prompt)
        self.assertIn("Oklahoma City vs San Antonio", prompt)
        self.assertIn("Probability: X%", prompt)

    def test_works_without_rules(self):
        prompt = build_prompt({"title": "Will it rain?"})
        self.assertIn("Will it rain?", prompt)
        self.assertNotIn("Resolution rules", prompt)


class EntryPriceTests(unittest.TestCase):
    def test_yes_buys_at_the_yes_ask_and_no_at_the_no_ask(self):
        self.assertAlmostEqual(entry_price(MARKET, "YES"), 0.55)
        self.assertAlmostEqual(entry_price(MARKET, "NO"), 0.47)


class OneMarketPerEventTests(unittest.TestCase):
    def test_keeps_first_market_of_each_event(self):
        markets = [
            {"ticker": "G1-A", "event_ticker": "G1"},
            {"ticker": "G1-B", "event_ticker": "G1"},
            {"ticker": "G2-A", "event_ticker": "G2"},
        ]
        self.assertEqual([m["ticker"] for m in one_market_per_event(markets)], ["G1-A", "G2-A"])

    def test_market_without_event_ticker_is_kept(self):
        markets = [{"ticker": "X"}, {"ticker": "Y"}]
        self.assertEqual(len(one_market_per_event(markets)), 2)


def fake(text):
    return lambda prompt: text


def boom(prompt):
    raise RuntimeError("api down")


class AnalyzeMarketTests(unittest.TestCase):
    def run_quiet(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return analyze_market(MARKET, verbose=False, **kwargs)

    def test_recommends_yes_when_llms_are_above_the_market(self):
        # market implied = midpoint .54; edges .26*.9 and .16*.5 -> ev = .314 / 2
        llms = [
            ("A", fake("Probability: 80%\nConfidence: 90%")),
            ("B", fake("Probability: 70%\nConfidence: 50%")),
        ]
        bet, result = self.run_quiet(llms=llms, threshold=0.05)
        self.assertTrue(bet)
        self.assertEqual(result.side, "YES")
        self.assertAlmostEqual(result.kalshi_prob, 0.54)
        self.assertAlmostEqual(result.ev, 0.157)

    def test_recommends_no_when_llms_are_below_the_market(self):
        llms = [("A", fake("Probability: 20%\nConfidence: 90%"))]
        bet, result = self.run_quiet(llms=llms, threshold=0.05)
        self.assertTrue(bet)
        self.assertEqual(result.side, "NO")

    def test_no_bet_below_threshold(self):
        llms = [("A", fake("Probability: 55%\nConfidence: 50%"))]
        bet, _ = self.run_quiet(llms=llms, threshold=0.05)
        self.assertFalse(bet)

    def test_failing_and_unparseable_llms_are_skipped(self):
        llms = [
            ("Down", boom),
            ("Rambler", fake("no idea")),
            ("Good", fake("Probability: 80%\nConfidence: 90%")),
        ]
        _, result = self.run_quiet(llms=llms)
        self.assertEqual([p.llm_name for p in result.predictions], ["Good"])

    def test_all_llms_failing_raises(self):
        with self.assertRaises(RuntimeError):
            self.run_quiet(llms=[("Down", boom)])

    def test_historical_weights_scale_the_score(self):
        llms = [("A", fake("Probability: 80%\nConfidence: 100%"))]
        _, full = self.run_quiet(llms=llms, historical_weights={"A": 1.0})
        _, half = self.run_quiet(llms=llms, historical_weights={"A": 0.5})
        self.assertAlmostEqual(half.ev, full.ev / 2)

    def test_predictions_are_recorded_when_a_db_is_given(self):
        conn = Tracker.connect(":memory:")
        llms = [
            ("A", fake("Probability: 80%\nConfidence: 90%")),
            ("B", fake("Probability: 70%\nConfidence: 50%")),
        ]
        self.run_quiet(llms=llms, db_conn=conn)
        rows = conn.execute(
            "SELECT llm, predicted_prob, market_prob, outcome FROM predictions ORDER BY llm"
        ).fetchall()
        self.assertEqual(rows, [("A", 0.80, 0.54, None), ("B", 0.70, 0.54, None)])

    def test_nothing_is_recorded_without_a_db(self):
        # Just makes sure the db_conn=None path doesn't need Tracker at all.
        self.run_quiet(llms=[("A", fake("Probability: 80%\nConfidence: 90%"))])


if __name__ == "__main__":
    unittest.main()
