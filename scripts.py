"""
scripts.py

Small ad-hoc demos for poking at one piece of the pipeline by hand. Each
was originally the `if __name__ == "__main__":` block of the module it
demos; they're collected here so the library modules themselves stay free
of script-only code.

(DailyLog.py and Collect.py are the project's real CLI entrypoints and stay
where they are -- these are just quick manual checks.)

    py scripts.py kalshi        list open NBA game markets with implied probabilities
    py scripts.py ev            EVCalculator worked example with made-up numbers
    py scripts.py gemini        ask Gemini one sample prediction question
    py scripts.py probability   full EV analysis on one real open Kalshi market
"""

import sys


def kalshi_demo() -> None:
    """List open NBA game markets with implied probabilities."""
    from Kalshi import NBA_GAME_SERIES, get_all_markets, implied_yes_prob

    for m in get_all_markets(market=NBA_GAME_SERIES):
        print(m["ticker"], "|", m["title"], "|", f"{implied_yes_prob(m):.0%}")


def ev_calculator_demo() -> None:
    """EVCalculator worked example: Cavaliers vs Pistons at 55 cents, three made-up LLM opinions."""
    from EVCalculator import EVCalculator, LLMPrediction

    # Finetuned historical weights per LLM (learned offline from past accuracy)
    historical_weights = {
        "gpt-4o":        1.0,
        "claude-sonnet": 1.0,
        "gemini-pro":    1.0,
    }

    calculator = EVCalculator(weights=historical_weights)

    # Cavaliers vs Pistons — Kalshi has Cavs winning at 55 cents
    kalshi_price = 55  # cents

    predictions = [
        LLMPrediction(llm_name="gpt-4o",        predicted_prob=0.72, confidence=0.80),
        LLMPrediction(llm_name="claude-sonnet",  predicted_prob=0.68, confidence=0.75),
        LLMPrediction(llm_name="gemini-pro",     predicted_prob=0.61, confidence=0.60),
    ]

    threshold = 0.05

    bet, result = calculator.should_bet(kalshi_price, predictions, threshold)

    print(result)
    print()
    print(f"Threshold:        {threshold}")
    print(f"Bet recommended:  {'YES' if bet else 'NO'}")


def gemini_demo() -> None:
    """Ask Gemini one sample prediction question."""
    from Gemini import ask_gemini

    print(ask_gemini(
        "What is the probability that the United States win the world cup? Also provide a "
        "precentage corresponding to how confident you are in that prediction. Answer in the "
        "form of'Probability: X%, Confidence: Y%'"
    ))


def probability_demo() -> None:
    """Full EV analysis on one real open Kalshi market, using accuracy weights learned so far."""
    import Tracker
    from Kalshi import NBA_GAME_SERIES, get_all_markets
    from Probability import analyze_market, list_markets, one_market_per_event

    list_markets(series=NBA_GAME_SERIES, limit=10)

    markets = one_market_per_event(get_all_markets(market=NBA_GAME_SERIES))
    if not markets:
        raise RuntimeError(f"No open {NBA_GAME_SERIES} markets found.")
    market = markets[0]

    conn = Tracker.connect()
    weights = Tracker.compute_weights(conn)

    analyze_market(
        market=market,
        threshold=0.05,
        historical_weights=weights or None,
        verbose=True,
        db_conn=conn,
    )


DEMOS = {
    "kalshi": kalshi_demo,
    "ev": ev_calculator_demo,
    "gemini": gemini_demo,
    "probability": probability_demo,
}


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else None
    if name not in DEMOS:
        sys.exit(__doc__)
    DEMOS[name]()
