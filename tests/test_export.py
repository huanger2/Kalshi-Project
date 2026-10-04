import os
import tempfile
import unittest

import openpyxl
import pandas as pd

import Export
import Tracker
from EVCalculator import LLMPrediction


def market(ticker):
    return {"ticker": ticker, "event_ticker": ticker + "-EVT", "title": ticker}


class ExportFrameTests(unittest.TestCase):
    def setUp(self):
        self.conn = Tracker.connect(":memory:")
        Tracker.record_predictions(
            self.conn, market("G1"), 0.60,
            [LLMPrediction("Sharp", 0.90, 0.9), LLMPrediction("Dull", 0.50, 0.5)],
        )
        Tracker.record_predictions(
            self.conn, market("G2"), 0.40, [LLMPrediction("Sharp", 0.30, 0.7)],
        )
        Tracker.settle(self.conn, "G1", yes_won=True)
        # G2 left unsettled on purpose.

    def test_settled_rows_get_a_label_and_brier_scores(self):
        df = Export._predictions_frame(self.conn)
        row = df[(df.ticker == "G1") & (df.llm == "Sharp")].iloc[0]
        self.assertEqual(row["outcome_label"], "YES")
        self.assertAlmostEqual(row["brier"], 0.01)          # (0.9 - 1)^2
        self.assertAlmostEqual(row["market_brier"], 0.16)   # (0.6 - 1)^2

    def test_unsettled_rows_are_pending_with_no_brier(self):
        df = Export._predictions_frame(self.conn)
        row = df[df.ticker == "G2"].iloc[0]
        self.assertEqual(row["outcome_label"], "PENDING")
        self.assertTrue(pd.isna(row["brier"]))

    def test_every_prediction_is_one_row(self):
        df = Export._predictions_frame(self.conn)
        self.assertEqual(len(df), 3)
        self.assertEqual(set(df.columns), set(Export.PREDICTIONS_COLUMNS))

    def test_summary_only_covers_settled_llms(self):
        summary = Export._summary_frame(self.conn)
        # Sharp has one settled prediction (G1); Dull's is settled too; neither's
        # unsettled G2 row should appear or affect the count.
        self.assertEqual(set(summary["llm"]), {"Sharp", "Dull"})
        sharp = summary[summary.llm == "Sharp"].iloc[0]
        self.assertEqual(sharp["settled_n"], 1)
        self.assertAlmostEqual(sharp["weight"], Tracker.brier_to_weight(0.01))


class ExportToExcelTests(unittest.TestCase):
    def test_writes_both_sheets_to_a_real_workbook(self):
        conn = Tracker.connect(":memory:")
        Tracker.record_predictions(
            conn, market("G1"), 0.55, [LLMPrediction("A", 0.7, 0.8)]
        )
        Tracker.settle(conn, "G1", yes_won=True)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "test.db")
            # export_to_excel takes a path and opens its own connection, so persist
            # the in-memory data to a real file first.
            import sqlite3
            file_conn = sqlite3.connect(db_path)
            conn.backup(file_conn)
            file_conn.close()

            out_path = os.path.join(tmp, "out.xlsx")
            result = Export.export_to_excel(db_path=db_path, out_path=out_path)

            self.assertEqual(result, out_path)
            self.assertTrue(os.path.exists(out_path))

            wb = openpyxl.load_workbook(out_path)
            self.assertEqual(wb.sheetnames, ["predictions", "llm_summary"])
            pred_sheet = wb["predictions"]
            self.assertEqual(pred_sheet.cell(row=1, column=1).value, "created_at")
            self.assertEqual(pred_sheet.max_row, 2)  # header + 1 prediction


if __name__ == "__main__":
    unittest.main()
