"""Paper betting (Betting.py), its hook in the daily run, and the Betting tab (webapp/betting.py)."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import AIWeights
import Betting
import Tracker
from EVCalculator import LLMPrediction
from tests.test_daily_log import LLMS, OCT4, TODAY_MARKETS, DailyLogTestCase, game_market
from webapp import betting
from webapp.main import app, get_db

RULES = {**Betting.DEFAULTS, "mode": "paper", "min_edge": 0.05, "stake_usd": 5.0, "max_daily_usd": 50.0,
         "min_price": 0.10, "max_price": 0.90, "min_ais": 2, "leagues": ["NBA", "NFL"]}


class BettingConfigTestCase(unittest.TestCase):
    """A throwaway betting.json, and every AI weighted 1.0 whatever ai_weights.json says."""

    rules = RULES

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.config_path = self.dir / "betting.json"
        self.config_path.write_text(json.dumps(self.rules), encoding="utf-8")
        for patcher in (mock.patch.object(Betting, "BETTING_PATH", self.config_path),
                        mock.patch.object(AIWeights, "get_weight", return_value=1.0),
                        mock.patch.dict("os.environ")):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("DATA_DIR", None)

    def set_rules(self, **changes):
        self.config_path.write_text(json.dumps({**self.rules, **changes}), encoding="utf-8")


class FeeTests(unittest.TestCase):
    def test_seven_percent_of_price_times_its_complement_rounded_up_to_a_cent(self):
        self.assertEqual(Betting.fee(100, 0.50), 1.75)
        self.assertEqual(Betting.fee(10, 0.50), 0.18)
        self.assertEqual(Betting.fee(1, 0.50), 0.02)

    def test_cheaper_at_the_extremes(self):
        self.assertLess(Betting.fee(100, 0.10), Betting.fee(100, 0.50))


class DecideTests(unittest.TestCase):
    def decide(self, ai_prob=0.70, ai_count=3, yes_ask=0.60, no_ask=0.42, league="NBA", **rules):
        return Betting.decide(ai_prob, ai_count, yes_ask, no_ask, league, {**RULES, **rules})

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

    def test_a_missing_ask_rules_out_only_that_side(self):
        bet, _ = self.decide(ai_prob=0.30, yes_ask=None, no_ask=0.50)
        self.assertEqual(bet["side"], "no")

    def test_a_stake_smaller_than_one_contract_is_no_bet(self):
        self.assertIsNone(self.decide(stake_usd=0.50)[0])


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
        self.config_path = Path(self.folder).parent / "betting.json"
        for patcher in (mock.patch.object(Betting, "BETTING_PATH", self.config_path),
                        mock.patch.object(AIWeights, "get_weight", return_value=1.0)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.set_rules(min_edge=0.02)

    def set_rules(self, **changes):
        self.config_path.write_text(json.dumps({**RULES, **changes}), encoding="utf-8")

    def bets(self):
        conn = Tracker.connect(self.db_path)
        try:
            return conn.execute("SELECT ticker, mode, side, price, count, cost, game_date, team, opponent "
                                "FROM bets ORDER BY id").fetchall()
        finally:
            conn.close()

    def test_a_game_with_enough_edge_gets_a_paper_bet(self):
        self.run_day(OCT4, TODAY_MARKETS[:2])
        self.assertEqual(self.bets(), [
            ("KXNBAGAME-26OCT04UTADEN-DEN", "paper", "yes", 0.62, 8, 4.96, "2026-10-04", "Denver", "Utah"),
        ])

    def test_no_bet_under_the_threshold(self):
        self.set_rules(min_edge=0.05)
        self.run_day(OCT4, TODAY_MARKETS[:2])
        self.assertEqual(self.bets(), [])

    def test_no_bets_while_off(self):
        self.set_rules(mode="off", min_edge=0.0)
        self.run_day(OCT4, TODAY_MARKETS[:2])
        self.assertEqual(self.bets(), [])

    def test_the_daily_cap_stops_further_bets(self):
        self.set_rules(min_edge=0.0, max_daily_usd=6.0)
        self.run_day(OCT4, TODAY_MARKETS)  # two games, each about $5 with its fee
        self.assertEqual(len(self.bets()), 1)

    def test_a_betting_failure_still_logs_the_game(self):
        with mock.patch.object(Betting, "consider", side_effect=RuntimeError("boom")):
            sheet = self.run_day(OCT4, TODAY_MARKETS[:2])
        self.assertEqual(len(sheet), 1)

    def test_the_same_market_is_not_bet_twice(self):
        conn = Tracker.connect(self.db_path)
        try:
            market = game_market("KXNBAGAME-26OCT04UTADEN-DEN", "Denver", ask="0.50")
            preds = [LLMPrediction("OpenAI", 0.8, 1.0), LLMPrediction("Claude", 0.8, 1.0)]
            prices = {"yes_ask": 0.50, "no_ask": 0.52}
            with mock.patch("builtins.print"):
                self.assertIsNotNone(Betting.consider(conn, market, preds, prices, "2026-10-04"))
                self.assertIsNone(Betting.consider(conn, market, preds, prices, "2026-10-04"))
        finally:
            conn.close()


def _seed(db_path, games, bets=()):
    """games: (ticker, league, yes_bid, yes_ask, ai_prob, outcome, tied); bets: (ticker, side, price, count)."""
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
                "INSERT INTO bets (created_at, mode, ticker, league, game_date, team, opponent, side, price, count, "
                " cost, fee, ai_prob, edge) VALUES ('2026-10-04T12:00', 'paper', ?, 'NBA', '2026-10-04', 'Home', "
                " 'Away', ?, ?, ?, ?, ?, 0.7, 0.08)",
                (ticker, side, price, count, price * count, Betting.fee(count, price)))
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
        _seed(self.db_path, GAMES, bets=[("KXNBAGAME-26OCT04AAABBB-AAA", "yes", 0.60, 8),
                                         ("KXNBAGAME-26OCT04CCCDDD-CCC", "yes", 0.60, 8),
                                         ("KXNBAGAME-26OCT04GGGHHH-GGG", "yes", 0.50, 10)])
        self.conn = Tracker.connect(self.db_path)
        self.addCleanup(self.conn.close)

    def test_bets_get_their_result_from_the_game(self):
        statuses = {b["ticker"][-3:]: b["status"] for b in betting.list_bets(self.conn)}
        self.assertEqual(statuses, {"AAA": "won", "CCC": "lost", "GGG": "pending"})

    def test_summary(self):
        s = betting.summarize(betting.list_bets(self.conn))
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

    def test_breakdowns_group_by_league_side_and_edge(self):
        groups = {g["title"]: g["rows"] for g in betting.breakdowns(betting.list_bets(self.conn))}
        self.assertEqual([r["label"] for r in groups["League"]], ["NBA"])
        self.assertEqual(groups["Side"][0]["bets"], 3)
        self.assertEqual([r["label"] for r in groups["Edge when placed"]], ["5-10 pts"])


class BettingPageTests(BettingConfigTestCase):
    def setUp(self):
        super().setUp()
        self.db_path = str(self.dir / "predictions.db")
        _seed(self.db_path, GAMES, bets=[("KXNBAGAME-26OCT04AAABBB-AAA", "yes", 0.60, 8)])

        def override_get_db():
            conn = Tracker.connect(self.db_path)
            try:
                yield conn
            finally:
                conn.close()

        app.dependency_overrides[get_db] = override_get_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        os.environ.update({"USERNAME": "admin", "PASSWORD": "s3cret"})
        os.environ.pop("SESSION_SECRET", None)
        self.client = TestClient(app)

    def sign_in(self):
        self.client.post("/login", data={"username": "admin", "password": "s3cret", "next": "/"})

    def valid_form(self, **changes):
        form = {"mode": "paper", "min_edge": "7.5", "stake_usd": "10", "max_daily_usd": "100", "min_price": "15",
                "max_price": "85", "min_ais": "3", "leagues": ["NBA", "NHL"]}
        form.update(changes)
        return form

    def test_admin_only(self):
        self.client.post("/guest")
        self.assertEqual(self.client.get("/betting").status_code, 403)
        self.assertEqual(self.client.post("/betting/rules", data=self.valid_form()).status_code, 403)
        self.assertEqual(json.loads(self.config_path.read_text())["min_edge"], 0.05)

    def test_page_shows_rules_stats_backtest_and_bets(self):
        self.sign_in()
        page = self.client.get("/betting")
        self.assertEqual(page.status_code, 200)
        for text in ("Paper betting", "What if?", "Breakdowns", "Home vs Away", 'value="5"', "Save rules"):
            self.assertIn(text, page.text)
        self.assertIn('href="/betting" class="active"', page.text)

    def test_saving_rules(self):
        self.sign_in()
        r = self.client.post("/betting/rules", data=self.valid_form(), follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        saved = json.loads(self.config_path.read_text())
        self.assertEqual({k: saved[k] for k in RULES}, {
            "mode": "paper", "min_edge": 0.075, "stake_usd": 10.0, "max_daily_usd": 100.0, "min_price": 0.15,
            "max_price": 0.85, "min_ais": 3, "leagues": ["NBA", "NHL"]})
        self.assertIn("Rules saved.", self.client.get(r.headers["location"]).text)

    def test_invalid_rules_save_nothing(self):
        self.sign_in()
        before = self.config_path.read_text()
        r = self.client.post("/betting/rules", data=self.valid_form(mode="live", min_price="90", stake_usd="abc"))
        self.assertEqual(r.status_code, 400)
        for text in ("Mode:", "lowest price", "Dollars per bet"):
            self.assertIn(text, r.text)
        self.assertEqual(self.config_path.read_text(), before)

    def test_hosted_saves_go_to_the_volume(self):
        volume = self.dir / "volume"
        volume.mkdir()
        os.environ["DATA_DIR"] = str(volume)
        self.sign_in()
        self.client.post("/betting/rules", data=self.valid_form())
        self.assertEqual(json.loads((volume / "betting.json").read_text())["min_edge"], 0.075)
        self.assertEqual(json.loads(self.config_path.read_text())["min_edge"], 0.05)
        self.client.post("/betting/rules/reset")
        self.assertFalse((volume / "betting.json").exists())


class RepoConfigTests(unittest.TestCase):
    def test_the_shipped_betting_json_is_valid_paper_betting(self):
        config = json.loads(Path(Betting.BETTING_PATH).read_text(encoding="utf-8"))
        self.assertEqual(config["mode"], "paper")
        self.assertEqual(set(Betting.DEFAULTS) - set(config), set())


if __name__ == "__main__":
    unittest.main()
