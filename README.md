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

Optional: `cryptography` (only for Kalshi's authenticated endpoints).

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

### Running it automatically every morning

[`schedule_task.ps1`](schedule_task.ps1) registers a Windows Task Scheduler
task that runs `DailyLog.py` once a day, so nothing has to stay open:

```powershell
.\schedule_task.ps1               # creates/updates it, runs daily at 09:00
.\schedule_task.ps1 -Time "07:30" # a different time
.\schedule_task.ps1 -Uninstall    # remove it
```

It runs `py.exe DailyLog.py` with this project folder as the working
directory, so `AIAPIKEYS.env`, `EmailAlerts.env`, `ai_settings.json`,
`predictions.db` and `daily_logs/` all resolve exactly as they do when you
run it by hand. View or edit the task in Task Scheduler (`taskschd.msc`),
under Task Scheduler Library as `KalshiNBA-DailyLog`.

This only runs while you're logged in, which covers a personal machine
that's normally on. To also run while logged off: open Task Scheduler, find
the task, Properties > General > "Run whether user is logged on or not",
and enter your password when prompted.

## Email alerts when an AI runs out of funds

[`Notifier.py`](Notifier.py) emails you whenever an AI API call fails with
what looks like a billing problem -- quota exhausted, credit balance too
low, billing not enabled -- rather than letting it silently scroll by in an
unattended overnight run. It hooks into `query_llms` in `Probability.py`,
so it covers `DailyLog.py`, `Collect.py` and `Probability.py` alike. One
email per AI per run, even if many games are queried.

Credentials live in `EmailAlerts.env`, a separate file from `AIAPIKEYS.env`,
also gitignored:

```
EMAIL_ADDRESS=you@gmail.com
EMAIL_APP_PASSWORD=
ALERT_EMAIL_TO=you@gmail.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
```

`EMAIL_APP_PASSWORD` must be a Gmail **App Password**, not your normal
Google password: turn on
[2-Step Verification](https://myaccount.google.com/signinoptions/two-step-verification),
then generate one at <https://myaccount.google.com/apppasswords> and paste
the 16 characters in with no spaces. Without it, `Notifier` prints a warning
and skips sending instead of crashing the pipeline.

## Tracking API spend

None of OpenAI, Anthropic or Gemini expose a "remaining balance" through a
normal API key -- that only shows in their web consoles (and a real
per-request cost figure from OpenAI/Anthropic needs a separate Admin/
Organization API key; Gemini has nothing comparable at all). Instead,
[`CostTracker.py`](CostTracker.py) reads the token counts every API response
already reports and multiplies them by a price you supply -- the same math
their billing is based on.

Fill in [`model_pricing.json`](model_pricing.json) with each model's *current*
price from its provider's own pricing page, in USD per 1,000,000 tokens --
prices change, and the numbers below are just the shape of the file, not
real quotes:

```json
{
    "models": {
        "claude-sonnet-5-5": {"input_per_million": null, "output_per_million": null},
        "gpt-4.1-mini": {"input_per_million": null, "output_per_million": null},
        "gemini-3.1-flash-lite": {"input_per_million": null, "output_per_million": null}
    }
}
```

A model left out, or still `null` as it ships, records its token counts
with an **unknown** cost rather than silently assuming $0. Every call to
`ask_openai` / `ask_claude` / `ask_gemini` logs its usage automatically; a
logging failure prints a warning instead of breaking the AI call.

**`api_costs.xlsx` stays current on its own.** Every `DailyLog.py` run
refreshes it from `predictions.db` after logging today's games, the same way
it already updates `daily_logs/` -- so the running total is always up to
date without a separate manual step. A locked/open spreadsheet just skips
that refresh with a message instead of failing the whole run; the next
day's run catches it up.

| Command | What it does |
|---|---|
| `python Collect.py usage` | Print each AI's logged token usage and estimated spend |
| `python Collect.py costs [file.xlsx]` | Refresh the Excel export by hand (`api_costs.xlsx` by default) |

## Other tools

| Command | What it does |
|---|---|
| `python Collect.py collect [N]` | Ask the AIs about up to N open games and store answers in `predictions.db` |
| `python Collect.py settle` | Record results for finished games in `predictions.db` |
| `python Collect.py weights` | Show each AI's accuracy (Brier score) and resulting weight |
| `python Collect.py export [file.xlsx]` | Export `predictions.db` to Excel |
| `python scripts.py probability` | Run the full expected-value analysis on one open game |
| `python scripts.py kalshi` | List open NBA game markets with implied probabilities |
| `python scripts.py ev` | EVCalculator worked example with made-up numbers |
| `python scripts.py gemini` | Ask Gemini one sample prediction question |

## Project layout

| File | Purpose |
|---|---|
| `DailyLog.py` | The daily data-collection run described above |
| `scripts.py` | Ad-hoc demos for poking at one piece of the pipeline by hand |
| `Kalshi.py` | Kalshi API: markets, prices, today's games, current odds, results |
| `ClaudeAi.py`, `OpenAi.py`, `Gemini.py` | One wrapper per AI API |
| `AISettings.py`, `ai_settings.json` | Which model each AI uses |
| `Probability.py` | Builds the prompt, parses AI replies, runs the EV analysis |
| `EVCalculator.py` | Confidence-weighted expected-value score vs. the market price |
| `Tracker.py`, `Export.py`, `Collect.py` | SQLite prediction tracking, accuracy weights, Excel export |
| `Notifier.py` | Emails an alert when an AI API call looks like a billing/quota failure |
| `CostTracker.py`, `CostExport.py`, `model_pricing.json` | Token-based API spend estimate and Excel export |
| `ExcelUtil.py` | Shared workbook-writing helper used by `Export.py` and `CostExport.py` |
| `schedule_task.ps1` | Registers the Windows Task Scheduler task that runs `DailyLog.py` daily |

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
