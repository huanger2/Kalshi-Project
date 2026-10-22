"""Paper, demo and live betting per user (Betting.py), its hook in the daily run, and the Betting tab (webapp/betting.py)."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import Accounts
import AIWeights
import Betting
import Kalshi
import Tracker
from EVCalculator import LLMPrediction
from tests.test_daily_log import LLMS, OCT4, TODAY_MARKETS, DailyLogTestCase, game_market
from webapp import betting
from webapp.main import app, get_db

RULES = {**Betting.DEFAULTS, "mode": "paper", "min_edge": 0.05, "stake_usd": 5.0, "max_daily_usd": 50.0,
         "min_price": 0.10, "max_price": 0.90, "min_ais": 2, "leagues": ["NBA", "NFL"]}


def _add_user(db_path, name="admin", rules=None, admin=True, password="s3cret") -> int:
    """A user in the test database, with `rules` as their betting rules. Returns their id."""
    conn = Tracker.connect(db_path)
    try:
        uid = Accounts.create_user(conn, name, password, is_admin=admin, check_rules=False)["id"]
        if rules is not None:
            Accounts.set_setting(conn, uid, "betting", rules)
        return uid
    finally:
        conn.close()


def _set_rules(db_path, uid, rules):
    conn = Tracker.connect(db_path)
    try:
        Accounts.set_setting(conn, uid, "betting", rules)
    finally:
        conn.close()


def fake_credentials(conn, user_id, env):
    """Stands in for a saved Kalshi key: Kalshi.buy/get_balance are mocked wherever it's used."""
    return Kalshi.Credentials(env, f"key-{user_id}", object())


def fake_key_info(conn, user_id, env):
    return {"key_id": f"key-{user_id}", "saved_at": "2026-10-01T09:00:00"}


class BettingConfigTestCase(unittest.TestCase):
    """A throwaway data folder, and every AI weighted 1.0 whatever ai_weights.json says."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        for patcher in (mock.patch.object(AIWeights, "get_weight", return_value=1.0),
                        mock.patch.dict("os.environ")):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("DATA_DIR", None)


class FeeTests(unittest.TestCase):
    def test_seven_percent_of_price_times_its_complement_rounded_up_to_a_cent(self):
        self.assertEqual(Betting.fee(100, 0.50), 1.75)
        self.assertEqual(Betting.fee(10, 0.50), 0.18)
        self.assertEqual(Betting.fee(1, 0.50), 0.02)

    def test_cheaper_at_the_extremes(self):
        self.assertLess(Betting.fee(100, 0.10), Betting.fee(100, 0.50))


class DecideTests(unittest.TestCase):
    def decide(self, ai_prob=0.70, ai_count=3, yes_ask=0.60, no_ask=0.42, league="NBA", ai_confidence=0.8, **rules):
        return Betting.decide(ai_prob, ai_count, yes_ask, no_ask, league, {**RULES, **rules},
                              ai_confidence=ai_confidence)

    def test_bets_yes_when_the_ai_odds_beat_the_yes_ask(self):
        bet, _ = self.decide()
        self.assertEqual(bet["side"], "yes")
        self.assertAlmostEqual(bet["edge"], 0.10)
        self.assertEqual(bet["count"], 8)  # $5 at 60 cents
        self.assertAlmostEqual(bet["cost"], 4.80)
        self.assertEqual(bet["fee"], Betting.fee(8, 0.60))

    def test_bets_no_when_the_ai_odds_are_well_under_the_market(self):
        bet, _ = self.decide(ai_prob=0.30, yes_ask=0.52, no_ask=0.50)
        self.assertEqual(bet["side"], "no")
        self.assertAlmostEqual(bet["edge"], 0.20)

    def test_no_bet_under_the_minimum_edge(self):
        bet, reason = self.decide(ai_prob=0.63)
        self.assertIsNone(bet)
        self.assertIn("under 5.0%", reason)

    def test_sides_priced_outside_the_range_are_skipped(self):
        bet, _ = self.decide(ai_prob=0.99, yes_ask=0.95, no_ask=0.06)
        self.assertIsNone(bet)

    def test_needs_enough_ais(self):
        self.assertIsNone(self.decide(ai_count=1)[0])

    def test_only_listed_leagues(self):
        self.assertIsNone(self.decide(league="MLB")[0])

    def test_no_bet_under_the_confidence_minimum_however_big_the_edge(self):
        bet, reason = self.decide(ai_prob=0.90, min_confidence=0.60, ai_confidence=0.55)
        self.assertIsNone(bet)
        self.assertIn("AI confidence 55% is under 60%", reason)
        self.assertIsNotNone(self.decide(ai_prob=0.90, min_confidence=0.60, ai_confidence=0.60)[0])

    def test_a_zero_confidence_minimum_is_off(self):
        self.assertIsNotNone(self.decide(min_confidence=0.0, ai_confidence=None)[0])
        self.assertIsNone(self.decide(min_confidence=0.3, ai_confidence=None)[0])  # unknown never clears a minimum

    def test_a_missing_ask_rules_out_only_that_side(self):
        bet, _ = self.decide(ai_prob=0.30, yes_ask=None, no_ask=0.50)
        self.assertEqual(bet["side"], "no")

    def test_a_stake_smaller_than_one_contract_is_no_bet(self):
        self.assertIsNone(self.decide(stake_usd=0.50)[0])

    def test_only_ticked_parts_of_the_season(self):
        rules = {**RULES, "segments": ["regular", "postseason"]}
        bet, reason = Betting.decide(0.70, 3, 0.60, 0.42, "NBA", rules, segment="preseason")
        self.assertIsNone(bet)
        self.assertIn("preseason", reason)
        self.assertIsNotNone(Betting.decide(0.70, 3, 0.60, 0.42, "NBA", rules, segment="regular")[0])
        self.assertIsNotNone(Betting.decide(0.70, 3, 0.60, 0.42, "NBA", rules)[0])  # no game date: rule skipped


class DailyCapTests(unittest.TestCase):
    @staticmethod
    def bet(name, edge, outlay, day="2026-10-04"):
        return {"ticker": name, "edge": edge, "cost": outlay, "fee": 0.0, "game_date": day}

    def names(self, bets):
        return [b["ticker"] for b in bets]

    def test_biggest_edges_first(self):
        kept, skipped = Betting.apply_daily_cap(
            [self.bet("small", 0.05, 5), self.bet("big", 0.20, 5), self.bet("mid", 0.10, 5)], cap=10)
        self.assertEqual((self.names(kept), self.names(skipped)), (["big", "mid"], ["small"]))

    def test_a_cheaper_bet_can_fill_what_a_bigger_one_cant(self):
        kept, _ = Betting.apply_daily_cap(
            [self.bet("a", 0.20, 6), self.bet("b", 0.15, 6), self.bet("c", 0.10, 3)], cap=10)
        self.assertEqual(self.names(kept), ["a", "c"])

    def test_each_day_has_its_own_cap_and_counts_earlier_spending(self):
        bets = [self.bet("d1", 0.10, 5, "2026-10-04"), self.bet("d2", 0.10, 5, "2026-10-05")]
        kept, _ = Betting.apply_daily_cap(bets, cap=6, spent_for=lambda day: 4 if day == "2026-10-04" else 0)
        self.assertEqual(self.names(kept), ["d2"])


class ResultTests(unittest.TestCase):
    BET = {"side": "yes", "count": 10, "cost": 6.0, "fee": 0.17}

    def test_a_winning_yes_bet_pays_a_dollar_a_contract(self):
        self.assertEqual(Betting.result(self.BET, 1), {"status": "won", "payout": 10.0, "pnl": 3.83})

    def test_a_losing_bet_loses_cost_and_fee(self):
        self.assertEqual(Betting.result(self.BET, 0)["pnl"], -6.17)

    def test_a_no_bet_wins_when_yes_loses(self):
        self.assertEqual(Betting.result({**self.BET, "side": "no"}, 0)["status"], "won")

    def test_a_tie_pays_half(self):
        self.assertEqual(Betting.result(self.BET, 0, tied=True), {"status": "tie", "payout": 5.0, "pnl": -1.17})

    def test_pending_until_settled(self):
        self.assertEqual(Betting.result(self.BET, None)["status"], "pending")


class DailyRunBettingTests(DailyLogTestCase):
    """
    TODAY_MARKETS' Denver game with LLMS: AI odds (70@80, 65@60, 55@50) come
    to 64.5% against a 62-cent YES ask (NO ask 40): YES has a 2.5 point edge.
    """

    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(AIWeights, "get_weight", return_value=1.0),
                        mock.patch.object(Accounts, "kalshi_credentials", side_effect=fake_credentials)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.uid = _add_user(self.db_path, "bettor", admin=False)
        self.set_rules(min_edge=0.02)

    def set_rules(self, uid=None, **changes):
        _set_rules(self.db_path, uid or self.uid, {**RULES, **changes})

    def bets(self):
        conn = Tracker.connect(self.db_path)
        try:
            return conn.execute("SELECT ticker, mode, side, price, count, cost, game_date, team, opponent "
                                "FROM bets ORDER BY id").fetchall()
        finally:
            conn.close()

    def bettor(self, conn, **rules):
        return {"user_id": self.uid, "username": "bettor", "config": {**RULES, **rules},
                "weights": None, "creds": fake_credentials(conn, self.uid, rules.get("mode", "paper"))}

    def test_a_game_with_enough_edge_gets_a_paper_bet(self):
        self.run_day(OCT4, TODAY_MARKETS[:2])
        self.assertEqual(self.bets(), [
            ("KXNBAGAME-26OCT04UTADEN-DEN", "paper", "yes", 0.62, 8, 4.96, "2026-10-04", "Denver", "Utah"),
        ])

    def test_no_bet_under_the_threshold(self):
        self.set_rules(min_edge=0.05)
        self.run_day(OCT4, TODAY_MARKETS[:2])
        self.assertEqual(self.bets(), [])

    def test_no_bet_on_a_preseason_game_when_preseason_is_unticked(self):
        # Oct 4 is NBA preseason (the shipped leagues.json starts the regular season Oct 20).
        self.set_rules(min_edge=0.02, segments=["regular", "postseason"])
        self.run_day(OCT4, TODAY_MARKETS[:2])
        self.assertEqual(self.bets(), [])

    def test_no_bets_while_off(self):
        self.set_rules(mode="off", min_edge=0.0)
        self.run_day(OCT4, TODAY_MARKETS[:2])
        self.assertEqual(self.bets(), [])

    def test_a_new_user_bets_nothing_until_they_turn_it_on(self):
        conn = Tracker.connect(self.db_path)
        try:
            Accounts.delete_setting(conn, self.uid, "betting")  # never saved any rules: mode off
        finally:
            conn.close()
        self.run_day(OCT4, TODAY_MARKETS[:2])
        self.assertEqual(self.bets(), [])

    def test_each_user_bets_by_their_own_rules(self):
        # A second user with a stricter threshold gets no bet on the 2.5 point edge.
        other = _add_user(self.db_path, "careful", admin=False)
        self.set_rules(uid=other, min_edge=0.05)
        self.run_day(OCT4, TODAY_MARKETS[:2])
        conn = Tracker.connect(self.db_path)
        try:
            owners = conn.execute("SELECT user_id FROM bets").fetchall()
        finally:
            conn.close()
        self.assertEqual(owners, [(self.uid,)])

    def test_each_users_own_ai_weights_decide_their_bets(self):
        # Weighting out the two keenest AIs leaves 55%: a NO edge of 5 points (45% vs 40c), not YES.
        conn = Tracker.connect(self.db_path)
        try:
            Accounts.set_setting(conn, self.uid, "ai_weights", {"openai": 0, "claude": 0, "gemini": 1})
        finally:
            conn.close()
        self.run_day(OCT4, TODAY_MARKETS[:2])
        self.assertEqual([b[2] for b in self.bets()], ["no"])

    def test_too_little_ai_confidence_means_no_bet(self):
        # LLMS say 80%, 60% and 50% confident: 63% averaged with equal weights.
        self.set_rules(min_edge=0.02, min_confidence=0.65)
        self.run_day(OCT4, TODAY_MARKETS[:2])
        self.assertEqual(self.bets(), [])

    def test_confidence_is_averaged_by_the_users_ai_weights(self):
        # Tripling OpenAI's weight (it's 80% confident) lifts the average to 70%: over 65%, so a bet.
        conn = Tracker.connect(self.db_path)
        try:
            Accounts.set_setting(conn, self.uid, "ai_weights", {"openai": 3, "claude": 1, "gemini": 1})
        finally:
            conn.close()
        self.set_rules(min_edge=0.02, min_confidence=0.65)
        self.run_day(OCT4, TODAY_MARKETS[:2])
        self.assertEqual(len(self.bets()), 1)

    def test_the_daily_cap_goes_to_the_biggest_edge_not_the_first_game(self):
        # Two games, each about $5 with its fee. Denver's (logged first) YES edge
        # is 2.5 points; Golden State's (59-cent ask) is 5.5, so it gets the money.
        self.set_rules(min_edge=0.0, max_daily_usd=6.0)
        self.run_day(OCT4, TODAY_MARKETS)
        self.assertEqual([b[0] for b in self.bets()], ["KXNBAGAME-26OCT04GSWLAC-GSW"])

    def test_a_later_run_only_gets_whats_left_under_the_cap(self):
        self.set_rules(min_edge=0.0, max_daily_usd=6.0)
        self.run_day(OCT4, TODAY_MARKETS[:2])
        self.run_day(OCT4, TODAY_MARKETS)  # Golden State shows up later the same day
        self.assertEqual([b[0] for b in self.bets()], ["KXNBAGAME-26OCT04UTADEN-DEN"])

    def test_a_betting_failure_still_logs_the_game(self):
        for name in ("candidate", "place", "load_config", "bettors"):
            with self.subTest(failing=name), \
                 mock.patch.object(Betting, name, side_effect=RuntimeError("boom")):
                sheet = self.run_day(OCT4, TODAY_MARKETS[:2])
                self.assertEqual(len(sheet), 1)

    def test_the_same_market_is_not_bet_twice(self):
        conn = Tracker.connect(self.db_path)
        try:
            market = game_market("KXNBAGAME-26OCT04UTADEN-DEN", "Denver", ask="0.50")
            preds = [LLMPrediction("OpenAI", 0.8, 1.0), LLMPrediction("Claude", 0.8, 1.0)]
            prices = {"yes_ask": 0.50, "no_ask": 0.52}
            with mock.patch("builtins.print"):
                self.assertIsNotNone(Betting.consider(conn, self.bettor(conn), market, preds, prices, "2026-10-04"))
                self.assertIsNone(Betting.consider(conn, self.bettor(conn), market, preds, prices, "2026-10-04"))
        finally:
            conn.close()

    def assert_sent_and_recorded_as_filled(self, mode):
        self.set_rules(mode=mode, min_edge=0.02)
        # A partial fill a cent better than the 62-cent ask the rules were checked against.
        fill = {"order_id": f"{mode}-1", "count": 6, "price": 0.61, "cost": 3.66, "fee": 0.06}
        with mock.patch("Kalshi.buy", return_value=fill) as buy, mock.patch("Kalshi.get_balance", return_value=1.0):
            self.run_day(OCT4, TODAY_MARKETS[:2])
        buy.assert_called_once_with("KXNBAGAME-26OCT04UTADEN-DEN", "yes", 8, 0.62, mock.ANY)
        creds = buy.call_args.args[4]
        self.assertEqual((creds.env, creds.key_id), (mode, f"key-{self.uid}"))  # this user's key for this exchange
        conn = Tracker.connect(self.db_path)
        try:
            row = conn.execute("SELECT mode, count, price, cost, fee, order_id, edge, user_id FROM bets").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[:6], (mode, 6, 0.61, 3.66, 0.06, f"{mode}-1"))
        self.assertAlmostEqual(row[6], 0.0347, places=3)  # the edge at the price actually paid
        self.assertEqual(row[7], self.uid)

    def test_demo_bets_are_sent_to_kalshi_and_recorded_as_filled(self):
        self.assert_sent_and_recorded_as_filled("demo")

    def test_live_bets_are_sent_to_kalshi_and_recorded_as_filled(self):
        self.assert_sent_and_recorded_as_filled("live")

    def test_demo_or_live_without_a_saved_key_places_nothing(self):
        self.set_rules(mode="demo", min_edge=0.02)
        with mock.patch.object(Accounts, "kalshi_credentials", return_value=None), \
             mock.patch("Kalshi.buy", side_effect=AssertionError("sent without a key")):
            self.assertEqual(len(self.run_day(OCT4, TODAY_MARKETS[:2])), 1)
        self.assertEqual(self.bets(), [])

    def test_a_demo_run_records_the_balance_even_without_bets(self):
        self.set_rules(mode="demo", min_edge=0.5)
        with mock.patch("Kalshi.get_balance", return_value=187.25):
            self.run_day(OCT4, TODAY_MARKETS[:2])
        conn = Tracker.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT user_id, mode, balance FROM balance_history").fetchall(),
                             [(self.uid, "demo", 187.25)])
        finally:
            conn.close()

    def test_a_balance_failure_still_logs_the_game(self):
        self.set_rules(mode="live", min_edge=0.5)
        with mock.patch("Kalshi.get_balance", side_effect=RuntimeError("HTTP 401")):
            self.assertEqual(len(self.run_day(OCT4, TODAY_MARKETS[:2])), 1)

    def test_paper_bets_never_reach_kalshi(self):
        with mock.patch("Kalshi.buy", side_effect=AssertionError("sent a paper bet")):
            self.run_day(OCT4, TODAY_MARKETS[:2])
        self.assertEqual(len(self.bets()), 1)

    def test_an_order_that_fills_nothing_or_fails_records_no_bet(self):
        self.set_rules(mode="demo", min_edge=0.02)
        nothing = {"order_id": "o", "count": 0, "price": 0.62, "cost": 0.0, "fee": 0.0}
        for result in ({"return_value": nothing}, {"side_effect": RuntimeError("HTTP 400 insufficient_balance")}):
            with self.subTest(**{k: str(v) for k, v in result.items()}), mock.patch("Kalshi.buy", **result), \
                 mock.patch("Kalshi.get_balance", return_value=1.0):
                sheet = self.run_day(OCT4, TODAY_MARKETS[:2])
                self.assertEqual(len(sheet), 1)  # the game is still logged
                self.assertEqual(self.bets(), [])

    def test_each_mode_has_its_own_cap_and_markets(self):
        # A paper bet using up all of a $6 cap doesn't stop a demo bet on the same market.
        conn = Tracker.connect(self.db_path)
        try:
            market = game_market("KXNBAGAME-26OCT04UTADEN-DEN", "Denver", ask="0.50")
            preds = [LLMPrediction("OpenAI", 0.8, 1.0), LLMPrediction("Claude", 0.8, 1.0)]
            prices = {"yes_ask": 0.50, "no_ask": 0.52}
            fill = {"order_id": "o", "count": 10, "price": 0.50, "cost": 5.0, "fee": 0.18}
            with mock.patch("builtins.print"), mock.patch("Kalshi.buy", return_value=fill) as buy:
                for mode in ("paper", "demo"):
                    bettor = self.bettor(conn, mode=mode, max_daily_usd=6.0)
                    self.assertIsNotNone(Betting.consider(conn, bettor, market, preds, prices, "2026-10-04"))
        finally:
            conn.close()
        self.assertEqual(buy.call_count, 1)
        self.assertEqual([b[1] for b in self.bets()], ["paper", "demo"])


def _seed(db_path, games, bets=(), user_id=None):
    """games: (ticker, league, yes_bid, yes_ask, ai_prob, outcome, tied); bets: (ticker, side, price, count), owned by user_id."""
    conn = Tracker.connect(db_path)
    try:
        for ticker, league, yes_bid, yes_ask, ai_prob, outcome, tied in games:
            for llm in ("OpenAI", "Claude"):
                conn.execute(
                    "INSERT INTO predictions (created_at, ticker, league, team, opponent, game_date, yes_bid, yes_ask, "
                    " llm, predicted_prob, confidence, market_prob, outcome, tied) "
                    "VALUES ('2026-10-04T12:00', ?, ?, 'Home', 'Away', '2026-10-04', ?, ?, ?, ?, 1.0, ?, ?, ?)",
                    (ticker, league, yes_bid, yes_ask, llm, ai_prob, (yes_bid + yes_ask) / 2, outcome, tied))
        for ticker, side, price, count in bets:
            conn.execute(
                "INSERT INTO bets (created_at, user_id, mode, ticker, league, game_date, team, opponent, side, price, "
                " count, cost, fee, ai_prob, edge) VALUES ('2026-10-04T12:00', ?, 'paper', ?, 'NBA', '2026-10-04', "
                " 'Home', 'Away', ?, ?, ?, ?, ?, 0.7, 0.08)",
                (user_id, ticker, side, price, count, price * count, Betting.fee(count, price)))
        conn.commit()
    finally:
        conn.close()


GAMES = [
    # AI 70% vs 60-cent ask: a 10 point YES edge, and YES won.
    ("KXNBAGAME-26OCT04AAABBB-AAA", "NBA", 0.58, 0.60, 0.70, 1, None),
    # AI 66% vs 60: 6 points, YES lost.
    ("KXNBAGAME-26OCT04CCCDDD-CCC", "NBA", 0.58, 0.60, 0.66, 0, None),
    # AI 30% vs NO ask 1 - 0.48 = 52 cents: an 18 point NO edge, NO won.
    ("KXNBAGAME-26OCT04EEEFFF-EEE", "NBA", 0.48, 0.50, 0.30, 0, None),
    # Not settled yet: left out of the backtest.
    ("KXNBAGAME-26OCT04GGGHHH-GGG", "NBA", 0.48, 0.50, 0.90, None, None),
]


class BettingViewTests(BettingConfigTestCase):
    def setUp(self):
        super().setUp()
        self.db_path = str(self.dir / "predictions.db")
        self.uid = _add_user(self.db_path, rules=RULES)
        _seed(self.db_path, GAMES, bets=[("KXNBAGAME-26OCT04AAABBB-AAA", "yes", 0.60, 8),
                                         ("KXNBAGAME-26OCT04CCCDDD-CCC", "yes", 0.60, 8),
                                         ("KXNBAGAME-26OCT04GGGHHH-GGG", "yes", 0.50, 10)], user_id=self.uid)
        self.conn = Tracker.connect(self.db_path)
        self.addCleanup(self.conn.close)

    def test_paper_balance_is_running_settled_pnl_by_game_day(self):
        bets = [{"game_date": "2026-10-05", "status": "lost", "pnl": -5.1},
                {"game_date": "2026-10-04", "status": "won", "pnl": 3.0},
                {"game_date": "2026-10-04", "status": "won", "pnl": 1.5},
                {"game_date": "2026-10-06", "status": "pending", "pnl": None}]
        self.assertEqual(betting.balance_series(self.conn, self.uid, "paper", bets), {
            "kind": "pnl", "points": [{"x": "2026-10-04", "y": 4.5}, {"x": "2026-10-05", "y": -0.6}]})

    def test_demo_and_live_balance_comes_from_recorded_snapshots(self):
        other = Accounts.create_user(self.conn, "other", "password1")["id"]
        with mock.patch("datetime.datetime") as dt:
            for when, uid, mode, value in (
                    ("2026-10-04T09:00:00", self.uid, "demo", 200.0), ("2026-10-04T10:00:00", self.uid, "demo", 200.0),
                    ("2026-10-04T11:00:00", self.uid, "demo", 195.04), ("2026-10-04T12:00:00", self.uid, "live", 50.0),
                    ("2026-10-04T13:00:00", other, "demo", 999.0), ("2026-10-05T09:00:00", self.uid, "demo", 195.04)):
                dt.now.return_value.isoformat.return_value = when
                Betting.record_balance(self.conn, uid, mode, value)
        # 10:00 repeated 9:00's value the same day, so it was skipped; the next day's repeat wasn't.
        # The other user's account isn't in this user's line.
        self.assertEqual(betting.balance_series(self.conn, self.uid, "demo", [])["points"], [
            {"x": "2026-10-04T09:00:00", "y": 200.0}, {"x": "2026-10-04T11:00:00", "y": 195.04},
            {"x": "2026-10-05T09:00:00", "y": 195.04}])

    def test_bets_get_their_result_from_the_game(self):
        statuses = {b["ticker"][-3:]: b["status"] for b in betting.list_bets(self.conn, self.uid)}
        self.assertEqual(statuses, {"AAA": "won", "CCC": "lost", "GGG": "pending"})

    def test_only_the_users_own_bets(self):
        other = Accounts.create_user(self.conn, "other", "password1")["id"]
        self.assertEqual(betting.list_bets(self.conn, other), [])

    def test_summary(self):
        s = betting.summarize(betting.list_bets(self.conn, self.uid))
        fee = Betting.fee(8, 0.60)
        self.assertEqual((s["bets"], s["pending"], s["won"], s["lost"]), (3, 1, 1, 1))
        self.assertAlmostEqual(s["pnl"], (8 - 4.8 - fee) + (-4.8 - fee))
        self.assertAlmostEqual(s["roi"], s["pnl"] / (2 * (4.8 + fee)))
        self.assertEqual(s["win_rate"], 0.5)

    def test_backtest_replays_the_rules_at_each_threshold(self):
        rows = {round(r["min_edge"], 2): r for r in betting.backtest(self.conn, RULES)}
        self.assertEqual(rows[0.04]["bets"], 3)   # all three settled games
        self.assertEqual(rows[0.08]["bets"], 2)   # drops the 6 point one
        self.assertEqual(rows[0.15]["bets"], 1)   # just the 18 point NO bet
        self.assertEqual(rows[0.20]["bets"], 0)
        self.assertEqual(rows[0.15]["won"], 1)
        self.assertEqual([e for e, r in rows.items() if r["current"]], [0.05])  # the rules' own threshold is a row
        self.assertEqual(rows[0.05]["bets"], 3)

    def test_backtest_applies_each_days_cap_to_the_biggest_edges(self):
        rows = {round(r["min_edge"], 2): r for r in betting.backtest(self.conn, {**RULES, "max_daily_usd": 6.0})}
        self.assertEqual(rows[0.04]["bets"], 1)  # all three qualify on one day; $6 covers one
        self.assertEqual(rows[0.04]["won"], 1)   # the 18 point NO bet, which won

    def test_backtest_follows_the_season_rule(self):
        rows = betting.backtest(self.conn, {**RULES, "segments": ["regular", "postseason"]})  # GAMES are preseason
        self.assertEqual({r["bets"] for r in rows}, {0})

    def test_breakdowns_group_by_league_side_and_edge(self):
        groups = {g["title"]: g["rows"] for g in betting.breakdowns(betting.list_bets(self.conn, self.uid))}
        self.assertEqual([r["label"] for r in groups["League"]], ["NBA"])
        self.assertEqual(groups["Side"][0]["bets"], 3)
        self.assertEqual([r["label"] for r in groups["Edge when placed"]], ["5-10 pts"])


class BettingPageTests(BettingConfigTestCase):
    def setUp(self):
        super().setUp()
        self.db_path = str(self.dir / "predictions.db")
        self.uid = _add_user(self.db_path, rules=RULES)  # "admin" / "s3cret"
        _seed(self.db_path, GAMES, bets=[("KXNBAGAME-26OCT04AAABBB-AAA", "yes", 0.60, 8)], user_id=self.uid)

        def override_get_db():
            conn = Tracker.connect(self.db_path)
            try:
                yield conn
            finally:
                conn.close()

        app.dependency_overrides[get_db] = override_get_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        os.environ.pop("SESSION_SECRET", None)
        self.client = TestClient(app)

    def sign_in(self, username="admin", password="s3cret"):
        self.client.post("/login", data={"username": username, "password": password, "next": "/"})

    def rules(self, uid=None):
        conn = Tracker.connect(self.db_path)
        try:
            return Betting.load_config(conn, uid or self.uid)
        finally:
            conn.close()

    def valid_form(self, **changes):
        form = {"mode": "paper", "min_edge": "7.5", "stake_usd": "10", "max_daily_usd": "100", "min_price": "15",
                "max_price": "85", "min_ais": "3", "leagues": ["NBA", "NHL"],
                "segments": ["regular", "postseason"]}
        form.update(changes)
        return form

    def test_guests_are_kept_out(self):
        self.client.post("/guest")
        self.assertEqual(self.client.get("/betting").status_code, 403)
        self.assertEqual(self.client.post("/betting/rules", data=self.valid_form()).status_code, 403)
        self.assertEqual(self.rules()["min_edge"], 0.05)

    def test_page_shows_rules_stats_backtest_and_bets(self):
        self.sign_in()
        page = self.client.get("/betting")
        self.assertEqual(page.status_code, 200)
        for text in ("Paper betting", "What if?", "Breakdowns", "Home vs Away", 'value="5"', "Save rules",
                     'name="segments" value="preseason" checked'):
            self.assertIn(text, page.text)
        self.assertIn('href="/betting" class="active"', page.text)

    def test_saving_rules(self):
        self.sign_in()
        r = self.client.post("/betting/rules", data=self.valid_form(), follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual({k: self.rules()[k] for k in RULES}, {
            "mode": "paper", "min_edge": 0.075, "stake_usd": 10.0, "max_daily_usd": 100.0, "min_price": 0.15,
            "max_price": 0.85, "min_ais": 3, "min_confidence": 0.0, "leagues": ["NBA", "NHL"],
            "segments": ["regular", "postseason"]})
        self.assertIn("Rules saved.", self.client.get(r.headers["location"]).text)

    def test_a_non_admin_user_has_their_own_rules_and_bets(self):
        other = _add_user(self.db_path, "friend", admin=False, password="friend-pass")
        self.sign_in("friend", "friend-pass")
        page = self.client.get("/betting").text
        self.assertNotIn("Home vs Away", page)  # the admin's bet
        self.assertIn("Betting is off", page)  # a new user starts with betting off
        self.client.post("/betting/rules", data=self.valid_form(min_edge="12"))
        self.assertEqual(self.rules(other)["min_edge"], 0.12)
        self.assertEqual(self.rules()["min_edge"], 0.05)  # the admin's rules didn't change

    def test_weights_are_saved_with_the_rules(self):
        self.sign_in()
        self.client.post("/betting/rules", data=self.valid_form(**{"weight::openai": "2", "weight::claude": "1",
                                                                  "weight::gemini": "0.5"}))
        conn = Tracker.connect(self.db_path)
        try:
            self.assertEqual(Accounts.get_setting(conn, self.uid, "ai_weights"),
                             {"openai": 2.0, "claude": 1.0, "gemini": 0.5})
        finally:
            conn.close()
        page = self.client.get("/betting").text
        self.assertIn('name="weight::openai"', page)
        self.assertIn("Back to the site defaults", page)
        self.client.post("/betting/weights/reset")
        conn = Tracker.connect(self.db_path)
        try:
            self.assertIsNone(Accounts.get_setting(conn, self.uid, "ai_weights"))
        finally:
            conn.close()

    def test_saving_rules_with_the_default_weights_keeps_following_the_defaults(self):
        self.sign_in()
        self.client.post("/betting/rules", data=self.valid_form(**{"weight::openai": "1", "weight::claude": "1",
                                                                  "weight::gemini": "1"}))
        conn = Tracker.connect(self.db_path)
        try:
            self.assertIsNone(Accounts.get_setting(conn, self.uid, "ai_weights"))  # no frozen copy of the defaults
        finally:
            conn.close()

    def test_all_zero_weights_are_refused(self):
        self.sign_in()
        r = self.client.post("/betting/rules", data=self.valid_form(**{"weight::openai": "0", "weight::claude": "0",
                                                                      "weight::gemini": "0"}))
        self.assertEqual(r.status_code, 400)
        self.assertIn("At least one AI", r.text)

    def test_preview_replays_unsaved_values_without_saving(self):
        self.sign_in()
        form = self.valid_form(min_edge="15", segments=["preseason"], leagues=["NBA"],
                               **{"weight::openai": "1", "weight::claude": "1", "weight::gemini": "1"})
        r = self.client.post("/betting/preview", data=form)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn('class="current-row"', data["rows_html"])
        self.assertIn("15%", data["rows_html"])  # the unsaved threshold is the marked row
        self.assertEqual(data["games"], 3)  # GAMES' three settled preseason games
        self.assertIsNotNone(data["brier"])
        self.assertEqual(self.rules()["min_edge"], 0.05)  # nothing saved

    def test_preview_weights_change_the_replay(self):
        self.sign_in()
        base = self.valid_form(min_edge="0", min_price="10", max_price="90", segments=["preseason"])
        even = self.client.post("/betting/preview", data={**base, "weight::openai": "1", "weight::claude": "1",
                                                          "weight::gemini": "1"}).json()
        # GAMES give both AIs the same probability, so weighting changes nothing -- but zeroing Claude
        # mustn't break anything either: the replay and score come back for the same games.
        tilted = self.client.post("/betting/preview", data={**base, "weight::openai": "1", "weight::claude": "0",
                                                            "weight::gemini": "1"}).json()
        self.assertEqual(even["games"], tilted["games"])
        self.assertAlmostEqual(even["brier"], tilted["brier"])

    def test_preview_reports_invalid_fields(self):
        self.sign_in()
        r = self.client.post("/betting/preview", data=self.valid_form(min_price="95", max_price="20"))
        self.assertEqual(r.status_code, 422)
        self.assertIn("lowest price", " ".join(r.json()["errors"]))

    def test_guests_cannot_preview(self):
        self.client.post("/guest")
        self.assertEqual(self.client.post("/betting/preview", data=self.valid_form()).status_code, 403)

    def test_saving_a_confidence_minimum(self):
        self.sign_in()
        self.client.post("/betting/rules", data=self.valid_form(min_confidence="45"))
        self.assertEqual(self.rules()["min_confidence"], 0.45)
        self.assertIn('id="min_confidence"', self.client.get("/betting").text)
        r = self.client.post("/betting/rules", data=self.valid_form(min_confidence="120"))
        self.assertEqual(r.status_code, 400)

    def test_the_preview_applies_the_confidence_minimum(self):
        self.sign_in()
        base = self.valid_form(min_edge="0", min_ais="2", segments=["preseason"])  # GAMES have two AIs each
        open_rows = self.client.post("/betting/preview", data={**base, "min_confidence": "0"}).json()["rows_html"]
        # GAMES are seeded at 100% confidence, so even a 99% minimum lets them through...
        self.assertEqual(self.client.post("/betting/preview", data={**base, "min_confidence": "99"}).json()["rows_html"],
                         open_rows)
        # ...and the rows differ once nothing can clear it.
        conn = Tracker.connect(self.db_path)
        try:
            conn.execute("UPDATE predictions SET confidence = 0.3")
            conn.commit()
        finally:
            conn.close()
        strict = self.client.post("/betting/preview", data={**base, "min_confidence": "50"}).json()["rows_html"]
        self.assertNotEqual(strict, open_rows)
        self.assertNotIn("<td>1</td>", strict)

    def test_invalid_rules_save_nothing(self):
        self.sign_in()
        r = self.client.post("/betting/rules", data=self.valid_form(mode="live", min_price="90", stake_usd="abc"))
        self.assertEqual(r.status_code, 400)
        for text in ("Mode:", "lowest price", "Dollars per bet"):
            self.assertIn(text, r.text)
        self.assertEqual(self.rules()["min_edge"], 0.05)

    def test_live_needs_its_box_ticked_and_demo_or_live_need_a_saved_key(self):
        self.sign_in()
        with mock.patch.object(Accounts, "kalshi_key_info", side_effect=fake_key_info):
            r = self.client.post("/betting/rules", data=self.valid_form(mode="live"))
            self.assertEqual(r.status_code, 400)
            self.assertIn("real money", r.text)
            self.assertEqual(self.rules()["mode"], "paper")
            self.client.post("/betting/rules", data=self.valid_form(mode="live", confirm_live="yes"))
            self.assertEqual(self.rules()["mode"], "live")
        r = self.client.post("/betting/rules", data=self.valid_form(mode="demo"))  # no demo key saved
        self.assertEqual(r.status_code, 400)
        self.assertIn("Kalshi demo API key", r.text)

    def test_demo_mode_shows_the_balance_and_keeps_each_modes_bets_apart(self):
        _set_rules(self.db_path, self.uid, {**RULES, "mode": "demo"})
        self.sign_in()
        with mock.patch.object(Accounts, "kalshi_credentials", side_effect=fake_credentials), \
             mock.patch("Kalshi.get_balance", return_value=250.5):
            page = self.client.get("/betting").text
            self.assertIn("$250.50", page)
            self.assertIn("Kalshi demo", page)
            self.assertIn("No kalshi demo bets yet", page)  # the paper bet isn't counted as demo
            self.assertNotIn("Home vs Away", page)
            self.assertIn('href="/betting?bets=paper"', page)
            self.assertIn("Home vs Away", self.client.get("/betting?bets=paper").text)

    def test_demo_mode_without_a_key_says_where_to_add_one(self):
        _set_rules(self.db_path, self.uid, {**RULES, "mode": "demo"})
        self.sign_in()
        page = self.client.get("/betting").text
        self.assertIn('href="/settings#kalshi"', page)
        self.assertIn("no Kalshi demo key saved", page)

    def test_a_balance_failure_is_shown_not_raised(self):
        _set_rules(self.db_path, self.uid, {**RULES, "mode": "live"})
        self.sign_in()
        with mock.patch.object(Accounts, "kalshi_credentials", side_effect=fake_credentials), \
             mock.patch("Kalshi.get_balance", side_effect=RuntimeError("HTTP 401")):
            r = self.client.get("/betting")
        self.assertEqual(r.status_code, 200)
        self.assertIn("HTTP 401", r.text)

    def test_the_balance_chart(self):
        self.sign_in()
        page = self.client.get("/betting").text
        self.assertIn("Paper balance over time", page)
        self.assertIn('id="balance-data"', page)  # the one settled paper bet is a point
        self.assertIn("/static/balance-chart.js", page)
        _set_rules(self.db_path, self.uid, {**RULES, "mode": "demo"})
        with mock.patch.object(Accounts, "kalshi_credentials", side_effect=fake_credentials), \
             mock.patch("Kalshi.get_balance", return_value=250.5):
            page = self.client.get("/betting").text  # this view records a first snapshot
        self.assertIn("Kalshi demo balance over time", page)
        self.assertIn('"y": 250.5', page)


class RepoConfigTests(unittest.TestCase):
    def test_the_shipped_betting_json_is_valid_first_admin_rules(self):
        config = json.loads(Path(Betting.BETTING_PATH).read_text(encoding="utf-8"))
        self.assertIn(config["mode"], Betting.MODES)
        self.assertEqual(set(Betting.DEFAULTS) - set(config), set())


if __name__ == "__main__":
    unittest.main()
