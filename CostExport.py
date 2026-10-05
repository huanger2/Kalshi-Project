"""
CostExport.py

Writes the API usage CostTracker has logged out to its own Excel workbook
(separate from predictions.xlsx): one row per API call, plus a per-provider
spend summary.

    py CostExport.py [db_path] [out_path]
"""

import sys

import pandas as pd

import CostTracker
import Tracker

DEFAULT_OUT = "api_costs.xlsx"

USAGE_COLUMNS = ["created_at", "provider", "model", "input_tokens", "output_tokens", "cost_usd", "context"]


def _usage_frame(conn) -> pd.DataFrame:
    """One row per logged API call."""
    df = pd.read_sql_query(
        "SELECT created_at, provider, model, input_tokens, output_tokens, cost_usd, context "
        "FROM api_usage ORDER BY created_at",
        conn,
    )
    return df[USAGE_COLUMNS]


def _summary_frame(db_path: str) -> pd.DataFrame:
    """One row per provider: call count, token totals, known spend, unpriced-call count."""
    rows = CostTracker.usage_summary(db_path)
    return pd.DataFrame(
        rows, columns=["provider", "calls", "input_tokens", "output_tokens", "cost_usd", "unpriced_calls"]
    )


def export_to_excel(db_path: str = Tracker.DEFAULT_DB, out_path: str = DEFAULT_OUT) -> str:
    """
    Write every logged API call, plus a per-provider spend summary, to an
    .xlsx workbook with a 'usage_log' sheet and a 'provider_summary' sheet.

    Returns the output path.
    """
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(CostTracker.SCHEMA)
        usage = _usage_frame(conn)
    finally:
        # Windows can't delete/replace a db file while a connection to it is
        # still open, so this must be closed before returning.
        conn.close()

    summary = _summary_frame(db_path)

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        usage.to_excel(writer, sheet_name="usage_log", index=False)
        summary.to_excel(writer, sheet_name="provider_summary", index=False)

    return out_path


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else Tracker.DEFAULT_DB
    out = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_OUT
    path = export_to_excel(db, out)
    print(f"wrote {path}")
