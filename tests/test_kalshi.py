import importlib
import unittest
from unittest import mock

import Kalshi

# Shape of a real KXNBAGAME market as returned by the Kalshi API (prices are
# dollar strings; there are no integer-cent yes_bid / yes_ask / last_price keys).
LIVE_MARKET = {
    "ticker": "KXNBAGAME-26OCT20OKCSAS-SAS",
    "event_ticker": "KXNBAGAME-26OCT20OKCSAS",
    "title": "San Antonio wins",
    "yes_sub_title": "San Antonio",
    "yes_bid_dollars": "0.5300",
    "yes_ask_dollars": "0.5500",
    "no_bid_dollars": "0.4500",
    "no_ask_dollars": "0.4700",
    "last_price_dollars": "0.5500",
    "result": "",
}


class PriceParsingTests(unittest.TestCase):
    def test_reads_dollar_string_prices(self):
        p = Kalshi.get_prices(LIVE_MARKET)
        self.assertAlmostEqual(p["yes_bid"], 0.53)
        self.assertAlmostEqual(p["yes_ask"], 0.55)
        self.assertAlmostEqual(p["no_ask"], 0.47)
        self.assertAlmostEqual(p["last"], 0.55)

    def test_reads_legacy_integer_cent_prices(self):
        p = Kalshi.get_prices({"yes_bid": 53, "yes_ask": 55, "last_price": 54})
        self.assertAlmostEqual(p["yes_bid"], 0.53)
        self.assertAlmostEqual(p["yes_ask"], 0.55)
        self.assertAlmostEqual(p["last"], 0.54)

    def test_missing_no_side_is_derived_from_yes_side(self):
        p = Kalshi.get_prices({"yes_bid_dollars": "0.40", "yes_ask_dollars": "0.44"})
        self.assertAlmostEqual(p["no_ask"], 0.60)  # 1 - yes_bid
        self.assertAlmostEqual(p["no_bid"], 0.56)  # 1 - yes_ask

    def test_missing_yes_side_is_derived_from_no_side(self):
        p = Kalshi.get_prices({"no_bid_dollars": "0.30", "no_ask_dollars": "0.35"})
        self.assertAlmostEqual(p["yes_ask"], 0.70)  # 1 - no_bid
        self.assertAlmostEqual(p["yes_bid"], 0.65)  # 1 - no_ask

    def test_blank_and_missing_values_are_none(self):
        p = Kalshi.get_prices({"yes_bid_dollars": "", "last_price_dollars": None})
        self.assertIsNone(p["last"])
        self.assertIsNone(p["yes_bid"])


class ImpliedProbTests(unittest.TestCase):
    def test_uses_bid_ask_midpoint_not_a_stale_last_trade(self):
        market = dict(LIVE_MARKET, last_price_dollars="0.9000")
        self.assertAlmostEqual(Kalshi.implied_yes_prob(market), 0.54)

    def test_falls_back_to_last_price_without_a_book(self):
        self.assertAlmostEqual(Kalshi.implied_yes_prob({"last_price_dollars": "0.61"}), 0.61)

    def test_regression_never_defaults_to_fifty_percent(self):
        # The original code read yes_bid/yes_ask/last_price, which the API no
        # longer returns, so every market silently priced at 50 cents.
        self.assertNotAlmostEqual(Kalshi.implied_yes_prob(LIVE_MARKET), 0.50)

    def test_no_price_data_raises(self):
        with self.assertRaises(ValueError):
            Kalshi.implied_yes_prob({"ticker": "X"})


class ResultTests(unittest.TestCase):
    def test_yes_no_and_unsettled(self):
        self.assertIs(Kalshi.get_market_result({"result": "yes"}), True)
        self.assertIs(Kalshi.get_market_result({"result": "NO"}), False)
        self.assertIsNone(Kalshi.get_market_result({"result": ""}))
        self.assertIsNone(Kalshi.get_market_result({}))


class PaginationTests(unittest.TestCase):
    def test_get_all_markets_follows_cursors(self):
        pages = [
            {"markets": [{"ticker": "A"}, {"ticker": "B"}], "cursor": "next"},
            {"markets": [{"ticker": "C"}], "cursor": ""},
        ]
        with mock.patch.object(Kalshi, "get_markets", side_effect=pages) as get:
            markets = Kalshi.get_all_markets(market="KXNBAGAME")
        self.assertEqual([m["ticker"] for m in markets], ["A", "B", "C"])
        self.assertEqual(get.call_args_list[1].kwargs["cursor"], "next")
        self.assertEqual(get.call_args_list[0].kwargs["series_ticker"], "KXNBAGAME")


class ImportSideEffectTests(unittest.TestCase):
    def test_importing_kalshi_makes_no_network_calls_and_needs_no_key(self):
        with mock.patch("requests.get", side_effect=AssertionError("network call at import")):
            importlib.reload(Kalshi)

    def test_llm_clients_are_not_created_at_import(self):
        import ClaudeAi, Gemini, OpenAi
        for module in (ClaudeAi, Gemini, OpenAi):
            importlib.reload(module)
            self.assertIsNone(module._client)


if __name__ == "__main__":
    unittest.main()
