"""Results recorded automatically in the background (webapp/auto_settle.py)."""

import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import Tracker
from EVCalculator import LLMPrediction
from tests.test_webapp_main import market
from webapp import auto_settle, daily_run

TODAY = datetime.date(2026, 10, 31)


class SettleOnceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = Tracker.connect(str(Path(tmp.name) / "predictions.db"))
        self.addCleanup(self.conn.close)
        for ticker, day in (("KXNBAGAME-26OCT30AAABBB-AAA", "2026-10-30"), ("KXNBAGAME-26OCT31CCCDDD-CCC", "2026-10-31"),
                            ("KXNBAGAME-26NOV01EEEFFF-EEE", "2026-11-01")):
            Tracker.record_predictions(self.conn, market(ticker, "Team"), 0.5, [LLMPrediction("OpenAI", 0.6, 0.8)],
                                       game_date=day)

    def outcomes(self):
        return dict(self.conn.execute("SELECT ticker, outcome FROM predictions").fetchall())

    def test_records_finished_games_and_leaves_the_rest(self):
        results = {"KXNBAGAME-26OCT30AAABBB-AAA": {"result": "yes"},   # finished
                   "KXNBAGAME-26OCT31CCCDDD-CCC": {"result": ""}}      # tonight's, still going
        looked_up = []

        def fetch(ticker):
            looked_up.append(ticker)
            return results[ticker]

        result = auto_settle.settle_once(self.conn, today=TODAY, fetch_market=fetch, sleep=lambda s: None)
        self.assertEqual(result, {"checked": 2, "settled": 1})
        self.assertNotIn("KXNBAGAME-26NOV01EEEFFF-EEE", looked_up)  # tomorrow's game isn't looked up yet
        self.assertEqual(self.outcomes()["KXNBAGAME-26OCT30AAABBB-AAA"], 1)
        self.assertIsNone(self.outcomes()["KXNBAGAME-26OCT31CCCDDD-CCC"])
        # Next round: the settled game isn't looked up again.
        looked_up.clear()
        auto_settle.settle_once(self.conn, today=TODAY, fetch_market=fetch, sleep=lambda s: None)
        self.assertEqual(looked_up, ["KXNBAGAME-26OCT31CCCDDD-CCC"])

    def test_an_unreachable_market_is_left_for_next_round(self):
        with mock.patch("builtins.print"):
            result = auto_settle.settle_once(self.conn, today=TODAY, sleep=lambda s: None,
                                             fetch_market=mock.Mock(side_effect=OSError("timed out")))
        self.assertEqual(result, {"checked": 2, "settled": 0})

    def test_lookups_are_spaced_out(self):
        pauses = []
        auto_settle.settle_once(self.conn, today=TODAY, fetch_market=lambda t: {"result": ""}, sleep=pauses.append)
        self.assertEqual(pauses, [auto_settle.SPACING_SECONDS])  # between the two lookups, not before the first


class ThreadTests(unittest.TestCase):
    def test_off_when_set_to_zero(self):
        with mock.patch.dict(os.environ, {"AUTO_SETTLE_MINUTES": "0"}), \
             mock.patch("threading.Thread", side_effect=AssertionError("started")):
            self.assertFalse(auto_settle.start())
        self.assertFalse(auto_settle.summary()["on"])

    def test_a_bad_setting_falls_back_to_the_default(self):
        with mock.patch.dict(os.environ, {"AUTO_SETTLE_MINUTES": "soon"}):
            self.assertEqual(auto_settle.interval_minutes(), auto_settle.DEFAULT_MINUTES)

    def test_skips_a_round_while_the_daily_run_is_going(self):
        with mock.patch.object(daily_run, "status", return_value={"state": "running"}), \
             mock.patch.object(Tracker, "connect", side_effect=AssertionError("touched the database")):
            auto_settle._round()


class PageTests(unittest.TestCase):
    def test_the_betting_tab_says_results_are_automatic(self):
        from tests.test_betting import BettingPageTests

        case = BettingPageTests("test_guests_are_kept_out")
        case.setUp()
        try:
            with mock.patch.dict(os.environ, {"AUTO_SETTLE_MINUTES": "15"}):
                case.sign_in()
                page = case.client.get("/betting").text
            self.assertIn("Results are recorded automatically", page)
            self.assertIn("every\n        15 minutes", page)
        finally:
            case.doCleanups()


if __name__ == "__main__":
    unittest.main()
