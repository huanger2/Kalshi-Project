# Kalshi-Project

Compares AI predictions against Kalshi's game markets for the NBA, NFL, NHL
and MLB. Each day it logs today's games with Kalshi's odds and what OpenAI,
Claude and Gemini think, then fills in who actually won, building up a record
of how the AIs do against the market before any real money is bet.

## Setup

Requires Python 3.10+.

```powershell
pip install requests python-dotenv pandas openpyxl openai google-genai fastapi uvicorn jinja2 python-multipart
```

Optional: `cryptography` (only for demo/live betting, which signs Kalshi requests).

Put your API keys in `AIAPIKEYS.env` in the project folder. It is gitignored,
so it is never committed.

```
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GEMINI_API_KEY=...
```

Kalshi market data is public, so the daily log needs no Kalshi key. Only
demo and live betting do (see the Betting tab below).

## Choosing the AI models

Each AI's model is set in [`ai_settings.json`](ai_settings.json):

```json
{
    "models": {
        "claude": "claude-sonnet-5-5",
        "openai": "gpt-5.1",
        "gemini": "gemini-3.8-flash"
    }
}
```

Edits take effect on the next call. A provider left out of the file uses the
default in [`AISettings.py`](AISettings.py).

## Choosing the leagues

Which leagues get logged each day is set in [`leagues.json`](leagues.json):

```json
{
    "enabled": ["NBA", "NFL", "NHL", "MLB"]
}
```

Every game costs one call to each AI, so on a busy day four leagues can mean
40+ games (a full NFL Sunday alone is ~14). Turn a league off with the
**tracking switch on its tab in the webapp** (which rewrites just this
`"enabled"` line), or remove it from the list by hand, to stop spending on
it; its existing data stays in `predictions.db` and the
webapp. Like `ai_settings.json`, edits take effect on the next run.

All four leagues' Kalshi markets (`KXNBAGAME`, `KXNFLGAME`, `KXNHLGAME`,
`KXMLBGAME`) have the same shape: one "<Team> wins" market per team per game.
[`Leagues.py`](Leagues.py) holds each league's series and calendar. The one
real difference is that **NFL games can end in a tie**, which this project
treats as no team winning: "Team won?" is 0 and Winner says "Tie" in the
spreadsheet, and the webapp shows "Tie" rather than a loss. It counts as a 0
in the Brier score, but doesn't count as a right or wrong pick for either
side: it's left out of "called the winner %" and the won/lost edge averages.
(Kalshi itself pays $0.50 per side on a tie; that payout isn't used here.)

## Daily log

Run once each morning:

```powershell
py DailyLog.py
```

Each run:

1. **Fills in results.** Games from earlier days that are still waiting on a
   result (yesterday's, plus any day that was missed) get their winner,
   written into the file for the day they were played.
2. **Logs today's games** in every league enabled in `leagues.json`. For each
   game still open on Kalshi, it records the current Kalshi odds and each
   AI's win probability and confidence.

Each day gets its own spreadsheet in `daily_logs/`, named by date
(`daily_logs/2026-10-04.xlsx`), with one row per game across all leagues and
a League column. Each row is written from one team's side: every win % on
the row is the chance *that* team wins, and "Team won?" is 1 or 0 once the
game is over (0 for an NFL tie, with Winner showing "Tie").

Columns are grouped by source under a colored header row:

| Game | Kalshi odds | OpenAI / Claude / Gemini | Result | Details |
|---|---|---|---|---|
| date, league, team, opponent | bid, ask, implied win % | win %, confidence, model | winner, team won?, when recorded | when logged, Kalshi IDs |

Each AI's `model` column records which model (from `ai_settings.json`)
actually produced that row's prediction, so a model upgrade doesn't leave
old and new predictions indistinguishable in the data.

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
under Task Scheduler Library as `KalshiNBA-DailyLog`. The name is left over
from when the project was NBA-only. It runs every enabled league, and keeping
the name means an already-registered task carries on without a duplicate.

This only runs while you're logged in, which covers a personal machine
that's normally on. To also run while logged off: open Task Scheduler, find
the task, Properties > General > "Run whether user is logged on or not",
and enter your password when prompted.

## Browsing the data (webapp)

The long-term goal of this project: a webapp to look through tracked
markets and compare each AI's prediction against Kalshi's price, instead of
digging through spreadsheets. [`webapp/`](webapp/) is the first pass at
that framework.

```powershell
py -m uvicorn webapp.main:app --reload
```

Then open <http://127.0.0.1:8000/>. It reads `predictions.db` directly (see
[`Tracker.py`](Tracker.py)) -- the one table `DailyLog.py` and `Collect.py`
both write into as they run, so the page is always current with no refresh
step of its own. It lists tracked games under an "All markets" tab and a
**tab per league** (`/league/NFL`), each filterable by date, by season e.g.
"2026-27", and independently by preseason/regular season/postseason. Each
league's tab has a switch to turn daily tracking for that league on or off
(untracked leagues show dimmed in the tab bar). It also has a detail page
per game showing every
AI's prediction next to Kalshi's price and the "edge" between them, and a
**tab per AI** (`/ai/{name}`) with its calibration stats and full
prediction history (also filterable by league, since an AI can be sharp on
one sport and poor at another):

- **Average edge when the team won / lost.** A skilled, well-calibrated AI's
  edge (predicted win % minus Kalshi's) should be positive on games its
  pick went on to win and negative on games it went on to lose -- edge that
  looks the same either way isn't actually predictive, whatever the raw
  Brier score says.
- **Called the winner %**, i.e. how often its >50%/<50% pick matched the
  outcome regardless of margin, next to the market's own for comparison.
- Brier score vs. the market's own, current EV weight, average confidence.

A couple of JSON endpoints (`/api/markets`, `/api/markets/{ticker}`,
`/api/accuracy`, `/api/ai/{name}`) sit alongside the HTML pages for
whatever consumes this next.

### AI odds (consensus probability)

Next to the AI odds everywhere (markets tables, each game's page, your
bets) is the **AI confidence**: each AI's own confidence rating, averaged
by the same AI weights, so you can see how sure the AIs were, not just
what they said.

The markets table's "AI odds" column combines the 3 AIs' predictions for a
game into one number -- not a bare average, but each one weighted by its own
confidence and its AI's weight, same `p_i` / `c_i` / `w_i` vocabulary as
[`EVCalculator.py`](EVCalculator.py)'s EV score:

```
p = sum_i(p_i * c_i * w_i) / sum_i(c_i * w_i)
```

An AI that says 90% at 95% confidence counts for more than one that says 90%
at 20% confidence. `w_i` comes from [`AIWeights.py`](AIWeights.py) /
[`ai_weights.json`](ai_weights.json) -- a manually editable dial, every AI
defaulting to 1.0 (equal weighting) until you decide otherwise:

```json
{
    "weights": {
        "claude": 1.0,
        "openai": 1.0,
        "gemini": 1.0
    }
}
```

This is deliberately separate from `Tracker.compute_weights()` (the
Brier-score-based "EV weight" shown on each AI's tab): that's an automatic
*measurement* of accuracy over settled games, and preseason games are
inherently noisy -- teams aren't really trying to win, so results don't
reflect either side's skill yet -- so an early Brier score swinging an AI's
weight around isn't a signal worth trusting yet either. `ai_weights.json`
stays at 1.0 for everyone until there's a real basis (e.g. a full regular
season of settled games) to intentionally favor one AI over another.

### Season and segment

Kalshi doesn't label a game market with any of this (checked directly
against the live API for all four leagues: no such field exists), so
[`SeasonType.py`](SeasonType.py) derives it instead, purely from the game
date and league -- nothing is stored, so a query always reflects the current
config, never a stale snapshot from whenever a row was inserted. These are
two independent things, and both the webapp and `Tracker.py` let you
filter/scope by either one on its own or combine them:

- **Season** -- which season a date belongs to. The NBA and NHL cross New
  Year's, so they're labeled `"2026-27"` (Oct 2026 - Jun 2027). The NFL is
  labeled by the year it kicks off (`"2026"` includes the Jan/Feb 2027
  playoffs and Super Bowl). MLB is one calendar year (`"2026"`).
- **Segment** -- `"preseason"`, `"regular"`, or `"postseason"`. Preseason
  games (and spring training) are inherently noisier (limited rotations, no
  real stakes) regardless of an AI's actual skill, so this matters for
  reading the accuracy numbers honestly.

Segment defaults to each league's generic calendar heuristic, approximately
right for most seasons:

| League | Regular season starts | Postseason starts |
|---|---|---|
| NBA | ~Oct 21 | ~Apr 14 |
| NFL | Thursday after Labor Day | 128 days later (Wild Card Saturday) |
| NHL | ~Oct 7 | ~Apr 18 |
| MLB | ~Mar 25 | ~Sep 29 |

Fill in the `"seasons"` part of [`leagues.json`](leagues.json) with a
season's actual published start dates for exact boundaries:

```json
"seasons": {
    "NBA": {"2026-27": {"regular_season_start": "2026-10-20", "postseason_start": "2027-04-14"}},
    "MLB": {"2026": {"regular_season_start": "2026-03-26", "postseason_start": null}}
}
```

`Tracker.brier_scores()` / `compute_weights()` also take optional `segment`,
`season_year` and `league` arguments to scope the EV-calculation weight
itself to one slice -- not just the webapp's display.

Rows from before the project tracked multiple leagues get their league
filled in automatically (from the Kalshi ticker) the first time anything
opens `predictions.db`. Old `daily_logs/*.xlsx` files without a League
column read as NBA.

If you have `daily_logs/*.xlsx` files from before `DailyLog.py` started
writing to `predictions.db` directly, backfill them once:

```powershell
py MigrateDailyLogs.py
```

Safe to re-run -- it only inserts rows for a (ticker, AI) pair it hasn't
seen before, so running it again after a new day's file appears just adds
that day.

This is a first framework pass, not a finished product: no pagination,
plain server-rendered pages. Treat it as the foundation to
build the "look at different markets" experience on, not the end state.

### Settings: yours, and the site's

Settings come in two kinds:

- **Yours** (any signed-in user): your password and Kalshi API keys on the
  **Settings** tab, and your betting rules and AI weights on the
  **Betting** tab. Your weights drive the "AI odds" you see on every page
  and the bets placed for you; until you save your own, you follow the site
  defaults. All of it is saved with your account in
  `predictions.db` (see [`Accounts.py`](Accounts.py)), and nobody else sees
  or uses it.
- **The site's** (**Admin → Shared settings**, admins only): each AI's
  model (`ai_settings.json`), the default AI weights for guests and anyone
  who hasn't set their own (`ai_weights.json`), model pricing
  (`model_pricing.json`), and each league's tracking and season dates
  (`leagues.json`). These are the same for everyone.

A section with an invalid value shows why and saves nothing.

Locally, saving edits the file itself. Hosted (`DATA_DIR` set), saving
writes a copy of the file to the volume that overrides the deployed one, so
it survives deploys. Edits to that file pushed from git then don't apply on
the server until you click **Reset to repo version** (see
[`ConfigFiles.py`](ConfigFiles.py)).

**Ask Gemini for updates** (top of Shared settings) has Gemini search the
web for each AI's newest model, those models' prices, and announced league
dates, then lists every proposed change next to the current value, with the
sources it used. **Nothing is applied until you tick changes and click
Apply selected**, and applied values go through the same validation as a
manual edit. AI answers can be wrong, and a wrong model ID breaks that AI's
next daily run, so check the sources first.

Gemini decides for itself whether to search. Each part is asked as a short
research question with search enabled, then formatted into JSON by a second
call, because a long format-heavy question makes it skip searching and
answer from memory. If it still answers a part without searching, the page
flags that part as unverified. One click takes about two minutes and makes
four Gemini calls, logged in API spend; Google bills web search separately.

### Betting tab

Signed in, the **Betting** tab sets your own rules for automatic bets (see
[`Betting.py`](Betting.py)) and shows how your bets have done. Every user
has their own rules, bets, daily cap and balance chart; a new account
starts with betting off. The mode says where your bets go:

| Mode | |
|---|---|
| Off | no bets |
| Paper | bets are recorded at the price they'd have cost; nothing is sent to Kalshi |
| Kalshi demo | orders go to [Kalshi's demo exchange](https://demo.kalshi.co), with play money |
| Live | **real-money orders on Kalshi** |

A demo or live bet is an immediate-or-cancel limit order at the price the
rules were checked against. It fills at that price or better, partly, or
not at all if the price has moved, and only what filled is recorded, at
Kalshi's price and fee. Each mode's bets are counted separately (own daily
cap, own totals; the tab switches between them), and Kalshi pays real
positions out itself when the game settles.

Demo and live bets are placed from **your own Kalshi account**, so each
needs your own API key (the demo site issues separate keys from the real
one). Create one on Kalshi under Account → API Keys, then add its ID and
private key on your **Settings** tab (paste it, or upload the `.key` file).
**Check it works** asks Kalshi for the account's balance. The private key
is stored encrypted and never shown again.

The rules can't be saved in demo or live mode without that mode's key, and
switching to live also needs its confirmation box ticked. The tab shows
your account's cash balance. Try a few days on demo before going live.

Each time the daily run logs a game, it compares each betting user's AI
odds (the same weighted consensus as the "AI odds" column, with their own
weights) with what a contract costs right now on each side, which is the
ask, not the midpoint:

    edge(YES) = AI odds - yes_ask           (backing the listed team)
    edge(NO)  = (1 - AI odds) - no_ask      (backing its opponent)

The side with the bigger edge gets a bet if all of these hold:

- the edge is at least the minimum
- the side is priced within the allowed range
- enough AIs answered
- the AIs are confident enough: their own confidence ratings, averaged by
  your AI weights (an AI weighted 0 doesn't count), are at least the
  **minimum AI confidence**. 0% turns this off; with it on, a game gets no
  bet however big its edge if the AIs say they're unsure
- the league is ticked
- the game's part of the season (preseason, regular season or postseason,
  from the season dates on the Settings tab) is ticked. Preseason is
  unticked by default.
- the day's cap isn't used up

A bet buys as many whole contracts as the dollars-per-bet amount covers.
When more games qualify in a day than the cap covers, the biggest edges get
the money. A later run that day only gets what's left under the cap.
Its cost includes Kalshi's trading fee, 7% of C × P × (1 − P) rounded up to
the cent. A bet's result and P&L come from its game's outcome, which the
daily run already settles. A tied NFL game pays $0.50 a contract.

Your AI weights sit with the rules, since they decide which games show an
edge. As you edit any rule or weight, the **What if?** table and the AI
odds' Brier score update to match (replayed on the server, nothing saved
until you click Save).

Besides totals and the bet list, the tab shows:

- **Balance over time**, a line chart under the totals. For demo and live
  it's the Kalshi account's cash balance, recorded after each daily run and
  whenever the tab sees a new value. Paper bets have no account, so for them
  it's the running profit/loss of settled bets from $0, by game day.

- **What if?** replays the current rules over every settled game logged so
  far, at the prices when each was logged, for a range of minimum edges, so
  you can see which threshold would have paid. Each day's cap goes to that
  day's biggest edges, as in the daily run.
- **Breakdowns** split your bets' results by league, side and edge.

Your rules are saved with your account. `betting.json` is only read once,
to give the first admin the rules the site had before accounts existed.

### Analytics tab

Signed in, the **Analytics** tab checks how your AI odds (with your AI
weights) have held up, and can suggest better weights and betting rules
(see [`Optimizer.py`](Optimizer.py)):

A **Parts of the season** filter at the top picks which settled games
everything on the tab uses, the optimizer included. It starts at the parts
your betting rules bet on, since preseason games are noisy (teams rest
players and don't play to win). Each run remembers which parts it used, and
the page says when the filter has changed since.

- **Calibration**: games grouped by the chance given to the listed team,
  against how often it won, for the AI odds and for Kalshi's price.
- **Who predicts best**: the Brier score of each AI alone, your weighted AI
  odds, and Kalshi's price.
- **Does confidence pay?**: the same replay grouped by the AIs' weighted
  confidence, to see whether the minimum AI confidence rule is worth
  setting.
- **Does a bigger edge pay?**: every settled game's better side, one
  contract at its ask with fees, grouped by edge. If the AI odds carry real
  information, ROI rises with the edge.
- **Optimizer** (button): fits your **AI weights** to minimize the AI odds'
  Brier score, pulled toward equal weights by an amount picked with
  leave-one-day-out cross-validation; then grid-searches the **betting
  rules** that choose bets (minimum edge, price range, AIs required, and
  minimum AI confidence -- tried at 0 and at levels taken from how
  confident the AIs have actually been),
  replayed over the settled games with the suggested weights. Your stake,
  daily cap, leagues and seasons are kept. Rules are ranked by their return
  minus one standard error, so a few lucky bets can't win.

With a few dozen games, an in-sample replay mostly measures luck, so every
suggestion comes with an honest check: weights are scored on held-out days,
and rules on a **walk-forward** test that re-picks rules before each game
day using only earlier days, then bets that day. A 90% bootstrap range shows
how far ROI could swing on luck alone. Each suggestion gets a plain verdict,
and **Apply** buttons save it through the same checks as the Settings and
Betting forms (applying rules never changes your betting mode).

### Accounts and signing in

The first page a visitor sees is a sign-in screen: sign in, or **Continue
as guest** to browse everything read-only (with the default AI weights).
The JSON `/api/` endpoints stay open (read-only, and Railway's health check
uses one).

| | Can |
|---|---|
| Guest | browse markets and AI pages |
| User | also: their own Betting tab, and Settings (password, AI weights, Kalshi keys) |
| Admin | also: **Admin** (shared settings, users, data) and the league tracking switches |

**Nobody signs up on their own.** An admin adds people under **Admin →
Users**, which shows a temporary password once; the new user picks their
own the first time they sign in. Admins can also reset someone's password,
make them an admin, or remove them (their bets stay in the history). A
sign-in lasts 30 days; changing a password signs that user out everywhere
else. Five wrong passwords in a row lock that username out for 15 minutes
from that address.

**The first admin** comes from two environment variables, the single login
this app used before accounts existed:

| Variable | |
|---|---|
| `USERNAME` | the first admin's username |
| `PASSWORD` | their password (change it on the Settings tab afterwards) |

The first sign-in (or daily run) with no accounts yet creates that admin and
moves the old setup onto it: `betting.json`'s rules, the `KALSHI_*` keys in
`AIAPIKEYS.env` (or Railway's Variables), and every bet placed so far. After
that, `USERNAME`/`PASSWORD` and the `KALSHI_*` variables aren't used and can
be removed.

**Windows already sets `USERNAME`** to your Windows account name, so locally
the first admin gets that name unless you set it in the shell you start the
server from:

```powershell
$env:USERNAME = "your-admin-name"; $env:PASSWORD = "your-password"
py -m webapp
```

Two server secrets, both optional but recommended when hosted:

| Variable | |
|---|---|
| `SECRETS_KEY` | encrypts users' saved Kalshi keys. Any long random string. Unset, a key is generated into `secrets.key` in the data folder; lose it (or change `SECRETS_KEY`) and saved keys can't be decrypted, so users would re-enter them |
| `SESSION_SECRET` | signs the login cookie. Unset, one is generated into the database |

Accounts live in `predictions.db` with everything else. Downloading it from
the Data page leaves the account tables out, and uploading a replacement
keeps the server's own accounts.

### Hosting on Railway

[`requirements.txt`](requirements.txt) and [`.python-version`](.python-version)
let Railway's builder (Railpack) detect and build the project, and
[`railway.json`](railway.json) starts the webapp with `python -m webapp`,
which listens on the `$PORT` Railway provides (8000 when run locally).

#### Where data lives: `DATA_DIR`

Everything generated -- `predictions.db`, `daily_logs/`, `api_costs.xlsx`,
`predictions.xlsx` -- goes in the working directory by default, as on a
personal machine. Set `DATA_DIR` (e.g. `/data`, a mounted volume) to put it
all there instead: a Railway service's own filesystem is wiped on every
deploy, a volume isn't. (`PREDICTIONS_DB` still overrides just the
database's path.)

With `DATA_DIR` set, the league tracking switches save to
`league_tracking.json` on the volume instead of editing `leagues.json`, so
a flipped switch survives deploys while season dates you edit in
`leagues.json` and push still take effect. Until a switch is first flipped,
`leagues.json`'s `"enabled"` list decides.

#### The daily run on Railway

A Railway volume attaches to exactly one service, so the daily run happens
inside the **web service** (which owns the volume), and a small **cron
service** just triggers it: [`trigger_daily_run.py`](trigger_daily_run.py)
calls `POST /internal/daily-run` with a shared secret, waits for the run to
finish, and exits 0 or 1, so the cron service's logs show each morning's
outcome. Meanwhile, the run's full output goes to the web service's logs.

Each run also settles any earlier game still pending in `predictions.db`
straight from Kalshi, even without its day's spreadsheet on the server.

#### One-time setup

**Web service** (the one already running the site):

1. Attach a volume to it, mounted at `/data`.
2. In its **Variables**, set:
   - `DATA_DIR=/data`
   - `USERNAME` and `PASSWORD` (creates the first admin; see Accounts and
     signing in)
   - `SECRETS_KEY`: another long random secret, made the same way as the
     token below (it encrypts users' Kalshi keys)
   - `DAILY_RUN_TOKEN`: a long random secret, e.g. from
     `py -c "import secrets; print(secrets.token_urlsafe(32))"`
   - your `AIAPIKEYS.env` and `EmailAlerts.env` contents (Raw Editor accepts
     them as-is)
3. Sign in, open **Admin → Data**, and upload your `predictions.db`,
   plus any recent `daily_logs/*.xlsx` you want on the server.

**Cron service:**

1. Add a new service from the same GitHub repo.
2. In its **Settings**, set the cron schedule to `0 14 * * *` (UTC: 9am
   US Central during daylight saving time, 8am in winter), and the restart
   policy to **Never** if offered. Leave the start command alone: Railway
   gives this service the website's (`python -m webapp`, from
   `railway.json`), and the `DAILY_TRIGGER` variable below makes that run
   the trigger once and exit. Without it the cron service starts a second
   copy of the website, which never exits, so it shows "running" forever and
   never triggers anything.
3. In its **Variables**, set `DAILY_TRIGGER=1`, `DAILY_RUN_URL` to the
   site's URL (e.g. `https://your-app.up.railway.app`) and
   `DAILY_RUN_TOKEN` to the same secret as the web service's. Never set
   `DAILY_TRIGGER` on the web service: it would stop serving the site.

To test it without waiting for 14:00 UTC, run the trigger from your own
machine. It runs the real daily job on the server, AI calls included:

```powershell
$env:DAILY_RUN_URL = "https://your-app.up.railway.app"; $env:DAILY_RUN_TOKEN = "<the secret>"
py trigger_daily_run.py
```

Once a Railway run has succeeded, remove the Windows task
(`.\schedule_task.ps1 -Uninstall`), so the two don't both pay for every
AI call each morning, into two diverging databases.

To run it by hand, an admin can press **Run daily run now** at the top of
All markets (every tracked league), or **Run NBA now** (etc.) on a league's
tab (just that league). It's the same run the cron triggers, so it only
asks the AIs about games not logged yet today. The status line under it
shows the last run's result, and while one is going the page refreshes
itself when it finishes.

The **Admin → Data** page also lists everything the daily run
generates for download, plus a `predictions.xlsx` of every prediction,
built fresh when downloaded.

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
        "gpt-5.1": {"input_per_million": null, "output_per_million": null},
        "gemini-3.8-flash": {"input_per_million": null, "output_per_million": null}
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
| `py Collect.py usage` | Print each AI's logged token usage and estimated spend |
| `py Collect.py costs [file.xlsx]` | Refresh the Excel export by hand (`api_costs.xlsx` by default) |

## Other tools

| Command | What it does |
|---|---|
| `py Collect.py collect [N]` | Ask the AIs about up to N open games (enabled leagues) and store answers in `predictions.db` |
| `py Collect.py settle` | Record results for finished games in `predictions.db` |
| `py Collect.py weights` | Show each AI's accuracy (Brier score) and resulting weight |
| `py Collect.py export [file.xlsx]` | Export `predictions.db` to Excel |
| `py scripts.py probability [league]` | Run the full expected-value analysis on one open game (default NBA) |
| `py scripts.py kalshi` | List open game markets in every league with implied probabilities |
| `py scripts.py ev` | EVCalculator worked example with made-up numbers |
| `py scripts.py gemini` | Ask Gemini one sample prediction question |

## Project layout

| File | Purpose |
|---|---|
| `DailyLog.py` | The daily data-collection run described above |
| `scripts.py` | Ad-hoc demos for poking at one piece of the pipeline by hand |
| `Kalshi.py` | Kalshi API: markets, prices, today's games, current odds, results (any league) |
| `Leagues.py`, `leagues.json` | The tracked leagues: Kalshi series, season calendars, which ones are enabled |
| `ClaudeAi.py`, `OpenAi.py`, `Gemini.py` | One wrapper per AI API |
| `AISettings.py`, `ai_settings.json` | Which model each AI uses |
| `Accounts.py` | Users, their own settings (betting rules, AI weights) and encrypted Kalshi keys; the first admin |
| `Optimizer.py` | The Analytics tab's optimizer and diagnostics: weight fitting, rule search, walk-forward checks |
| `AIWeights.py`, `ai_weights.json` | Each AI's weight for the AI-odds consensus: site defaults, and each user's own |
| `Betting.py`, `betting.json` | Per-user betting: which games get a bet, its size and Kalshi fee, placing it, and its result (`betting.json` only seeds the first admin) |
| `Probability.py` | Builds the prompt, parses AI replies, runs the EV analysis |
| `EVCalculator.py` | Confidence-weighted expected-value score vs. the market price, and the AI-odds consensus probability |
| `Tracker.py`, `Export.py`, `Collect.py` | SQLite prediction tracking (the webapp's data source too), accuracy weights, Excel export |
| `webapp/` | FastAPI app for browsing tracked markets and AI-vs-Kalshi comparisons |
| `SeasonType.py` | Derives a game's season (e.g. "2026-27" or "2026") and segment (preseason/regular/postseason) from its date and league |
| `MigrateDailyLogs.py` | One-time backfill of `daily_logs/*.xlsx` into `predictions.db` |
| `Notifier.py` | Emails an alert when an AI API call looks like a billing/quota failure |
| `CostTracker.py`, `CostExport.py`, `model_pricing.json` | Token-based API spend estimate and Excel export |
| `ExcelUtil.py` | Shared workbook-writing helper used by `Export.py` and `CostExport.py` |
| `schedule_task.ps1` | Registers the Windows Task Scheduler task that runs `DailyLog.py` daily (when not hosted) |
| `trigger_daily_run.py` | Railway cron service's start command: asks the web service to do the daily run |
| `DataDir.py` | Where generated data lives (`DATA_DIR`, e.g. a Railway volume) |

## Tests

```powershell
py -m unittest discover tests
```

All tests mock the network, so they need no keys and cost nothing. The AI tests
also include opt-in live checks against the real APIs; these make small paid
calls:

```powershell
$env:RUN_LIVE_AI_TESTS = "1"; py -m unittest tests.test_ai_apis -v
```
