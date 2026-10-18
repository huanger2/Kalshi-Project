"""The Settings tab's "Ask Gemini for updates": parsing, proposed changes, and applying ticked ones."""

import datetime
import json
from unittest import mock

import AISettings
import CostTracker
import Leagues
from tests.test_settings import ConfigTestCase, SettingsPageTestCase, TODAY
from webapp import settings, suggestions

REPLY = {
    "models": {"openai": "gpt-6", "claude": "claude-sonnet-5-5", "gemini": "gemini-4-flash"},
    "pricing": {
        "gpt-6": {"input_per_million": 5, "output_per_million": 20},
        "gpt-5.1": {"input_per_million": 1.25, "output_per_million": 10.0},   # unchanged
        "claude-sonnet-5-5": {"input_per_million": 2.5, "output_per_million": 15},
        "gemini-4-flash": {"input_per_million": "cheap", "output_per_million": None},  # unusable
        "some-other-model": {"input_per_million": 1, "output_per_million": 1},  # not ours
    },
    "leagues": {
        "NBA": {"2027-28": {"regular_season_start": "2027-10-19", "postseason_start": None}},
        "MLB": {"2027": {"regular_season_start": "2027-03-25", "postseason_start": "not announced"}},
        "NHL": {"2019-20": {"regular_season_start": "2019-10-02", "postseason_start": None}},  # not asked
    },
}


class FakeGemini:
    """Stands in for both calls: research returns notes naming the part; format returns its JSON slice."""

    def __init__(self, reply=REPLY, sources=({"title": "OpenAI pricing", "uri": "https://openai.com/api/pricing"},),
                 queries=("latest models",), fail_part=None):
        self.reply, self.sources, self.queries, self.fail_part = reply, list(sources), list(queries), fail_part
        self.research_prompts, self.format_prompts = {}, {}

    @staticmethod
    def _part(prompt):
        return "leagues" if ("regular season" in prompt and "API models" not in prompt) else "models"

    def research(self, prompt):
        part = self._part(prompt)
        self.research_prompts[part] = prompt
        if part == self.fail_part:
            raise RuntimeError("quota exhausted")
        return f"notes for {part}", self.sources, self.queries

    def format(self, prompt):
        part = "leagues" if '"leagues"' in prompt else "models"
        self.format_prompts[part] = prompt
        keys = ("leagues",) if part == "leagues" else ("models", "pricing")
        return "```json\n" + json.dumps({k: self.reply[k] for k in keys if k in self.reply}) + "\n```"

    def fetch(self):
        return suggestions.fetch(research=self.research, format_=self.format, today=TODAY)


class ParseTests(ConfigTestCase):
    def test_fenced_or_bare_json(self):
        self.assertEqual(suggestions.parse_reply('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(suggestions.parse_reply('Sure! {"a": 1} Hope that helps.'), {"a": 1})

    def test_no_usable_json_is_a_clear_error(self):
        for text in ("I couldn't find that.", "{not json}", "[1, 2]"):
            with self.assertRaises(ValueError, msg=text):
                suggestions.parse_reply(text)

    def test_prompts_name_current_models_and_the_seasons_to_check(self):
        fake = FakeGemini()
        fake.fetch()
        self.assertIn("OpenAI gpt-5.1", fake.research_prompts["models"])
        self.assertIn("NFL 2026 and 2027", fake.research_prompts["leagues"])
        self.assertIn("2026-10-17", fake.research_prompts["leagues"])
        self.assertTrue(fake.research_prompts["models"].startswith("Search the web"))

    def test_the_format_step_is_given_the_research_notes(self):
        fake = FakeGemini()
        fake.fetch()
        self.assertIn("notes for leagues", fake.format_prompts["leagues"])
        self.assertIn("Use only facts stated in the notes", fake.format_prompts["models"])

    def test_one_part_failing_still_returns_the_other(self):
        result = FakeGemini(fail_part="leagues").fetch()
        self.assertEqual(result["problems"], ["league dates: quota exhausted"])
        self.assertTrue(any(c["id"].startswith("models|") for c in result["changes"]))
        self.assertFalse(any(c["id"].startswith("leagues|") for c in result["changes"]))

    def test_both_parts_failing_raises(self):
        def research(prompt):
            raise RuntimeError("no key")
        with self.assertRaises(ValueError):
            suggestions.fetch(research=research, format_=lambda p: "{}", today=TODAY)

    def test_an_answer_without_searching_is_flagged_unverified(self):
        result = FakeGemini(sources=(), queries=()).fetch()
        self.assertEqual(result["unsearched"], ["AI models and pricing", "league dates"])


class ChangesTests(ConfigTestCase):
    def setUp(self):
        super().setUp()
        self.result = FakeGemini().fetch()
        self.by_id = {c["id"]: c for c in self.result["changes"]}

    def test_only_real_changes_to_things_we_manage(self):
        self.assertEqual(set(self.by_id), {
            "models|openai", "models|gemini",
            "pricing|gpt-6", "pricing|claude-sonnet-5-5",
            "leagues|NBA|2027-28|regular", "leagues|MLB|2027|regular",
        })

    def test_change_details(self):
        self.assertEqual(self.by_id["models|openai"]["current"], "gpt-5.1")
        self.assertEqual(self.by_id["pricing|gpt-6"]["current"], "not listed")
        self.assertEqual(self.by_id["pricing|gpt-6"]["suggested"], "$5 in / $20 out")
        self.assertIn("default", self.by_id["leagues|NBA|2027-28|regular"]["current"])

    def test_sources_are_passed_through(self):
        self.assertEqual(self.result["sources"][0]["uri"], "https://openai.com/api/pricing")

    def test_nothing_is_saved_by_asking(self):
        self.assertEqual(AISettings.get_model("openai"), "gpt-5.1")


class ApplyTests(ConfigTestCase):
    def form(self, *ids):
        changes = {c["id"]: c for c in FakeGemini().fetch()["changes"]}
        out = {}
        for change_id in ids:
            out[f"apply::{change_id}"] = "on"
            out[f"value::{change_id}"] = changes[change_id]["value"]
        return out

    def test_only_ticked_changes_apply(self):
        saved, errors = suggestions.apply(self.form("models|openai", "pricing|gpt-6"), today=TODAY)
        self.assertEqual((saved, errors), (["AI models", "model pricing"], []))
        self.assertEqual(AISettings.get_model("openai"), "gpt-6")
        self.assertEqual(AISettings.get_model("gemini"), "gemini-3.8-flash")  # not ticked
        self.assertEqual(CostTracker.get_pricing("gpt-6"), (5.0, 20.0))
        self.assertEqual(CostTracker.get_pricing("claude-sonnet-5-5"), (3.0, 15.0))  # not ticked

    def test_league_dates_keep_tracking_and_other_dates(self):
        Leagues.set_enabled("MLB", False)
        saved, errors = suggestions.apply(self.form("leagues|NBA|2027-28|regular"), today=TODAY)
        self.assertEqual((saved, errors), (["league dates"], []))
        seasons = self.read("leagues.json")["seasons"]
        self.assertEqual(seasons["NBA"]["2027-28"]["regular_season_start"], "2027-10-19")
        self.assertEqual(seasons["NHL"]["2026-27"]["regular_season_start"], "2026-10-07")
        self.assertNotIn("MLB", Leagues.enabled_leagues())

    def test_a_tampered_value_is_still_validated(self):
        form = {"apply::models|openai": "on", "value::models|openai": json.dumps("bad name with spaces")}
        saved, errors = suggestions.apply(form, today=TODAY)
        self.assertEqual(saved, [])
        self.assertTrue(errors)
        self.assertEqual(AISettings.get_model("openai"), "gpt-5.1")

    def test_an_unreadable_value_is_reported(self):
        saved, errors = suggestions.apply({"apply::models|openai": "on", "value::models|openai": "{"}, today=TODAY)
        self.assertEqual(saved, [])
        self.assertIn("unreadable", errors[0])


class SuggestionPageTests(SettingsPageTestCase):
    def test_guests_cannot_ask_or_apply(self):
        self.client.post("/guest")
        with mock.patch.object(suggestions, "fetch") as fetch:
            self.assertEqual(self.client.post("/settings/suggestions").status_code, 403)
            fetch.assert_not_called()
        self.assertEqual(self.client.post("/settings/suggestions/apply", data={}).status_code, 403)

    def test_asking_shows_the_review_table_and_sources(self):
        self.sign_in()
        real = suggestions.fetch
        fake = FakeGemini()
        with mock.patch.object(suggestions, "fetch",
                               side_effect=lambda: real(research=fake.research, format_=fake.format, today=TODAY)):
            r = self.client.post("/settings/suggestions")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Apply selected", r.text)
        self.assertIn('name="apply::models|openai"', r.text)
        self.assertIn("https://openai.com/api/pricing", r.text)
        self.assertNotIn("from memory", r.text)
        review_table = r.text.split('class="settings-table suggestions"')[1].split("</table>")[0]
        self.assertNotIn("checked", review_table)  # nothing pre-ticked: every change is opt-in

    def test_a_gemini_failure_is_shown_not_a_crash(self):
        self.sign_in()
        with mock.patch.object(suggestions, "fetch", side_effect=ValueError("Gemini's reply had no JSON in it.")):
            r = self.client.post("/settings/suggestions")
        self.assertEqual(r.status_code, 502)
        self.assertIn("Couldn&#39;t get suggestions from Gemini", r.text)

    def test_applying_redirects_with_what_was_saved(self):
        self.sign_in()
        r = self.client.post("/settings/suggestions/apply",
                             data={"apply::models|openai": "on", "value::models|openai": json.dumps("gpt-6")},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertIn("Applied: AI models.", self.client.get(r.headers["location"]).text)
        self.assertEqual(AISettings.get_model("openai"), "gpt-6")
