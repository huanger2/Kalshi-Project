"""
Leagues.py

The sports leagues this project tracks, and everything that differs between
them: which Kalshi series holds a league's per-game markets, and how its
calendar is laid out (which months a season covers, how it's labeled, and
the generic preseason/regular/postseason boundaries SeasonType.py falls
back on when leagues.json doesn't give a season's real dates).

Every league's per-game markets on Kalshi have the same shape (checked
directly against the live API) -- one binary "<Team> wins" market per team,
under an event ticker like KXNFLGAME-26OCT26DALPHI -- so nothing downstream
needs to know which league a market came from except to pick its series
and its calendar.

The one behavioral difference: an NFL game can end in a tie (Kalshi
settles it as result "scalar", see Kalshi.is_tie()), which this project
treats as neither team winning -- see Tracker.settle(). NBA, NHL and MLB
games always produce a winner.

Which leagues DailyLog.py and Collect.py query is set by "enabled" in
leagues.json, read fresh on every call like ai_settings.json -- drop a
league from the list to stop spending AI calls on it.
"""

import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

CONFIG_PATH = Path(__file__).with_name("leagues.json")

# Rows recorded before this project tracked more than one league -- and any
# ticker that matches no league's series (test fixtures use made-up ones) --
# are NBA's, the league it started with.
DEFAULT_LEAGUE = "NBA"


@dataclass(frozen=True)
class League:
    code: str
    series: str               # Kalshi series of per-game winner markets
    season_start_month: int   # a date in this month or later belongs to the season starting that year
    two_year_label: bool      # "2026-27" (season crosses New Year's) vs. "2026"
    # start year -> (regular season start, postseason start), generic
    # calendar defaults used when leagues.json leaves a season's dates null.
    default_boundaries: Callable[[int], tuple[datetime.date, datetime.date]]

    def season_start_year(self, day: datetime.date) -> int:
        return day.year if day.month >= self.season_start_month else day.year - 1

    def season_label(self, start_year: int) -> str:
        return f"{start_year}-{str(start_year + 1)[-2:]}" if self.two_year_label else str(start_year)


def _fixed(regular: tuple[int, int], postseason: tuple[int, int], start_month: int):
    """Boundaries on fixed (month, day)s, each placed in whichever year keeps it inside the season."""
    def boundaries(start_year: int) -> tuple[datetime.date, datetime.date]:
        def place(month: int, day: int) -> datetime.date:
            return datetime.date(start_year if month >= start_month else start_year + 1, month, day)
        return place(*regular), place(*postseason)
    return boundaries


def _nfl_boundaries(start_year: int) -> tuple[datetime.date, datetime.date]:
    """
    The NFL calendar is formulaic enough to compute: kickoff is the Thursday
    after Labor Day (the first Monday of September), and Wild Card weekend
    starts the Saturday after week 18 -- 128 days after kickoff (e.g.
    Sep 4, 2025 -> Jan 10, 2026).
    """
    sep1 = datetime.date(start_year, 9, 1)
    labor_day = sep1 + datetime.timedelta(days=(0 - sep1.weekday()) % 7)
    kickoff = labor_day + datetime.timedelta(days=3)
    return kickoff, kickoff + datetime.timedelta(days=128)


LEAGUES: dict[str, League] = {
    league.code: league
    for league in [
        # Oct - Jun. Regular season ~Oct 21, play-in/playoffs ~Apr 14.
        League("NBA", "KXNBAGAME", 7, True, _fixed((10, 21), (4, 14), start_month=7)),
        # Aug (preseason) - Feb (Super Bowl).
        League("NFL", "KXNFLGAME", 3, False, _nfl_boundaries),
        # Sep (preseason) - Jun. Regular season ~Oct 7, playoffs ~Apr 18.
        League("NHL", "KXNHLGAME", 7, True, _fixed((10, 7), (4, 18), start_month=7)),
        # Feb (spring training) - early Nov, all one calendar year. Opening
        # day ~Mar 25, Wild Card Series ~Sep 29.
        League("MLB", "KXMLBGAME", 1, False, _fixed((3, 25), (9, 29), start_month=1)),
    ]
}


def get(code: Optional[str]) -> League:
    """The League for `code` ("NFL", "nfl"), or DEFAULT_LEAGUE's for a missing one."""
    return LEAGUES[(code or DEFAULT_LEAGUE).upper()]


def league_for_ticker(ticker: Optional[str]) -> Optional[str]:
    """Which league a market or event ticker belongs to (KXNFLGAME-26OCT26DALPHI -> "NFL"), or None."""
    for league in LEAGUES.values():
        if (ticker or "").startswith(league.series + "-"):
            return league.code
    return None


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        # Fail loudly: silently ignoring a typo would misclassify every game.
        raise ValueError(f"{CONFIG_PATH.name} is not valid JSON: {e}") from e


def enabled_leagues() -> list[str]:
    """
    The leagues to log each day, from leagues.json's "enabled" list (every
    league if the file or the key is missing). An unknown code raises
    rather than being skipped, so a typo doesn't silently stop a league
    from being tracked.
    """
    codes = load_config().get("enabled")
    if codes is None:
        return list(LEAGUES)
    unknown = [c for c in codes if c.upper() not in LEAGUES]
    if unknown:
        raise ValueError(f"{CONFIG_PATH.name} lists unknown league(s) {unknown}; known: {list(LEAGUES)}")
    return [c.upper() for c in codes]


def season_overrides(code: str, season: str) -> dict:
    """leagues.json's explicit boundary dates for one league's season, if any."""
    return load_config().get("seasons", {}).get(code, {}).get(season) or {}
