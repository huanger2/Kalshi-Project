"""
ExcelUtil.py

The one bit of spreadsheet-writing logic shared by Export.py and
CostExport.py, so it isn't copy-pasted between them.
"""

import pandas as pd


def write_workbook(sheets: dict[str, "pd.DataFrame"], out_path: str) -> str:
    """Write each (sheet name, DataFrame) pair to one .xlsx workbook. Returns out_path."""
    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, sheet_name=name, index=False)
    return out_path
