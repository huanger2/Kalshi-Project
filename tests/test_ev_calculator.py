import unittest

from EVCalculator import EVCalculator, LLMPrediction


def preds(*triples):
    return [LLMPrediction(name, p, c) for name, p, c in triples]


class LLMPredictionValidationTests(unittest.TestCase):
    def test_rejects_probability_outside_unit_interval(self):
        with self.assertRaises(ValueError):
            LLMPrediction("x", 1.2, 0.5)
        with self.assertRaises(ValueError):
            LLMPrediction("x", -0.1, 0.5)

    def test_rejects_confidence_outside_unit_interval(self):
        with self.assertRaises(ValueError):
            LLMPrediction("x", 0.5, 1.5)

    def test_rejects_negative_weight(self):
        with self.assertRaises(ValueError):
            LLMPrediction("x", 0.5, 0.5, weight=-1)


class EVCalculatorTests(unittest.TestCase):
    def test_hand_computed_ev(self):
        # contributions: .17*.8 + .13*.75 + .06*.6 = .2695 ; ev = .2695 / 3
        p = preds(("a", 0.72, 0.80), ("b", 0.68, 0.75), ("c", 0.61, 0.60))
        result = EVCalculator().calculate(55, p)
        self.assertAlmostEqual(result.ev, 0.2695 / 3)
        self.assertAlmostEqual(result.kalshi_prob, 0.55)

    def test_llms_above_market_favour_yes(self):
        result = EVCalculator().calculate(0.40, preds(("a", 0.70, 1.0)))
        self.assertEqual(result.side, "YES")
        self.assertEqual(result.consensus_direction, "under")

    def test_llms_below_market_favour_no(self):
        result = EVCalculator().calculate(0.70, preds(("a", 0.40, 1.0)))
        self.assertEqual(result.side, "NO")
        self.assertEqual(result.consensus_direction, "over")

    def test_ev_is_symmetric_between_the_two_sides(self):
        # The two outcomes always sum to 1, so viewing the same game from the other
        # team's market (every probability flipped) must give the same score and
        # the opposite side.
        yes_view = EVCalculator().calculate(0.40, preds(("a", 0.70, 0.9), ("b", 0.60, 0.5)))
        no_view = EVCalculator().calculate(0.60, preds(("a", 0.30, 0.9), ("b", 0.40, 0.5)))
        self.assertAlmostEqual(yes_view.ev, no_view.ev)
        self.assertEqual(yes_view.side, "YES")
        self.assertEqual(no_view.side, "NO")

    def test_no_edge_gives_zero_ev(self):
        result = EVCalculator().calculate(0.55, preds(("a", 0.55, 1.0)))
        self.assertAlmostEqual(result.ev, 0.0)

    def test_disagreeing_llms_cancel_out(self):
        result = EVCalculator().calculate(0.50, preds(("a", 0.70, 1.0), ("b", 0.30, 1.0)))
        self.assertAlmostEqual(result.ev, 0.0)

    def test_zero_confidence_contributes_nothing(self):
        result = EVCalculator().calculate(0.50, preds(("a", 0.99, 0.0)))
        self.assertAlmostEqual(result.ev, 0.0)

    def test_weights_dict_overrides_prediction_weight(self):
        p = [LLMPrediction("a", 0.80, 1.0, weight=1.0)]
        full = EVCalculator().calculate(0.50, p)
        half = EVCalculator(weights={"a": 0.5}).calculate(0.50, p)
        self.assertAlmostEqual(half.ev, full.ev / 2)

    def test_zero_weight_llm_is_ignored_in_the_sum(self):
        p = preds(("good", 0.70, 1.0), ("bad", 0.10, 1.0))
        result = EVCalculator(weights={"good": 1.0, "bad": 0.0}).calculate(0.50, p)
        self.assertEqual(result.side, "YES")
        self.assertAlmostEqual(result.ev, 0.20 / 2)

    def test_cents_and_probability_inputs_are_equivalent(self):
        p = preds(("a", 0.70, 1.0))
        self.assertAlmostEqual(
            EVCalculator().calculate(55, p).ev,
            EVCalculator().calculate(0.55, p).ev,
        )

    def test_one_cent_is_not_mistaken_for_certainty(self):
        result = EVCalculator().calculate(1, preds(("a", 0.50, 1.0)))
        self.assertAlmostEqual(result.kalshi_prob, 0.01)

    def test_out_of_range_price_raises(self):
        with self.assertRaises(ValueError):
            EVCalculator().calculate(150, preds(("a", 0.5, 1.0)))

    def test_empty_predictions_raise(self):
        with self.assertRaises(ValueError):
            EVCalculator().calculate(0.5, [])

    def test_should_bet_respects_threshold(self):
        p = preds(("a", 0.60, 1.0))  # ev = 0.10
        bet, _ = EVCalculator().should_bet(0.50, p, threshold=0.05)
        self.assertTrue(bet)
        bet, _ = EVCalculator().should_bet(0.50, p, threshold=0.20)
        self.assertFalse(bet)

    def test_str_renders_side_and_each_llm(self):
        text = str(EVCalculator().calculate(0.5, preds(("alpha", 0.7, 0.8))))
        self.assertIn("alpha", text)
        self.assertIn("Favoured side:    YES", text)


if __name__ == "__main__":
    unittest.main()
