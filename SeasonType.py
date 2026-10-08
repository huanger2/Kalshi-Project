"""
SeasonType.py

Classifies an NBA game date as "preseason", "regular", or "postseason".

Kalshi's market data doesn't label this itself -- a KXNBAGAME market has no
category/game_type field, just the two teams, a date, and generic "Pro
Basketball game" rules text (checked directly against the live API). So
this is date-based instead.

Defaults to a generic calendar heuristic (regular season starts ~Oct 21,
postseason ~Apr 14) that holds approximately for most seasons but can be
off by a few days. Fill in nba_season.json with a season's actual
published start dates for exact boundaries; read fresh on every call, like
ai_settings.json, so an edit takes effect without restarting anything.
"""

import datetime
import json
from pathlib import Path
from typing import Optional

CONFIG_PATH = Path(__file__).with_name("nba_season.json")

# (month, day) -- generic, not tied to any specific season's actual calendar.
DEFAULT_REGULAR_SEASON_START = (10, 21)
DEFAULT_POSTSEASON_START = (4, 14)


def season_key(day: datetime.date) -> str:
    """The season a date belongs to, e.g. "2026-27" for Oct 2026 - Jun 2027."""
    start_year = day.year if day.month >= 7 else day.year - 1
    return f"{start_year}-{str(start_year + 1)[-2:]}"


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        # Fail loudly: silently ignoring a typo would misclassify every game.
        raise ValueError(f"{CONFIG_PATH.name} is not valid JSON: {e}") from e


def _date_in_same_season(day: datetime.date, month: int, day_of_month: int) -> datetime.date:
    """Build `month`/`day_of_month` in whichever calendar year keeps it in `day`'s NBA season."""
    start_year = day.year if day.month >= 7 else day.year - 1
    # A season spans two calendar years (Oct Y - Jun Y+1): a boundary from
    # July-December falls in the season's start year, January-June in its
    # second calendar year.
    boundary_year = start_year if month >= 7 else start_year + 1
    return datetime.date(boundary_year, month, day_of_month)


def _boundary(day: datetime.date, overrides: dict, key: str, default_month_day: tuple[int, int]) -> datetime.date:
    override = (overrides or {}).get(key)
    if override:
        return datetime.date.fromisoformat(override)
    return _date_in_same_season(day, *default_month_day)


def classify(day: datetime.date) -> str:
    """"preseason", "regular", or "postseason" for the NBA season `day` falls in."""
    overrides = _load_config().get(season_key(day))
    regular_start = _boundary(day, overrides, "regular_season_start", DEFAULT_REGULAR_SEASON_START)
    postseason_start = _boundary(day, overrides, "postseason_start", DEFAULT_POSTSEASON_START)

    if day < regular_start:
        return "preseason"
    if day < postseason_start:
        return "regular"
    return "postseason"


def classify_iso(game_date: Optional[str]) -> Optional[str]:
    """Same as classify(), but takes (and tolerates a missing/blank) ISO date string."""
    if not game_date:
        return None
    return classify(datetime.date.fromisoformat(game_date))
