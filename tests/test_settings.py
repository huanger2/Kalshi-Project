"""The admin Settings tab and the config-file copies it saves to (ConfigFiles.py)."""

import datetime
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import AISettings
import AIWeights
import ConfigFiles
import CostTracker
import Leagues
import Tracker
from webapp import settings
from webapp.main import app, get_db

TODAY = datetime.date(2026, 10, 17)


class ConfigTestCase(unittest.TestCase):
    """Throwaway copies of all four JSON files; DATA_DIR unset (a local setup) unless a test sets it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.repo = self.dir / "repo"
        self.volume = self.dir / "volume"
        self.repo.mkdir()
        self.volume.mkdir()
        self.paths = {}
        for module, attr in ((AISettings, "SETTINGS_PATH"), (AIWeights, "WEIGHTS_PATH"),
                             (CostTracker, "PRICING_PATH"), (Leagues, "CONFIG_PATH")):
            original = getattr(module, attr)
            copy = self.repo / original.name
            shutil.copy(original, copy)
            self.paths[original.name] = copy
            patcher = mock.patch.object(module, attr, copy)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Same starting point whatever the real leagues.json currently enables.
        config = json.loads(self.paths["leagues.json"].read_text(encoding="utf-8"))
        config["enabled"] = list(Leagues.LEAGUES)
        self.paths["leagues.json"].write_text(json.dumps(config), encoding="utf-8")
        env = mock.patch.dict("os.environ")
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("DATA_DIR", None)

    def hosted(self):
        os.environ["DATA_DIR"] = str(self.volume)

    def read(self, name, where=None):
        return json.loads(((where or self.repo) / name).read_text(encoding="utf-8"))


class ConfigFilesTests(ConfigTestCase):
    def test_locally_a_save_edits_the_file_itself(self):
        path = self.paths["ai_weights.json"]
        self.assertEqual(ConfigFiles.save(path, {"weights": {"openai": 2.0}}), path)
        self.assertEqual(self.read("ai_weights.json"), {"weights": {"openai": 2.0}})
        self.assertFalse(ConfigFiles.has_override(path))

    def test_hosted_a_save_goes_to_the_volume_and_overrides(self):
        self.hosted()
        path = self.paths["ai_weights.json"]
        before = path.read_text(encoding="utf-8")
        ConfigFiles.save(path, {"weights": {"openai": 2.0}})
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertTrue(ConfigFiles.has_override(path))
        self.assertEqual(AIWeights.get_weight("openai"), 2.0)

    def test_reset_puts_the_deployed_file_back_in_effect(self):
        self.hosted()
        path = self.paths["ai_weights.json"]
        ConfigFiles.save(path, {"weights": {"openai": 2.0}})
        self.assertTrue(ConfigFiles.reset(path))
        self.assertEqual(AIWeights.get_weight("openai"), 1.0)
        self.assertFalse(ConfigFiles.reset(path))

    def test_layout_keeps_leaf_objects_on_one_line(self):
        text = ConfigFiles.dumps({"models": {"m": {"input_per_million": 1.0, "output_per_million": 2.0}}})
        self.assertEqual(text, '{\n    "models": {\n        "m": {"input_per_million": 1.0, "output_per_million": 2.0}\n    }\n}')


class SaveSectionTests(ConfigTestCase):
    def test_models(self):
        settings.save_models({"model::openai": "gpt-6", "model::claude": " claude-x ", "model::gemini": "gemini-y"})
        self.assertEqual(AISettings.get_model("openai"), "gpt-6")
        self.assertEqual(AISettings.get_model("claude"), "claude-x")

    def test_a_blank_or_spaced_model_name_saves_nothing(self):
        before = self.paths["ai_settings.json"].read_text(encoding="utf-8")
        with self.assertRaises(settings.SettingsError) as e:
            settings.save_models({"model::openai": "", "model::claude": "two words", "model::gemini": "ok"})
        self.assertEqual(len(e.exception.errors), 2)
        self.assertEqual(self.paths["ai_settings.json"].read_text(encoding="utf-8"), before)

    def test_weights(self):
        settings.save_weights({"weight::openai": "0.5", "weight::claude": "2", "weight::gemini": "0"})
        self.assertEqual(self.read("ai_weights.json")["weights"], {"openai": 0.5, "claude": 2.0, "gemini": 0.0})

    def test_bad_weights_are_rejected(self):
        for bad in ("-1", "11", "abc", "", "nan"):
            with self.assertRaises(settings.SettingsError, msg=bad):
                settings.save_weights({"weight::openai": bad, "weight::claude": "1", "weight::gemini": "1"})

    def test_pricing_edit_remove_and_add(self):
        settings.save_pricing({
            "input::gpt-5.1": "2", "output::gpt-5.1": "",          # blank = unknown, not $0
            "delete::gpt-4.1-mini": "on",
            "input::claude-sonnet-5-5": "3", "output::claude-sonnet-5-5": "15",
            "input::gemini-3.1-flash-lite": "0.25", "output::gemini-3.1-flash-lite": "1.5",
            "input::gemini-3.8-flash": "0.75", "output::gemini-3.8-flash": "3.75",
            "new_model": "gpt-6", "new_input": "5", "new_output": "20",
        })
        config = self.read("model_pricing.json")
        self.assertIn("_comment", config)  # untouched keys survive
        models = config["models"]
        self.assertNotIn("gpt-4.1-mini", models)
        self.assertEqual(models["gpt-5.1"], {"input_per_million": 2.0, "output_per_million": None})
        self.assertEqual(models["gpt-6"], {"input_per_million": 5.0, "output_per_million": 20.0})
        self.assertEqual(CostTracker.get_pricing("gpt-6"), (5.0, 20.0))

    def test_adding_a_duplicate_model_is_rejected(self):
        with self.assertRaises(settings.SettingsError):
            settings.save_pricing({"new_model": "gpt-5.1", "new_input": "1"})

    def test_league_dates_and_tracking(self):
        form = {"tracked::NBA": "on", "tracked::NFL": "on",
                "regular::NHL::2026-27": "2026-10-07", "postseason::NHL::2026-27": "2027-04-19"}
        settings.save_leagues(form, today=TODAY)
        self.assertEqual(Leagues.enabled_leagues(), ["NBA", "NFL"])
        seasons = self.read("leagues.json")["seasons"]
        self.assertEqual(seasons["NHL"]["2026-27"], {"regular_season_start": "2026-10-07", "postseason_start": "2027-04-19"})
        self.assertNotIn("2027-28", seasons["NHL"])  # the blank next-season row isn't written

    def test_postseason_before_regular_season_is_rejected(self):
        with self.assertRaises(settings.SettingsError):
            settings.save_leagues({"regular::NBA::2026-27": "2027-04-20", "postseason::NBA::2026-27": "2026-10-20"},
                                  today=TODAY)

    def test_hosted_league_saves_leave_the_deployed_file_alone(self):
        self.hosted()
        before = self.paths["leagues.json"].read_text(encoding="utf-8")
        settings.save_leagues({"tracked::NBA": "on", "regular::NBA::2026-27": "2026-10-20"}, today=TODAY)
        self.assertEqual(self.paths["leagues.json"].read_text(encoding="utf-8"), before)
        self.assertEqual(self.read("leagues.json", self.volume)["seasons"]["NBA"]["2026-27"]["regular_season_start"],
                         "2026-10-20")
        self.assertEqual(Leagues.enabled_leagues(), ["NBA"])

    def test_view_shows_current_and_next_season_with_defaults(self):
        nfl = next(lg for lg in settings.view(today=TODAY)["leagues"] if lg["code"] == "NFL")
        labels = [row["season"] for row in nfl["seasons"]]
        self.assertIn("2027", labels)
        self.assertIn("2026", labels)
        self.assertEqual(next(r for r in nfl["seasons"] if r["season"] == "2027")["regular_default"], "2027-09-09")


class SettingsPageTests(ConfigTestCase):
    def setUp(self):
        super().setUp()
        db = str(self.dir / "predictions.db")

        def override_get_db():
            conn = Tracker.connect(db)
            try:
                yield conn
            finally:
                conn.close()

        app.dependency_overrides[get_db] = override_get_db
        self.addCleanup(app.dependency_overrides.pop, get_db, None)
        os.environ.update({"USERNAME": "admin", "PASSWORD": "s3cret"})
        os.environ.pop("SESSION_SECRET", None)
        self.client = TestClient(app)

    def sign_in(self):
        self.client.post("/login", data={"username": "admin", "password": "s3cret", "next": "/"})

    def test_guests_are_kept_out(self):
        self.client.post("/guest")
        self.assertEqual(self.client.get("/settings").status_code, 403)
        before = self.paths["ai_weights.json"].read_text(encoding="utf-8")
        r = self.client.post("/settings/weights", data={"weight::openai": "9", "weight::claude": "1", "weight::gemini": "1"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.paths["ai_weights.json"].read_text(encoding="utf-8"), before)
        self.assertNotIn('href="/settings"', self.client.get("/").text)

    def test_admin_sees_every_section_and_the_tab(self):
        self.sign_in()
        r = self.client.get("/settings")
        self.assertEqual(r.status_code, 200)
        for section in ('id="models"', 'id="weights"', 'id="pricing"', 'id="leagues"', 'href="/settings"'):
            self.assertIn(section, r.text)
        self.assertIn('value="gpt-5.1"', r.text)

    def test_saving_redirects_back_with_a_confirmation(self):
        self.sign_in()
        r = self.client.post("/settings/weights", data={"weight::openai": "2", "weight::claude": "1", "weight::gemini": "1"},
                             follow_redirects=False)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/settings?saved=weights#weights"))
        self.assertIn("Saved.", self.client.get(r.headers["location"]).text)
        self.assertEqual(AIWeights.get_weight("openai"), 2.0)

    def test_an_invalid_save_shows_why_and_changes_nothing(self):
        self.sign_in()
        r = self.client.post("/settings/weights", data={"weight::openai": "-3", "weight::claude": "1", "weight::gemini": "1"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Nothing was saved", r.text)
        self.assertEqual(AIWeights.get_weight("openai"), 1.0)

    def test_unknown_section_is_a_404(self):
        self.sign_in()
        self.assertEqual(self.client.post("/settings/secrets", data={}).status_code, 404)

    def test_hosted_reset_button(self):
        self.hosted()
        self.sign_in()
        self.client.post("/settings/weights", data={"weight::openai": "2", "weight::claude": "1", "weight::gemini": "1"})
        self.assertIn("Reset to repo version", self.client.get("/settings").text)
        self.client.post("/settings/weights/reset")
        self.assertEqual(AIWeights.get_weight("openai"), 1.0)
        self.assertNotIn("Reset to repo version", self.client.get("/settings").text)


if __name__ == "__main__":
    unittest.main()
