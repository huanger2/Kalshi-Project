import unittest

import Tracker
from EVCalculator import LLMPrediction
from webapp import queries


def market(ticker, team, event=None, **extra):
    m = {"ticker": ticker, "event_ticker": event or ticker.rsplit("-", 1)[0], "title": f"{team} wins",
         "yes_sub_title": team}
    m.update(extra)
    return m


def pred(llm, prob, conf=0.8):
    return LLMPrediction(llm, prob, conf)


class QueriesTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = Tracker.connect(":memory:")

    def record(self, ticker, team, market_prob, predictions, **kwargs):
        Tracker.record_predictions(self.conn, market(ticker, team), market_prob, predictions, **kwargs)


class ListGamesTests(QueriesTestCase):
    def test_one_row_per_game_not_per_ai(self):
        self.record("G1", "Denver", 0.6, [pred("OpenAI", 0.7), pred("Claude", 0.65), pred("Gemini", 0.5)])
        games = queries.list_games(self.conn)
        self.assertEqual(len(games), 1)
        self.assertEqual(games[0]["ai_count"], 3)

    def test_includes_odds_and_opponent(self):
        self.record("G1", "Denver", 0.61, [pred("OpenAI", 0.7)],
                     opponent="Utah", yes_bid=0.60, yes_ask=0.62, game_date="2026-10-04")
        game = queries.list_games(self.conn)[0]
        self.assertEqual(game["team"], "Denver")
        self.assertEqual(game["opponent"], "Utah")
        self.assertAlmostEqual(game["yes_bid"], 0.60)
        self.assertAlmostEqual(game["yes_ask"], 0.62)
        self.assertAlmostEqual(game["market_prob"], 0.61)
        self.assertEqual(game["game_date"], "2026-10-04")

    def test_unsettled_game_has_a_none_outcome(self):
        self.record("G1", "Denver", 0.6, [pred("OpenAI", 0.7)])
        self.assertIsNone(queries.list_games(self.conn)[0]["outcome"])

    def test_settled_game_carries_its_outcome(self):
        self.record("G1", "Denver", 0.6, [pred("OpenAI", 0.7)])
        Tracker.settle(self.conn, "G1", True)
        self.assertEqual(queries.list_games(self.conn)[0]["outcome"], 1)

    def test_filters_by_game_date(self):
        self.record("G1", "Denver", 0.6, [pred("OpenAI", 0.7)], game_date="2026-10-04")
        self.record("G2", "Utah", 0.5, [pred("OpenAI", 0.5)], game_date="2026-10-05")
        games = queries.list_games(self.conn, game_date="2026-10-04")
        self.assertEqual([g["ticker"] for g in games], ["G1"])

    def test_newest_games_first(self):
        self.record("G1", "Denver", 0.6, [pred("OpenAI", 0.7)], game_date="2026-10-04")
        self.record("G2", "Utah", 0.5, [pred("OpenAI", 0.5)], game_date="2026-10-06")
        games = queries.list_games(self.conn)
        self.assertEqual([g["ticker"] for g in games], ["G2", "G1"])

    def test_no_games_is_an_empty_list(self):
        self.assertEqual(queries.list_games(self.conn), [])


class ListGameDatesTests(QueriesTestCase):
    def test_distinct_dates_newest_first(self):
        self.record("G1", "Denver", 0.6, [pred("OpenAI", 0.7)], game_date="2026-10-04")
        self.record("G2", "Utah", 0.5, [pred("OpenAI", 0.5)], game_date="2026-10-04")
        self.record("G3", "Golden State", 0.5, [pred("OpenAI", 0.5)], game_date="2026-10-06")
        self.assertEqual(queries.list_game_dates(self.conn), ["2026-10-06", "2026-10-04"])


class GetGameTests(QueriesTestCase):
    def test_returns_none_for_an_untracked_ticker(self):
        self.assertIsNone(queries.get_game(self.conn, "NOPE"))

    def test_includes_every_ais_prediction(self):
        self.record("G1", "Denver", 0.5, [pred("OpenAI", 0.7, 0.8), pred("Claude", 0.4, 0.6)])
        game = queries.get_game(self.conn, "G1")
        llms = {p["llm"]: p for p in game["predictions"]}
        self.assertEqual(set(llms), {"OpenAI", "Claude"})
        self.assertAlmostEqual(llms["OpenAI"]["predicted_prob"], 0.7)
        self.assertAlmostEqual(llms["OpenAI"]["confidence"], 0.8)

    def test_edge_is_predicted_minus_market(self):
        self.record("G1", "Denver", 0.40, [pred("OpenAI", 0.70)])
        edge = queries.get_game(self.conn, "G1")["predictions"][0]["edge"]
        self.assertAlmostEqual(edge, 0.30)

    def test_negative_edge_when_the_ai_is_below_the_market(self):
        self.record("G1", "Denver", 0.70, [pred("OpenAI", 0.40)])
        edge = queries.get_game(self.conn, "G1")["predictions"][0]["edge"]
        self.assertAlmostEqual(edge, -0.30)

    def test_includes_settlement_info(self):
        self.record("G1", "Denver", 0.5, [pred("OpenAI", 0.7)])
        Tracker.settle(self.conn, "G1", False)
        game = queries.get_game(self.conn, "G1")
        self.assertEqual(game["outcome"], 0)
        self.assertIsNotNone(game["settled_at"])


class AccuracySummaryTests(QueriesTestCase):
    def test_empty_before_anything_settles(self):
        self.record("G1", "Denver", 0.5, [pred("OpenAI", 0.7)])
        self.assertEqual(queries.accuracy_summary(self.conn), [])

    def test_includes_weight_alongside_brier(self):
        for i in range(25):
            self.record(f"G{i}", "Denver", 0.5, [pred("OpenAI", 0.9)])
            Tracker.settle(self.conn, f"G{i}", True)  # OpenAI is spot-on every time
        summary = queries.accuracy_summary(self.conn)
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["llm"], "OpenAI")
        self.assertEqual(summary[0]["settled_n"], 25)
        self.assertGreater(summary[0]["weight"], 0.9)  # 0.9 vs outcome=1 is a very good Brier


if __name__ == "__main__":
    unittest.main()
