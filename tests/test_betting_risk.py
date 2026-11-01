"""The loss limit (Betting.loss_limit()), the Betting tab's risk stats, and the bets CSV download."""

import csv
import datetime
import io
import unittest

import Betting
import Tracker
from tests import test_betting
from tests.test_betting import GAMES, RULES, BettingConfigTestCase, _add_user, _seed, _set_rules
from webapp import betting

DAY = datetime.date(2026, 10, 4)  # the seeded games' day
LOST = "KXNBAGAME-26OCT04CCCDDD-CCC"  # YES lost
WON = "KXNBAGAME-26OCT04AAABBB-AAA"  # YES won


class LossLimitTests(BettingConfigTestCase):
    def setUp(self):
        super().setUp()
        self.db_path = str(self.dir / "predictions.db")
        self.uid = _add_user(self.db_path, rules=RULES)
        # One losing bet: 8 x 60 cents, plus the fee.
        _seed(self.db_path, GAMES, bets=[(LOST, "yes", 0.60, 8)], user_id=self.uid)
        self.conn = Tracker.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.loss = 4.8 + Betting.fee(8, 0.60)

    def limit(self, usd, today=DAY, mode="paper"):
        return Betting.loss_limit(self.conn, self.uid, {**RULES, "mode": mode, "loss_limit_usd": usd}, today)

    def test_off_by_default(self):
        self.assertEqual(Betting.DEFAULTS["loss_limit_usd"], 0.0)
        self.assertFalse(self.limit(0)["hit"])
        self.assertIsNone(self.limit(0)["pnl"])

    def test_hit_once_recent_losses_reach_it(self):
        self.assertAlmostEqual(Betting.recent_pnl(self.conn, self.uid, "paper", DAY), -self.loss, places=2)
        hit = self.limit(4)
        self.assertTrue(hit["hit"])
        self.assertIn("loss limit reached", hit["why"])
        self.assertFalse(self.limit(10)["hit"])

    def test_losses_age_out_of_the_window(self):
        later = DAY + datetime.timedelta(days=Betting.LOSS_LIMIT_DAYS)
        self.assertFalse(self.limit(4, today=later)["hit"])
        self.assertTrue(self.limit(4, today=later - datetime.timedelta(days=1))["hit"])

    def test_other_modes_bets_dont_count(self):
        self.assertFalse(self.limit(4, mode="demo")["hit"])

    def test_pending_bets_dont_count(self):
        conn = self.conn
        conn.execute("DELETE FROM bets")
        conn.execute(
            "INSERT INTO bets (created_at, user_id, mode, ticker, league, game_date, team, opponent, side, price, "
            " count, cost, fee, ai_prob, edge) VALUES ('2026-10-04T12:00', ?, 'paper', "
            " 'KXNBAGAME-26OCT04GGGHHH-GGG', 'NBA', '2026-10-04', 'Home', 'Away', 'yes', 0.5, 10, 5.0, 0.18, 0.9, 0.4)",
            (self.uid,))
        conn.commit()
        self.assertFalse(self.limit(1)["hit"])

    def test_a_paused_bettor_gets_no_new_bets(self):
        bettor = {"user_id": self.uid, "username": "admin", "config": RULES, "weights": {}, "creds": None,
                  "fair_model": None, "paused": "loss limit reached: test"}
        why = []
        bet = Betting.candidate(self.conn, bettor, {"ticker": "KXNBAGAME-26OCT04GGGHHH-GGG"},
                                [], {"yes_ask": 0.5, "no_ask": 0.5}, "2026-10-04", why=why)
        self.assertIsNone(bet)
        self.assertEqual(why, ["loss limit reached: test"])

    def test_bettor_for_pauses_while_the_limit_is_hit(self):
        today = datetime.date.today().isoformat()
        self.conn.execute("UPDATE bets SET game_date = ?", (today,))
        self.conn.commit()
        _set_rules(self.db_path, self.uid, {**RULES, "loss_limit_usd": 4})
        bettor, _ = Betting.bettor_for(self.conn, {"id": self.uid, "username": "admin"})
        self.assertIn("loss limit reached", bettor["paused"])
        _set_rules(self.db_path, self.uid, {**RULES, "loss_limit_usd": 50})
        bettor, _ = Betting.bettor_for(self.conn, {"id": self.uid, "username": "admin"})
        self.assertIsNone(bettor["paused"])


class RiskTests(unittest.TestCase):
    def test_none_before_anything_settles(self):
        self.assertIsNone(betting.risk([{"game_date": "2026-10-01", "status": "pending", "pnl": None}]))

    def test_drawdown_streak_and_days(self):
        bets = [  # newest first, as list_bets() orders them
            {"id": 6, "game_date": "2026-10-05", "status": "pending", "pnl": None},
            {"id": 5, "game_date": "2026-10-04", "status": "lost", "pnl": -2.0},
            {"id": 4, "game_date": "2026-10-04", "status": "lost", "pnl": -3.0},
            {"id": 3, "game_date": "2026-10-03", "status": "won", "pnl": 1.0},
            {"id": 2, "game_date": "2026-10-02", "status": "lost", "pnl": -4.0},
            {"id": 1, "game_date": "2026-10-01", "status": "won", "pnl": 6.0},
        ]
        r = betting.risk(bets)
        # Running profit by day: 6, 2, 3, -2. Peak 6, low -2: an 8 dollar drawdown.
        self.assertEqual(r["max_drawdown"], 8.0)
        self.assertEqual((r["streak_status"], r["streak"]), ("lost", 2))
        self.assertEqual(r["best_day"], {"day": "2026-10-01", "pnl": 6.0})
        self.assertEqual(r["worst_day"], {"day": "2026-10-04", "pnl": -5.0})

    def test_a_tie_ends_the_streak(self):
        r = betting.risk([{"id": 2, "game_date": "2026-10-02", "status": "tie", "pnl": -0.1},
                          {"id": 1, "game_date": "2026-10-01", "status": "won", "pnl": 1.0}])
        self.assertIsNone(r["streak_status"])
        self.assertEqual(r["streak"], 0)


class PageTests(unittest.TestCase):
    """On the Betting tab, through test_betting's page fixture (a signed-out client, one won paper bet)."""

    def setUp(self):
        fixture = test_betting.BettingPageTests("test_guests_are_kept_out")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.db_path, self.uid, self.client = fixture.db_path, fixture.uid, fixture.client
        self.sign_in, self.rules, self.valid_form = fixture.sign_in, fixture.rules, fixture.valid_form

    def test_the_page_shows_risk_stats(self):
        self.sign_in()
        page = self.client.get("/betting").text
        for text in ("Max drawdown", "Streak", "Best day", "Worst day", "1 win"):
            self.assertIn(text, page)

    def test_saving_a_loss_limit(self):
        self.sign_in()
        r = self.client.post("/betting/rules", data=self.valid_form(loss_limit_usd="25"), follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.rules()["loss_limit_usd"], 25.0)
        # Blank turns it off; a negative is refused and the typed value kept.
        self.client.post("/betting/rules", data=self.valid_form(loss_limit_usd=""))
        self.assertEqual(self.rules()["loss_limit_usd"], 0.0)
        r = self.client.post("/betting/rules", data=self.valid_form(loss_limit_usd="-5"))
        self.assertEqual(r.status_code, 400)
        self.assertIn('id="loss_limit_usd" name="loss_limit_usd" type="number" step="any" min="0"\n'
                      '                       value="-5"', r.text)

    def test_the_page_warns_when_the_limit_is_hit(self):
        conn = Tracker.connect(self.db_path)
        try:
            conn.execute("UPDATE bets SET ticker = ?, game_date = ?", (LOST, datetime.date.today().isoformat()))
            conn.commit()
        finally:
            conn.close()
        _set_rules(self.db_path, self.uid, {**RULES, "loss_limit_usd": 4})
        self.sign_in()
        page = self.client.get("/betting").text
        self.assertIn("Loss limit reached: no new bets.", page)
        self.assertIn("reached: paused", page)

    def test_downloading_bets_as_csv(self):
        other = _add_user(self.db_path, "friend", admin=False, password="friend-pass")
        _seed(self.db_path, [], bets=[(LOST, "no", 0.40, 3)], user_id=other)
        self.sign_in()
        r = self.client.get("/betting/bets.csv?bets=paper")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/csv"))
        self.assertIn('filename="fantasia-paper-bets-', r.headers["content-disposition"])
        rows = list(csv.DictReader(io.StringIO(r.text)))
        self.assertEqual(len(rows), 1)  # only the signed-in user's bet
        self.assertEqual((rows[0]["ticker"], rows[0]["status"], rows[0]["count"]), (WON, "won", "8"))
        self.assertEqual(list(rows[0]), betting.CSV_COLUMNS)
        self.assertIn('href="/betting/bets.csv?bets=paper"', self.client.get("/betting").text)

    def test_guests_cant_download_bets(self):
        self.client.post("/guest")
        self.assertEqual(self.client.get("/betting/bets.csv").status_code, 403)


if __name__ == "__main__":
    unittest.main()
