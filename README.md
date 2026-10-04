# Kalshi-Project

Compares AI predictions against Kalshi's NBA game markets. Each day it logs
today's games with Kalshi's odds and what OpenAI, Claude and Gemini think, then
fills in who actually won, building up a record of how the AIs do against the
market before any real money is bet.

## Setup

Requires Python 3.10+.

```powershell
pip install requests python-dotenv pandas openpyxl openai google-genai
```

Optional: `cryptography` (only for Kalshi's authenticated endpoints) and
`nba_api` (only for `Schedule.py`).

Put your API keys in `AIAPIKEYS.env` in the project folder. It is gitignored,
so it is never committed.

```
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GEMINI_API_KEY=...
```

Kalshi market data is public, so the daily log needs no Kalshi key.

## Choosing the AI models

Each AI's model is set in [`ai_settings.json`](ai_settings.json):

```json
{
    "models": {
        "claude": "claude-sonnet-5-5",
        "openai": "gpt-4.1-mini",
        "gemini": "gemini-3.1-flash-lite"
    }
}
```

Edits take effect on the next call. A provider left out of the file uses the
default in [`AISettings.py`](AISettings.py).

## Daily log

Run once each morning:

```powershell
python DailyLog.py
```

Each run:

1. **Fills in results.** Games from earlier days that are still waiting on a
   result (yesterday's, plus any day that was missed) get their winner,
   written into the file for the day they were played.
2. **Logs today's games.** For each game still open on Kalshi, it records the
   current Kalshi odds and each AI's win probability and confidence.

Each day gets its own spreadsheet in `daily_logs/`, named by date
(`daily_logs/2026-10-04.xlsx`), with one row per game. Each row is written
from one team's side: every win % on the row is the chance *that* team wins,
and "Team won?" is 1 or 0 once the game is over.

Columns are grouped by source under a colored header row:

| Game | Kalshi odds | OpenAI / Claude / Gemini | Result | Details |
|---|---|---|---|---|
| date, team, opponent | bid, ask, implied win % | win %, confidence | winner, team won?, when recorded | when logged, Kalshi IDs |

Each file also has a **Column guide** sheet explaining every column and its source.

Notes:

- Running it twice in a day is safe; games already logged are skipped.
- If one AI fails, only its cells are left blank. If every AI fails for a game,
  that game is left out so the next run tries again.
- Close the day's file in Excel before running, or the script stops and asks you to.
- `daily_logs/` is gitignored because it is generated data.

## Other tools

| Command | What it does |
|---|---|
| `python Collect.py collect [N]` | Ask the AIs about up to N open games and store answers in `predictions.db` |
| `python Collect.py settle` | Record results for finished games in `predictions.db` |
| `python Collect.py weights` | Show each AI's accuracy (Brier score) and resulting weight |
| `python Collect.py export [file.xlsx]` | Export `predictions.db` to Excel |
| `python Probability.py` | Run the full expected-value analysis on one open game |
| `python Kalshi.py` | List open NBA game markets with implied probabilities |

## Project layout

| File | Purpose |
|---|---|
| `DailyLog.py` | The daily data-collection run described above |
| `Kalshi.py` | Kalshi API: markets, prices, today's games, current odds, results |
| `ClaudeAi.py`, `OpenAi.py`, `Gemini.py` | One wrapper per AI API |
| `AISettings.py`, `ai_settings.json` | Which model each AI uses |
| `Probability.py` | Builds the prompt, parses AI replies, runs the EV analysis |
| `EVCalculator.py` | Confidence-weighted expected-value score vs. the market price |
| `Tracker.py`, `Export.py`, `Collect.py` | SQLite prediction tracking, accuracy weights, Excel export |

## Tests

```powershell
python -m unittest discover tests
```

All tests mock the network, so they need no keys and cost nothing. The AI tests
also include opt-in live checks against the real APIs; these make small paid
calls:

```powershell
$env:RUN_LIVE_AI_TESTS = "1"; python -m unittest tests.test_ai_apis -v
```
