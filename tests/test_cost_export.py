import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import openpyxl

import CostExport
import CostTracker


class ExportToExcelTests(unittest.TestCase):
    def test_writes_both_sheets_to_a_real_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            pricing = {"m": {"input_per_million": 2.0, "output_per_million": 10.0}}
            with mock.patch.object(CostTracker, "_load_pricing", return_value=pricing):
                CostTracker.record_usage("OpenAI", "m", 1000, 500, context="Lakers win", db_path=db_path)
                CostTracker.record_usage("Claude", "m", 2000, 1000, db_path=db_path)

            out_path = os.path.join(tmp, "out.xlsx")
            result = CostExport.export_to_excel(db_path=db_path, out_path=out_path)

            self.assertEqual(result, out_path)
            self.assertTrue(os.path.exists(out_path))

            wb = openpyxl.load_workbook(out_path)
            self.assertEqual(wb.sheetnames, ["usage_log", "provider_summary"])

            usage = wb["usage_log"]
            self.assertEqual(
                [c.value for c in usage[1]],
                ["created_at", "provider", "model", "input_tokens", "output_tokens", "cost_usd", "context"],
            )
            self.assertEqual(usage.max_row, 3)  # header + 2 calls

            summary = wb["provider_summary"]
            self.assertEqual(
                [c.value for c in summary[1]],
                ["provider", "calls", "input_tokens", "output_tokens", "cost_usd", "unpriced_calls"],
            )
            self.assertEqual(summary.max_row, 3)  # header + OpenAI + Claude

    def test_empty_database_still_produces_a_readable_workbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "empty.db")
            sqlite3.connect(db_path).close()  # a plain file with no api_usage table yet

            out_path = os.path.join(tmp, "out.xlsx")
            CostExport.export_to_excel(db_path=db_path, out_path=out_path)

            wb = openpyxl.load_workbook(out_path)
            self.assertEqual(wb["usage_log"].max_row, 1)  # header only
            self.assertEqual(wb["provider_summary"].max_row, 1)

    def test_connection_is_closed_so_the_db_file_can_be_reused(self):
        # Windows can't reopen/delete a sqlite file while a connection to it is
        # still open from an earlier call -- this is a regression guard for that.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            CostTracker.record_usage("OpenAI", "m", 10, 10, db_path=db_path)
            CostExport.export_to_excel(db_path=db_path, out_path=os.path.join(tmp, "out1.xlsx"))
            # If the first export left its connection open, this second write would fail on Windows.
            CostExport.export_to_excel(db_path=db_path, out_path=os.path.join(tmp, "out2.xlsx"))


if __name__ == "__main__":
    unittest.main()
