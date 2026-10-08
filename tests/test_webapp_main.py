import tempfile
import unittest

from fastapi.testclient import TestClient

import Tracker
from EVCalculator import LLMPrediction
from webapp.main import app, get_db


def market(ticker, team, **extra):
    m = {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "title": f"{team} wins",
         "yes_sub_title": team}
    m.update(extra)
    return m


class WebAppTestCase(unittest.TestCase):
    """
    Overrides the get_db dependency so every request in a test hits an
    isolated in-memory database instead of the real predictions.db -- the
    standard FastAPI pattern for this (app.dependency_overrides).
    """

    def setUp(self):
        # A real (throwaway) file, not ":memory:": FastAPI's sync routes run
        # in a thread pool, and a sqlite3 connection can't be used outside
        # the thread that created it -- an in-memory db can't be reopened
        # from a fresh connection the way a file can, so each request needs
        # its own connection to the same file, exactly like the real app.
        self.db_path = tempfile.mktemp(suffix=".db")
        self.addCleanup(self._remove_db)

        def override_get_db():
            conn = Tracker.connect(self.db_path)
            try:
                yield conn
            finally:
                conn.close()

        app.dependency_overrides[get_db] = override_get_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        self.client = TestClient(app)

    def _remove_db(self):
        import os
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def record(self, ticker, team, market_prob, prob, **kwargs):
        conn = Tracker.connect(self.db_path)
        try:
            Tracker.record_predictions(
                conn, market(ticker, team), market_prob, [LLMPrediction("OpenAI", prob, 0.8)], **kwargs
            )
        finally:
            conn.close()

    def settle(self, ticker, yes_won):
        conn = Tracker.connect(self.db_path)
        try:
            Tracker.settle(conn, ticker, yes_won)
        finally:
            conn.close()


class MarketsPageTests(WebAppTestCase):
    def test_empty_state_is_a_200_not_an_error(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("No markets tracked", r.text)

    def test_lists_a_tracked_game(self):
        self.record("G1", "Denver", 0.61, 0.70, opponent="Utah", game_date="2026-10-04")
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Denver", r.text)
        self.assertIn("Utah", r.text)

    def test_date_filter_excludes_other_days(self):
        self.record("G1", "Denver", 0.6, 0.7, game_date="2026-10-04")
        self.record("G2", "Golden State", 0.5, 0.5, game_date="2026-10-06")
        r = self.client.get("/", params={"date": "2026-10-04"})
        self.assertIn("Denver", r.text)
        self.assertNotIn("Golden State", r.text)

    def test_season_filter_excludes_other_segments(self):
        self.record("G1", "Denver", 0.6, 0.7, game_date="2026-10-05")   # preseason
        self.record("G2", "Golden State", 0.5, 0.5, game_date="2026-12-25")  # regular
        r = self.client.get("/", params={"season": "regular"})
        self.assertIn("Golden State", r.text)
        self.assertNotIn("Denver", r.text)

    def test_season_dropdown_lists_every_segment(self):
        r = self.client.get("/")
        for season in ("preseason", "regular", "postseason"):
            self.assertIn(f'value="{season}"', r.text)


class MarketDetailPageTests(WebAppTestCase):
    def test_shows_each_ais_prediction(self):
        self.record("G1", "Denver", 0.50, 0.70, opponent="Utah")
        r = self.client.get("/markets/G1")
        self.assertEqual(r.status_code, 200)
        self.assertIn("OpenAI", r.text)
        self.assertIn("Denver", r.text)

    def test_unknown_ticker_is_a_404(self):
        r = self.client.get("/markets/NOT-TRACKED")
        self.assertEqual(r.status_code, 404)


class AiDetailPageTests(WebAppTestCase):
    def test_shows_stats_and_the_prediction_history(self):
        self.record("G1", "Denver", 0.40, 0.70, opponent="Utah", game_date="2026-10-04")
        r = self.client.get("/ai/OpenAI")
        self.assertEqual(r.status_code, 200)
        self.assertIn("OpenAI", r.text)
        self.assertIn("Denver", r.text)

    def test_empty_state_before_anything_settles(self):
        r = self.client.get("/ai/OpenAI")
        self.assertEqual(r.status_code, 200)
        self.assertIn("No settled games yet", r.text)

    def test_renders_when_every_settled_game_is_a_win(self):
        # Regression: avg_edge_on_loss is independently None here (no losses
        # settled yet) even though settled_n > 0 -- must not be a 500.
        self.record("G1", "Denver", 0.5, 0.7)
        self.settle("G1", True)
        r = self.client.get("/ai/OpenAI")
        self.assertEqual(r.status_code, 200)
        self.assertIn("no losses settled yet", r.text)

    def test_renders_when_every_settled_game_is_a_loss(self):
        self.record("G1", "Denver", 0.5, 0.7)
        self.settle("G1", False)
        r = self.client.get("/ai/OpenAI")
        self.assertEqual(r.status_code, 200)
        self.assertIn("no wins settled yet", r.text)

    def test_is_case_insensitive(self):
        r = self.client.get("/ai/openai")
        self.assertEqual(r.status_code, 200)

    def test_unknown_ai_is_a_404(self):
        r = self.client.get("/ai/NotARealAI")
        self.assertEqual(r.status_code, 404)

    def test_season_filter_scopes_the_stats_and_history(self):
        self.record("G1", "Denver", 0.40, 0.10, game_date="2026-10-05")   # preseason, settled, wrong
        self.settle("G1", True)
        self.record("G2", "Utah", 0.40, 0.70, game_date="2026-12-25")     # regular, settled, right
        self.settle("G2", True)

        r = self.client.get("/ai/OpenAI", params={"season": "regular"})
        self.assertIn("Utah", r.text)
        self.assertNotIn("Denver", r.text)

    def test_nav_tabs_link_to_every_ai(self):
        r = self.client.get("/ai/OpenAI")
        for name in ("OpenAI", "Claude", "Gemini"):
            self.assertIn(f'href="/ai/{name}"', r.text)


class JsonApiTests(WebAppTestCase):
    def test_api_markets_returns_json(self):
        self.record("G1", "Denver", 0.6, 0.7, opponent="Utah")
        r = self.client.get("/api/markets")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["team"], "Denver")

    def test_api_market_detail_includes_edge(self):
        self.record("G1", "Denver", 0.40, 0.70)
        r = self.client.get("/api/markets/G1")
        self.assertEqual(r.status_code, 200)
        self.assertAlmostEqual(r.json()["predictions"][0]["edge"], 0.30)

    def test_api_market_detail_404_for_unknown_ticker(self):
        r = self.client.get("/api/markets/NOT-TRACKED")
        self.assertEqual(r.status_code, 404)

    def test_api_accuracy_returns_json(self):
        r = self.client.get("/api/accuracy")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])

    def test_api_ai_detail_returns_stats_and_predictions(self):
        self.record("G1", "Denver", 0.40, 0.70)
        r = self.client.get("/api/ai/OpenAI")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["stats"]["llm"], "OpenAI")
        self.assertEqual(len(body["predictions"]), 1)

    def test_api_ai_detail_404_for_unknown_ai(self):
        r = self.client.get("/api/ai/NotARealAI")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
