import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import AIWeights
from AIWeights import DEFAULT_WEIGHTS, get_weight


class GetWeightTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "ai_weights.json"
        patcher = mock.patch.object(AIWeights, "WEIGHTS_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, settings):
        self.path.write_text(json.dumps(settings), encoding="utf-8")

    def test_reads_each_ais_weight_from_the_file(self):
        self.write({"weights": {"claude": 0.5, "openai": 1.5, "gemini": 0.0}})
        self.assertEqual(get_weight("claude"), 0.5)
        self.assertEqual(get_weight("openai"), 1.5)
        self.assertEqual(get_weight("gemini"), 0.0)

    def test_is_case_insensitive(self):
        self.write({"weights": {"openai": 2.0}})
        self.assertEqual(get_weight("OpenAI"), 2.0)
        self.assertEqual(get_weight("OPENAI"), 2.0)

    def test_edits_take_effect_without_reloading(self):
        self.write({"weights": {"claude": 1.0}})
        self.assertEqual(get_weight("claude"), 1.0)
        self.write({"weights": {"claude": 0.25}})
        self.assertEqual(get_weight("claude"), 0.25)

    def test_missing_file_defaults_every_known_ai_to_one(self):
        self.assertEqual(get_weight("openai"), 1.0)
        self.assertEqual(get_weight("claude"), 1.0)
        self.assertEqual(get_weight("gemini"), 1.0)

    def test_an_ai_missing_from_the_file_defaults_to_one(self):
        self.write({"weights": {"claude": 0.5}})
        self.assertEqual(get_weight("openai"), 1.0)

    def test_an_unknown_name_defaults_to_one_not_an_error(self):
        self.assertEqual(get_weight("SomeNewAI"), 1.0)

    def test_invalid_json_raises_instead_of_silently_using_defaults(self):
        self.path.write_text('{"weights": {"claude": 0.5,}}', encoding="utf-8")
        with self.assertRaises(ValueError):
            get_weight("claude")


class ShippedWeightsFileTests(unittest.TestCase):
    def test_repo_weights_file_is_valid_and_defaults_every_ai_to_one(self):
        settings = json.loads(AIWeights.WEIGHTS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(set(settings["weights"]), set(DEFAULT_WEIGHTS))
        self.assertTrue(all(w == 1.0 for w in settings["weights"].values()))


if __name__ == "__main__":
    unittest.main()
