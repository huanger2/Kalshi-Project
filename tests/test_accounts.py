"""Accounts (Accounts.py): users, per-user settings and Kalshi keys, the first admin, and the pages that manage them."""

import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import Accounts
import AIWeights
import Betting
import Kalshi
import Leagues
import Tracker
from EVCalculator import LLMPrediction
from tests.test_kalshi import _pem, _rsa_key
from tests.test_webapp_main import market
from webapp import auth
from webapp.main import app, get_db


class AccountsDbTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db_path = str(self.dir / "predictions.db")
        self.conn = Tracker.connect(self.db_path)
        self.addCleanup(self.conn.close)


class PasswordTests(unittest.TestCase):
    def test_a_hash_verifies_only_its_own_password(self):
        stored = Accounts.hash_password("correct horse")
        self.assertTrue(Accounts.verify_password("correct horse", stored))
        self.assertFalse(Accounts.verify_password("correct horsE", stored))
        self.assertNotIn("correct horse", stored)

    def test_the_same_password_hashes_differently_each_time(self):
        self.assertNotEqual(Accounts.hash_password("same one"), Accounts.hash_password("same one"))

    def test_garbage_never_verifies(self):
        for stored in ("", "plain", "md5$1$aa$bb", "pbkdf2_sha256$x$y$z"):
            self.assertFalse(Accounts.verify_password("anything", stored))


class UserTests(AccountsDbTestCase):
    def test_create_and_authenticate(self):
        user = Accounts.create_user(self.conn, "Pat", "a-good-password")
        self.assertEqual(Accounts.authenticate(self.conn, "pat", "a-good-password")["id"], user["id"])  # any case
        self.assertIsNone(Accounts.authenticate(self.conn, "pat", "wrong-password"))
        self.assertIsNone(Accounts.authenticate(self.conn, "nobody", "a-good-password"))

    def test_usernames_and_passwords_are_checked(self):
        for name, password in (("ab", "a-good-password"), ("has space", "a-good-password"), ("fine", "short")):
            with self.subTest(name=name), self.assertRaises(Accounts.AccountError):
                Accounts.create_user(self.conn, name, password)
        Accounts.create_user(self.conn, "taken", "a-good-password")
        with self.assertRaisesRegex(Accounts.AccountError, "already"):
            Accounts.create_user(self.conn, "TAKEN", "a-good-password")

    def test_changing_a_password_needs_the_current_one(self):
        user = Accounts.create_user(self.conn, "pat", "a-good-password")
        with self.assertRaises(Accounts.AccountError):
            Accounts.change_own_password(self.conn, user["id"], "not-it", "another-password")
        Accounts.change_own_password(self.conn, user["id"], "a-good-password", "another-password")
        self.assertIsNotNone(Accounts.authenticate(self.conn, "pat", "another-password"))
        self.assertGreater(Accounts.get_user(self.conn, user["id"])["session_version"], user["session_version"])

    def test_the_last_admin_cant_be_removed_or_demoted(self):
        admin = Accounts.create_user(self.conn, "boss", "a-good-password", is_admin=True)
        with self.assertRaises(Accounts.AccountError):
            Accounts.set_admin(self.conn, admin["id"], False)
        with self.assertRaises(Accounts.AccountError):
            Accounts.delete_user(self.conn, admin["id"])
        other = Accounts.create_user(self.conn, "deputy", "a-good-password", is_admin=True)
        Accounts.set_admin(self.conn, admin["id"], False)  # fine now there's another admin
        self.assertFalse(Accounts.get_user(self.conn, admin["id"])["is_admin"])
        self.assertTrue(Accounts.get_user(self.conn, other["id"])["is_admin"])

    def test_deleting_a_user_keeps_their_bets_in_the_history(self):
        Accounts.create_user(self.conn, "boss", "a-good-password", is_admin=True)
        user = Accounts.create_user(self.conn, "leaving", "a-good-password")
        Accounts.set_setting(self.conn, user["id"], "betting", {"mode": "paper"})
        self.conn.execute("INSERT INTO bets (created_at, user_id, mode, ticker, side, price, count, cost, fee, "
                          "ai_prob, edge) VALUES ('t', ?, 'paper', 'T', 'yes', 0.5, 1, 0.5, 0.02, 0.6, 0.1)",
                          (user["id"],))
        Accounts.delete_user(self.conn, user["id"])
        self.assertIsNone(Accounts.get_user(self.conn, user["id"]))
        self.assertIsNone(Accounts.get_setting(self.conn, user["id"], "betting"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM bets").fetchone()[0], 1)


class KalshiKeyTests(AccountsDbTestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = _rsa_key()

    def setUp(self):
        super().setUp()
        self.uid = Accounts.create_user(self.conn, "pat", "a-good-password")["id"]

    def test_saved_encrypted_and_read_back(self):
        Accounts.save_kalshi_key(self.conn, self.uid, "demo", " key-123 ", _pem(self.key))
        stored = self.conn.execute("SELECT private_key FROM user_kalshi_keys").fetchone()[0]
        self.assertNotIn(b"PRIVATE KEY", bytes(stored))
        creds = Accounts.kalshi_credentials(self.conn, self.uid, "demo")
        self.assertEqual((creds.env, creds.key_id), ("demo", "key-123"))
        self.assertEqual(creds.private_key.private_numbers(), self.key.private_numbers())
        self.assertEqual(Accounts.kalshi_key_info(self.conn, self.uid, "demo")["key_id"], "key-123")
        self.assertIsNone(Accounts.kalshi_credentials(self.conn, self.uid, "live"))  # each exchange separately

    def test_a_bad_key_is_refused(self):
        with self.assertRaisesRegex(Accounts.AccountError, "couldn't be read"):
            Accounts.save_kalshi_key(self.conn, self.uid, "demo", "key-123", "not a key")
        with self.assertRaisesRegex(Accounts.AccountError, "ID"):
            Accounts.save_kalshi_key(self.conn, self.uid, "demo", "  ", _pem(self.key))
        self.assertIsNone(Accounts.kalshi_key_info(self.conn, self.uid, "demo"))

    def test_a_changed_secrets_key_makes_saved_keys_unreadable_not_wrong(self):
        Accounts.save_kalshi_key(self.conn, self.uid, "live", "key-123", _pem(self.key))
        with mock.patch.dict(os.environ, {"SECRETS_KEY": "a different secret"}):
            with self.assertRaisesRegex(Kalshi.TradingError, "SECRETS_KEY changed"):
                Accounts.kalshi_credentials(self.conn, self.uid, "live")

    def test_without_secrets_key_a_key_file_is_generated_in_the_data_folder(self):
        with mock.patch.dict(os.environ, {"DATA_DIR": str(self.dir)}):
            os.environ.pop("SECRETS_KEY")
            Accounts.save_kalshi_key(self.conn, self.uid, "demo", "key-123", _pem(self.key))
            self.assertTrue((self.dir / "secrets.key").exists())
            self.assertIsNotNone(Accounts.kalshi_credentials(self.conn, self.uid, "demo"))


class BootstrapTests(AccountsDbTestCase):
    """The single-login era's USERNAME/PASSWORD, betting.json and KALSHI_* keys become the first admin's."""

    @classmethod
    def setUpClass(cls):
        cls.key = _rsa_key()

    def setUp(self):
        super().setUp()
        rules = self.dir / "betting.json"
        rules.write_text(json.dumps({"_comment": "x", "mode": "demo", "min_edge": 0.07}), encoding="utf-8")
        for patcher in (mock.patch.object(Betting, "BETTING_PATH", rules),
                        mock.patch.dict(os.environ, {"USERNAME": "eric", "PASSWORD": "old-env-password"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        # An unowned bet from before accounts existed.
        self.conn.execute("INSERT INTO bets (created_at, mode, ticker, side, price, count, cost, fee, ai_prob, edge) "
                          "VALUES ('t', 'paper', 'T', 'yes', 0.5, 1, 0.5, 0.02, 0.6, 0.1)")
        self.conn.commit()

    def test_the_first_admin_gets_everything_from_before(self):
        legacy = {"demo": ("demo-id", _pem(self.key)), "live": None}
        with mock.patch.object(Kalshi, "legacy_env_credentials", side_effect=legacy.get):
            admin = Accounts.bootstrap(self.conn)
        self.assertEqual((admin["username"], admin["is_admin"]), ("eric", True))
        self.assertIsNotNone(Accounts.authenticate(self.conn, "eric", "old-env-password"))
        self.assertEqual(Betting.load_config(self.conn, admin["id"])["min_edge"], 0.07)
        self.assertEqual(Betting.load_config(self.conn, admin["id"])["mode"], "demo")
        self.assertEqual(Accounts.kalshi_key_info(self.conn, admin["id"], "demo")["key_id"], "demo-id")
        self.assertIsNone(Accounts.kalshi_key_info(self.conn, admin["id"], "live"))
        self.assertEqual(self.conn.execute("SELECT user_id FROM bets").fetchone()[0], admin["id"])

    def test_only_once(self):
        Accounts.bootstrap(self.conn)
        with mock.patch.dict(os.environ, {"USERNAME": "someone-else"}):
            self.assertIsNone(Accounts.bootstrap(self.conn))
        self.assertEqual([u["username"] for u in Accounts.list_users(self.conn)], ["eric"])

    def test_nothing_without_a_username_and_password(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("PASSWORD")
            self.assertIsNone(Accounts.bootstrap(self.conn))
        self.assertEqual(Accounts.count_users(self.conn), 0)


class WeightsTests(AccountsDbTestCase):
    def test_a_user_overrides_only_what_they_set(self):
        uid = Accounts.create_user(self.conn, "pat", "a-good-password")["id"]
        with mock.patch.object(AIWeights, "get_weight", return_value=1.0):
            self.assertEqual(AIWeights.for_user(self.conn, uid), {"claude": 1.0, "openai": 1.0, "gemini": 1.0})
            Accounts.set_setting(self.conn, uid, "ai_weights", {"openai": 2.5})
            self.assertEqual(AIWeights.for_user(self.conn, uid)["openai"], 2.5)
            self.assertEqual(AIWeights.for_user(self.conn, None)["openai"], 1.0)  # a guest: the defaults


class AccountsPageTestCase(unittest.TestCase):
    """A throwaway database and config folder, an admin "boss" and a user "pat", and a client signed in as nobody."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.db_path = str(self.dir / "predictions.db")

        def override_get_db():
            conn = Tracker.connect(self.db_path)
            try:
                yield conn
            finally:
                conn.close()

        app.dependency_overrides[get_db] = override_get_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        config = self.dir / "leagues.json"
        shutil.copy(Leagues.CONFIG_PATH, config)
        for patcher in (mock.patch.dict(os.environ, {"DATA_DIR": str(self.dir)}),
                        mock.patch.object(Tracker, "DEFAULT_DB", self.db_path),
                        mock.patch.object(Leagues, "CONFIG_PATH", config),
                        mock.patch.object(AIWeights, "get_weight", return_value=1.0)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("SESSION_SECRET", "USERNAME", "PASSWORD"):
            os.environ.pop(name, None)
        auth._failures.clear()
        with self.db() as conn:
            self.admin = Accounts.create_user(conn, "boss", "boss-password", is_admin=True)
            self.user = Accounts.create_user(conn, "pat", "pat-password")
        self.client = TestClient(app)

    def db(self):
        class _Conn:
            def __enter__(inner):
                inner.conn = Tracker.connect(self.db_path)
                return inner.conn

            def __exit__(inner, *exc):
                inner.conn.close()
        return _Conn()

    def sign_in(self, username, password):
        return self.client.post("/login", data={"username": username, "password": password, "next": "/"},
                                follow_redirects=False)


class RoleTests(AccountsPageTestCase):
    def test_a_user_gets_betting_and_settings_but_not_admin(self):
        self.sign_in("pat", "pat-password")
        home = self.client.get("/").text
        self.assertIn('href="/betting"', home)
        self.assertIn('href="/settings"', home)
        self.assertNotIn('href="/admin/settings"', home)
        self.assertIn("Signed in as <strong", home)
        for path in ("/admin/settings", "/admin/users", "/admin/data"):
            self.assertEqual(self.client.get(path).status_code, 403, path)
        self.assertEqual(self.client.post("/admin/users", data={"username": "sneaky"}).status_code, 403)
        self.assertEqual(self.client.post("/league/NBA/tracking", data={"enabled": "0"}).status_code, 403)

    def test_an_admin_gets_the_admin_pages(self):
        self.sign_in("boss", "boss-password")
        self.assertIn('href="/admin/settings"', self.client.get("/").text)
        for path in ("/admin/settings", "/admin/users", "/admin/data"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn('class="mode-switch admin-nav"', r.text)

    def test_guests_get_neither(self):
        self.client.post("/guest")
        self.assertEqual(self.client.get("/settings").status_code, 403)
        self.assertEqual(self.client.get("/betting").status_code, 403)

    def test_repeated_wrong_passwords_are_throttled(self):
        for _ in range(auth.MAX_FAILURES):
            self.assertEqual(self.sign_in("pat", "nope").status_code, 401)
        self.assertEqual(self.sign_in("pat", "pat-password").status_code, 429)  # even the right one, for now


class UserAdminTests(AccountsPageTestCase):
    def test_adding_a_user_shows_a_temporary_password_they_must_replace(self):
        self.sign_in("boss", "boss-password")
        r = self.client.post("/admin/users", data={"username": "newbie"})
        self.assertEqual(r.status_code, 200)
        temporary = r.text.split("Their temporary password is <code>")[1].split("</code>")[0]
        self.client.post("/logout")

        self.sign_in("newbie", temporary)
        self.assertEqual(self.client.get("/", follow_redirects=False).headers["location"], "/settings#account")
        self.assertIn("temporary password", self.client.get("/settings").text)
        r = self.client.post("/settings/password", data={"current_password": temporary,
                                                          "new_password": "my-own-password",
                                                          "confirm_password": "my-own-password"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 200)  # still signed in, free to go

    def test_reset_promote_and_remove(self):
        self.sign_in("boss", "boss-password")
        uid = self.user["id"]
        r = self.client.post(f"/admin/users/{uid}/password")
        self.assertIn("New temporary password for <strong>pat</strong>", r.text)
        with self.db() as conn:
            self.assertIsNone(Accounts.authenticate(conn, "pat", "pat-password"))
            self.assertTrue(Accounts.get_user(conn, uid)["must_change_password"])
        self.client.post(f"/admin/users/{uid}/admin", data={"is_admin": "yes"})
        with self.db() as conn:
            self.assertTrue(Accounts.get_user(conn, uid)["is_admin"])
        self.client.post(f"/admin/users/{uid}/delete")
        with self.db() as conn:
            self.assertIsNone(Accounts.get_user(conn, uid))

    def test_an_admin_cant_remove_themselves(self):
        self.sign_in("boss", "boss-password")
        r = self.client.post(f"/admin/users/{self.admin['id']}/delete")
        self.assertEqual(r.status_code, 400)
        self.assertIn("can&#39;t remove your own account", r.text)

    def test_a_taken_username_is_refused(self):
        self.sign_in("boss", "boss-password")
        r = self.client.post("/admin/users", data={"username": "PAT"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("already", r.text)


class OwnSettingsTests(AccountsPageTestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = _rsa_key()

    def record_game(self):
        conn = Tracker.connect(self.db_path)
        try:
            Tracker.record_predictions(conn, market("KXNBAGAME-26OCT20OKCSAS-SAS", "San Antonio"), 0.5,
                                       [LLMPrediction("OpenAI", 0.80, 1.0), LLMPrediction("Claude", 0.40, 1.0)])
        finally:
            conn.close()

    def test_own_ai_weights_change_only_the_odds_that_user_sees(self):
        self.record_game()
        self.sign_in("pat", "pat-password")
        self.assertIn(">60%<", self.client.get("/").text.replace(" ", "").replace("\n", ""))  # equal weights
        self.client.post("/betting/rules", data={
            "mode": "off", "min_edge": "5", "stake_usd": "5", "max_daily_usd": "50", "min_price": "10",
            "max_price": "90", "min_ais": "2", "leagues": ["NBA"], "segments": ["regular"],
            "weight::openai": "1", "weight::claude": "0", "weight::gemini": "1"})
        self.assertIn(">80%<", self.client.get("/").text.replace(" ", "").replace("\n", ""))  # OpenAI only
        self.client.post("/logout")
        self.client.post("/guest")
        self.assertIn(">60%<", self.client.get("/").text.replace(" ", "").replace("\n", ""))  # guests: the defaults
        self.sign_in("pat", "pat-password")
        self.client.post("/betting/weights/reset")
        with self.db() as conn:
            self.assertIsNone(Accounts.get_setting(conn, self.user["id"], "ai_weights"))

    def test_ai_confidence_shows_beside_the_ai_odds_with_the_users_weights(self):
        # OpenAI says 80% at confidence 1.0, Claude 40% at 1.0: confidence 100% either way --
        # so record a game where they differ.
        conn = Tracker.connect(self.db_path)
        try:
            Tracker.record_predictions(conn, market("KXNBAGAME-26OCT21LALBOS-LAL", "Los Angeles L"), 0.5,
                                       [LLMPrediction("OpenAI", 0.70, 0.9), LLMPrediction("Claude", 0.60, 0.3)])
        finally:
            conn.close()
        self.sign_in("pat", "pat-password")
        flat = lambda html: html.replace(" ", "").replace("\n", "")
        self.assertIn("AIconfidence", flat(self.client.get("/").text))
        self.assertIn('class="confidence">60%<', flat(self.client.get("/").text))  # (0.9 + 0.3) / 2
        table = self.client.get("/").text.split('<table class="games">')[1].split("</table>")[0]
        header = table.split("</thead>")[0]
        header_cells = header.count("<th>") + header.count("<th ")  # (not <thead>)
        for row in table.split("<tbody>")[1].split("</tr>")[:-1]:
            self.assertEqual(row.count("<td"), header_cells)  # every row lines up with the headers
        page = self.client.get("/markets/KXNBAGAME-26OCT21LALBOS-LAL").text
        self.assertIn("AI confidence", page)
        self.assertIn(">60%<", flat(page))
        with self.db() as conn:
            Accounts.set_setting(conn, self.user["id"], "ai_weights", {"openai": 1, "claude": 0, "gemini": 1})
        self.assertIn('class="confidence">90%<', flat(self.client.get("/").text))  # Claude weighted out
        self.assertIn(">90%<", flat(self.client.get("/markets/KXNBAGAME-26OCT21LALBOS-LAL").text))

    def test_saving_checking_and_removing_a_kalshi_key(self):
        self.sign_in("pat", "pat-password")
        r = self.client.post("/settings/kalshi/demo", data={"key_id": "demo-key", "private_key": _pem(self.key)},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        page = self.client.get("/settings").text
        self.assertIn("demo-key", page)
        self.assertNotIn("PRIVATE KEY-----\n", page)  # never shown back
        with mock.patch("Kalshi.get_balance", return_value=123.45) as balance:
            self.assertIn("$123.45 available", self.client.post("/settings/kalshi/demo/check").text)
        self.assertEqual(balance.call_args.args[0].key_id, "demo-key")
        self.client.post("/settings/kalshi/demo/delete")
        with self.db() as conn:
            self.assertIsNone(Accounts.kalshi_key_info(conn, self.user["id"], "demo"))

    def test_a_key_file_upload_works_too(self):
        self.sign_in("pat", "pat-password")
        self.client.post("/settings/kalshi/live", data={"key_id": "live-key"},
                         files={"key_file": ("kalshi.key", _pem(self.key).encode())})
        with self.db() as conn:
            self.assertEqual(Accounts.kalshi_key_info(conn, self.user["id"], "live")["key_id"], "live-key")

    def test_a_bad_key_says_why(self):
        self.sign_in("pat", "pat-password")
        r = self.client.post("/settings/kalshi/demo", data={"key_id": "k", "private_key": "nonsense"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("couldn&#39;t be read", r.text)

    def test_one_users_keys_are_not_anothers(self):
        self.sign_in("pat", "pat-password")
        self.client.post("/settings/kalshi/demo", data={"key_id": "pats-key", "private_key": _pem(self.key)})
        self.client.post("/logout")
        self.sign_in("boss", "boss-password")
        self.assertNotIn("pats-key", self.client.get("/settings").text)

    def test_a_wrong_current_password_changes_nothing(self):
        self.sign_in("pat", "pat-password")
        r = self.client.post("/settings/password", data={"current_password": "nope", "new_password": "another-one",
                                                          "confirm_password": "another-one"})
        self.assertEqual(r.status_code, 400)
        with self.db() as conn:
            self.assertIsNotNone(Accounts.authenticate(conn, "pat", "pat-password"))


class DataFileTests(AccountsPageTestCase):
    def test_the_database_download_leaves_out_accounts(self):
        with self.db() as conn:
            Accounts.set_setting(conn, self.user["id"], "betting", {"mode": "paper"})
        self.sign_in("boss", "boss-password")
        r = self.client.get("/admin/data/download/predictions.db")
        self.assertEqual(r.status_code, 200)
        path = self.dir / "downloaded.db"
        path.write_bytes(r.content)
        conn = sqlite3.connect(path)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        finally:
            conn.close()
        self.assertIn("predictions", tables)
        self.assertFalse(tables & set(Accounts.ACCOUNT_TABLES))

    def test_replacing_the_database_keeps_this_servers_accounts(self):
        upload = self.dir / "from-elsewhere.db"
        conn = Tracker.connect(str(upload))
        try:
            Tracker.record_predictions(conn, market("KXNBAGAME-26OCT20OKCSAS-SAS", "San Antonio"), 0.5,
                                       [LLMPrediction("OpenAI", 0.8, 1.0)])
            Accounts.create_user(conn, "intruder", "intruder-pass", is_admin=True)
            conn.execute("INSERT INTO bets (created_at, user_id, mode, ticker, side, price, count, cost, fee, "
                         "ai_prob, edge) VALUES ('t', 99, 'paper', 'T', 'yes', 0.5, 1, 0.5, 0.02, 0.6, 0.1)")
            conn.commit()
        finally:
            conn.close()
        self.sign_in("boss", "boss-password")
        r = self.client.post("/admin/data/predictions-db", files={"file": ("p.db", upload.read_bytes())})
        self.assertIn("Database replaced", r.text)
        with self.db() as conn:
            self.assertEqual({u["username"] for u in Accounts.list_users(conn)}, {"boss", "pat"})
            self.assertEqual(conn.execute("SELECT user_id FROM bets").fetchone()[0], self.admin["id"])
        self.assertEqual(self.client.get("/admin/data").status_code, 200)  # still signed in


if __name__ == "__main__":
    unittest.main()
