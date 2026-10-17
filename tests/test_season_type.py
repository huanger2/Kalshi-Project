import datetime
import unittest
from unittest import mock

import Leagues
from SeasonType import segment, segment_iso, season_year, season_year_iso


class SeasonYearTests(unittest.TestCase):
    def test_october_belongs_to_the_season_starting_that_year(self):
        self.assertEqual(season_year(datetime.date(2026, 10, 5)), "2026-27")

    def test_april_belongs_to_the_season_that_started_the_previous_october(self):
        self.assertEqual(season_year(datetime.date(2027, 4, 20)), "2026-27")

    def test_june_still_belongs_to_the_season_that_started_the_previous_october(self):
        self.assertEqual(season_year(datetime.date(2027, 6, 20)), "2026-27")

    def test_july_belongs_to_the_next_seasons_key(self):
        # No games exist in July in practice, but the boundary itself must be consistent.
        self.assertEqual(season_year(datetime.date(2027, 7, 1)), "2027-28")


class SegmentDefaultHeuristicTests(unittest.TestCase):
    """No leagues.json entry for this season -- uses the generic calendar fallback."""

    def setUp(self):
        patcher = mock.patch.object(Leagues, "load_config", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_early_october_is_preseason(self):
        self.assertEqual(segment(datetime.date(2026, 10, 5)), "preseason")

    def test_the_day_before_the_default_regular_season_start_is_still_preseason(self):
        self.assertEqual(segment(datetime.date(2026, 10, 20)), "preseason")

    def test_the_default_regular_season_start_date_itself_is_regular(self):
        self.assertEqual(segment(datetime.date(2026, 10, 21)), "regular")

    def test_midseason_is_regular(self):
        self.assertEqual(segment(datetime.date(2027, 1, 15)), "regular")

    def test_the_day_before_the_default_postseason_start_is_still_regular(self):
        self.assertEqual(segment(datetime.date(2027, 4, 13)), "regular")

    def test_the_default_postseason_start_date_itself_is_postseason(self):
        self.assertEqual(segment(datetime.date(2027, 4, 14)), "postseason")

    def test_june_is_postseason(self):
        self.assertEqual(segment(datetime.date(2027, 6, 20)), "postseason")


class SegmentWithOverridesTests(unittest.TestCase):
    def test_an_explicit_regular_season_start_overrides_the_default(self):
        config = {"seasons": {"NBA": {"2026-27": {"regular_season_start": "2026-10-24", "postseason_start": None}}}}
        with mock.patch.object(Leagues, "load_config", return_value=config):
            self.assertEqual(segment(datetime.date(2026, 10, 22)), "preseason")  # would be "regular" by default
            self.assertEqual(segment(datetime.date(2026, 10, 24)), "regular")

    def test_an_explicit_postseason_start_overrides_the_default(self):
        config = {"seasons": {"NBA": {"2026-27": {"regular_season_start": None, "postseason_start": "2027-04-18"}}}}
        with mock.patch.object(Leagues, "load_config", return_value=config):
            self.assertEqual(segment(datetime.date(2027, 4, 15)), "regular")  # would be "postseason" by default
            self.assertEqual(segment(datetime.date(2027, 4, 18)), "postseason")

    def test_a_season_not_listed_in_the_config_uses_the_default(self):
        config = {"seasons": {"NBA": {"2030-31": {"regular_season_start": "2030-11-01", "postseason_start": None}}}}
        with mock.patch.object(Leagues, "load_config", return_value=config):
            self.assertEqual(segment(datetime.date(2026, 10, 5)), "preseason")  # unaffected by 2030-31's override

    def test_null_overrides_fall_back_to_the_default_cleanly(self):
        config = {"seasons": {"NBA": {"2026-27": {"regular_season_start": None, "postseason_start": None}}}}
        with mock.patch.object(Leagues, "load_config", return_value=config):
            self.assertEqual(segment(datetime.date(2026, 10, 5)), "preseason")
            self.assertEqual(segment(datetime.date(2027, 1, 15)), "regular")


class SegmentIsoTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(Leagues, "load_config", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parses_an_iso_date_string(self):
        self.assertEqual(segment_iso("2026-10-05"), "preseason")

    def test_none_is_none(self):
        self.assertIsNone(segment_iso(None))

    def test_blank_string_is_none(self):
        self.assertIsNone(segment_iso(""))


class SeasonYearIsoTests(unittest.TestCase):
    def test_parses_an_iso_date_string(self):
        self.assertEqual(season_year_iso("2026-10-05"), "2026-27")

    def test_an_april_date_still_resolves_to_the_season_it_started_in(self):
        self.assertEqual(season_year_iso("2027-04-20"), "2026-27")

    def test_none_is_none(self):
        self.assertIsNone(season_year_iso(None))

    def test_blank_string_is_none(self):
        self.assertIsNone(season_year_iso(""))


class OtherLeagueSeasonYearTests(unittest.TestCase):
    def test_nhl_spans_two_years_like_the_nba(self):
        self.assertEqual(season_year(datetime.date(2027, 4, 20), "NHL"), "2026-27")

    def test_nfl_is_labeled_by_its_start_year(self):
        self.assertEqual(season_year(datetime.date(2026, 10, 25), "NFL"), "2026")

    def test_a_january_nfl_playoff_game_belongs_to_the_previous_years_season(self):
        self.assertEqual(season_year(datetime.date(2027, 2, 7), "NFL"), "2026")

    def test_mlb_is_one_calendar_year(self):
        self.assertEqual(season_year(datetime.date(2026, 3, 1), "MLB"), "2026")
        self.assertEqual(season_year(datetime.date(2026, 11, 1), "MLB"), "2026")

    def test_league_code_is_case_insensitive(self):
        self.assertEqual(season_year(datetime.date(2026, 10, 25), "nfl"), "2026")


class OtherLeagueSegmentTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(Leagues, "load_config", return_value={})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_nfl_boundaries_follow_labor_day(self):
        # 2025: Labor Day Sep 1 -> kickoff Thu Sep 4; Wild Card weekend Sat Jan 10, 2026.
        self.assertEqual(segment(datetime.date(2025, 8, 28), "NFL"), "preseason")
        self.assertEqual(segment(datetime.date(2025, 9, 4), "NFL"), "regular")
        self.assertEqual(segment(datetime.date(2026, 1, 4), "NFL"), "regular")
        self.assertEqual(segment(datetime.date(2026, 1, 10), "NFL"), "postseason")
        self.assertEqual(segment(datetime.date(2026, 2, 8), "NFL"), "postseason")

    def test_nhl_regular_season_starts_earlier_than_the_nba(self):
        self.assertEqual(segment(datetime.date(2026, 10, 10), "NHL"), "regular")
        self.assertEqual(segment(datetime.date(2026, 10, 10), "NBA"), "preseason")

    def test_mlb_spring_training_regular_season_and_october(self):
        self.assertEqual(segment(datetime.date(2026, 3, 10), "MLB"), "preseason")
        self.assertEqual(segment(datetime.date(2026, 7, 4), "MLB"), "regular")
        self.assertEqual(segment(datetime.date(2026, 10, 17), "MLB"), "postseason")

    def test_overrides_are_per_league(self):
        config = {"seasons": {"MLB": {"2026": {"regular_season_start": None, "postseason_start": "2026-10-20"}}}}
        with mock.patch.object(Leagues, "load_config", return_value=config):
            self.assertEqual(segment(datetime.date(2026, 10, 17), "MLB"), "regular")
            # The NHL's identically-dated game is unaffected by MLB's override.
            self.assertEqual(segment(datetime.date(2026, 10, 17), "NHL"), "regular")
            self.assertEqual(segment(datetime.date(2026, 4, 20), "NHL"), "postseason")

    def test_no_league_means_nba(self):
        self.assertEqual(segment(datetime.date(2026, 10, 10)), segment(datetime.date(2026, 10, 10), "NBA"))


if __name__ == "__main__":
    unittest.main()
