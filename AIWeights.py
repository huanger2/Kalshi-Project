"""
AIWeights.py

Manually adjustable per-AI weight for combining predictions into a single
consensus probability (see EVCalculator.consensus_probability(), used by
webapp/queries.py's "AI odds" column). This is a separate dial from
Tracker.compute_weights(), which is automatically derived from each AI's
Brier score over its settled games -- that's a measurement of accuracy, and
early on it can already swing far from a fair starting point (preseason
games are inherently noisier -- limited rotations, no real stakes --
regardless of an AI's actual skill, so a bad preseason Brier score doesn't
mean much yet). This is a plain, directly editable number instead: every AI
defaults to equal weighting (1.0) until you decide otherwise.

ai_weights.json holds the site-wide defaults (an admin edits them on the
Admin tab): what guests see and what a user starts from. Each signed-in
user can override them for themselves (for_user(), saved as their
"ai_weights" setting -- see Accounts.py), and their own weights drive both
the AI odds they're shown and the bets placed for them. The file is read
fresh on every call, so an edit takes effect without restarting anything.
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

import ConfigFiles

WEIGHTS_PATH = Path(__file__).with_name("ai_weights.json")

DEFAULT_WEIGHTS = {
    "claude": 1.0,
    "openai": 1.0,
    "gemini": 1.0,
}


def get_weight(llm_name: str) -> float:
    """
    This AI's configured weight ('OpenAI', 'claude', 'GEMINI' all work --
    matched case-insensitively against ai_weights.json). Unknown names and
    a missing file both fall back to 1.0, not an error, since this is only
    ever a display/combination input, not a correctness-critical lookup
    like AISettings.get_model().
    """
    try:
        settings = json.loads(ConfigFiles.effective(WEIGHTS_PATH).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return DEFAULT_WEIGHTS.get(llm_name.lower(), 1.0)
    except json.JSONDecodeError as e:
        raise ValueError(f"{WEIGHTS_PATH.name} is not valid JSON: {e}") from e

    weights = settings.get("weights", {})
    return weights.get(llm_name.lower(), DEFAULT_WEIGHTS.get(llm_name.lower(), 1.0))


def defaults() -> dict[str, float]:
    """The site-wide weight of every known AI (lower-case names)."""
    return {name: get_weight(name) for name in DEFAULT_WEIGHTS}


def for_user(conn: Optional[sqlite3.Connection], user_id: Optional[int]) -> dict[str, float]:
    """
    The weights in effect for one user: their own where they've set one,
    the site-wide default otherwise. No user (a guest): the defaults.
    """
    import Accounts

    weights = defaults()
    if conn is not None and user_id is not None:
        own = Accounts.get_setting(conn, user_id, "ai_weights") or {}
        weights.update({k.lower(): float(v) for k, v in own.items()})
    return weights


def lookup(weights: Optional[dict[str, float]], llm_name: str) -> float:
    """An AI's weight from a for_user()/defaults() dict (any capitalization), else the site default."""
    if weights is None:
        return get_weight(llm_name)
    return weights.get(llm_name.lower(), get_weight(llm_name))
