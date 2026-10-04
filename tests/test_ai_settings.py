import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import AISettings
from AISettings import DEFAULT_MODELS, get_model


class GetModelTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "ai_settings.json"
        patcher = mock.patch.object(AISettings, "SETTINGS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, settings):
        self.path.write_text(json.dumps(settings), encoding="utf-8")

    def test_reads_each_provider_from_the_file(self):
        self.write({"models": {"claude": "c-model", "openai": "o-model", "gemini": "g-model"}})
        self.assertEqual(get_model("claude"), "c-model")
        self.assertEqual(get_model("openai"), "o-model")
        self.assertEqual(get_model("gemini"), "g-model")

    def test_edits_take_effect_without_reloading(self):
        self.write({"models": {"claude": "first"}})
        self.assertEqual(get_model("claude"), "first")
        self.write({"models": {"claude": "second"}})
        self.assertEqual(get_model("claude"), "second")

    def test_missing_file_uses_defaults(self):
        self.assertEqual(get_model("openai"), DEFAULT_MODELS["openai"])

    def test_missing_or_blank_provider_uses_default(self):
        self.write({"models": {"claude": "c-model", "gemini": ""}})
        self.assertEqual(get_model("openai"), DEFAULT_MODELS["openai"])
        self.assertEqual(get_model("gemini"), DEFAULT_MODELS["gemini"])

    def test_invalid_json_raises_instead_of_silently_using_defaults(self):
        self.path.write_text('{"models": {"claude": "x",}}', encoding="utf-8")
        with self.assertRaises(ValueError):
            get_model("claude")

    def test_unknown_provider_raises(self):
        with self.assertRaises(KeyError):
            get_model("mistral")


class ShippedSettingsFileTests(unittest.TestCase):
    def test_repo_settings_file_is_valid_and_names_every_provider(self):
        settings = json.loads(AISettings.SETTINGS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(settings["models"]), set(DEFAULT_MODELS))


if __name__ == "__main__":
    unittest.main()
