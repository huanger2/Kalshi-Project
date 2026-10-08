import datetime
import os
import tempfile
import unittest

import DailyLog
import MigrateDailyLogs
import Tracker

LLM_NAMES = ["OpenAI", "Claude", "Gemini"]


def log_row(ticker, team, opponent, settled=False, **overrides):
    row = {
        "game_date": "2026-10-04",
        "event_ticker": ticker.rsplit("-", 1)[0],
        "ticker": ticker,
        "team": team,
        "opponent": opponent,
        "logged_at": "2026-10-04T09:00:00",
        "yes_bid": 0.58,
        "yes_ask": 0.60,
        "market_prob": 0.59,
        "openai_prob": 0.70, "openai_confidence": 0.80, "openai_model": "gpt-5.1",
        "claude_prob": 0.65, "claude_confidence": 0.60, "claude_model": "claude-sonnet-5-5",
        "gemini_prob": 0.55, "gemini_confidence": 0.50, "gemini_model": "gemini-3.8-flash",
        "winner": None, "team_won": None, "settled_at": None,
    }
    if settled:
        row["winner"] = team
        row["team_won"] = 1
        row["settled_at"] = "2026-10-05T09:00:00"
    row.update(overrides)
    return row


class MigrateDailyLogsTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = os.path.join(tmp.name, "daily_logs")
        self.db_path = os.path.join(tmp.name, "predictions.db")
        os.makedirs(self.folder)

    def write_day(self, day: str, rows: list[dict]):
        import pandas as pd
        path = DailyLog.path_for(datetime.date.fromisoformat(day), self.folder)
        DailyLog.save(pd.DataFrame(rows), path, LLM_NAMES)

    def query(self, sql):
        conn = Tracker.connect(self.db_path)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()


class MigrateFileTests(MigrateDailyLogsTestCase):
    def test_inserts_one_row_per_ai_per_game(self):
        self.write_day("2026-10-04", [log_row("KXNBAGAME-26OCT04UTADEN-DEN", "Denver", "Utah")])
        n = MigrateDailyLogs.migrate_all(self.folder, self.db_path)
        self.assertEqual(n, 3)

        rows = self.query(
            "SELECT llm, team, opponent, model, predicted_prob, confidence, market_prob, outcome "
            "FROM predictions ORDER BY llm"
        )
        self.assertEqual(rows, [
            ("Claude", "Denver", "Utah", "claude-sonnet-5-5", 0.65, 0.60, 0.59, None),
            ("Gemini", "Denver", "Utah", "gemini-3.8-flash", 0.55, 0.50, 0.59, None),
            ("OpenAI", "Denver", "Utah", "gpt-5.1", 0.70, 0.80, 0.59, None),
        ])

    def test_settled_game_carries_its_outcome(self):
        self.write_day("2026-10-04", [log_row("KXNBAGAME-26OCT04UTADEN-DEN", "Denver", "Utah", settled=True)])
        MigrateDailyLogs.migrate_all(self.folder, self.db_path)
        outcomes = {r[0] for r in self.query("SELECT outcome FROM predictions")}
        self.assertEqual(outcomes, {1})

    def test_an_ai_that_failed_on_a_game_is_not_migrated_as_a_fake_prediction(self):
        row = log_row("KXNBAGAME-26OCT04UTADEN-DEN", "Denver", "Utah",
                       openai_prob=None, openai_confidence=None, openai_model=None)
        self.write_day("2026-10-04", [row])
        MigrateDailyLogs.migrate_all(self.folder, self.db_path)
        llms = {r[0] for r in self.query("SELECT llm FROM predictions")}
        self.assertEqual(llms, {"Claude", "Gemini"})

    def test_preserves_the_models_actually_used_not_the_current_config(self):
        # The point of carrying `model` through unmodified instead of
        # re-resolving it: an old row logged under a since-upgraded model
        # must not silently become indistinguishable from a fresh one.
        row = log_row("KXNBAGAME-26OCT04UTADEN-DEN", "Denver", "Utah", openai_model="gpt-4.1-mini")
        self.write_day("2026-10-04", [row])
        MigrateDailyLogs.migrate_all(self.folder, self.db_path)
        model = self.query("SELECT model FROM predictions WHERE llm = 'OpenAI'")[0][0]
        self.assertEqual(model, "gpt-4.1-mini")

    def test_empty_folder_migrates_nothing(self):
        self.assertEqual(MigrateDailyLogs.migrate_all(self.folder, self.db_path), 0)


class MigrateAllTests(MigrateDailyLogsTestCase):
    def test_migrates_every_day_in_the_folder(self):
        self.write_day("2026-10-04", [log_row("KXNBAGAME-26OCT04UTADEN-DEN", "Denver", "Utah")])
        self.write_day("2026-10-05", [log_row("KXNBAGAME-26OCT05LALSAC-SAC", "Sacramento", "Los Angeles L")])
        n = MigrateDailyLogs.migrate_all(self.folder, self.db_path)
        self.assertEqual(n, 6)
        tickers = {r[0] for r in self.query("SELECT DISTINCT ticker FROM predictions")}
        self.assertEqual(tickers, {"KXNBAGAME-26OCT04UTADEN-DEN", "KXNBAGAME-26OCT05LALSAC-SAC"})

    def test_rerunning_does_not_duplicate_rows(self):
        self.write_day("2026-10-04", [log_row("KXNBAGAME-26OCT04UTADEN-DEN", "Denver", "Utah")])
        MigrateDailyLogs.migrate_all(self.folder, self.db_path)
        second_run = MigrateDailyLogs.migrate_all(self.folder, self.db_path)
        self.assertEqual(second_run, 0)
        self.assertEqual(self.query("SELECT COUNT(*) FROM predictions")[0][0], 3)

    def test_a_new_day_added_later_is_picked_up_on_rerun(self):
        self.write_day("2026-10-04", [log_row("KXNBAGAME-26OCT04UTADEN-DEN", "Denver", "Utah")])
        MigrateDailyLogs.migrate_all(self.folder, self.db_path)

        self.write_day("2026-10-05", [log_row("KXNBAGAME-26OCT05LALSAC-SAC", "Sacramento", "Los Angeles L")])
        second_run = MigrateDailyLogs.migrate_all(self.folder, self.db_path)

        self.assertEqual(second_run, 3)
        self.assertEqual(self.query("SELECT COUNT(*) FROM predictions")[0][0], 6)


if __name__ == "__main__":
    unittest.main()
