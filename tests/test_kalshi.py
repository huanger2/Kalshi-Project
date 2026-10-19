import base64
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

    def test_a_scalar_settlement_is_a_tie(self):
        self.assertTrue(Kalshi.is_tie({"result": "scalar", "settlement_value_dollars": "0.5000"}))
        self.assertFalse(Kalshi.is_tie({"result": "no"}))
        self.assertFalse(Kalshi.is_tie({}))


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


def _rsa_key():
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(key, pkcs1=False) -> str:
    from cryptography.hazmat.primitives import serialization
    fmt = serialization.PrivateFormat.TraditionalOpenSSL if pkcs1 else serialization.PrivateFormat.PKCS8
    return key.private_bytes(serialization.Encoding.PEM, fmt, serialization.NoEncryption()).decode()


def _verify(key, headers, method, path):
    """Raises unless headers carry a valid signature over timestamp + method + path."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    msg = (headers["KALSHI-ACCESS-TIMESTAMP"] + method + path).encode()
    key.public_key().verify(base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]), msg,
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                            hashes.SHA256())


class PrivateKeyParsingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = _rsa_key()

    def same_key(self, parsed):
        self.assertEqual(parsed.private_numbers(), self.key.private_numbers())

    def test_plain_pem(self):
        self.same_key(Kalshi.parse_private_key(_pem(self.key)))

    def test_pkcs1_pem(self):
        self.same_key(Kalshi.parse_private_key(_pem(self.key, pkcs1=True)))

    def test_newlines_flattened_to_spaces(self):
        # How a key pasted into a one-line .env value ends up.
        self.same_key(Kalshi.parse_private_key(_pem(self.key).replace("\n", " ")))

    def test_literal_backslash_n_and_quotes(self):
        self.same_key(Kalshi.parse_private_key('"' + _pem(self.key).replace("\n", "\n") + '"'))

    def test_bare_base64_body(self):
        body = "".join(_pem(self.key).strip().splitlines()[1:-1])
        self.same_key(Kalshi.parse_private_key(body))

    def test_garbage_is_a_trading_error(self):
        with self.assertRaises(Kalshi.TradingError):
            Kalshi.parse_private_key("not a key")


class AuthHeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = _rsa_key()

    def env(self, **values):
        clean = {name: "" for names in Kalshi.CREDENTIAL_VARS.values() for name in names.values()}
        return mock.patch.dict(os.environ, {**clean, **values})

    def test_signs_with_the_key_from_the_env_var(self):
        with self.env(KALSHI_API_KEY=_pem(self.key).replace("\n", " "), KALSHI_API_KEY_ID="key-id\n\n"), \
             mock.patch.object(Kalshi, "load_dotenv"):
            headers = Kalshi.get_auth_headers("GET", "/trade-api/v2/portfolio/balance")
        self.assertEqual(headers["KALSHI-ACCESS-KEY"], "key-id")  # whitespace stripped
        _verify(self.key, headers, "GET", "/trade-api/v2/portfolio/balance")

    def test_demo_uses_its_own_keys(self):
        with self.env(KALSHI_API_KEY_ID="live-id", KALSHI_DEMO_API_KEY_ID="demo-id",
                      KALSHI_DEMO_API_KEY=_pem(self.key)), mock.patch.object(Kalshi, "load_dotenv"):
            headers = Kalshi.get_auth_headers("POST", "/x", env="demo")
            self.assertTrue(Kalshi.has_credentials("demo"))
        self.assertEqual(headers["KALSHI-ACCESS-KEY"], "demo-id")

    def test_missing_keys(self):
        with self.env(), mock.patch.object(Kalshi, "load_dotenv"), mock.patch("os.path.isfile", return_value=False):
            self.assertFalse(Kalshi.has_credentials("live"))
            self.assertFalse(Kalshi.has_credentials("demo"))
            with self.assertRaises(Kalshi.TradingError):
                Kalshi.get_auth_headers("GET", "/x", env="demo")

    def test_env_var_is_loaded_with_override_so_a_stale_os_value_cannot_win(self):
        with self.env(KALSHI_API_KEY_ID="id", KALSHI_API_KEY=_pem(self.key)), \
             mock.patch.object(Kalshi, "load_dotenv") as load_dotenv:
            Kalshi.get_auth_headers("GET", "/markets")
        load_dotenv.assert_called_once_with("AIAPIKEYS.env", override=True)


class TradingTests(unittest.TestCase):
    """get_balance() / buy() against a faked Kalshi: what's sent, and how the reply is read."""

    def setUp(self):
        patcher = mock.patch.object(Kalshi, "get_auth_headers", return_value={"KALSHI-ACCESS-KEY": "k"})
        self.auth = patcher.start()
        self.addCleanup(patcher.stop)

    def reply(self, body, status=200):
        response = mock.Mock(status_code=status)
        response.json.return_value = body
        return mock.patch("requests.request", return_value=response)

    def test_balance(self):
        with self.reply({"balance": 12345, "balance_dollars": "123.4500"}) as request:
            self.assertEqual(Kalshi.get_balance("demo"), 123.45)
        self.assertEqual(request.call_args.args[:2], ("GET", "https://demo-api.kalshi.co/trade-api/v2/portfolio/balance"))
        # The signature covers the full path from the root.
        self.auth.assert_called_once_with("GET", "/trade-api/v2/portfolio/balance", env="demo")

    def test_buying_yes_is_a_bid_at_the_yes_price(self):
        with self.reply({"order_id": "o1", "fill_count": "8.00", "remaining_count": "0.00",
                         "average_fill_price": "0.6100", "average_fee_paid": "0.0167"}) as request:
            fill = Kalshi.buy("KXNBAGAME-X-DEN", "yes", 8, 0.62, env="live", client_order_id="c1")
        method, url = request.call_args.args[:2]
        self.assertEqual((method, url), ("POST", Kalshi.BASE + "/portfolio/events/orders"))
        self.assertEqual(request.call_args.kwargs["json"], {
            "ticker": "KXNBAGAME-X-DEN", "side": "bid", "count": "8", "price": "0.6200",
            "time_in_force": "immediate_or_cancel", "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": "c1"})
        self.assertEqual(fill, {"order_id": "o1", "count": 8, "price": 0.61, "cost": 4.88, "fee": 0.14})

    def test_buying_no_is_an_ask_on_the_yes_leg(self):
        # NO at 40 cents is selling YES at 60; the fill comes back on the YES leg too.
        with self.reply({"order_id": "o2", "fill_count": "5", "average_fill_price": "0.6100",
                         "average_fee_paid": "0.0167"}) as request:
            fill = Kalshi.buy("T", "no", 12, 0.40, env="demo")
        body = request.call_args.kwargs["json"]
        self.assertEqual((body["side"], body["price"], body["count"]), ("ask", "0.6000", "12"))
        self.assertEqual((fill["count"], fill["price"], fill["cost"]), (5, 0.39, 1.95))

    def test_nothing_filled(self):
        with self.reply({"order_id": "o3", "fill_count": "0.00", "remaining_count": "0.00"}):
            self.assertEqual(Kalshi.buy("T", "yes", 3, 0.5, env="demo")["count"], 0)

    def test_a_refusal_raises_with_kalshis_message(self):
        with self.reply({"error": {"code": "insufficient_balance"}}, status=400):
            with self.assertRaisesRegex(Kalshi.TradingError, "insufficient_balance"):
                Kalshi.buy("T", "yes", 3, 0.5, env="live")


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
            games = Kalshi.get_games_on(OCT4)
        self.assertEqual([m["ticker"] for m in games],
                         ["KXNBAGAME-26OCT04UTADEN-UTA", "KXNBAGAME-26OCT04UTADEN-DEN"])

    def test_queries_the_given_leagues_series(self):
        with mock.patch.object(Kalshi, "get_all_markets", return_value=[]) as get_all:
            Kalshi.get_games_on(OCT4, "NHL")
        self.assertEqual(get_all.call_args.kwargs["market"], "KXNHLGAME")

    def test_reads_the_date_from_an_mlb_ticker_with_a_start_time(self):
        self.assertEqual(Kalshi.game_date({"event_ticker": "KXMLBGAME-26OCT172000TBCLE"}),
                         datetime.date(2026, 10, 17))

    def test_queries_the_game_series_bounded_by_close_time(self):
        with mock.patch.object(Kalshi, "get_all_markets", return_value=[]) as get_all:
            Kalshi.get_games_on(OCT4)
        kwargs = get_all.call_args.kwargs
        self.assertEqual(kwargs["market"], "KXNBAGAME")
        self.assertIsNone(kwargs["status"])  # finished games today still count
        since = datetime.datetime.fromtimestamp(kwargs["min_close_ts"])
        self.assertLessEqual(since.date(), OCT4)

    def test_todays_games_uses_the_given_day(self):
        with mock.patch.object(Kalshi, "get_games_on", return_value=[]) as on_day:
            Kalshi.get_todays_games(today=OCT4)
        on_day.assert_called_once_with(OCT4, "NBA")

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
        with mock.patch.object(Kalshi, "get_games_on", return_value=markets):
            results = Kalshi.get_results(OCT3)
        self.assertEqual(results, [
            {"event_ticker": "KXNBAGAME-26OCT03MIATOR", "winner": "Miami", "loser": "Toronto", "tie": False},
            {"event_ticker": "KXNBAGAME-26OCT03BOSNYK", "winner": "New York", "loser": "Boston", "tie": False},
        ])

    def test_an_nfl_tie_is_a_result_with_no_winner(self):
        markets = [
            game_market("KXNFLGAME-26OCT11NYGWAS-WAS", "Washington", "scalar", settlement_value_dollars="0.5000"),
            game_market("KXNFLGAME-26OCT11NYGWAS-NYG", "New York G", "scalar", settlement_value_dollars="0.5000"),
        ]
        with mock.patch.object(Kalshi, "get_games_on", return_value=markets):
            results = Kalshi.get_results(OCT3, "NFL")
        self.assertEqual(results, [
            {"event_ticker": "KXNFLGAME-26OCT11NYGWAS", "winner": None, "loser": None, "tie": True},
        ])

    def test_unfinished_games_are_left_out(self):
        markets = [
            game_market("KXNBAGAME-26OCT03MIATOR-MIA", "Miami"),
            game_market("KXNBAGAME-26OCT03MIATOR-TOR", "Toronto"),
        ]
        with mock.patch.object(Kalshi, "get_games_on", return_value=markets):
            self.assertEqual(Kalshi.get_results(OCT3), [])

    def test_yesterday_is_the_day_before_today(self):
        with mock.patch.object(Kalshi, "get_results", return_value=[]) as results:
            Kalshi.get_yesterdays_results(today=OCT4)
        results.assert_called_once_with(OCT3, "NBA")

    def test_yesterday_across_a_month_boundary(self):
        with mock.patch.object(Kalshi, "get_results", return_value=[]) as results:
            Kalshi.get_yesterdays_results(today=datetime.date(2026, 11, 1))
        results.assert_called_once_with(datetime.date(2026, 10, 31), "NBA")


if __name__ == "__main__":
    unittest.main()
