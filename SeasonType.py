"""
SeasonType.py

Two independent things you can know about a game date in a given league,
neither of which Kalshi's market data labels itself (checked directly
against the live API: a KXNBAGAME market has no category/game_type/season
field, just the two teams, a date, and generic "Pro Basketball game" rules
text -- and the NFL/NHL/MLB series are the same):

    season_year(day, league)  -- which season, e.g. "2026-27" (NBA/NHL) or "2026" (NFL/MLB)
    segment(day, league)      -- "preseason", "regular", or "postseason"

Both are pure functions of the date and league, so neither is stored
anywhere -- a query always reflects the current leagues.json, never a
stale snapshot from whenever a row was inserted. `league` defaults to
Leagues.DEFAULT_LEAGUE (NBA), the only league tracked before the others
were added.

segment() defaults to each league's generic calendar heuristic (see
Leagues.py) that holds approximately for most seasons. Fill in
leagues.json's "seasons" with a season's actual published start dates for
exact boundaries; read fresh on every call, like ai_settings.json, so an
edit takes effect without restarting anything.
"""

import datetime
from typing import Optional

import Leagues


def season_year(day: datetime.date, league: Optional[str] = None) -> str:
    """Which season a date belongs to, e.g. "2026-27" for an NBA game in Oct 2026 - Jun 2027."""
    lg = Leagues.get(league)
    return lg.season_label(lg.season_start_year(day))


def season_year_iso(game_date: Optional[str], league: Optional[str] = None) -> Optional[str]:
    """Same as season_year(), but takes (and tolerates a missing/blank) ISO date string."""
    if not game_date:
        return None
    return season_year(datetime.date.fromisoformat(game_date), league)


def _config_date(value: str, league: str, key: str) -> datetime.date:
    """
    A hand-typed leagues.json date. Accepts "2026-10-7" as well as
    "2026-10-07" (fromisoformat alone rejects the former); anything else
    raises an error naming the entry, rather than a bare parse failure deep
    inside a SQL query.
    """
    try:
        year, month, day = (int(part) for part in value.split("-"))
        return datetime.date(year, month, day)
    except (ValueError, AttributeError):
        raise ValueError(
            f"{Leagues.CONFIG_PATH.name}: {league} {key} {value!r} isn't a date like \"2026-10-07\""
        ) from None


def segment(day: datetime.date, league: Optional[str] = None) -> str:
    """"preseason", "regular", or "postseason" -- which part of its season `day` falls in."""
    lg = Leagues.get(league)
    start_year = lg.season_start_year(day)
    overrides = Leagues.season_overrides(lg.code, lg.season_label(start_year))
    regular_start, postseason_start = lg.default_boundaries(start_year)
    if overrides.get("regular_season_start"):
        regular_start = _config_date(overrides["regular_season_start"], lg.code, "regular_season_start")
    if overrides.get("postseason_start"):
        postseason_start = _config_date(overrides["postseason_start"], lg.code, "postseason_start")

    if day < regular_start:
        return "preseason"
    if day < postseason_start:
        return "regular"
    return "postseason"


def segment_iso(game_date: Optional[str], league: Optional[str] = None) -> Optional[str]:
    """Same as segment(), but takes (and tolerates a missing/blank) ISO date string."""
    if not game_date:
        return None
    return segment(datetime.date.fromisoformat(game_date), league)
