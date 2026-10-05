from dataclasses import dataclass
from typing import Optional


@dataclass
class LLMPrediction:
    """A single LLM's prediction for a market outcome."""
    llm_name: str
    predicted_prob: float        # p_i: LLM's predicted probability (0-1)
    confidence: float            # c_i: LLM's confidence in its prediction (0-1)
    weight: float = 1.0          # w_i: finetuned historical weight (default equal weighting)

    def __post_init__(self):
        # Explicit raises (not asserts) so validation survives `python -O`.
        if not 0 <= self.predicted_prob <= 1:
            raise ValueError(f"predicted_prob must be in [0, 1], got {self.predicted_prob}")
        if not 0 <= self.confidence <= 1:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")
        if self.weight < 0:
            raise ValueError(f"weight must be >= 0, got {self.weight}")


@dataclass
class EVResult:
    """Result of an EV calculation."""
    ev: float
    kalshi_prob: float
    predictions: list[LLMPrediction]
    contributions: list[dict]     # per-LLM breakdown
    consensus_direction: str      # "over" or "under" relative to Kalshi
    side: str = "YES"             # which side of the market the edge favours: "YES" or "NO"

    def __str__(self):
        lines = [
            f"EV Score:         {self.ev:.4f}",
            f"Kalshi Prob:      {self.kalshi_prob:.2%}",
            f"Consensus:        Market is {self.consensus_direction}priced vs LLMs",
            f"Favoured side:    {self.side}",
            f"",
            f"{'LLM':<20} {'p_i':>6} {'c_i':>6} {'w_i':>6} {'contribution':>14}",
            "-" * 58,
        ]
        for c in self.contributions:
            lines.append(
                f"{c['llm']:<20} {c['predicted_prob']:>6.2%} {c['confidence']:>6.2%} "
                f"{c['weight']:>6.2f} {c['contribution']:>14.4f}"
            )
        return "\n".join(lines)


class EVCalculator:
    """
    Calculates expected value of a prediction market position.

    Formula:
        EV = (1/N) * |sum_i( (p_i - p_k) * c_i * w_i )|

    Where:
        p_i = LLM i's predicted probability
        p_k = Kalshi's implied probability
        c_i = LLM i's confidence in its prediction
        w_i = finetuned historical weight for LLM i
        N   = number of LLMs
    """

    def __init__(self, weights: Optional[dict[str, float]] = None):
        """
        Args:
            weights: Optional dict of {llm_name: weight} for historical finetuned weights.
                     If provided, overrides the weight set on each LLMPrediction.
        """
        self.weights = weights or {}

    def calculate(self, kalshi_price: float, predictions: list[LLMPrediction]) -> EVResult:
        """
        Calculate EV for a market.

        Args:
            kalshi_price: Kalshi YES price in cents (1-99) or as a probability (0-1).
                          Values >= 1 are treated as cents, so 1 means 1 cent, not 100%
                          (a market already at 100% is resolved and can't be bet on).
            predictions:  List of LLMPrediction objects. Each predicted_prob is the
                          probability of the YES outcome; NO is always 1 - YES.

        Returns:
            EVResult with EV score, favoured side and per-LLM breakdown.
        """
        if not predictions:
            raise ValueError("Must provide at least one LLM prediction.")

        # Normalize Kalshi price to probability
        p_k = kalshi_price / 100 if kalshi_price >= 1 else kalshi_price
        if not 0 <= p_k <= 1:
            raise ValueError(f"kalshi_price out of range: {kalshi_price}")

        N = len(predictions)
        total = 0.0
        contributions = []

        for pred in predictions:
            # Use finetuned weight if available, otherwise use prediction's own weight
            w_i = self.weights.get(pred.llm_name, pred.weight)
            p_i = pred.predicted_prob
            c_i = pred.confidence

            contribution = (p_i - p_k) * c_i * w_i
            total += contribution

            contributions.append({
                "llm": pred.llm_name,
                "predicted_prob": p_i,
                "confidence": c_i,
                "weight": w_i,
                "contribution": contribution,
            })

        ev = abs(total) / N

        # total > 0: LLMs think YES is likelier than the market does (YES underpriced).
        # Otherwise YES is overpriced, which is the same as NO being underpriced
        # (the two sides always sum to 1), so the edge is on NO.
        consensus_direction = "under" if total > 0 else "over"
        side = "YES" if total > 0 else "NO"

        return EVResult(
            ev=ev,
            kalshi_prob=p_k,
            predictions=predictions,
            contributions=contributions,
            consensus_direction=consensus_direction,
            side=side,
        )

    def should_bet(self, kalshi_price: float, predictions: list[LLMPrediction], threshold: float) -> tuple[bool, EVResult]:
        """
        Returns whether EV exceeds the threshold, and the full EVResult.

        Args:
            kalshi_price: Kalshi market price (cents or probability).
            predictions:  List of LLMPrediction objects.
            threshold:    Minimum EV score to trigger a bet.
        """
        result = self.calculate(kalshi_price, predictions)
        return result.ev >= threshold, result
