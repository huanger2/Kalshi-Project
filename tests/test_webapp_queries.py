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

    def test_includes_the_derived_season_type(self):
        self.record("G1", "Denver", 0.5, [pred("OpenAI", 0.7)], game_date="2026-10-05")
        self.assertEqual(queries.list_games(self.conn)[0]["season_type"], "preseason")

    def test_filters_by_season_type(self):
        self.record("G1", "Denver", 0.5, [pred("OpenAI", 0.7)], game_date="2026-10-05")  # preseason
        self.record("G2", "Utah", 0.5, [pred("OpenAI", 0.5)], game_date="2026-12-25")    # regular
        games = queries.list_games(self.conn, season_type="regular")
        self.assertEqual([g["ticker"] for g in games], ["G2"])

    def test_date_and_season_type_filters_combine(self):
        self.record("G1", "Denver", 0.5, [pred("OpenAI", 0.7)], game_date="2026-10-05")
        self.record("G2", "Utah", 0.5, [pred("OpenAI", 0.5)], game_date="2026-10-05")
        games = queries.list_games(self.conn, game_date="2026-10-05", season_type="postseason")
        self.assertEqual(games, [])


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

    def test_includes_the_derived_season_type(self):
        self.record("G1", "Denver", 0.5, [pred("OpenAI", 0.7)], game_date="2027-05-01")
        self.assertEqual(queries.get_game(self.conn, "G1")["season_type"], "postseason")


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

    def test_season_type_filters_the_summary(self):
        self.record("G1", "Denver", 0.5, [pred("OpenAI", 0.9)], game_date="2026-10-05")
        Tracker.settle(self.conn, "G1", True)
        self.assertEqual(queries.accuracy_summary(self.conn, season_type="postseason"), [])
        self.assertEqual(len(queries.accuracy_summary(self.conn, season_type="preseason")), 1)


class AiStatsTests(QueriesTestCase):
    def test_unseen_ai_has_zero_counts_and_none_stats(self):
        stats = queries.ai_stats(self.conn, "OpenAI")
        self.assertEqual(stats["total_n"], 0)
        self.assertEqual(stats["settled_n"], 0)
        self.assertEqual(stats["pending_n"], 0)
        self.assertIsNone(stats["avg_edge_on_win"])
        self.assertIsNone(stats["brier"])
        self.assertEqual(stats["weight"], 1.0)  # the project-wide default

    def test_pending_n_excludes_unsettled_games(self):
        self.record("G1", "Denver", 0.5, [pred("OpenAI", 0.6)])
        self.record("G2", "Utah", 0.5, [pred("OpenAI", 0.6)])
        Tracker.settle(self.conn, "G1", True)
        stats = queries.ai_stats(self.conn, "OpenAI")
        self.assertEqual(stats["total_n"], 2)
        self.assertEqual(stats["settled_n"], 1)
        self.assertEqual(stats["pending_n"], 1)

    def test_avg_edge_on_win_only_averages_wins(self):
        self.record("G1", "Denver", 0.40, [pred("OpenAI", 0.70)])  # edge +0.30
        self.record("G2", "Utah", 0.40, [pred("OpenAI", 0.50)])    # edge +0.10
        self.record("G3", "Golden State", 0.60, [pred("OpenAI", 0.30)])  # a loss; must not count
        Tracker.settle(self.conn, "G1", True)
        Tracker.settle(self.conn, "G2", True)
        Tracker.settle(self.conn, "G3", False)
        self.assertAlmostEqual(queries.ai_stats(self.conn, "OpenAI")["avg_edge_on_win"], 0.20)

    def test_avg_edge_on_loss_only_averages_losses(self):
        self.record("G1", "Denver", 0.70, [pred("OpenAI", 0.30)])  # edge -0.40, a loss
        self.record("G2", "Utah", 0.60, [pred("OpenAI", 0.70)])    # a win; must not count
        Tracker.settle(self.conn, "G1", False)
        Tracker.settle(self.conn, "G2", True)
        self.assertAlmostEqual(queries.ai_stats(self.conn, "OpenAI")["avg_edge_on_loss"], -0.40)

    def test_direction_accuracy_counts_above_fifty_calls_that_matched(self):
        self.record("G1", "Denver", 0.5, [pred("OpenAI", 0.70)])  # called it, right
        self.record("G2", "Utah", 0.5, [pred("OpenAI", 0.70)])    # called it, wrong
        self.record("G3", "Golden State", 0.5, [pred("OpenAI", 0.30)])  # called against it, right
        Tracker.settle(self.conn, "G1", True)
        Tracker.settle(self.conn, "G2", False)
        Tracker.settle(self.conn, "G3", False)
        self.assertAlmostEqual(queries.ai_stats(self.conn, "OpenAI")["direction_accuracy"], 2 / 3)

    def test_an_exact_fifty_percent_call_does_not_count_as_correct(self):
        self.record("G1", "Denver", 0.5, [pred("OpenAI", 0.50)])
        Tracker.settle(self.conn, "G1", True)
        self.assertAlmostEqual(queries.ai_stats(self.conn, "OpenAI")["direction_accuracy"], 0.0)

    def test_market_direction_accuracy_is_scored_the_same_way(self):
        self.record("G1", "Denver", 0.70, [pred("OpenAI", 0.1)])  # market called it; right
        Tracker.settle(self.conn, "G1", True)
        self.assertAlmostEqual(queries.ai_stats(self.conn, "OpenAI")["market_direction_accuracy"], 1.0)

    def test_weight_and_brier_match_tracker(self):
        for i in range(25):
            self.record(f"G{i}", "Denver", 0.5, [pred("OpenAI", 0.9)])
            Tracker.settle(self.conn, f"G{i}", True)
        stats = queries.ai_stats(self.conn, "OpenAI")
        self.assertAlmostEqual(stats["brier"], Tracker.brier_scores(self.conn)["OpenAI"]["brier"])
        self.assertAlmostEqual(stats["weight"], Tracker.compute_weights(self.conn)["OpenAI"])

    def test_season_type_scopes_every_stat(self):
        # Preseason: wrong every time. Regular season: right every time.
        self.record("G1", "Denver", 0.40, [pred("OpenAI", 0.10)], game_date="2026-10-05")
        Tracker.settle(self.conn, "G1", True)
        self.record("G2", "Utah", 0.40, [pred("OpenAI", 0.70)], game_date="2026-12-25")
        Tracker.settle(self.conn, "G2", True)

        preseason = queries.ai_stats(self.conn, "OpenAI", season_type="preseason")
        regular = queries.ai_stats(self.conn, "OpenAI", season_type="regular")

        self.assertEqual(preseason["settled_n"], 1)
        self.assertEqual(regular["settled_n"], 1)
        self.assertAlmostEqual(preseason["direction_accuracy"], 0.0)  # called it wrong
        self.assertAlmostEqual(regular["direction_accuracy"], 1.0)    # called it right

    def test_season_type_with_no_matching_games_gives_none_not_an_error(self):
        self.record("G1", "Denver", 0.5, [pred("OpenAI", 0.7)], game_date="2026-10-05")
        Tracker.settle(self.conn, "G1", True)
        stats = queries.ai_stats(self.conn, "OpenAI", season_type="postseason")
        self.assertEqual(stats["settled_n"], 0)
        self.assertIsNone(stats["avg_edge_on_win"])
        self.assertIsNone(stats["brier"])


class AiPredictionsTests(QueriesTestCase):
    def test_only_that_ais_rows_come_back(self):
        self.record("G1", "Denver", 0.5, [pred("OpenAI", 0.7), pred("Claude", 0.4)])
        rows = queries.ai_predictions(self.conn, "OpenAI")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["predicted_prob"], 0.7)  # OpenAI's, not Claude's 0.4

    def test_includes_edge_and_outcome(self):
        self.record("G1", "Denver", 0.40, [pred("OpenAI", 0.70)], opponent="Utah")
        Tracker.settle(self.conn, "G1", True)
        row = queries.ai_predictions(self.conn, "OpenAI")[0]
        self.assertAlmostEqual(row["edge"], 0.30)
        self.assertEqual(row["outcome"], 1)
        self.assertEqual(row["opponent"], "Utah")

    def test_includes_the_derived_season_type(self):
        self.record("G1", "Denver", 0.5, [pred("OpenAI", 0.7)], game_date="2026-10-05")
        self.assertEqual(queries.ai_predictions(self.conn, "OpenAI")[0]["season_type"], "preseason")

    def test_filters_by_season_type(self):
        self.record("G1", "Denver", 0.5, [pred("OpenAI", 0.7)], game_date="2026-10-05")  # preseason
        self.record("G2", "Utah", 0.5, [pred("OpenAI", 0.5)], game_date="2026-12-25")    # regular
        rows = queries.ai_predictions(self.conn, "OpenAI", season_type="regular")
        self.assertEqual([r["ticker"] for r in rows], ["G2"])

    def test_unknown_ai_returns_an_empty_list(self):
        self.record("G1", "Denver", 0.5, [pred("OpenAI", 0.7)])
        self.assertEqual(queries.ai_predictions(self.conn, "NotTracked"), [])


if __name__ == "__main__":
    unittest.main()
