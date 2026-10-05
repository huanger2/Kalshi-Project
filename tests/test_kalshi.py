import datetime
import importlib
import os
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


class AuthHeaderTests(unittest.TestCase):
    """get_auth_headers() had no coverage before the whitespace-stripping fix."""

    def test_bearer_key_builds_authorization_header(self):
        with mock.patch.dict(os.environ, {"KALSHI_API_KEY": "secret", "KALSHI_API_KEY_ID": "key-id"}), \
             mock.patch.object(Kalshi, "load_dotenv"):
            headers = Kalshi.get_auth_headers("GET", "/markets")
        self.assertEqual(headers, {"Authorization": "Bearer secret", "KALSHI-ACCESS-KEY": "key-id"})

    def test_whitespace_is_stripped_from_the_key_and_key_id(self):
        # Regression: a trailing newline (e.g. from a stale OS-level env var
        # shadowing AIAPIKEYS.env) breaks the Authorization header outright.
        with mock.patch.dict(os.environ, {"KALSHI_API_KEY": "secret\n\n", "KALSHI_API_KEY_ID": "key-id\n\n"}), \
             mock.patch.object(Kalshi, "load_dotenv"):
            headers = Kalshi.get_auth_headers("GET", "/markets")
        self.assertEqual(headers, {"Authorization": "Bearer secret", "KALSHI-ACCESS-KEY": "key-id"})

    def test_env_var_is_loaded_with_override_so_a_stale_os_value_cannot_win(self):
        with mock.patch.object(Kalshi, "load_dotenv") as load_dotenv:
            with mock.patch.dict(os.environ, {"KALSHI_API_KEY": "secret"}):
                Kalshi.get_auth_headers("GET", "/markets")
        load_dotenv.assert_called_once_with("AIAPIKEYS.env", override=True)


class ImportSideEffectTests(unittest.TestCase):
    def test_importing_kalshi_makes_no_network_calls_and_needs_no_key(self):
        with mock.patch("requests.get", side_effect=AssertionError("network call at import")):
            importlib.reload(Kalshi)

    def test_llm_clients_are_not_created_at_import(self):
        import ClaudeAi, Gemini, OpenAi
        for module in (ClaudeAi, Gemini, OpenAi):
            importlib.reload(module)
            self.assertIsNone(module._client)


# ---------------------------------------------------------------------------
# NBA games by day (mocked, no network)
# ---------------------------------------------------------------------------

def game_market(ticker, team, result="", **prices):
    """A KXNBAGAME market for `ticker` (e.g. KXNBAGAME-26OCT03MIATOR-MIA)."""
    return {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "yes_sub_title": team,
        "result": result,
        **prices,
    }


OCT3 = datetime.date(2026, 10, 3)
OCT4 = datetime.date(2026, 10, 4)


class GameDateTests(unittest.TestCase):
    def test_reads_date_from_event_ticker(self):
        self.assertEqual(Kalshi.game_date({"event_ticker": "KXNBAGAME-26OCT03MIATOR"}), OCT3)

    def test_falls_back_to_market_ticker(self):
        self.assertEqual(Kalshi.game_date({"ticker": "KXNBAGAME-26DEC25LALBOS-LAL"}),
                         datetime.date(2026, 12, 25))

    def test_unparseable_ticker_is_none(self):
        self.assertIsNone(Kalshi.game_date({"ticker": "KXNBA-26"}))
        self.assertIsNone(Kalshi.game_date({"ticker": "KXNBAGAME-26XYZ03MIATOR"}))
        self.assertIsNone(Kalshi.game_date({}))


class GamesOnDayTests(unittest.TestCase):
    MARKETS = [
        game_market("KXNBAGAME-26OCT03MIATOR-MIA", "Miami", "yes"),
        game_market("KXNBAGAME-26OCT03MIATOR-TOR", "Toronto", "no"),
        game_market("KXNBAGAME-26OCT04UTADEN-UTA", "Utah"),
        game_market("KXNBAGAME-26OCT04UTADEN-DEN", "Denver"),
    ]

    def test_keeps_only_that_days_games(self):
        with mock.patch.object(Kalshi, "get_all_markets", return_value=self.MARKETS):
            games = Kalshi.get_nba_games_on(OCT4)
        self.assertEqual([m["ticker"] for m in games],
                         ["KXNBAGAME-26OCT04UTADEN-UTA", "KXNBAGAME-26OCT04UTADEN-DEN"])

    def test_queries_the_game_series_bounded_by_close_time(self):
        with mock.patch.object(Kalshi, "get_all_markets", return_value=[]) as get_all:
            Kalshi.get_nba_games_on(OCT4)
        kwargs = get_all.call_args.kwargs
        self.assertEqual(kwargs["market"], Kalshi.NBA_GAME_SERIES)
        self.assertIsNone(kwargs["status"])  # finished games today still count
        since = datetime.datetime.fromtimestamp(kwargs["min_close_ts"])
        self.assertLessEqual(since.date(), OCT4)

    def test_todays_games_uses_the_given_day(self):
        with mock.patch.object(Kalshi, "get_nba_games_on", return_value=[]) as on_day:
            Kalshi.get_todays_nba_games(today=OCT4)
        on_day.assert_called_once_with(OCT4)

    def test_min_close_ts_is_sent_to_the_api(self):
        response = mock.Mock(**{"json.return_value": {"markets": []}})
        with mock.patch("requests.get", return_value=response) as get:
            Kalshi.get_markets(series_ticker="KXNBAGAME", min_close_ts=123)
        self.assertEqual(get.call_args.kwargs["params"]["min_close_ts"], 123)


class CurrentOddsTests(unittest.TestCase):
    def test_returns_fresh_prices_and_implied_probability(self):
        market = game_market("KXNBAGAME-26OCT04UTADEN-DEN", "Denver",
                             yes_bid_dollars="0.60", yes_ask_dollars="0.62", last_price_dollars="0.61")
        with mock.patch.object(Kalshi, "get_market", return_value=market) as get:
            odds = Kalshi.get_current_odds("KXNBAGAME-26OCT04UTADEN-DEN")

        get.assert_called_once_with("KXNBAGAME-26OCT04UTADEN-DEN")
        self.assertEqual(odds["team"], "Denver")
        self.assertAlmostEqual(odds["yes_ask"], 0.62)
        self.assertAlmostEqual(odds["no_ask"], 0.40)  # 1 - yes_bid
        self.assertAlmostEqual(odds["implied_prob"], 0.61)

    def test_market_without_prices_raises(self):
        with mock.patch.object(Kalshi, "get_market", return_value={"ticker": "X"}):
            with self.assertRaises(ValueError):
                Kalshi.get_current_odds("X")


class ResultsTests(unittest.TestCase):
    def test_one_result_per_finished_game(self):
        markets = [
            game_market("KXNBAGAME-26OCT03MIATOR-MIA", "Miami", "yes"),
            game_market("KXNBAGAME-26OCT03MIATOR-TOR", "Toronto", "no"),
            game_market("KXNBAGAME-26OCT03BOSNYK-BOS", "Boston", "no"),
            game_market("KXNBAGAME-26OCT03BOSNYK-NYK", "New York", "yes"),
        ]
        with mock.patch.object(Kalshi, "get_nba_games_on", return_value=markets):
            results = Kalshi.get_nba_results(OCT3)
        self.assertEqual(results, [
            {"event_ticker": "KXNBAGAME-26OCT03MIATOR", "winner": "Miami", "loser": "Toronto"},
            {"event_ticker": "KXNBAGAME-26OCT03BOSNYK", "winner": "New York", "loser": "Boston"},
        ])

    def test_unfinished_games_are_left_out(self):
        markets = [
            game_market("KXNBAGAME-26OCT03MIATOR-MIA", "Miami"),
            game_market("KXNBAGAME-26OCT03MIATOR-TOR", "Toronto"),
        ]
        with mock.patch.object(Kalshi, "get_nba_games_on", return_value=markets):
            self.assertEqual(Kalshi.get_nba_results(OCT3), [])

    def test_yesterday_is_the_day_before_today(self):
        with mock.patch.object(Kalshi, "get_nba_results", return_value=[]) as results:
            Kalshi.get_yesterdays_nba_results(today=OCT4)
        results.assert_called_once_with(OCT3)

    def test_yesterday_across_a_month_boundary(self):
        with mock.patch.object(Kalshi, "get_nba_results", return_value=[]) as results:
            Kalshi.get_yesterdays_nba_results(today=datetime.date(2026, 11, 1))
        results.assert_called_once_with(datetime.date(2026, 10, 31))


if __name__ == "__main__":
    unittest.main()
