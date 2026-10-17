import unittest

import Tracker
from EVCalculator import LLMPrediction


def market(ticker, event=None):
    return {"ticker": ticker, "event_ticker": event or ticker + "-EVT", "title": ticker}


def pred(llm, prob, conf=0.8):
    return LLMPrediction(llm, prob, conf)


class TrackerTestCase(unittest.TestCase):
    def setUp(self):
        self.conn = Tracker.connect(":memory:")

    def add(self, ticker, llm, prob, market_prob=0.5, **kwargs):
        Tracker.record_predictions(self.conn, market(ticker), market_prob, [pred(llm, prob)], **kwargs)


class EnrichedFieldsTests(TrackerTestCase):
    """The extra columns added to back the webapp's 'AI vs Kalshi' views."""

    def test_team_is_read_from_the_market_dict(self):
        m = dict(market("G1"), yes_sub_title="Denver")
        Tracker.record_predictions(self.conn, m, 0.5, [pred("OpenAI", 0.6)])
        row = self.conn.execute("SELECT team FROM predictions").fetchone()
        self.assertEqual(row[0], "Denver")

    def test_optional_context_fields_are_stored(self):
        Tracker.record_predictions(
            self.conn, market("G1"), 0.5, [pred("OpenAI", 0.6)],
            opponent="Utah", yes_bid=0.58, yes_ask=0.60, game_date="2026-10-07",
        )
        row = self.conn.execute(
            "SELECT opponent, yes_bid, yes_ask, game_date FROM predictions"
        ).fetchone()
        self.assertEqual(row, ("Utah", 0.58, 0.60, "2026-10-07"))

    def test_model_is_looked_up_for_a_real_provider(self):
        Tracker.record_predictions(self.conn, market("G1"), 0.5, [pred("OpenAI", 0.6)])
        model = self.conn.execute("SELECT model FROM predictions").fetchone()[0]
        from AISettings import get_model
        self.assertEqual(model, get_model("openai"))

    def test_an_unrecognized_llm_name_still_records_with_no_model(self):
        # Test fixtures elsewhere use made-up names like "Sharp"/"A" -- that
        # must not stop the prediction itself from being recorded.
        Tracker.record_predictions(self.conn, market("G1"), 0.5, [pred("NotARealProvider", 0.6)])
        row = self.conn.execute("SELECT llm, model, predicted_prob FROM predictions").fetchone()
        self.assertEqual(row, ("NotARealProvider", None, 0.6))

    def test_an_older_database_without_the_new_columns_is_migrated_in_place(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.executescript(
            "CREATE TABLE predictions ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, ticker TEXT NOT NULL,"
            " event_ticker TEXT, title TEXT, llm TEXT NOT NULL, predicted_prob REAL NOT NULL,"
            " confidence REAL NOT NULL, market_prob REAL NOT NULL, outcome INTEGER, settled_at TEXT)"
        )
        conn.execute(
            "INSERT INTO predictions (created_at, ticker, llm, predicted_prob, confidence, market_prob) "
            "VALUES ('t', 'G1', 'OpenAI', 0.6, 0.8, 0.5)"
        )
        conn.commit()

        Tracker._migrate(conn)  # what connect() runs internally
        conn.commit()

        columns = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
        self.assertTrue({"team", "opponent", "game_date", "yes_bid", "yes_ask", "model"} <= columns)
        # The pre-existing row survives the migration untouched.
        self.assertEqual(conn.execute("SELECT ticker, llm FROM predictions").fetchone(), ("G1", "OpenAI"))


class RecordAndSettleTests(TrackerTestCase):
    def test_record_stores_one_row_per_llm(self):
        n = Tracker.record_predictions(
            self.conn, market("G1"), 0.54, [pred("A", 0.7), pred("B", 0.4)]
        )
        self.assertEqual(n, 2)
        rows = self.conn.execute(
            "SELECT llm, predicted_prob, confidence, market_prob, outcome FROM predictions ORDER BY llm"
        ).fetchall()
        self.assertEqual(rows, [("A", 0.7, 0.8, 0.54, None), ("B", 0.4, 0.8, 0.54, None)])

    def test_settle_sets_outcome_for_every_llm_on_that_ticker_only(self):
        self.add("G1", "A", 0.7)
        self.add("G1", "B", 0.6)
        self.add("G2", "A", 0.5)
        updated = Tracker.settle(self.conn, "G1", yes_won=True)
        self.assertEqual(updated, 2)
        outcomes = dict(self.conn.execute("SELECT ticker || llm, outcome FROM predictions").fetchall())
        self.assertEqual(outcomes, {"G1A": 1, "G1B": 1, "G2A": None})

    def test_settle_records_a_no_result_as_zero(self):
        self.add("G1", "A", 0.7)
        Tracker.settle(self.conn, "G1", yes_won=False)
        self.assertEqual(self.conn.execute("SELECT outcome FROM predictions").fetchone()[0], 0)

    def test_settling_twice_does_not_overwrite(self):
        self.add("G1", "A", 0.7)
        Tracker.settle(self.conn, "G1", yes_won=True)
        self.assertEqual(Tracker.settle(self.conn, "G1", yes_won=False), 0)
        self.assertEqual(self.conn.execute("SELECT outcome FROM predictions").fetchone()[0], 1)

    def test_unsettled_tickers(self):
        self.add("G1", "A", 0.7)
        self.add("G2", "A", 0.7)
        Tracker.settle(self.conn, "G1", yes_won=True)
        self.assertEqual(Tracker.unsettled_tickers(self.conn), ["G2"])


class SettleFromKalshiTests(TrackerTestCase):
    def test_settles_only_markets_with_a_result(self):
        for t in ("YESGAME", "NOGAME", "LIVEGAME"):
            self.add(t, "A", 0.6)
        results = {"YESGAME": "yes", "NOGAME": "no", "LIVEGAME": ""}

        settled = Tracker.settle_from_kalshi(
            self.conn, fetch_market=lambda t: {"ticker": t, "result": results[t]}
        )

        self.assertEqual(settled, {"YESGAME": True, "NOGAME": False})
        self.assertEqual(Tracker.unsettled_tickers(self.conn), ["LIVEGAME"])

    def test_a_tie_settles_as_no_team_winning_and_is_flagged(self):
        self.add("TIEGAME", "A", 0.6)
        settled = Tracker.settle_from_kalshi(
            self.conn,
            fetch_market=lambda t: {"ticker": t, "result": "scalar", "settlement_value_dollars": "0.5000"},
        )
        self.assertEqual(settled, {"TIEGAME": False})
        self.assertEqual(self.conn.execute("SELECT outcome, tied FROM predictions").fetchone(), (0, 1))

    def test_a_tie_counts_toward_brier_as_a_zero(self):
        self.add("TIEGAME", "A", 0.6)
        Tracker.settle(self.conn, "TIEGAME", False, tie=True)
        self.assertAlmostEqual(Tracker.brier_scores(self.conn)["A"]["brier"], 0.36)  # (0.6 - 0)^2


class LeagueTests(TrackerTestCase):
    def test_league_is_derived_from_the_ticker(self):
        self.add("KXNFLGAME-26OCT26DALPHI-PHI", "A", 0.6)
        self.add("KXNHLGAME-26OCT23TBCOL-TB", "A", 0.6)
        leagues = dict(self.conn.execute("SELECT ticker, league FROM predictions"))
        self.assertEqual(leagues, {"KXNFLGAME-26OCT26DALPHI-PHI": "NFL", "KXNHLGAME-26OCT23TBCOL-TB": "NHL"})

    def test_connect_backfills_league_on_rows_from_before_the_column_existed(self):
        import os
        import sqlite3
        import tempfile

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "old.db")
        old = sqlite3.connect(path)
        old.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, "
                    "ticker TEXT NOT NULL, event_ticker TEXT, title TEXT, llm TEXT NOT NULL, "
                    "predicted_prob REAL NOT NULL, confidence REAL NOT NULL, market_prob REAL NOT NULL, "
                    "outcome INTEGER, settled_at TEXT)")
        old.execute("INSERT INTO predictions (created_at, ticker, llm, predicted_prob, confidence, market_prob) "
                    "VALUES ('x', 'KXNBAGAME-26OCT04UTADEN-DEN', 'A', 0.6, 0.8, 0.5)")
        old.commit()
        old.close()

        conn = Tracker.connect(path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT league FROM predictions").fetchone()[0], "NBA")

    def test_league_scopes_brier_scores(self):
        self.add("KXNFLGAME-26OCT26DALPHI-PHI", "A", 0.9, game_date="2026-10-26")
        self.add("KXNHLGAME-26OCT23TBCOL-TB", "A", 0.9, game_date="2026-10-23")
        Tracker.settle(self.conn, "KXNFLGAME-26OCT26DALPHI-PHI", True)   # (0.9-1)^2 = 0.01
        Tracker.settle(self.conn, "KXNHLGAME-26OCT23TBCOL-TB", False)    # (0.9-0)^2 = 0.81
        self.assertAlmostEqual(Tracker.brier_scores(self.conn, league="NFL")["A"]["brier"], 0.01)
        self.assertAlmostEqual(Tracker.brier_scores(self.conn, league="NHL")["A"]["brier"], 0.81)

    def test_season_year_uses_each_rows_own_league_calendar(self):
        self.add("KXNFLGAME-26OCT26DALPHI-PHI", "A", 0.9, game_date="2026-10-26")
        self.add("KXNHLGAME-26OCT23TBCOL-TB", "A", 0.9, game_date="2026-10-23")
        Tracker.settle(self.conn, "KXNFLGAME-26OCT26DALPHI-PHI", True)
        Tracker.settle(self.conn, "KXNHLGAME-26OCT23TBCOL-TB", False)
        self.assertEqual(Tracker.brier_scores(self.conn, season_year="2026")["A"]["n"], 1)
        self.assertEqual(Tracker.brier_scores(self.conn, season_year="2026-27")["A"]["n"], 1)


class BrierTests(TrackerTestCase):
    def test_brier_score_is_mean_squared_error_against_outcome(self):
        self.add("G1", "A", 0.9)  # YES -> (0.9-1)^2 = 0.01
        self.add("G2", "A", 0.9)  # NO  -> (0.9-0)^2 = 0.81
        Tracker.settle(self.conn, "G1", True)
        Tracker.settle(self.conn, "G2", False)
        stats = Tracker.brier_scores(self.conn)["A"]
        self.assertEqual(stats["n"], 2)
        self.assertAlmostEqual(stats["brier"], (0.01 + 0.81) / 2)

    def test_market_brier_is_tracked_on_the_same_games(self):
        self.add("G1", "A", 0.9, market_prob=0.6)  # (0.6-1)^2 = 0.16
        Tracker.settle(self.conn, "G1", True)
        self.assertAlmostEqual(Tracker.brier_scores(self.conn)["A"]["market_brier"], 0.16)

    def test_unsettled_predictions_are_excluded(self):
        self.add("G1", "A", 0.9)
        self.assertEqual(Tracker.brier_scores(self.conn), {})

    def test_llms_are_scored_independently(self):
        self.add("G1", "Sharp", 1.0)
        self.add("G1", "Dull", 0.0)
        Tracker.settle(self.conn, "G1", True)
        scores = Tracker.brier_scores(self.conn)
        self.assertAlmostEqual(scores["Sharp"]["brier"], 0.0)
        self.assertAlmostEqual(scores["Dull"]["brier"], 1.0)

    def test_segment_scopes_to_one_part_of_the_season(self):
        # Preseason game: wildly wrong. Regular-season game: spot on.
        self.add("G1", "A", 0.9, game_date="2026-10-05")  # preseason
        self.add("G2", "A", 0.9, game_date="2026-12-25")  # regular
        Tracker.settle(self.conn, "G1", False)  # (0.9-0)^2 = 0.81
        Tracker.settle(self.conn, "G2", True)   # (0.9-1)^2 = 0.01

        self.assertAlmostEqual(Tracker.brier_scores(self.conn, segment="preseason")["A"]["brier"], 0.81)
        self.assertAlmostEqual(Tracker.brier_scores(self.conn, segment="regular")["A"]["brier"], 0.01)
        self.assertAlmostEqual(Tracker.brier_scores(self.conn)["A"]["brier"], (0.81 + 0.01) / 2)

    def test_segment_with_no_matching_games_is_empty(self):
        self.add("G1", "A", 0.9, game_date="2026-10-05")
        Tracker.settle(self.conn, "G1", True)
        self.assertEqual(Tracker.brier_scores(self.conn, segment="postseason"), {})

    def test_season_year_scopes_independently_of_segment(self):
        self.add("G1", "A", 0.9, game_date="2026-10-05")   # 2026-27 season, preseason
        self.add("G2", "A", 0.9, game_date="2027-11-01")   # 2027-28 season, regular
        Tracker.settle(self.conn, "G1", False)  # 0.81
        Tracker.settle(self.conn, "G2", True)   # 0.01

        self.assertAlmostEqual(Tracker.brier_scores(self.conn, season_year="2026-27")["A"]["brier"], 0.81)
        self.assertAlmostEqual(Tracker.brier_scores(self.conn, season_year="2027-28")["A"]["brier"], 0.01)

    def test_segment_and_season_year_combine(self):
        self.add("G1", "A", 0.9, game_date="2026-10-05")   # 2026-27, preseason
        self.add("G2", "A", 0.9, game_date="2027-10-05")   # 2027-28, preseason -- same segment, different year
        Tracker.settle(self.conn, "G1", False)  # 0.81
        Tracker.settle(self.conn, "G2", True)   # 0.01

        scoped = Tracker.brier_scores(self.conn, segment="preseason", season_year="2026-27")["A"]
        self.assertEqual(scoped["n"], 1)
        self.assertAlmostEqual(scoped["brier"], 0.81)


class WeightTests(TrackerTestCase):
    def test_brier_to_weight_mapping(self):
        self.assertEqual(Tracker.brier_to_weight(0.0), 1.0)      # perfect
        self.assertEqual(Tracker.brier_to_weight(0.125), 0.5)
        self.assertEqual(Tracker.brier_to_weight(0.25), 0.0)     # coin flip
        self.assertEqual(Tracker.brier_to_weight(0.6), 0.0)      # worse than a coin flip
        self.assertEqual(Tracker.brier_to_weight(-0.1), 1.0)     # never above 1

    def settle_games(self, llm, probs_and_outcomes):
        for i, (prob, yes_won) in enumerate(probs_and_outcomes):
            ticker = f"{llm}-G{i}"
            self.add(ticker, llm, prob)
            Tracker.settle(self.conn, ticker, yes_won)

    def test_llm_below_min_samples_keeps_the_default_weight(self):
        self.settle_games("A", [(1.0, True)] * 5)
        self.assertEqual(Tracker.compute_weights(self.conn, min_samples=20), {"A": 1.0})

    def test_default_weight_is_configurable(self):
        self.settle_games("A", [(1.0, True)] * 2)
        self.assertEqual(Tracker.compute_weights(self.conn, min_samples=20, default_weight=0.5), {"A": 0.5})

    def test_accurate_llm_gets_more_weight_than_inaccurate_llm(self):
        self.settle_games("Sharp", [(0.9, True)] * 10 + [(0.1, False)] * 10)
        self.settle_games("Coin", [(0.5, True)] * 10 + [(0.5, False)] * 10)
        weights = Tracker.compute_weights(self.conn, min_samples=20)
        self.assertAlmostEqual(weights["Sharp"], 1 - 0.01 / 0.25)
        self.assertAlmostEqual(weights["Coin"], 0.0)

    def test_weights_always_within_zero_and_one(self):
        self.settle_games("Wrong", [(1.0, False)] * 25)
        weights = Tracker.compute_weights(self.conn, min_samples=20)
        self.assertEqual(weights["Wrong"], 0.0)

    def test_unsettled_llm_appears_with_the_default(self):
        self.add("G1", "New", 0.7)
        self.assertEqual(Tracker.compute_weights(self.conn), {"New": 1.0})

    def test_segment_is_passed_through_to_brier_scores(self):
        for i in range(20):
            self.add(f"G{i}", "Sharp", 0.9, game_date="2026-10-05")  # preseason, 20 games
            Tracker.settle(self.conn, f"G{i}", False)  # badly wrong every time
        for i in range(20):
            self.add(f"R{i}", "Sharp", 0.9, game_date="2026-12-25")  # regular, 20 games
            Tracker.settle(self.conn, f"R{i}", True)  # spot on every time

        preseason_weight = Tracker.compute_weights(self.conn, min_samples=20, segment="preseason")["Sharp"]
        regular_weight = Tracker.compute_weights(self.conn, min_samples=20, segment="regular")["Sharp"]
        self.assertAlmostEqual(preseason_weight, 0.0)
        self.assertAlmostEqual(regular_weight, 1 - 0.01 / 0.25)  # (0.9-1)^2 = 0.01 Brier every time

    def test_weights_feed_straight_into_the_ev_calculator(self):
        from EVCalculator import EVCalculator

        self.settle_games("Sharp", [(0.9, True)] * 10 + [(0.1, False)] * 10)
        weights = Tracker.compute_weights(self.conn, min_samples=20)
        result = EVCalculator(weights=weights).calculate(0.5, [pred("Sharp", 0.8, 1.0)])
        self.assertAlmostEqual(result.ev, 0.3 * weights["Sharp"])


if __name__ == "__main__":
    unittest.main()
