import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import SeasonType
from SeasonType import classify, classify_iso, season_key


class SeasonKeyTests(unittest.TestCase):
    def test_october_belongs_to_the_season_starting_that_year(self):
        self.assertEqual(season_key(datetime.date(2026, 10, 5)), "2026-27")

    def test_april_belongs_to_the_season_that_started_the_previous_october(self):
        self.assertEqual(season_key(datetime.date(2027, 4, 20)), "2026-27")

    def test_june_still_belongs_to_the_season_that_started_the_previous_october(self):
        self.assertEqual(season_key(datetime.date(2027, 6, 20)), "2026-27")

    def test_july_belongs_to_the_next_seasons_key(self):
        # No games exist in July in practice, but the boundary itself must be consistent.
        self.assertEqual(season_key(datetime.date(2027, 7, 1)), "2027-28")


class ClassifyDefaultHeuristicTests(unittest.TestCase):
    """No nba_season.json entry for this season -- uses the generic calendar fallback."""

    def setUp(self):
        patcher = mock.patch.object(SeasonType, "_load_config", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_early_october_is_preseason(self):
        self.assertEqual(classify(datetime.date(2026, 10, 5)), "preseason")

    def test_the_day_before_the_default_regular_season_start_is_still_preseason(self):
        self.assertEqual(classify(datetime.date(2026, 10, 20)), "preseason")

    def test_the_default_regular_season_start_date_itself_is_regular(self):
        self.assertEqual(classify(datetime.date(2026, 10, 21)), "regular")

    def test_midseason_is_regular(self):
        self.assertEqual(classify(datetime.date(2027, 1, 15)), "regular")

    def test_the_day_before_the_default_postseason_start_is_still_regular(self):
        self.assertEqual(classify(datetime.date(2027, 4, 13)), "regular")

    def test_the_default_postseason_start_date_itself_is_postseason(self):
        self.assertEqual(classify(datetime.date(2027, 4, 14)), "postseason")

    def test_june_is_postseason(self):
        self.assertEqual(classify(datetime.date(2027, 6, 20)), "postseason")


class ClassifyWithOverridesTests(unittest.TestCase):
    def test_an_explicit_regular_season_start_overrides_the_default(self):
        config = {"2026-27": {"regular_season_start": "2026-10-24", "postseason_start": None}}
        with mock.patch.object(SeasonType, "_load_config", return_value=config):
            self.assertEqual(classify(datetime.date(2026, 10, 22)), "preseason")  # would be "regular" by default
            self.assertEqual(classify(datetime.date(2026, 10, 24)), "regular")

    def test_an_explicit_postseason_start_overrides_the_default(self):
        config = {"2026-27": {"regular_season_start": None, "postseason_start": "2027-04-18"}}
        with mock.patch.object(SeasonType, "_load_config", return_value=config):
            self.assertEqual(classify(datetime.date(2027, 4, 15)), "regular")  # would be "postseason" by default
            self.assertEqual(classify(datetime.date(2027, 4, 18)), "postseason")

    def test_a_season_not_listed_in_the_config_uses_the_default(self):
        config = {"2030-31": {"regular_season_start": "2030-11-01", "postseason_start": None}}
        with mock.patch.object(SeasonType, "_load_config", return_value=config):
            self.assertEqual(classify(datetime.date(2026, 10, 5)), "preseason")  # unaffected by 2030-31's override

    def test_null_overrides_fall_back_to_the_default_cleanly(self):
        config = {"2026-27": {"regular_season_start": None, "postseason_start": None}}
        with mock.patch.object(SeasonType, "_load_config", return_value=config):
            self.assertEqual(classify(datetime.date(2026, 10, 5)), "preseason")
            self.assertEqual(classify(datetime.date(2027, 1, 15)), "regular")


class ConfigFileTests(unittest.TestCase):
    def test_missing_file_is_treated_as_no_overrides(self):
        with mock.patch.object(SeasonType, "CONFIG_PATH", Path(tempfile.mktemp())):
            self.assertEqual(SeasonType._load_config(), {})

    def test_invalid_json_raises_instead_of_silently_using_defaults(self):
        bad = Path(tempfile.mktemp(suffix=".json"))
        bad.write_text("{not valid json", encoding="utf-8")
        try:
            with mock.patch.object(SeasonType, "CONFIG_PATH", bad):
                with self.assertRaises(ValueError):
                    SeasonType._load_config()
        finally:
            bad.unlink()

    def test_the_shipped_config_file_is_valid_json(self):
        config = json.loads(SeasonType.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertIn("2026-27", config)


class ClassifyIsoTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(SeasonType, "_load_config", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parses_an_iso_date_string(self):
        self.assertEqual(classify_iso("2026-10-05"), "preseason")

    def test_none_is_none(self):
        self.assertIsNone(classify_iso(None))

    def test_blank_string_is_none(self):
        self.assertIsNone(classify_iso(""))


if __name__ == "__main__":
    unittest.main()
