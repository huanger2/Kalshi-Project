"""
DailyLog.py

Run once at the start of the day to collect data before live betting:

  1. Fill in the results of earlier games that are still pending (yesterday's,
     plus any day that was missed), in the file for the day they were played.
  2. Log today's NBA games: Kalshi's current odds and each AI's predicted
     probability and confidence.

Each day gets its own spreadsheet in daily_logs/, named by date
(daily_logs/2026-10-04.xlsx), with one row per game. The row is for one team
(the YES side of the market that was asked about); team_won is 1 if that team
won, 0 if it lost, blank until the game is settled. Re-running on the same day
skips games that are already logged.

Each sheet groups its columns by source (Game, Kalshi odds, each AI, Result)
under a colored header row, and a 'Column guide' sheet explains every column.

    py DailyLog.py [folder]
    py DailyLog.py backfill [YYYY-MM-DD] [folder]

A game is only retried automatically if *every* AI failed for it; if one AI
fails while the others succeed, the game is still logged, with that AI's
cells left permanently blank. `backfill` fixes an already-logged day: for
each AI with a blank cell, it re-fetches that market and re-queries just
that AI, leaving everything else untouched.
"""

import datetime
import itertools
import os
import sys
from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import CostExport
from Kalshi import get_market, get_nba_results, get_prices, get_todays_nba_games, implied_yes_prob
from Probability import build_prompt, default_llms, parse_response, query_llms

DEFAULT_DIR = "daily_logs"
DEFAULT_LLM_NAMES = ["OpenAI", "Claude", "Gemini"]


# ---------------------------------------------------------------------------
# Columns: internal key, spreadsheet header, the source group it sits under,
# and the explanation shown on the "Column guide" sheet.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Column:
    key: str
    header: str
    group: str
    source: str
    meaning: str
    number_format: Optional[str] = None


PERCENT = "0.0%"
GROUP_COLORS = {"Game": "D9D9D9", "Kalshi odds": "C6E0B4", "Result": "FFE699", "Details": "EDEDED"}
AI_COLORS = ["BDD7EE", "F8CBAD", "D9C3E9", "B4E5E0", "FFD3E0"]


def column_spec(llm_names: list[str]) -> list[Column]:
    spec = [
        Column("game_date", "Game date", "Game", "Kalshi",
               "Date the game is scheduled (US time)."),
        Column("team", "Team", "Game", "Kalshi",
               "The team this row is about. Every win % on the row is the chance THIS team wins."),
        Column("opponent", "Opponent", "Game", "Kalshi",
               "The other team in the game."),
        Column("yes_bid", "Kalshi bid", "Kalshi odds", "Kalshi",
               "Highest price buyers were offering for a 'Team wins' contract when the row was "
               "logged, as a % of its $1 payout.", PERCENT),
        Column("yes_ask", "Kalshi ask", "Kalshi odds", "Kalshi",
               "Lowest price a 'Team wins' contract could be bought for when the row was logged. "
               "This is what betting on Team would have cost.", PERCENT),
        Column("market_prob", "Kalshi implied win %", "Kalshi odds", "Kalshi",
               "Midpoint of the bid and ask: the market's estimate of the chance Team wins.", PERCENT),
    ]
    for name in llm_names:
        key = name.lower()
        spec += [
            Column(f"{key}_prob", f"{name} win %", name, name,
                   f"{name}'s estimate of the chance Team wins (model set in ai_settings.json).", PERCENT),
            Column(f"{key}_confidence", f"{name} confidence", name, name,
                   f"How sure {name} says it is of its estimate: 0% = pure guess, 100% = certain.", PERCENT),
        ]
    spec += [
        Column("winner", "Winner", "Result", "Kalshi",
               "The team that won. Filled in by the first run after the game finishes."),
        Column("team_won", "Team won? (1 = yes)", "Result", "Kalshi",
               "1 if Team won, 0 if Team lost, blank until the game is settled.", "0"),
        Column("settled_at", "Result recorded at", "Result", "This script",
               "When the result was filled in."),
        Column("logged_at", "Logged at", "Details", "This script",
               "When the Kalshi odds and AI answers on this row were captured."),
        Column("event_ticker", "Kalshi game ID", "Details", "Kalshi",
               "Kalshi's ID for the game (covers both teams)."),
        Column("ticker", "Kalshi market ID", "Details", "Kalshi",
               "Kalshi's ID for the 'Team wins' market whose odds are on this row."),
    ]
    return spec


def columns(llm_names: list[str]) -> list[str]:
    return [c.key for c in column_spec(llm_names)]


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Reading and writing the workbook
# ---------------------------------------------------------------------------

def _header_to_key(header: str) -> str:
    """Map a spreadsheet header back to its key (any AI name works)."""
    fixed = {c.header: c.key for c in column_spec([])}
    if header in fixed:
        return fixed[header]
    if header.endswith(" win %"):
        return header[: -len(" win %")].lower() + "_prob"
    if header.endswith(" confidence"):
        return header[: -len(" confidence")].lower() + "_confidence"
    return header


def _is_numeric(key: str) -> bool:
    return key in ("yes_bid", "yes_ask", "team_won") or key.endswith(("_prob", "_confidence"))


def load(path: str) -> pd.DataFrame:
    """The log so far, or an empty one. Dates are kept as ISO strings."""
    if not os.path.exists(path):
        return pd.DataFrame(columns=columns(DEFAULT_LLM_NAMES))

    # Everything as text first: columns that are still all blank would
    # otherwise load as floats and reject team names later.
    if "game_date" in pd.read_excel(path, nrows=0).columns:
        df = pd.read_excel(path, dtype=object)  # older single-header layout
    else:
        df = pd.read_excel(path, header=1, dtype=object)  # row 1 is the source groups
        df = df.rename(columns=_header_to_key)

    for key in df.columns:
        if _is_numeric(key):
            df[key] = pd.to_numeric(df[key])
    return df


def _cell_value(value):
    if pd.isna(value):
        return None
    return value.item() if hasattr(value, "item") else value  # numpy -> plain Python


def save(df: pd.DataFrame, path: str, llm_names: list[str] = DEFAULT_LLM_NAMES) -> None:
    """
    Write the log as a formatted workbook: row 1 groups the columns by source,
    row 2 names each column, and a 'Column guide' sheet explains them all.
    """
    spec = column_spec(llm_names)
    known = {c.key for c in spec}
    spec += [Column(k, k, "Other", "", "") for k in df.columns if k not in known]
    df = df.reindex(columns=[c.key for c in spec])

    colors = dict(GROUP_COLORS, Other="FFFFFF")
    colors.update({name: AI_COLORS[i % len(AI_COLORS)] for i, name in enumerate(llm_names)})

    bold = Font(bold=True)
    centered = Alignment(horizontal="center", vertical="center", wrap_text=True)
    divider = Border(left=Side(style="medium", color="808080"))

    wb = Workbook()
    ws = wb.active
    ws.title = "Daily log"

    # Row 1: one merged, colored cell per source group.
    col = 1
    group_starts = set()
    for group, members in itertools.groupby(spec, key=lambda c: c.group):
        width = len(list(members))
        group_starts.add(col)
        ws.cell(1, col, group)
        if width > 1:
            ws.merge_cells(start_row=1, start_column=col, end_row=1, end_column=col + width - 1)
        for c in range(col, col + width):
            ws.cell(1, c).fill = PatternFill("solid", fgColor=colors[group])
        ws.cell(1, col).font = bold
        ws.cell(1, col).alignment = centered
        col += width

    # Row 2: column names, colored to match their group.
    for i, column in enumerate(spec, 1):
        cell = ws.cell(2, i, column.header)
        cell.font = bold
        cell.alignment = centered
        cell.fill = PatternFill("solid", fgColor=colors[column.group])
        ws.column_dimensions[get_column_letter(i)].width = max(11, min(len(column.header) + 3, 22))
    ws.row_dimensions[2].height = 32

    # Data from row 3.
    for r, record in enumerate(df.itertuples(index=False), 3):
        for i, (value, column) in enumerate(zip(record, spec), 1):
            cell = ws.cell(r, i, _cell_value(value))
            if column.number_format:
                cell.number_format = column.number_format

    # A divider line down the left edge of each group.
    for i in group_starts - {1}:
        for r in range(1, ws.max_row + 1):
            ws.cell(r, i).border = divider

    ws.freeze_panes = "D3"  # keep the headers and Game columns visible
    ws.auto_filter.ref = f"A2:{get_column_letter(len(spec))}{max(ws.max_row, 2)}"

    guide = wb.create_sheet("Column guide")
    for i, (header, width) in enumerate([("Column", 24), ("Source", 14), ("What it means", 95)], 1):
        cell = guide.cell(1, i, header)
        cell.font = bold
        guide.column_dimensions[get_column_letter(i)].width = width
    for r, column in enumerate((c for c in spec if c.group != "Other"), 2):
        guide.cell(r, 1, column.header).fill = PatternFill("solid", fgColor=colors[column.group])
        guide.cell(r, 2, column.source)
        guide.cell(r, 3, column.meaning).alignment = Alignment(wrap_text=True, vertical="top")
    guide.freeze_panes = "A2"

    try:
        wb.save(path)
    except PermissionError:
        raise SystemExit(f"Can't write {path}; close it in Excel and run again.") from None


def fill_results(
    df: pd.DataFrame,
    today: datetime.date,
    fetch_results: Optional[Callable[[datetime.date], list[dict]]] = None,
) -> int:
    """Fill winner / team_won for pending games before `today`. Returns rows settled."""
    fetch_results = fetch_results or get_nba_results
    pending = df["winner"].isna() & (df["game_date"] < today.isoformat())
    settled = 0
    for day in sorted(df.loc[pending, "game_date"].unique()):
        results = {r["event_ticker"]: r for r in fetch_results(datetime.date.fromisoformat(day))}
        for idx in df.index[pending & (df["game_date"] == day)]:
            result = results.get(df.at[idx, "event_ticker"])
            if result is None:
                continue
            df.at[idx, "winner"] = result["winner"]
            df.at[idx, "team_won"] = int(df.at[idx, "team"] == result["winner"])
            df.at[idx, "settled_at"] = _now()
            settled += 1
    return settled


def todays_rows(
    today: datetime.date,
    already_logged: set[str],
    llms: list[tuple[str, Callable[[str], str]]],
) -> list[dict]:
    """One row per game today that isn't logged yet and is still open for trading."""
    games: dict[str, list[dict]] = {}
    for market in get_todays_nba_games(today):
        games.setdefault(market["event_ticker"], []).append(market)

    rows = []
    for event, markets in games.items():
        if event in already_logged:
            continue
        market = markets[0]
        if market.get("status") != "active":
            print(f"skipped {event}: market is {market.get('status')}, not open")
            continue

        predictions = query_llms(market, llms, verbose=False)
        if not predictions:
            # Leave it unlogged so the next run tries again.
            print(f"skipped {event}: every AI query failed")
            continue

        prices = get_prices(market)
        row = {
            "game_date": today.isoformat(),
            "event_ticker": event,
            "ticker": market["ticker"],
            "team": market.get("yes_sub_title"),
            "opponent": next((m.get("yes_sub_title") for m in markets[1:]), None),
            "logged_at": _now(),
            "yes_bid": prices["yes_bid"],
            "yes_ask": prices["yes_ask"],
            "market_prob": implied_yes_prob(market),
        }
        for p in predictions:
            row[f"{p.llm_name.lower()}_prob"] = p.predicted_prob
            row[f"{p.llm_name.lower()}_confidence"] = p.confidence
        rows.append(row)
        print(f"logged {event}: {row['team']} vs {row['opponent']}")
    return rows


def backfill_missing_predictions(
    day: datetime.date,
    folder: str = DEFAULT_DIR,
    llms: Optional[list[tuple[str, Callable[[str], str]]]] = None,
    fetch_market=get_market,
) -> int:
    """
    Fill in still-blank AI prediction cells for an already-logged day.

    todays_rows() only retries a game if *every* AI failed for it, so a game
    where one AI failed and the others succeeded gets logged anyway, leaving
    that AI's cells permanently blank. This re-fetches the market for each
    blank cell and re-queries just that AI, leaving every other cell (and
    every other AI) untouched.

    Returns the number of cells filled in.
    """
    llms = llms or default_llms()
    path = path_for(day, folder)
    df = load(path)
    if df.empty:
        return 0

    filled = 0
    for llm_name, ask_fn in llms:
        key = llm_name.lower()
        prob_col, conf_col = f"{key}_prob", f"{key}_confidence"
        if prob_col not in df.columns:
            continue

        for idx in df.index[df[prob_col].isna()]:
            ticker = df.at[idx, "ticker"]
            try:
                prompt = build_prompt(fetch_market(ticker))
                prob, conf = parse_response(ask_fn(prompt), llm_name)
            except Exception as e:
                print(f"[{llm_name}] could not backfill {ticker}: {e}")
                continue
            df.at[idx, prob_col] = prob
            df.at[idx, conf_col] = conf
            filled += 1
            print(f"backfilled {llm_name} for {df.at[idx, 'team']} ({ticker}): "
                  f"{prob:.0%} probability, {conf:.0%} confidence")

    if filled:
        save(df, path, [name for name, _ in llms])
    return filled


def path_for(day: datetime.date, folder: str = DEFAULT_DIR) -> str:
    """The spreadsheet for one day's games, e.g. daily_logs/2026-10-04.xlsx."""
    return os.path.join(folder, f"{day.isoformat()}.xlsx")


def earlier_days(folder: str, today: datetime.date) -> list[datetime.date]:
    """Dates of the day files in `folder` from before `today`, oldest first."""
    days = []
    for name in os.listdir(folder):
        stem, ext = os.path.splitext(name)
        try:
            day = datetime.date.fromisoformat(stem)  # also skips Excel's ~$ lock files
        except ValueError:
            continue
        if ext == ".xlsx" and day < today:
            days.append(day)
    return sorted(days)


def run(
    folder: str = DEFAULT_DIR,
    today: Optional[datetime.date] = None,
    llms: Optional[list[tuple[str, Callable[[str], str]]]] = None,
) -> tuple[int, int]:
    """
    Fill in results in earlier days' files, then write today's games to
    today's file. Returns (games settled, games logged).
    """
    today = today or datetime.date.today()
    llms = llms or default_llms()
    llm_names = [name for name, _ in llms]
    os.makedirs(folder, exist_ok=True)

    settled = 0
    for day in earlier_days(folder, today):
        path = path_for(day, folder)
        df = load(path)
        n = fill_results(df, today)
        if n:
            save(df, path, llm_names)
            settled += n

    path = path_for(today, folder)
    df = load(path)
    new_rows = todays_rows(today, set(df["event_ticker"].dropna()), llms)
    if new_rows:
        new = pd.DataFrame(new_rows)
        df = new if df.empty else pd.concat([df, new], ignore_index=True)
        save(df, path, llm_names)

    print(f"settled {settled} earlier game(s); logged {len(new_rows)} game(s) to {path}")

    # Every AI call today was already logged to predictions.db as it happened
    # (see CostTracker.py); refresh the running api_costs.xlsx from it so the
    # spreadsheet never falls behind without needing a separate manual step.
    # Best-effort: a locked/open spreadsheet shouldn't fail the whole daily run.
    try:
        cost_path = CostExport.export_to_excel()
        print(f"updated {cost_path}")
    except Exception as e:
        print(f"could not update cost spreadsheet: {e}")

    return settled, len(new_rows)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        day = datetime.date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else datetime.date.today()
        folder = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_DIR
        n = backfill_missing_predictions(day, folder)
        print(f"backfilled {n} cell(s)" if n else "nothing to backfill")
    else:
        run(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DIR)
