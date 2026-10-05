"""
Export.py

Writes everything Tracker has recorded out to an Excel workbook for manual
review: one row per (market, LLM) prediction, plus a per-LLM accuracy
summary. Every prediction is logged at the default weight of 1.0 regardless
of any weight computed so far -- this is the raw data you train the real
weights from, so it must not already be weighted.

    py Export.py [db_path] [out_path]
"""

import sys

import pandas as pd

import Tracker
from ExcelUtil import write_workbook

DEFAULT_OUT = "predictions.xlsx"

PREDICTIONS_COLUMNS = [
    "created_at", "ticker", "event_ticker", "title", "llm",
    "predicted_prob", "confidence", "market_prob",
    "outcome_label", "brier", "market_brier", "settled_at",
]


def _predictions_frame(conn) -> pd.DataFrame:
    """One row per recorded (market, LLM) prediction, with derived accuracy columns."""
    df = pd.read_sql_query(
        "SELECT created_at, ticker, event_ticker, title, llm, "
        "       predicted_prob, confidence, market_prob, outcome, settled_at "
        "FROM predictions ORDER BY ticker, llm",
        conn,
    )

    df["outcome_label"] = df["outcome"].map({1: "YES", 0: "NO"}).fillna("PENDING")

    settled = df["outcome"].notna()
    df["brier"] = pd.NA
    df["market_brier"] = pd.NA
    df.loc[settled, "brier"] = (df.loc[settled, "predicted_prob"] - df.loc[settled, "outcome"]) ** 2
    df.loc[settled, "market_brier"] = (df.loc[settled, "market_prob"] - df.loc[settled, "outcome"]) ** 2

    return df[PREDICTIONS_COLUMNS]


def _summary_frame(conn) -> pd.DataFrame:
    """One row per LLM: how many settled predictions it has and its resulting weight."""
    scores = Tracker.brier_scores(conn)
    rows = [
        {
            "llm": llm,
            "settled_n": stats["n"],
            "brier": stats["brier"],
            "market_brier": stats["market_brier"],
            "weight": Tracker.brier_to_weight(stats["brier"]),
        }
        for llm, stats in sorted(scores.items())
    ]
    return pd.DataFrame(rows, columns=["llm", "settled_n", "brier", "market_brier", "weight"])


def export_to_excel(db_path: str = Tracker.DEFAULT_DB, out_path: str = DEFAULT_OUT) -> str:
    """
    Write every recorded prediction, plus a per-LLM accuracy summary, to an
    .xlsx workbook with a 'predictions' sheet and an 'llm_summary' sheet.

    Returns the output path.
    """
    conn = Tracker.connect(db_path)
    try:
        predictions = _predictions_frame(conn)
        summary = _summary_frame(conn)
    finally:
        # Windows can't delete/replace a db file while a connection to it is
        # still open, so this must be closed before returning.
        conn.close()

    return write_workbook({"predictions": predictions, "llm_summary": summary}, out_path)


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else Tracker.DEFAULT_DB
    out = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUT
    path = export_to_excel(db, out)
    print(f"wrote {path}")
