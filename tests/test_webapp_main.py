import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import time

import Leagues
import Accounts
import Tracker
from EVCalculator import LLMPrediction
from webapp import auth
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

        # A throwaway copy of leagues.json, so the tracking switch's writes
        # never touch the real file.
        config_dir = tempfile.TemporaryDirectory()
        self.addCleanup(config_dir.cleanup)
        self.config_path = Path(config_dir.name) / "leagues.json"
        shutil.copy(Leagues.CONFIG_PATH, self.config_path)
        # Start every test with all leagues tracked, whatever the real file says today.
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        config["enabled"] = list(Leagues.LEAGUES)
        self.config_path.write_text(json.dumps(config), encoding="utf-8")
        patcher = mock.patch.object(Leagues, "CONFIG_PATH", self.config_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        # The admin login is read from the environment; pin it for every test.
        env = mock.patch.dict("os.environ", {"USERNAME": "admin", "PASSWORD": "s3cret"})
        env.start()
        self.addCleanup(env.stop)
        env_secret = mock.patch.dict("os.environ")
        env_secret.start()
        self.addCleanup(env_secret.stop)
        os.environ.pop("SESSION_SECRET", None)

        # Every page past the login screen needs a choice made; browse as a
        # guest unless a test signs in.
        self.client = TestClient(app)
        self.client.post("/guest")

    def sign_in(self, username="admin", password="s3cret"):
        return self.client.post("/login", data={"username": username, "password": password, "next": "/"},
                                follow_redirects=False)

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

    def test_the_all_markets_tab_shows_every_league(self):
        self.record("KXNFLGAME-26OCT25KCSEA-SEA", "Seattle", 0.6, 0.7, game_date="2026-10-25")
        self.record("KXNHLGAME-26OCT23TBCOL-TB", "Tampa Bay", 0.5, 0.5, game_date="2026-10-23")
        r = self.client.get("/")
        self.assertIn("Seattle", r.text)
        self.assertIn("Tampa Bay", r.text)

    def test_every_page_has_a_tab_per_league(self):
        for url in ("/", "/ai/OpenAI"):
            r = self.client.get(url)
            for league in ("NBA", "NFL", "NHL", "MLB"):
                self.assertIn(f'href="/league/{league}"', r.text)

    def test_a_tie_renders_as_a_tie(self):
        self.record("KXNFLGAME-26OCT11NYGWAS-WAS", "Washington", 0.5, 0.6, game_date="2026-10-11")
        conn = Tracker.connect(self.db_path)
        Tracker.settle(conn, "KXNFLGAME-26OCT11NYGWAS-WAS", False, tie=True)
        conn.close()
        self.assertIn(">Tie<", self.client.get("/").text)
        self.assertIn(">tie<", self.client.get("/ai/OpenAI").text)
        self.assertIn(">Tie<", self.client.get("/markets/KXNFLGAME-26OCT11NYGWAS-WAS").text)

    def test_segment_filter_excludes_other_segments(self):
        self.record("G1", "Denver", 0.6, 0.7, game_date="2026-10-05")   # preseason
        self.record("G2", "Golden State", 0.5, 0.5, game_date="2026-12-25")  # regular
        r = self.client.get("/", params={"segment": "regular"})
        self.assertIn("Golden State", r.text)
        self.assertNotIn("Denver", r.text)

    def test_segment_dropdown_lists_every_segment(self):
        r = self.client.get("/")
        for segment in ("preseason", "regular", "postseason"):
            self.assertIn(f'value="{segment}"', r.text)

    def test_season_filter_excludes_other_years(self):
        self.record("G1", "Denver", 0.6, 0.7, game_date="2026-12-25")   # 2026-27 season
        self.record("G2", "Golden State", 0.5, 0.5, game_date="2027-12-25")  # 2027-28 season
        r = self.client.get("/", params={"season": "2026-27"})
        self.assertIn("Denver", r.text)
        self.assertNotIn("Golden State", r.text)

    def test_season_dropdown_lists_every_tracked_year(self):
        self.record("G1", "Denver", 0.6, 0.7, game_date="2026-12-25")
        r = self.client.get("/")
        self.assertIn('value="2026-27"', r.text)

    def test_season_and_segment_filters_combine(self):
        self.record("G1", "Denver", 0.6, 0.7, game_date="2026-10-05")       # 2026-27, preseason
        self.record("G2", "Golden State", 0.5, 0.5, game_date="2026-12-25")  # 2026-27, regular
        self.record("G3", "Boston", 0.5, 0.5, game_date="2027-12-25")        # 2027-28, regular
        r = self.client.get("/", params={"season": "2026-27", "segment": "regular"})
        self.assertIn("Golden State", r.text)
        self.assertNotIn("Denver", r.text)
        self.assertNotIn("Boston", r.text)


class LoginTests(WebAppTestCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)  # a fresh visitor: no guest/admin choice made yet

    def test_a_first_visit_goes_to_the_login_page_and_remembers_where_it_was_headed(self):
        r = self.client.get("/league/NFL?season=2026", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login?next=%2Fleague%2FNFL%3Fseason%3D2026")
        page = self.client.get(r.headers["location"]).text
        self.assertIn("Continue as guest", page)
        self.assertIn('value="/league/NFL?season=2026"', page)

    def test_the_login_page_has_no_nav_before_a_choice_is_made(self):
        self.assertNotIn('href="/league/NBA"', self.client.get("/login").text)

    def test_continuing_as_a_guest_goes_where_the_visitor_was_headed(self):
        r = self.client.post("/guest", data={"next": "/league/NHL"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/league/NHL")
        self.assertEqual(self.client.get("/league/NHL").status_code, 200)

    def test_correct_credentials_sign_in(self):
        r = self.sign_in()
        self.assertEqual(r.status_code, 303)
        self.assertIn("Sign out", self.client.get("/").text)

    def test_wrong_credentials_are_rejected(self):
        for username, password in (("admin", "nope"), ("someone", "s3cret"), ("", "")):
            r = self.sign_in(username, password)
            self.assertEqual(r.status_code, 401)
            self.assertIn("Incorrect username or password", r.text)
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)  # still not in

    def test_nobody_can_sign_in_before_there_is_an_account(self):
        # No users yet, and no USERNAME/PASSWORD to create the first admin from.
        with mock.patch.dict("os.environ"):
            os.environ.pop("PASSWORD")
            r = self.sign_in("admin", "")
        self.assertEqual(r.status_code, 401)
        self.assertIn("no accounts on this server yet", r.text)

    def test_numbers_for_inputs_are_exact(self):
        from webapp.main import _num
        self.assertEqual(_num(0.07 * 100), "7")
        self.assertEqual(_num(12.5), "12.5")
        self.assertEqual(_num(123456.78), "123456.78")
        self.assertEqual(_num(0.0), "0")
        self.assertEqual(_num(None), "")

    def test_a_forged_cookie_is_not_trusted(self):
        self.sign_in()  # the account exists; the cookie just isn't signed by this server
        self.client.cookies.set(auth.COOKIE, "u.1.1.9999999999.deadbeef")
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)

    def test_a_password_change_signs_the_user_out_everywhere(self):
        self.sign_in()
        conn = Tracker.connect(self.db_path)
        try:
            Accounts.set_password(conn, Accounts.first_admin_id(conn), "a-new-password")
        finally:
            conn.close()
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)

    def test_an_expired_sign_in_is_not_trusted(self):
        self.sign_in()
        conn = Tracker.connect(self.db_path)
        try:
            user = Accounts.get_user(conn, Accounts.first_admin_id(conn))
            self.client.cookies.set(auth.COOKIE, auth.user_cookie(conn, user, now=time.time() - auth.MAX_AGE - 60))
        finally:
            conn.close()
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)

    def test_signing_out_returns_to_the_login_page(self):
        self.sign_in()
        r = self.client.post("/logout", follow_redirects=False)
        self.assertEqual(r.headers["location"], "/login")
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)

    def test_next_never_redirects_off_site(self):
        for target in ("//evil.example", "https://evil.example", "/\evil.example"):
            r = self.client.post("/guest", data={"next": target}, follow_redirects=False)
            self.assertEqual(r.headers["location"], "/", target)

    def test_the_json_api_and_static_files_stay_open(self):
        self.assertEqual(self.client.get("/api/accuracy").status_code, 200)
        self.assertEqual(self.client.get("/static/style.css").status_code, 200)


class GuestSwitchTests(WebAppTestCase):
    """The default WebAppTestCase visitor is a guest."""

    def test_a_guest_does_not_see_the_tracking_switch(self):
        r = self.client.get("/league/NFL")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('role="switch"', r.text)
        self.assertNotIn("/tracking", r.text)

    def test_a_guest_cannot_post_to_the_switch(self):
        before = self.config_path.read_text(encoding="utf-8")
        r = self.client.post("/league/NFL/tracking", data={"enabled": "0"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_a_visitor_with_no_session_cannot_post_to_the_switch(self):
        r = TestClient(app).post("/league/NFL/tracking", data={"enabled": "0"})
        self.assertEqual(r.status_code, 403)

    def test_a_guest_sees_a_sign_in_link(self):
        self.assertIn(">Sign in</a>", self.client.get("/").text)


class LeagueTabTests(WebAppTestCase):
    def setUp(self):
        super().setUp()
        self.sign_in()  # the tracking switch is admin-only
        self.record("KXNFLGAME-26OCT25KCSEA-SEA", "Seattle", 0.6, 0.7, game_date="2026-10-25")
        self.record("KXNHLGAME-26OCT23TBCOL-TB", "Tampa Bay", 0.5, 0.5, game_date="2026-10-23")

    def enabled(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))["enabled"]

    def test_a_league_tab_shows_only_that_leagues_games(self):
        r = self.client.get("/league/NFL")
        self.assertEqual(r.status_code, 200)
        self.assertIn("NFL markets", r.text)
        self.assertIn("Seattle", r.text)
        self.assertNotIn("Tampa Bay", r.text)

    def test_the_tab_is_case_insensitive(self):
        self.assertIn("Seattle", self.client.get("/league/nfl").text)

    def test_an_unknown_league_is_a_404(self):
        self.assertEqual(self.client.get("/league/WNBA").status_code, 404)
        self.assertEqual(self.client.post("/league/WNBA/tracking", data={"enabled": "0"}).status_code, 404)

    def test_filters_stay_on_the_league_tab(self):
        self.assertIn('action="/league/NHL"', self.client.get("/league/NHL").text)

    def test_the_switch_shows_whether_the_league_is_tracked(self):
        self.assertIn('aria-checked="true"', self.client.get("/league/NFL").text)
        Leagues.set_enabled("NFL", False)
        r = self.client.get("/league/NFL")
        self.assertIn('aria-checked="false"', r.text)
        self.assertIn("Not tracking", r.text)

    def test_turning_tracking_off_updates_leagues_json_and_redirects_back(self):
        r = self.client.post("/league/NFL/tracking", data={"enabled": "0"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/league/NFL")
        self.assertNotIn("NFL", self.enabled())

    def test_turning_tracking_back_on(self):
        self.client.post("/league/NFL/tracking", data={"enabled": "0"})
        self.client.post("/league/NFL/tracking", data={"enabled": "1"})
        self.assertIn("NFL", self.enabled())

    def test_an_untracked_leagues_games_are_still_shown(self):
        self.client.post("/league/NFL/tracking", data={"enabled": "0"})
        self.assertIn("Seattle", self.client.get("/league/NFL").text)

    def test_an_untracked_league_is_dimmed_in_the_nav(self):
        self.client.post("/league/NHL/tracking", data={"enabled": "0"})
        self.assertIn('title="Not tracking NHL"', self.client.get("/").text)
        self.assertNotIn('title="Not tracking NFL"', self.client.get("/").text)

    def test_a_game_page_highlights_and_links_back_to_its_league(self):
        r = self.client.get("/markets/KXNHLGAME-26OCT23TBCOL-TB")
        self.assertIn('href="/league/NHL">&larr; NHL markets', r.text)


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

    def test_segment_filter_scopes_the_stats_and_history(self):
        self.record("G1", "Denver", 0.40, 0.10, game_date="2026-10-05")   # preseason, settled, wrong
        self.settle("G1", True)
        self.record("G2", "Utah", 0.40, 0.70, game_date="2026-12-25")     # regular, settled, right
        self.settle("G2", True)

        r = self.client.get("/ai/OpenAI", params={"segment": "regular"})
        self.assertIn("Utah", r.text)
        self.assertNotIn("Denver", r.text)

    def test_season_filter_scopes_the_stats_and_history(self):
        self.record("G1", "Denver", 0.40, 0.10, game_date="2026-12-25")   # 2026-27, settled, wrong
        self.settle("G1", True)
        self.record("G2", "Utah", 0.40, 0.70, game_date="2027-12-25")     # 2027-28, settled, right
        self.settle("G2", True)

        r = self.client.get("/ai/OpenAI", params={"season": "2027-28"})
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
