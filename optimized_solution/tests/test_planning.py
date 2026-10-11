"""Action parsing and bid-form value lookup. Invented figures only."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kernel import Gap
from planning import batch_until_navigation, german_money, group_gaps, is_sign_control, lookup_value, parse_actions


class Planning(unittest.TestCase):
    def test_batch_stops_when_the_page_would_change(self):
        actions = parse_actions('{"actions":[{"op":"fill","target":1,"text":"abc"},{"op":"click","target":2},{"op":"extract"}]}')
        self.assertEqual([item.op for item in batch_until_navigation(actions)], ["fill", "click"])

    def test_german_money_and_value_keys(self):
        self.assertEqual(german_money("1234.5"), "1.234,50")
        answer = {"tender_reference": "T-1", "decision": "bid", "currency": "EUR", "discount_percent": 5,
                  "total_net": 10, "positions": [{"position": "1", "quantity": 3, "unit_price": 96.67}]}
        self.assertEqual(lookup_value(answer, None, "position.1.unit_price"), "96,67")
        self.assertEqual(lookup_value(answer, None, "position.1.quantity"), "3")
        self.assertEqual(lookup_value(answer, "4821", "tan"), "4821")
        self.assertIsNone(lookup_value(answer, None, "position.9.unit_price"))

    def test_sign_guard_and_gap_grouping(self):
        self.assertTrue(is_sign_control("Verbindlich einreichen"))
        self.assertFalse(is_sign_control("Suche starten"))
        grouped = group_gaps([Gap("erp", "open the material"), Gap("erp", "open the material"), Gap("mail", "find the TAN")])
        self.assertEqual(grouped["erp"], ["open the material"])
        self.assertEqual(list(grouped), ["erp", "mail"])


if __name__ == "__main__":
    unittest.main()
