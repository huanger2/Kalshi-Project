import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import CostTracker


def with_pricing(pricing: dict):
    """Patch CostTracker to read `pricing` instead of the real model_pricing.json."""
    return mock.patch.object(CostTracker, "_load_pricing", return_value=pricing)


class PricingLookupTests(unittest.TestCase):
    def test_known_model_returns_its_prices(self):
        with with_pricing({"gpt-x": {"input_per_million": 1.0, "output_per_million": 2.0}}):
            self.assertEqual(CostTracker.get_pricing("gpt-x"), (1.0, 2.0))

    def test_unknown_model_returns_none_none(self):
        with with_pricing({}):
            self.assertEqual(CostTracker.get_pricing("nope"), (None, None))

    def test_model_present_but_still_null_returns_none_none(self):
        # This is model_pricing.json's shipped default before anyone fills it in.
        with with_pricing({"gpt-x": {"input_per_million": None, "output_per_million": None}}):
            self.assertEqual(CostTracker.get_pricing("gpt-x"), (None, None))

    def test_malformed_pricing_file_raises_loudly(self):
        bad = Path(tempfile.mktemp(suffix=".json"))
        bad.write_text("{not valid json", encoding="utf-8")
        try:
            with mock.patch.object(CostTracker, "PRICING_PATH", bad):
                with self.assertRaises(ValueError):
                    CostTracker._load_pricing()
        finally:
            bad.unlink()


class EstimateCostTests(unittest.TestCase):
    def test_computes_cost_from_both_token_counts(self):
        with with_pricing({"m": {"input_per_million": 2.0, "output_per_million": 10.0}}):
            # 1000 in * $2/1M + 500 out * $10/1M = 0.002 + 0.005 = 0.007
            self.assertAlmostEqual(CostTracker.estimate_cost("m", 1000, 500), 0.007)

    def test_unpriced_model_returns_none(self):
        with with_pricing({}):
            self.assertIsNone(CostTracker.estimate_cost("m", 1000, 500))

    def test_missing_token_counts_return_none(self):
        with with_pricing({"m": {"input_per_million": 2.0, "output_per_million": 10.0}}):
            self.assertIsNone(CostTracker.estimate_cost("m", None, 500))
            self.assertIsNone(CostTracker.estimate_cost("m", 1000, None))

    def test_partially_priced_model_returns_none_not_a_half_estimate(self):
        with with_pricing({"m": {"input_per_million": 2.0, "output_per_million": None}}):
            self.assertIsNone(CostTracker.estimate_cost("m", 1000, 500))


class RecordAndSummarizeTests(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.mktemp(suffix=".db")
        self.addCleanup(lambda: Path(self.db).unlink(missing_ok=True))

    def test_record_usage_is_retrievable_in_the_summary(self):
        with with_pricing({"m": {"input_per_million": 2.0, "output_per_million": 10.0}}):
            CostTracker.record_usage("OpenAI", "m", 1000, 500, context="game 1", db_path=self.db)
        summary = CostTracker.usage_summary(self.db)
        self.assertEqual(summary, [
            {"provider": "OpenAI", "calls": 1, "input_tokens": 1000, "output_tokens": 500,
             "cost_usd": 0.007, "unpriced_calls": 0},
        ])

    def test_multiple_calls_for_one_provider_are_summed(self):
        with with_pricing({"m": {"input_per_million": 1.0, "output_per_million": 1.0}}):
            CostTracker.record_usage("OpenAI", "m", 100, 100, db_path=self.db)
            CostTracker.record_usage("OpenAI", "m", 200, 200, db_path=self.db)
        summary = {r["provider"]: r for r in CostTracker.usage_summary(self.db)}
        self.assertEqual(summary["OpenAI"]["calls"], 2)
        self.assertEqual(summary["OpenAI"]["input_tokens"], 300)
        self.assertEqual(summary["OpenAI"]["output_tokens"], 300)
        self.assertAlmostEqual(summary["OpenAI"]["cost_usd"], 0.0006)

    def test_providers_are_tracked_separately(self):
        with with_pricing({}):
            CostTracker.record_usage("OpenAI", "m", 100, 100, db_path=self.db)
            CostTracker.record_usage("Claude", "m", 50, 50, db_path=self.db)
        summary = {r["provider"] for r in CostTracker.usage_summary(self.db)}
        self.assertEqual(summary, {"OpenAI", "Claude"})

    def test_unpriced_calls_are_counted_and_cost_is_none(self):
        with with_pricing({}):
            CostTracker.record_usage("Gemini", "m", 100, 100, db_path=self.db)
            CostTracker.record_usage("Gemini", "m", 100, 100, db_path=self.db)
        row = CostTracker.usage_summary(self.db)[0]
        self.assertIsNone(row["cost_usd"])
        self.assertEqual(row["unpriced_calls"], 2)

    def test_empty_database_returns_an_empty_list(self):
        self.assertEqual(CostTracker.usage_summary(self.db), [])

    def test_record_usage_never_raises_even_if_the_db_path_is_unwritable(self):
        bad_path = str(Path(tempfile.mkdtemp()) / "no" / "such" / "dir" / "x.db")
        try:
            CostTracker.record_usage("OpenAI", "m", 100, 100, db_path=bad_path)
        except Exception as e:  # pragma: no cover - this is exactly what must not happen
            self.fail(f"record_usage raised: {e}")


if __name__ == "__main__":
    unittest.main()
