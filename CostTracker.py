"""
CostTracker.py

Estimates what each AI API call costs and logs it, so spend can be tracked
over time per provider.

None of OpenAI, Anthropic or Gemini expose a "remaining balance" through a
normal API key -- that only shows in their web consoles (and a real
per-request cost figure from OpenAI/Anthropic needs a separate Admin/
Organization API key, a different credential type from a regular project
key; Gemini has nothing comparable at all). Instead, every API response
already reports how many tokens it used, and multiplying that by a price
you supply gives the same number their billing is actually based on.

Pricing lives in model_pricing.json (USD per 1,000,000 tokens, matching how
providers publish prices) and is read fresh on every call, like
AISettings.py. A model missing from the file -- or still null, as it ships
by default -- records its token counts with an unknown ("None") cost rather
than silently assuming $0.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import Tracker

PRICING_PATH = Path(__file__).with_name("model_pricing.json")

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT    NOT NULL,
    provider      TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    cost_usd      REAL,             -- NULL if model_pricing.json has no price for this model
    context       TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_usage_provider ON api_usage (provider);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_pricing() -> dict:
    try:
        raw = json.loads(PRICING_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        # Fail loudly: silently ignoring a typo would misprice every call.
        raise ValueError(f"{PRICING_PATH.name} is not valid JSON: {e}") from e
    return raw.get("models", {})


def get_pricing(model: str) -> tuple[Optional[float], Optional[float]]:
    """(input $/1M tokens, output $/1M tokens) for `model`, or (None, None) if unpriced."""
    entry = _load_pricing().get(model) or {}
    return entry.get("input_per_million"), entry.get("output_per_million")


def estimate_cost(
    model: str, input_tokens: Optional[int], output_tokens: Optional[int]
) -> Optional[float]:
    """Estimated USD cost of one call, or None if pricing or token counts are unavailable."""
    if input_tokens is None or output_tokens is None:
        return None
    input_price, output_price = get_pricing(model)
    if input_price is None or output_price is None:
        return None
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def record_usage(
    provider: str,
    model: str,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    context: str = "",
    db_path: str = Tracker.DEFAULT_DB,
) -> None:
    """
    Log one API call's token usage and estimated cost.

    Never raises -- a logging failure shouldn't take down the AI call it's
    trying to measure; it prints a warning instead.
    """
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO api_usage "
                "(created_at, provider, model, input_tokens, output_tokens, cost_usd, context) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(), provider, model, input_tokens, output_tokens,
                    estimate_cost(model, input_tokens, output_tokens), context,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[CostTracker] failed to record usage for {provider}: {e}")


def usage_summary(db_path: str = Tracker.DEFAULT_DB) -> list[dict]:
    """
    Per-provider totals since tracking started: call count, token totals, known
    cost (None if every call for that provider is unpriced), and how many
    calls had no price to estimate from.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        rows = conn.execute(
            "SELECT provider, COUNT(*), "
            "       SUM(input_tokens), SUM(output_tokens), SUM(cost_usd), "
            "       SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) "
            "FROM api_usage GROUP BY provider ORDER BY provider"
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "provider": provider,
            "calls": calls,
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
            "cost_usd": cost_usd,
            "unpriced_calls": unpriced,
        }
        for provider, calls, input_tokens, output_tokens, cost_usd, unpriced in rows
    ]
