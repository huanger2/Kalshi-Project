"""Running hosted (Railway): DATA_DIR, the cron-triggered daily run, and the admin Data page."""

import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import openpyxl
from fastapi.testclient import TestClient

import CostTracker
import Accounts
import DataDir
import Leagues
import Tracker
import trigger_daily_run
from EVCalculator import LLMPrediction
from webapp import daily_run, data_files
from webapp.main import app, get_db


class DataDirTests(unittest.TestCase):
    def test_unset_means_the_working_directory(self):
        with mock.patch.dict("os.environ"):
            os.environ.pop("DATA_DIR", None)
            self.assertEqual(DataDir.data_path("predictions.db"), "predictions.db")

    def test_set_puts_files_inside_it(self):
        with mock.patch.dict("os.environ", {"DATA_DIR": "/data"}):
            self.assertEqual(DataDir.data_path("daily_logs"), os.path.join("/data", "daily_logs"))


class HostedTrackingStateTests(unittest.TestCase):
    """With DATA_DIR set, the switches save to the volume and leave leagues.json alone."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.config = self.dir / "leagues.json"
        self.config.write_text('{"enabled": ["NBA", "NFL", "NHL", "MLB"], "seasons": {}}', encoding="utf-8")
        for patcher in (mock.patch.object(Leagues, "CONFIG_PATH", self.config),
                        mock.patch.dict("os.environ", {"DATA_DIR": str(self.dir / "volume")})):
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.dir / "volume").mkdir()

    def test_before_any_switch_is_flipped_leagues_json_decides(self):
        self.assertEqual(Leagues.enabled_leagues(), ["NBA", "NFL", "NHL", "MLB"])

    def test_a_flip_is_saved_on_the_volume_not_in_leagues_json(self):
        before = self.config.read_text(encoding="utf-8")
        Leagues.set_enabled("MLB", False)
        self.assertEqual(self.config.read_text(encoding="utf-8"), before)
        state = json.loads((self.dir / "volume" / Leagues.STATE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(state, {"enabled": ["NBA", "NFL", "NHL"]})
        self.assertEqual(Leagues.enabled_leagues(), ["NBA", "NFL", "NHL"])

    def test_the_saved_state_outlives_a_deploy_that_resets_leagues_json(self):
        Leagues.set_enabled("NHL", False)
        self.config.write_text('{"enabled": ["NBA", "NFL", "NHL", "MLB"], "seasons": {}}', encoding="utf-8")
        self.assertEqual(Leagues.enabled_leagues(), ["NBA", "NFL", "MLB"])


class DailyRunTests(unittest.TestCase):
    def tearDown(self):
        # Let any background run finish so tests don't leak into each other.
        daily_run._lock.acquire()
        daily_run._lock.release()

    def test_token_check(self):
        with mock.patch.dict("os.environ", {"DAILY_RUN_TOKEN": "tok"}):
            self.assertTrue(daily_run.token_ok("Bearer tok"))
            self.assertFalse(daily_run.token_ok("Bearer nope"))
            self.assertFalse(daily_run.token_ok(None))
        with mock.patch.dict("os.environ"):
            os.environ.pop("DAILY_RUN_TOKEN", None)
            self.assertFalse(daily_run.token_ok("Bearer "))

    def test_a_run_reports_its_result(self):
        run_id = daily_run.start(run=lambda: (3, 7))
        daily_run._lock.acquire()
        daily_run._lock.release()
        status = daily_run.status()
        self.assertEqual((status["run_id"], status["state"], status["settled"], status["logged"]),
                         (run_id, "done", 3, 7))

    def test_a_failure_is_reported_not_raised(self):
        def boom():
            raise SystemExit("Can't write daily_logs/2026-10-04.xlsx")
        with mock.patch("traceback.print_exc"):
            daily_run.start(run=boom)
            daily_run._lock.acquire()
            daily_run._lock.release()
        self.assertEqual(daily_run.status()["state"], "failed")
        self.assertIn("Can't write", daily_run.status()["error"])

    def test_only_one_run_at_a_time(self):
        gate = threading.Event()
        first = daily_run.start(run=lambda: (gate.wait(5), (0, 0))[1])
        try:
            self.assertIsNotNone(first)
            self.assertIsNone(daily_run.start(run=lambda: (0, 0)))
        finally:
            gate.set()


class WebAppHostingTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.db_path = os.path.join(self.dir, "predictions.db")
        self.logs = os.path.join(self.dir, "daily_logs")

        def override_get_db():
            conn = Tracker.connect(self.db_path)
            try:
                yield conn
            finally:
                conn.close()

        app.dependency_overrides[get_db] = override_get_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)

        import CostExport
        import DailyLog
        config = Path(self.dir) / "leagues.json"
        shutil.copy(Leagues.CONFIG_PATH, config)
        for patcher in (
            mock.patch.dict("os.environ", {"USERNAME": "admin", "PASSWORD": "s3cret", "DAILY_RUN_TOKEN": "tok"}),
            mock.patch.object(Tracker, "DEFAULT_DB", self.db_path),
            mock.patch.object(DailyLog, "DEFAULT_DIR", self.logs),
            mock.patch.object(CostExport, "DEFAULT_OUT", os.path.join(self.dir, "api_costs.xlsx")),
            mock.patch.object(Leagues, "CONFIG_PATH", config),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("SESSION_SECRET", None)
        self.client = TestClient(app)

    def assert_database_untouched(self):
        """Still the (empty) database the page visits created: nothing swapped in, no backup made."""
        conn = Tracker.connect(self.db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0], 0)
        finally:
            conn.close()
        self.assertFalse(any(".bak-" in n for n in os.listdir(self.dir)))

    def sign_in(self):
        self.client.post("/login", data={"username": "admin", "password": "s3cret", "next": "/"})

    def as_guest(self):
        self.client.post("/guest")


class DailyRunEndpointTests(WebAppHostingTestCase):
    def tearDown(self):
        daily_run._lock.acquire()
        daily_run._lock.release()

    def test_needs_the_token(self):
        self.assertEqual(self.client.post("/internal/daily-run").status_code, 401)
        self.assertEqual(self.client.post("/internal/daily-run", headers={"Authorization": "Bearer x"}).status_code, 401)
        self.assertEqual(self.client.get("/internal/daily-run").status_code, 401)

    def test_a_signed_in_admin_still_needs_the_token(self):
        self.sign_in()
        self.assertEqual(self.client.post("/internal/daily-run").status_code, 401)

    def test_refuses_everything_when_no_token_is_configured(self):
        with mock.patch.dict("os.environ"):
            os.environ.pop("DAILY_RUN_TOKEN")
            r = self.client.post("/internal/daily-run", headers={"Authorization": "Bearer "})
        self.assertEqual(r.status_code, 503)

    def test_starts_a_run_and_reports_it(self):
        real_start = daily_run.start
        with mock.patch.object(daily_run, "start", side_effect=lambda: real_start(run=lambda: (1, 2))):
            r = self.client.post("/internal/daily-run", headers={"Authorization": "Bearer tok"})
        self.assertEqual(r.status_code, 202)
        daily_run._lock.acquire()
        daily_run._lock.release()
        status = self.client.get("/internal/daily-run", headers={"Authorization": "Bearer tok"}).json()
        self.assertEqual((status["state"], status["logged"]), ("done", 2))

    def test_409_while_a_run_is_in_progress(self):
        with mock.patch.object(daily_run, "start", return_value=None):
            r = self.client.post("/internal/daily-run", headers={"Authorization": "Bearer tok"})
        self.assertEqual(r.status_code, 409)


class RunNowButtonTests(WebAppHostingTestCase):
    """The admin's "run the daily run now" button at the top of the markets pages."""

    IDLE = {"run_id": 3, "state": "done", "started_at": 0.0, "finished_at": 60.0, "settled": 2, "logged": 5,
            "error": None, "leagues": None}

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(daily_run, "status", return_value=dict(self.IDLE))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_admins_get_the_button_and_the_status(self):
        self.sign_in()
        home = self.client.get("/").text
        self.assertIn('action="/admin/daily-run"', home)
        self.assertIn("Run daily run now", home)
        self.assertIn("logged 5", home)
        league = self.client.get("/league/NBA").text
        self.assertIn('name="league" value="NBA"', league)
        self.assertIn("Run NBA now", league)

    def test_guests_and_users_get_neither(self):
        self.as_guest()
        self.assertNotIn('action="/admin/daily-run"', self.client.get("/").text)
        self.assertEqual(self.client.post("/admin/daily-run").status_code, 403)
        self.assertEqual(self.client.get("/admin/daily-run").status_code, 403)
        conn = Tracker.connect(self.db_path)
        try:
            Accounts.bootstrap(conn)
            Accounts.create_user(conn, "pat", "pat-password")
        finally:
            conn.close()
        self.client.post("/login", data={"username": "pat", "password": "pat-password", "next": "/"})
        self.assertNotIn('action="/admin/daily-run"', self.client.get("/").text)
        self.assertEqual(self.client.post("/admin/daily-run").status_code, 403)

    def test_running_one_league_from_its_tab(self):
        self.sign_in()
        with mock.patch.object(daily_run, "start", return_value=4) as start:
            r = self.client.post("/admin/daily-run", data={"league": "nba"}, follow_redirects=False)
        start.assert_called_once_with(leagues=["NBA"])
        self.assertEqual(r.headers["location"], "/league/NBA?run=started")
        self.assertIn("Started the daily run", self.client.get(r.headers["location"]).text)

    def test_running_every_league_and_a_busy_refusal(self):
        self.sign_in()
        with mock.patch.object(daily_run, "start", return_value=None) as start:
            r = self.client.post("/admin/daily-run", follow_redirects=False)
        start.assert_called_once_with(leagues=None)
        self.assertEqual(r.headers["location"], "/?run=busy")
        self.assertIn("already going", self.client.get(r.headers["location"]).text)

    def test_an_unknown_league_is_a_404(self):
        self.sign_in()
        with mock.patch.object(daily_run, "start") as start:
            self.assertEqual(self.client.post("/admin/daily-run", data={"league": "XFL"}).status_code, 404)
        start.assert_not_called()

    def test_while_running_the_page_says_so_and_polls(self):
        daily_run.status.return_value = {**self.IDLE, "state": "running", "finished_at": None, "leagues": ["NBA"]}
        self.sign_in()
        page = self.client.get("/league/NBA").text
        self.assertIn("Daily run #3 for NBA", page)
        self.assertIn("/static/daily-run-status.js", page)
        self.assertIn("disabled", page.split('action="/admin/daily-run"')[1].split("</form>")[0])
        self.assertEqual(self.client.get("/admin/daily-run").json()["state"], "running")


class DailyRunLeagueTests(unittest.TestCase):
    def test_a_run_for_some_leagues_passes_them_to_the_daily_log(self):
        import DailyLog
        saved = dict(daily_run._status)
        self.addCleanup(daily_run._status.update, saved)
        done = threading.Event()
        with mock.patch.object(daily_run, "_save"),              mock.patch.object(DailyLog, "run", side_effect=lambda **kw: (done.set(), (0, 0))[1]) as run:
            self.assertIsNotNone(daily_run.start(leagues=["NFL"]))
            self.assertTrue(done.wait(5))
            for _ in range(50):  # let the background thread record its result
                if daily_run.status()["state"] != "running":
                    break
                time.sleep(0.02)
        run.assert_called_once_with(leagues=["NFL"])
        self.assertEqual(daily_run.status()["leagues"], ["NFL"])

    def test_ages_read_in_words(self):
        self.assertEqual(daily_run._ago(1000.0, 1030.0), "just now")
        self.assertEqual(daily_run._ago(1000.0, 1000.0 + 12 * 60), "12 min ago")
        self.assertEqual(daily_run._ago(0.0, 3 * 3600.0), "3 h ago")
        self.assertEqual(daily_run._ago(0.0, 2 * 86400.0), "2 days ago")
        self.assertIsNone(daily_run._ago(None, 5.0))


class TriggerScriptTests(unittest.TestCase):
    def run_main(self, responses, env=None):
        calls = []

        def fake_call(method, url, token):
            calls.append((method, url, token))
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        env = env if env is not None else {"DAILY_RUN_URL": "https://app.example/", "DAILY_RUN_TOKEN": "tok"}
        with mock.patch.dict("os.environ", env, clear=False), \
             mock.patch.object(trigger_daily_run, "_call", side_effect=fake_call), \
             mock.patch("builtins.print"):
            code = trigger_daily_run.main(sleep=lambda s: None)
        return code, calls

    def test_starts_then_waits_for_success(self):
        code, calls = self.run_main([
            (202, {"run_id": 4}),
            (200, {"run_id": 4, "state": "running"}),
            (200, {"run_id": 4, "state": "done", "settled": 9, "logged": 12}),
        ])
        self.assertEqual(code, 0)
        self.assertEqual(calls[0], ("POST", "https://app.example/internal/daily-run", "tok"))

    def test_a_failed_run_exits_nonzero(self):
        code, _ = self.run_main([(202, {"run_id": 1}), (200, {"run_id": 1, "state": "failed", "error": "x"})])
        self.assertEqual(code, 1)

    def test_a_bad_token_exits_nonzero_without_polling(self):
        code, calls = self.run_main([(401, {"detail": "Bad or missing daily-run token"})])
        self.assertEqual((code, len(calls)), (1, 1))

    def test_survives_a_blip_while_polling(self):
        code, _ = self.run_main([
            (202, {"run_id": 2}), OSError("connection reset"),
            (200, {"run_id": 2, "state": "done", "settled": 0, "logged": 0}),
        ])
        self.assertEqual(code, 0)

    def test_a_webapp_restart_mid_run_is_reported(self):
        code, _ = self.run_main([(202, {"run_id": 5}), (200, {"run_id": 0, "state": "idle"})])
        self.assertEqual(code, 1)

    def test_an_unreachable_url_exits_nonzero(self):
        code, calls = self.run_main([OSError("Name or service not known")])
        self.assertEqual((code, len(calls)), (1, 1))

    def test_a_long_run_logs_that_it_is_still_going(self):
        clock = iter(range(0, 10_000, 15))  # each now() call: another poll interval later
        lines = []
        responses = [(202, {"run_id": 7})] + [(200, {"run_id": 7, "state": "running"})] * 30 +                     [(200, {"run_id": 7, "state": "done", "settled": 1, "logged": 2})]
        with mock.patch.dict("os.environ", {"DAILY_RUN_URL": "https://app.example", "DAILY_RUN_TOKEN": "tok"}),              mock.patch.object(trigger_daily_run, "_call", side_effect=responses),              mock.patch("builtins.print", side_effect=lambda msg, **kw: lines.append((msg, kw))):
            self.assertEqual(trigger_daily_run.main(sleep=lambda s: None, now=lambda: next(clock)), 0)
        self.assertTrue(any("still running" in msg for msg, _ in lines))
        self.assertTrue(all(kw.get("flush") for _, kw in lines))  # never held back in a pipe

    def test_the_start_command_runs_the_trigger_on_the_cron_service(self):
        # Railway gives the cron service the website's start command (python -m webapp);
        # DAILY_TRIGGER makes that run the trigger once and exit, instead of a server that never stops.
        from webapp import __main__ as entry
        with mock.patch.dict("os.environ", {"DAILY_TRIGGER": "1"}),              mock.patch.object(trigger_daily_run, "main", return_value=0) as trigger,              mock.patch("uvicorn.run", side_effect=AssertionError("started the website")):
            with self.assertRaises(SystemExit) as exit_:
                entry.main()
        trigger.assert_called_once()
        self.assertEqual(exit_.exception.code, 0)

    def test_the_start_command_serves_the_website_otherwise(self):
        from webapp import __main__ as entry
        with mock.patch.dict("os.environ"):
            os.environ.pop("DAILY_TRIGGER", None)
            with mock.patch.object(trigger_daily_run, "main", side_effect=AssertionError("ran the trigger")),                  mock.patch("uvicorn.run") as serve:
                entry.main()
        self.assertEqual(serve.call_args.args[0], "webapp.main:app")

    def test_missing_settings(self):
        with mock.patch.dict("os.environ"):
            os.environ.pop("DAILY_RUN_URL", None)
            code, calls = self.run_main([], env={"DAILY_RUN_TOKEN": "tok"})
        self.assertEqual((code, calls), (1, []))


def _db_bytes(rows=1) -> bytes:
    tmp = tempfile.mktemp(suffix=".db")
    conn = Tracker.connect(tmp)
    for i in range(rows):
        Tracker.record_predictions(conn, {"ticker": f"KXNBAGAME-26OCT0{i + 1}AAABBB-AAA"}, 0.5,
                                   [LLMPrediction("OpenAI", 0.6, 0.8)], game_date=f"2026-10-0{i + 1}")
    conn.close()
    data = Path(tmp).read_bytes()
    os.remove(tmp)
    return data


def _xlsx_bytes(path_for_day) -> bytes:
    import DailyLog
    import pandas as pd
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "x.xlsx")
    DailyLog.save(pd.DataFrame([{"game_date": "2026-10-04", "ticker": "KXNBAGAME-26OCT04UTADEN-DEN",
                                 "event_ticker": "KXNBAGAME-26OCT04UTADEN", "team": "Denver",
                                 "market_prob": 0.6}]), path)
    data = Path(path).read_bytes()
    shutil.rmtree(tmp)
    return data


class AdminDataPageTests(WebAppHostingTestCase):
    def test_guests_and_strangers_are_kept_out(self):
        self.assertEqual(self.client.get("/admin/data", follow_redirects=False).status_code, 303)  # to login
        self.as_guest()
        self.assertEqual(self.client.get("/admin/data").status_code, 403)
        self.assertEqual(self.client.get("/admin/data/download/predictions.xlsx").status_code, 403)
        r = self.client.post("/admin/data/predictions-db", files={"file": ("p.db", _db_bytes())})
        self.assertEqual(r.status_code, 403)
        self.assert_database_untouched()

    def test_admin_sees_the_page_and_a_data_link(self):
        self.sign_in()
        r = self.client.get("/admin/data")
        self.assertEqual(r.status_code, 200)
        self.assertIn('href="/admin/data"', r.text)

    def test_guests_do_not_see_the_data_link(self):
        self.as_guest()
        self.assertNotIn('href="/admin/data"', self.client.get("/").text)

    def test_every_page_has_the_theme_switch(self):
        # Including the sign-in page, before anyone has a role.
        self.assertIn('data-theme-choice="dark"', self.client.get("/login").text)
        self.as_guest()
        page = self.client.get("/").text
        for choice in ("light", "dark", "auto"):
            self.assertIn(f'data-theme-choice="{choice}"', page)

    def test_uploading_a_database_replaces_it_and_keeps_a_backup(self):
        self.sign_in()
        conn = Tracker.connect(self.db_path)
        conn.close()
        r = self.client.post("/admin/data/predictions-db", files={"file": ("predictions.db", _db_bytes(rows=2))})
        self.assertEqual(r.status_code, 200)
        self.assertIn("2 prediction rows", r.text)
        self.assertTrue(any(n.startswith("predictions.db.bak-") for n in os.listdir(self.dir)))
        conn = Tracker.connect(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0], 2)

    def test_a_non_database_upload_is_rejected_and_nothing_changes(self):
        self.sign_in()
        r = self.client.post("/admin/data/predictions-db", files={"file": ("x.db", b"not a database")})
        self.assertEqual(r.status_code, 400)
        self.assertIn("isn&#39;t a SQLite database", r.text)
        self.assert_database_untouched()

    def test_a_database_without_predictions_is_rejected(self):
        self.sign_in()
        tmp = tempfile.mktemp(suffix=".db")
        sqlite3.connect(tmp).execute("CREATE TABLE other (x)").connection.close()
        data = Path(tmp).read_bytes()
        os.remove(tmp)
        r = self.client.post("/admin/data/predictions-db", files={"file": ("x.db", data)})
        self.assertEqual(r.status_code, 400)
        self.assertIn("no predictions table", r.text)

    def test_no_replacing_the_database_mid_run(self):
        self.sign_in()
        with mock.patch.object(daily_run, "status", return_value={"state": "running", "run_id": 1}):
            r = self.client.post("/admin/data/predictions-db", files={"file": ("p.db", _db_bytes())})
        self.assertEqual(r.status_code, 409)

    def test_uploading_day_files(self):
        self.sign_in()
        data = _xlsx_bytes(None)
        r = self.client.post("/admin/data/daily-logs", files=[
            ("files", ("2026-10-04.xlsx", data)), ("files", ("notes.xlsx", data)), ("files", ("2026-10-05.xlsx", b"junk")),
        ])
        self.assertIn("Added 1 day file", r.text)
        self.assertIn("Rejected notes.xlsx, 2026-10-05.xlsx", r.text)
        self.assertEqual(os.listdir(self.logs), ["2026-10-04.xlsx"])

        r = self.client.post("/admin/data/daily-logs", files=[("files", ("2026-10-04.xlsx", data))])
        self.assertIn("Skipped 2026-10-04.xlsx", r.text)

    def test_day_files_are_one_dropdown(self):
        self.sign_in()
        data = _xlsx_bytes(None)
        self.client.post("/admin/data/daily-logs", files=[
            ("files", ("2026-10-04.xlsx", data)), ("files", ("2026-10-05.xlsx", data)),
        ])
        page = self.client.get("/admin/data").text
        self.assertEqual(page.count('<option value="daily_logs/'), 2)
        self.assertNotIn('href="/admin/data/download/daily_logs/', page)
        self.assertLess(page.index("2026-10-05"), page.index("2026-10-04"))  # newest first
        r = self.client.get("/admin/data/download", params={"name": "daily_logs/2026-10-04.xlsx"})
        self.assertEqual(r.content, data)
        self.assertEqual(self.client.get("/admin/data/download", params={"name": "../leagues.json"}).status_code, 404)

    def test_api_spend_summary(self):
        self.sign_in()
        self.assertIn("No AI calls logged yet", self.client.get("/admin/data").text)

        conn = sqlite3.connect(self.db_path)
        conn.executescript(CostTracker.SCHEMA)
        conn.executemany(
            "INSERT INTO api_usage (created_at, provider, model, input_tokens, output_tokens, cost_usd) "
            "VALUES ('2026-10-18T00:00:00+00:00', ?, 'm', ?, ?, ?)",
            [("claude", 1000, 200, 0.5), ("claude", 1500, 300, 0.25), ("gemini", 400, 100, None)],
        )
        conn.commit()
        conn.close()

        page = self.client.get("/admin/data").text
        self.assertIn("API spend", page)
        self.assertIn("2,500", page)    # claude's input tokens
        self.assertIn("$0.75", page)    # claude's cost, and the total (gemini is unpriced)
        self.assertIn("unknown", page)  # gemini's cost
        self.assertIn('class="total-row"', page)
        self.assertNotIn("No AI calls logged yet", page)

    def test_downloads(self):
        self.sign_in()
        conn = Tracker.connect(self.db_path)
        Tracker.record_predictions(conn, {"ticker": "KXNBAGAME-26OCT04UTADEN-DEN"}, 0.6,
                                   [LLMPrediction("OpenAI", 0.7, 0.8)], game_date="2026-10-04")
        conn.close()
        self.assertEqual(self.client.get("/admin/data/download/predictions.db").content[:16], data_files.SQLITE_MAGIC)
        export = self.client.get("/admin/data/download/predictions.xlsx")
        self.assertEqual(export.status_code, 200)
        self.assertIn("predictions", openpyxl.load_workbook(io.BytesIO(export.content)).sheetnames[0].lower())

    def test_only_listed_files_can_be_downloaded(self):
        self.sign_in()
        for name in ("../leagues.json", "daily_logs/../../secret", "leagues.json"):
            self.assertEqual(self.client.get(f"/admin/data/download/{name}").status_code, 404, name)


if __name__ == "__main__":
    unittest.main()


class StorageProblemTests(unittest.TestCase):
    def test_a_writable_folder_is_fine(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(DataDir.problem(os.path.join(d, "predictions.db")))

    def test_a_missing_folder_says_to_attach_a_volume(self):
        with mock.patch.dict("os.environ", {"DATA_DIR": "/no/such/volume"}):
            message = DataDir.problem("/no/such/volume/predictions.db")
        self.assertIn("doesn't exist", message)
        self.assertIn("attach a volume", message)

    def test_an_unwritable_folder_says_to_run_as_root(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("os.access", return_value=False):
            message = DataDir.problem(os.path.join(d, "predictions.db"))
        self.assertIn("RAILWAY_RUN_UID=0", message)


class DatabaseUnavailablePageTests(unittest.TestCase):
    """What Railway showed as a bare 500 traceback on every page: DATA_DIR pointing at no volume."""

    def setUp(self):
        missing = os.path.join(tempfile.gettempdir(), "no-volume-here", "predictions.db")

        def override_get_db():
            conn = Tracker.connect(missing)
            try:
                yield conn
            finally:
                conn.close()

        app.dependency_overrides[get_db] = override_get_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        for patcher in (mock.patch.object(Tracker, "DEFAULT_DB", missing),
                        mock.patch.dict("os.environ", {"DATA_DIR": os.path.dirname(missing)})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.client.cookies.set("kalshi_session", "guest")

    def test_pages_explain_the_fix_instead_of_crashing(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 503)
        self.assertIn("Database unavailable", r.text)
        self.assertIn("attach a volume", r.text)

    def test_the_api_and_health_check_answer_in_json(self):
        r = self.client.get("/api/accuracy")
        self.assertEqual(r.status_code, 503)
        self.assertIn("attach a volume", r.json()["detail"])


class PersistedRunStatusTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, daily_run.STATUS_NAME)
        saved = dict(daily_run._status)
        self.addCleanup(daily_run._status.update, saved)
        patcher = mock.patch.dict("os.environ", {"DATA_DIR": tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def finish(self):
        daily_run._lock.acquire()
        daily_run._lock.release()

    def test_a_finished_run_is_saved_and_survives_a_restart(self):
        daily_run.start(run=lambda: (2, 5))
        self.finish()
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["state"], "done")
        daily_run._status.update(run_id=0, state="idle", logged=None)  # a fresh process
        daily_run._load()
        self.assertEqual((daily_run.status()["state"], daily_run.status()["logged"]), ("done", 5))

    def test_a_run_cut_off_by_a_restart_reads_as_interrupted(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"run_id": 3, "state": "running"}, f)
        daily_run._load()
        self.assertEqual(daily_run.status()["state"], "failed")
        self.assertIn("Interrupted", daily_run.status()["error"])

    def test_nothing_is_written_locally(self):
        with mock.patch.dict("os.environ"):
            os.environ.pop("DATA_DIR")
            self.assertIsNone(daily_run._status_path())
