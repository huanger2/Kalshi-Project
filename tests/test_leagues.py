import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import Leagues


class LeagueForTickerTests(unittest.TestCase):
    def test_each_leagues_series_maps_back_to_it(self):
        self.assertEqual(Leagues.league_for_ticker("KXNBAGAME-26OCT23PORSAC-SAC"), "NBA")
        self.assertEqual(Leagues.league_for_ticker("KXNFLGAME-26OCT26DALPHI"), "NFL")
        self.assertEqual(Leagues.league_for_ticker("KXNHLGAME-26OCT23TBCOL-TB"), "NHL")
        self.assertEqual(Leagues.league_for_ticker("KXMLBGAME-26OCT172000TBCLE-TB"), "MLB")

    def test_the_championship_series_is_not_a_game_market(self):
        self.assertIsNone(Leagues.league_for_ticker("KXNBA-27-BOS"))

    def test_unknown_or_missing_is_none(self):
        self.assertIsNone(Leagues.league_for_ticker("G1"))
        self.assertIsNone(Leagues.league_for_ticker(None))


class GetTests(unittest.TestCase):
    def test_missing_code_is_the_default_league(self):
        self.assertEqual(Leagues.get(None).code, Leagues.DEFAULT_LEAGUE)

    def test_case_insensitive(self):
        self.assertEqual(Leagues.get("mlb").series, "KXMLBGAME")


class EnabledLeaguesTests(unittest.TestCase):
    def test_missing_key_enables_every_league(self):
        with mock.patch.object(Leagues, "load_config", return_value={}):
            self.assertEqual(Leagues.enabled_leagues(), ["NBA", "NFL", "NHL", "MLB"])

    def test_reads_the_configured_list(self):
        with mock.patch.object(Leagues, "load_config", return_value={"enabled": ["nhl", "MLB"]}):
            self.assertEqual(Leagues.enabled_leagues(), ["NHL", "MLB"])

    def test_an_unknown_league_raises_instead_of_being_skipped(self):
        with mock.patch.object(Leagues, "load_config", return_value={"enabled": ["NBA", "WNBA"]}):
            with self.assertRaises(ValueError):
                Leagues.enabled_leagues()


class ConfigFileTests(unittest.TestCase):
    def test_missing_file_is_treated_as_no_config(self):
        with mock.patch.object(Leagues, "CONFIG_PATH", Path(tempfile.mktemp())):
            self.assertEqual(Leagues.load_config(), {})

    def test_invalid_json_raises_instead_of_silently_using_defaults(self):
        bad = Path(tempfile.mktemp(suffix=".json"))
        bad.write_text("{not valid json", encoding="utf-8")
        try:
            with mock.patch.object(Leagues, "CONFIG_PATH", bad):
                with self.assertRaises(ValueError):
                    Leagues.load_config()
        finally:
            bad.unlink()

    def test_the_shipped_config_file_is_valid_and_covers_every_league(self):
        config = json.loads(Leagues.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(sorted(config["enabled"]), sorted(Leagues.LEAGUES))
        self.assertEqual(sorted(config["seasons"]), sorted(Leagues.LEAGUES))


if __name__ == "__main__":
    unittest.main()
