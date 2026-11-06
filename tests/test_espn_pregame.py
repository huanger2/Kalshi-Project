"""ESPN's win probability as a fourth source (ESPN.py), and betting shortly before tip-off (webapp/pregame.py)."""

import datetime
import unittest
from unittest import mock

import Accounts
import AISettings
import Betting
import DailyLog
import ESPN
import Probability
import Tracker
from tests import test_betting
from tests.test_daily_log import LLMS, OCT4, TODAY_MARKETS, game_market
from webapp import pregame

UTC = datetime.timezone.utc
DEN = "KXNBAGAME-26OCT04UTADEN-DEN"  # Utah at Denver
TIP = datetime.datetime(2026, 10, 5, 1, 0, tzinfo=UTC)  # 9pm ET on Oct 4


def espn_api(projections=None, games=None):
    """A stand-in for ESPN's site API: the Oct 4 scoreboard, and each game's predictor."""
    projections = projections if projections is not None else {"home": "71.5", "away": "28.5"}
    games = games if games is not None else [("401", "DEN", "UTAH", "2026-10-05T01:00Z"),
                                             ("402", "LAC", "GS", "2026-10-05T02:30Z")]
    calls = []

    def fetch(url, params):
        calls.append((url, params))
        if url.endswith("/scoreboard"):
            assert params == {"dates": "20261004"}, params
            return {"events": [{"id": eid, "date": start, "competitions": [{"competitors": [
                {"homeAway": "home", "team": {"abbreviation": home}},
                {"homeAway": "away", "team": {"abbreviation": away}}]}]} for eid, home, away, start in games]}
        if url.endswith("/summary"):
            return {"predictor": {"homeTeam": {"gameProjection": projections["home"]},
                                  "awayTeam": {"gameProjection": projections["away"]}}} if projections else {}
        raise AssertionError(url)

    fetch.calls = calls
    return fetch


class ESPNTests(unittest.TestCase):
    def setUp(self):
        ESPN._scoreboards.clear()
        self.addCleanup(ESPN._scoreboards.clear)

    def test_reads_the_teams_from_the_tickers(self):
        self.assertEqual(ESPN.parse_market(game_market(DEN, "Denver")),
                         {"league": "NBA", "day": OCT4, "team": "DEN", "opponent": "UTA", "team_is_home": True})
        away = ESPN.parse_market(game_market("KXNFLGAME-26NOV16LACBAL-LAC", "Los Angeles C"))
        self.assertEqual((away["league"], away["team"], away["opponent"], away["team_is_home"]),
                         ("NFL", "LAC", "BAL", False))
        self.assertIsNone(ESPN.parse_market({"ticker": "KXNBAGAME-BAD", "event_ticker": "KXNBAGAME"}))

    def test_each_sides_win_probability(self):
        fetch = espn_api()
        self.assertAlmostEqual(ESPN.win_probability(game_market(DEN, "Denver"), fetch), 0.715)
        # Utah is the away side; ESPN's "UTAH" is Kalshi's "UTA". The scoreboard is fetched once.
        utah = game_market("KXNBAGAME-26OCT04UTADEN-UTA", "Utah")
        self.assertAlmostEqual(ESPN.win_probability(utah, fetch), 0.285)
        self.assertEqual(sum(url.endswith("/scoreboard") for url, _ in fetch.calls), 1)

    def test_aliases_match_espns_abbreviations_to_kalshis(self):
        gsw = game_market("KXNBAGAME-26OCT04GSWLAC-GSW", "Golden State")
        self.assertEqual(ESPN.start_time(gsw, espn_api()), datetime.datetime(2026, 10, 5, 2, 30, tzinfo=UTC))

    def test_no_prediction_cases(self):
        with self.assertRaises(ESPN.NoPrediction):  # no predictor for NHL; ESPN isn't even asked
            ESPN.win_probability(game_market("KXNHLGAME-26OCT04BOSNYR-BOS", "Boston"),
                                 mock.Mock(side_effect=AssertionError("fetched")))
        with self.assertRaises(ESPN.NoPrediction):  # not on ESPN's scoreboard
            ESPN.win_probability(game_market("KXNBAGAME-26OCT04BOSNYK-BOS", "Boston"), espn_api())
        with self.assertRaises(ESPN.NoPrediction):  # listed, but no predictor yet
            ESPN.win_probability(game_market(DEN, "Denver"), espn_api(projections={}))

    def test_start_time_is_none_when_espn_cant_be_reached(self):
        with mock.patch("builtins.print"):
            self.assertIsNone(ESPN.start_time(game_market(DEN, "Denver"), mock.Mock(side_effect=OSError("down"))))

    def test_it_records_a_fixed_confidence_and_its_own_model_name(self):
        with mock.patch.object(ESPN, "win_probability", return_value=0.6):
            self.assertEqual(ESPN.ask_espn({}), (0.6, ESPN.CONFIDENCE))
        self.assertEqual(AISettings.get_model("espn"), ESPN.MODEL)
        self.assertIn(("ESPN", ESPN.ask_espn), Probability.default_llms())


def fake_espn(prob):
    def ask(market):
        return prob, ESPN.CONFIDENCE
    ask.takes_market = True
    return ask


def failing_espn(market):
    raise ESPN.NoPrediction("none")


failing_espn.takes_market = True


NOV5_DEN = "KXNBAGAME-26NOV05UTADEN-DEN"  # a regular-season game: Utah at Denver


def finished_game_api(chart=None, reachable=True):
    """ESPN's API after Nov 5's Utah at Denver: no predictor any more, just the win-probability chart."""
    chart = chart if chart is not None else [{"homeWinPercentage": 0.64, "tiePercentage": 0.0},
                                             {"homeWinPercentage": 0.61, "tiePercentage": 0.0}]
    calls = []

    def fetch(url, params):
        calls.append(url)
        if not reachable:
            raise OSError("ESPN is down")
        if url.endswith("/scoreboard"):
            assert params == {"dates": "20261105"}, params
            return {"events": [{"id": "501", "date": "2026-11-06T02:00Z", "competitions": [{"competitors": [
                {"homeAway": "home", "team": {"abbreviation": "DEN"}},
                {"homeAway": "away", "team": {"abbreviation": "UTAH"}}]}]}]}
        return {"predictor": None, "winprobability": chart}

    fetch.calls = calls
    return fetch


class BackfillTests(unittest.TestCase):
    def setUp(self):
        ESPN._scoreboards.clear()
        self.addCleanup(ESPN._scoreboards.clear)
        self.conn = Tracker.connect(":memory:")
        self.addCleanup(self.conn.close)
        from EVCalculator import LLMPrediction
        for ticker, team, day, preds in (
                (NOV5_DEN, "Denver", "2026-11-05", [LLMPrediction("OpenAI", 0.6, 0.4)]),
                ("KXNHLGAME-26NOV05BOSNYR-BOS", "Boston", "2026-11-05", [LLMPrediction("OpenAI", 0.5, 0.4)]),
                (DEN, "Denver", "2026-10-04", [LLMPrediction("OpenAI", 0.6, 0.4)]),  # preseason
                ("KXNBAGAME-26NOV05GSWLAC-LAC", "Los Angeles C", "2026-11-05",
                 [LLMPrediction("OpenAI", 0.5, 0.4), LLMPrediction("ESPN", 0.55, 0.5)])):  # already has ESPN
            Tracker.record_predictions(self.conn, game_market(ticker, team), 0.61, preds, opponent="Utah",
                                       yes_bid=0.6, yes_ask=0.62, game_date=day)
        Tracker.settle(self.conn, NOV5_DEN, yes_won=True)

    def espn_rows(self):
        return self.conn.execute(
            "SELECT ticker, predicted_prob, confidence, model, market_prob, yes_ask, game_date, outcome, created_at "
            "FROM predictions WHERE llm = 'ESPN' ORDER BY ticker").fetchall()

    def test_a_finished_game_uses_the_charts_opening_point(self):
        market = {"ticker": NOV5_DEN, "event_ticker": NOV5_DEN.rsplit("-", 1)[0]}
        self.assertAlmostEqual(ESPN.win_probability(market, finished_game_api()), 0.64)
        ESPN._scoreboards.clear()
        utah = {"ticker": "KXNBAGAME-26NOV05UTADEN-UTA", "event_ticker": market["event_ticker"]}
        # The away side: what's left after the home side and a tie (NFL).
        chart = [{"homeWinPercentage": 0.60, "tiePercentage": 0.02}]
        self.assertAlmostEqual(ESPN.win_probability(utah, finished_game_api(chart)), 0.38)
        ESPN._scoreboards.clear()
        with self.assertRaises(ESPN.NoPrediction):
            ESPN.win_probability(market, finished_game_api(chart=[]))

    def test_fills_the_games_missing_espn_once(self):
        fetch = finished_game_api()
        logged_at = self.conn.execute("SELECT created_at FROM predictions WHERE ticker = ?", (NOV5_DEN,)).fetchone()[0]
        result = ESPN.backfill(self.conn, fetch, sleep=lambda s: None)
        self.assertEqual(result, {"added": 1, "missing": 0, "preseason": 1})
        self.assertEqual(self.espn_rows()[0], ("KXNBAGAME-26NOV05GSWLAC-LAC", 0.55, 0.5, ESPN.MODEL, 0.61, 0.62,
                                              "2026-11-05", None, self.espn_rows()[0][-1]))
        # The new row sits with the game's others: same prices, outcome and logged time.
        self.assertEqual(self.espn_rows()[1], (NOV5_DEN, 0.64, ESPN.CONFIDENCE, ESPN.MODEL, 0.61, 0.62,
                                              "2026-11-05", 1, logged_at))
        fetch.calls.clear()
        self.assertEqual(ESPN.backfill(self.conn, fetch, sleep=lambda s: None)["added"], 0)
        self.assertEqual(fetch.calls, [])  # nothing left to look up (NHL and preseason are never asked)

    def test_espn_being_down_leaves_games_for_next_time(self):
        with mock.patch("builtins.print"):
            result = ESPN.backfill(self.conn, finished_game_api(reachable=False), sleep=lambda s: None)
        self.assertEqual(result, {"added": 0, "missing": 1, "preseason": 1})
        self.assertEqual(len(self.espn_rows()), 1)  # just the game that already had one


class SourceTests(unittest.TestCase):
    def test_a_market_source_gets_the_market_not_a_prompt(self):
        with mock.patch("builtins.print"):
            preds = Probability.query_llms(game_market(DEN, "Denver"), LLMS[:1] + [("ESPN", fake_espn(0.7))],
                                           verbose=False)
        self.assertEqual([(p.llm_name, p.predicted_prob) for p in preds], [("OpenAI", 0.7), ("ESPN", 0.7)])
        self.assertEqual(Probability.ask_source("ESPN", fake_espn(0.4), {}), (0.4, ESPN.CONFIDENCE))


class DailyRunTests(unittest.TestCase):
    """Through test_betting's daily-run fixture: TODAY_MARKETS on Oct 4, one paper bettor (min edge 2 points)."""

    def setUp(self):
        fixture = test_betting.DailyRunBettingTests("test_a_game_with_enough_edge_gets_a_paper_bet")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.f = fixture

    def predictions(self):
        conn = Tracker.connect(self.f.db_path)
        try:
            return conn.execute("SELECT llm, predicted_prob, model FROM predictions ORDER BY llm").fetchall()
        finally:
            conn.close()

    def test_espn_is_logged_next_to_the_ais(self):
        self.f.run_day(OCT4, TODAY_MARKETS[:2], llms=LLMS + [("ESPN", fake_espn(0.715))])
        self.assertIn(("ESPN", 0.715, ESPN.MODEL), self.predictions())
        sheet = self.f.sheet(OCT4)
        self.assertAlmostEqual(sheet.iloc[0]["espn_prob"], 0.715)

    def test_espn_answering_alone_leaves_the_game_for_the_next_run(self):
        broken = [(name, mock.Mock(side_effect=OSError("down"))) for name, _ in LLMS]
        with mock.patch("builtins.print"):
            self.f.run_day(OCT4, TODAY_MARKETS[:2], llms=broken + [("ESPN", fake_espn(0.7))])
        self.assertEqual(self.predictions(), [])

    def test_a_game_espn_cant_predict_is_logged_without_it(self):
        with mock.patch("builtins.print"):
            self.f.run_day(OCT4, TODAY_MARKETS[:2], llms=LLMS + [("ESPN", failing_espn)])
        self.assertEqual({row[0] for row in self.predictions()}, {"OpenAI", "Claude", "Gemini"})

    def test_the_daily_run_backfills_espn_when_espn_is_a_source(self):
        with mock.patch.object(ESPN, "backfill", return_value={"added": 0}) as backfill:
            self.f.run_day(OCT4, TODAY_MARKETS[:2])
            backfill.assert_not_called()  # tests' AIs only: ESPN isn't asked
            self.f.run_day(OCT4, TODAY_MARKETS[:2], llms=LLMS + [("ESPN", fake_espn(0.7))])
            backfill.assert_called_once()

    def test_pre_game_bettors_arent_bet_at_the_daily_run(self):
        self.f.set_rules(min_edge=0.02, bet_lead_minutes=60)
        self.f.run_day(OCT4, TODAY_MARKETS[:2])
        self.assertEqual(self.f.bets(), [])

    def rerun(self, now, tip=TIP, kalshi_tip=None, fetch=None):
        """Betting.rerun_today() for the bettor at `now`, with ESPN's tip-off `tip` (None: ESPN doesn't know)."""
        def tip_for(market, espn_only=False):
            return tip if espn_only else kalshi_tip

        conn = Tracker.connect(self.f.db_path)
        try:
            user = Accounts.get_user(conn, self.f.uid)
            with mock.patch("builtins.print"):
                return Betting.rerun_today(conn, user, fetch_market=fetch or self.f.open_market, today=OCT4,
                                           now=now, tip_for=tip_for)
        finally:
            conn.close()

    def test_bets_wait_for_their_time_before_tip_off(self):
        self.f.set_rules(min_edge=0.02, bet_lead_minutes=60)
        self.f.run_day(OCT4, TODAY_MARKETS[:2])
        fetch = mock.Mock(side_effect=self.f.open_market)
        early = self.rerun(TIP - datetime.timedelta(hours=3), fetch=fetch)
        self.assertEqual((early["placed"], early["waiting"]), (0, 1))
        self.assertIn("in about 2 h 00 min", early["outcomes"][0]["outcome"])
        fetch.assert_not_called()  # not due: Kalshi isn't even asked
        due = self.rerun(TIP - datetime.timedelta(minutes=50))
        self.assertEqual(due["placed"], 1)
        self.assertEqual([b[0] for b in self.f.bets()], [DEN])

    def test_a_game_that_has_started_isnt_bet(self):
        self.f.set_rules(min_edge=0.02, bet_lead_minutes=60)
        self.f.run_day(OCT4, TODAY_MARKETS[:2])
        late = self.rerun(TIP + datetime.timedelta(minutes=5))
        self.assertEqual((late["placed"], late["closed"]), (0, 1))
        self.assertEqual(late["outcomes"][0]["outcome"], "the game has already started")

    def test_kalshis_expiry_gives_the_tip_off_when_espn_doesnt(self):
        self.f.set_rules(min_edge=0.02, bet_lead_minutes=30)
        self.f.run_day(OCT4, TODAY_MARKETS[:2])
        waiting = self.rerun(TIP - datetime.timedelta(hours=1), tip=None, kalshi_tip=TIP)
        self.assertEqual((waiting["placed"], waiting["waiting"]), (0, 1))
        self.assertEqual(self.rerun(TIP - datetime.timedelta(minutes=20), tip=None, kalshi_tip=TIP)["placed"], 1)

    def test_kalshis_expected_expiry_is_three_hours_after_tip_off(self):
        with mock.patch.object(ESPN, "start_time", return_value=None):
            self.assertEqual(Betting.tip_time({"expected_expiration_time": "2026-10-05T04:00:00Z"}), TIP)
            self.assertIsNone(Betting.tip_time({"expected_expiration_time": "2026-10-05T04:00:00Z"}, espn_only=True))

    def test_at_the_daily_run_timing_is_ignored(self):
        self.f.run_day(OCT4, [])  # nothing logged yet: no slate
        self.assertIsNone(Betting.bet_timing(TIP, 0, TIP + datetime.timedelta(hours=1)))
        self.assertIsNone(Betting.bet_timing(None, 60, TIP))  # unknown tip-off: bet now

    def test_the_pre_game_round_bets_each_game_once(self):
        self.f.set_rules(min_edge=0.02, bet_lead_minutes=60)
        friend = test_betting._add_user(self.f.db_path, "friend", admin=False, password="friend-pass",
                                        rules={**test_betting.RULES, "min_edge": 0.02, "bet_lead_minutes": 60})
        self.f.run_day(OCT4, TODAY_MARKETS[:2])
        fetch = mock.Mock(side_effect=self.f.open_market)
        conn = Tracker.connect(self.f.db_path)
        try:
            with mock.patch("builtins.print"):
                first = pregame.bet_once(conn, now=TIP - datetime.timedelta(minutes=45), fetch_market=fetch,
                                         tip_for=lambda market, espn_only=False: TIP, today=OCT4)
                again = pregame.bet_once(conn, now=TIP - datetime.timedelta(minutes=40), fetch_market=fetch,
                                         tip_for=lambda market, espn_only=False: TIP, today=OCT4)
            users = conn.execute("SELECT user_id FROM bets ORDER BY user_id").fetchall()
        finally:
            conn.close()
        self.assertEqual(first, {"users": 2, "placed": 2})
        self.assertEqual(again, {"users": 2, "placed": 0})
        self.assertEqual([u[0] for u in users], sorted([self.f.uid, friend]))
        self.assertEqual(fetch.call_count, 1)  # one Kalshi lookup, shared by both users' round

    def test_the_round_is_off_when_set_to_zero(self):
        with mock.patch.dict("os.environ", {"PREGAME_CHECK_MINUTES": "0"}), \
             mock.patch("threading.Thread", side_effect=AssertionError("started")):
            self.assertFalse(pregame.start())


class PageTests(unittest.TestCase):
    def setUp(self):
        fixture = test_betting.BettingPageTests("test_guests_are_kept_out")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.client, self.rules, self.valid_form = fixture.client, fixture.rules, fixture.valid_form
        fixture.sign_in()

    def test_choosing_when_to_bet(self):
        self.client.post("/betting/rules", data=self.valid_form(bet_lead_minutes="60"))
        self.assertEqual(self.rules()["bet_lead_minutes"], 60)
        page = self.client.get("/betting").text
        self.assertIn('<option value="60" selected>60 minutes before each game\'s tip-off (recommended)', page)
        self.assertIn("Bets go in about 60 minutes before", page)
        r = self.client.post("/betting/rules", data=self.valid_form(bet_lead_minutes="30.5"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("whole number of minutes", r.text)

    def test_the_espn_weight_is_saved_with_the_others(self):
        self.client.post("/betting/rules", data=self.valid_form(**{
            "weight::openai": "1", "weight::claude": "1", "weight::gemini": "1", "weight::espn": "2.5"}))
        self.assertIn('name="weight::espn"', self.client.get("/betting").text)


if __name__ == "__main__":
    unittest.main()
