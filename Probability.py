"""
Probability.py

Fetches a Kalshi market, queries OpenAI / Claude / Gemini for their
probability and confidence on the YES outcome, then runs the EVCalculator
to determine whether the market is worth betting on.
"""

import re
from typing import Callable, Optional

from EVCalculator import EVCalculator, LLMPrediction
from Kalshi import NBA_GAME_SERIES, get_all_markets, get_prices, implied_yes_prob


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """You are a prediction market analyst. A Kalshi prediction market asks:

"{question}"
{rules}
The market resolves YES or NO. Based on your knowledge, estimate:
1. The probability that this market resolves YES (0–100%).
2. Your confidence in that estimate (0–100%), where 100% means you are certain
   and 0% means you have no idea.

Respond ONLY in this exact format (no extra text):
Probability: X%
Confidence: Y%"""


def build_prompt(market: dict) -> str:
    """
    Build the LLM prompt for a market.

    NBA game markets come as one market per team and share an event, so the
    title alone ("San Antonio wins") is ambiguous without the rules text that
    names the game; include it whenever Kalshi provides it.
    """
    question = market["title"]
    rules_text = market.get("rules_primary")
    rules = f"\nResolution rules: {rules_text}\n" if rules_text else ""
    return PROMPT_TEMPLATE.format(question=question, rules=rules)


def parse_response(response: str, llm_name: str) -> tuple[float, float]:
    """
    Parse 'Probability: X%\\nConfidence: Y%' from an LLM response.
    Returns (probability, confidence) as floats in [0, 1].
    Raises ValueError if parsing fails.

    The '%' is required on purpose: without it "0.72" would be indistinguishable
    from 0.72% and silently record a wildly wrong prediction.
    """
    prob_match = re.search(r"Probability[:\s]+([0-9]+(?:\.[0-9]+)?)\s*%", response, re.IGNORECASE)
    conf_match = re.search(r"Confidence[:\s]+([0-9]+(?:\.[0-9]+)?)\s*%", response, re.IGNORECASE)

    if not prob_match or not conf_match:
        raise ValueError(
            f"[{llm_name}] Could not parse response:\n{response}"
        )

    probability = float(prob_match.group(1)) / 100.0
    confidence  = float(conf_match.group(1)) / 100.0

    # Clamp to [0, 1] just in case an LLM returns e.g. 101%
    probability = max(0.0, min(1.0, probability))
    confidence  = max(0.0, min(1.0, confidence))

    return probability, confidence


def default_llms() -> list[tuple[str, Callable[[str], str]]]:
    """The (name, ask_function) pairs queried by default."""
    from OpenAi import ask_openai
    from ClaudeAi import ask_claude
    from Gemini import ask_gemini

    return [
        ("OpenAI", ask_openai),
        ("Claude", ask_claude),
        ("Gemini", ask_gemini),
    ]


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def entry_price(market: dict, side: str) -> Optional[float]:
    """
    Price (as a probability) you would actually pay to buy `side` right now,
    i.e. the ask, not the last trade. None if the book has no ask for it.
    """
    prices = get_prices(market)
    return prices["yes_ask"] if side == "YES" else prices["no_ask"]


def query_llms(
    market: dict,
    llms: list[tuple[str, Callable[[str], str]]],
    verbose: bool = True,
) -> list[LLMPrediction]:
    """Ask each LLM about the market; LLMs that error or reply unparseably are skipped."""
    prompt = build_prompt(market)
    predictions: list[LLMPrediction] = []

    for llm_name, ask_fn in llms:
        try:
            raw = ask_fn(prompt)
            if verbose:
                print(f"[{llm_name}] raw response:\n  {raw.strip()}\n")

            prob, conf = parse_response(raw, llm_name)
            predictions.append(
                LLMPrediction(
                    llm_name=llm_name,
                    predicted_prob=prob,
                    confidence=conf,
                )
            )
        except Exception as e:
            print(f"[{llm_name}] ERROR — skipping: {e}")

    return predictions


def analyze_market(
    market: dict,
    threshold: float = 0.05,
    historical_weights: dict[str, float] | None = None,
    verbose: bool = True,
    llms: list[tuple[str, Callable[[str], str]]] | None = None,
    db_conn=None,
) -> tuple[bool, object]:
    """
    Query all LLMs about a Kalshi market and run the EV calculation.

    Args:
        market:             A single market dict from the Kalshi API.
        threshold:          Minimum EV score to recommend a bet.
        historical_weights: Per-LLM accuracy weights in [0, 1] (defaults to equal 1.0).
        verbose:            Print results to stdout.
        llms:               (name, ask_fn) pairs to query. Defaults to OpenAI, Claude, Gemini.
        db_conn:            Optional Tracker connection; if given, every prediction is
                            stored so weights can be computed once the game settles.

    Returns:
        (bet_recommended: bool, ev_result: EVResult)
    """
    title  = market["title"]
    ticker = market["ticker"]
    p_yes  = implied_yes_prob(market)
    prices = get_prices(market)

    if verbose:
        def cents(p):
            return "n/a" if p is None else f"{p * 100:.0f}¢"

        print(f"\n{'='*60}")
        print(f"Market : {ticker}")
        print(f"Title  : {title}")
        print(f"YES bid/ask: {cents(prices['yes_bid'])} / {cents(prices['yes_ask'])}  |  "
              f"last price: {cents(prices['last'])}  |  implied YES: {p_yes:.0%}")
        print(f"{'='*60}\n")

    predictions = query_llms(market, llms or default_llms(), verbose=verbose)

    if not predictions:
        raise RuntimeError("All LLM queries failed; cannot calculate EV.")

    if db_conn is not None:
        import Tracker
        Tracker.record_predictions(db_conn, market, p_yes, predictions)

    # --- Run EV calculation ---
    weights    = historical_weights or {p.llm_name: 1.0 for p in predictions}
    calculator = EVCalculator(weights=weights)
    bet, result = calculator.should_bet(p_yes, predictions, threshold)

    if verbose:
        print(result)
        print()
        cost = entry_price(market, result.side)
        print(f"Entry price ({result.side}): {'n/a' if cost is None else f'{cost * 100:.0f}¢'}")
        print(f"Threshold:       {threshold}")
        print(f"Bet recommended: {'YES ✓' if bet else 'NO ✗'}")

    return bet, result


# ---------------------------------------------------------------------------
# Market selection helpers
# ---------------------------------------------------------------------------

def one_market_per_event(markets: list[dict]) -> list[dict]:
    """
    Keep the first market of each event.

    A game has two complementary markets (one per team) whose probabilities sum
    to 1, so querying both doubles LLM cost and logs perfectly correlated
    duplicate rows that would overstate the sample size.
    """
    seen = set()
    unique = []
    for m in markets:
        key = m.get("event_ticker") or m["ticker"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(m)
    return unique


def pick_market_by_ticker(ticker: str, series: str = NBA_GAME_SERIES) -> dict:
    """Fetch all open markets for a series and return the one matching ticker."""
    markets = get_all_markets(market=series)
    for m in markets:
        if m["ticker"] == ticker:
            return m
    raise ValueError(f"Ticker '{ticker}' not found in series '{series}'.")


def list_markets(series: str = NBA_GAME_SERIES, limit: int = 20) -> None:
    """Print open markets for a series so you can pick a ticker."""
    markets = get_all_markets(market=series)
    print(f"\nOpen markets for series '{series}' ({len(markets)} total, showing first {limit}):\n")
    for m in markets[:limit]:
        try:
            last = f"{implied_yes_prob(m) * 100:.0f}"
        except ValueError:
            last = "??"
        print(f"  {m['ticker']:<40} {last:>4}¢  |  {m['title']}")


# ---------------------------------------------------------------------------
# Example usage
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import Tracker

    list_markets(series=NBA_GAME_SERIES, limit=10)

    markets = one_market_per_event(get_all_markets(market=NBA_GAME_SERIES))
    if not markets:
        raise RuntimeError(f"No open {NBA_GAME_SERIES} markets found.")
    market = markets[0]

    conn = Tracker.connect()
    weights = Tracker.compute_weights(conn)

    bet, result = analyze_market(
        market=market,
        threshold=0.05,
        historical_weights=weights or None,
        verbose=True,
        db_conn=conn,
    )
