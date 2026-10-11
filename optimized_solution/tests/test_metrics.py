"""Local cost estimate from the published Qwen prices."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from metrics import Metrics


class MetricsTest(unittest.TestCase):
    def test_published_qwen_prices(self):
        metrics = Metrics()
        metrics.add_llm(input_tokens=1_000_000, output_tokens=1_000_000, cached_tokens=0, latency_s=1.5, estimated=False)
        self.assertEqual(metrics.estimated_cost_usd, __import__("decimal").Decimal("0.15") + __import__("decimal").Decimal("0.47"))
        metrics.add_llm(input_tokens=1_000_000, output_tokens=0, cached_tokens=1_000_000, latency_s=0.2, estimated=False)
        fresh = metrics.estimated_cost_usd - __import__("decimal").Decimal("0.62")
        self.assertEqual(fresh, __import__("decimal").Decimal("0.016"))
        report = metrics.report()
        self.assertEqual(report["llm_calls"], 2)
        self.assertEqual(report["input_tokens"], 2_000_000)
        self.assertIn("elapsed_s", report)
        self.assertFalse(report["submitted"])


if __name__ == "__main__":
    unittest.main()
