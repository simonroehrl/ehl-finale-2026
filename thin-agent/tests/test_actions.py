import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

from actions import lookup_value, parse_actions, render_turn  # noqa: E402


class Actions(unittest.TestCase):
    def test_parses_a_short_batch(self):
        actions = parse_actions('{"actions":[{"op":"fill","target":1,"text":"u"},{"op":"click","target":2}]}')
        self.assertEqual([a.op for a in actions], ["fill", "click"])
        self.assertEqual(actions[0].text, "u")

    def test_lookup_formats_german_money(self):
        answer = {"tender_reference": "AB-2031-9", "discount_percent": 3.5,
                  "positions": [{"position": "01.01", "unit_price": 12.5, "quantity": 3}]}
        self.assertEqual(lookup_value(answer, None, "tender_reference"), "AB-2031-9")
        self.assertEqual(lookup_value(answer, None, "position.01.01.unit_price"), "12,50")
        self.assertEqual(lookup_value(answer, None, "position.01.01.quantity"), "3")
        self.assertEqual(lookup_value(answer, "T1", "tan"), "T1")

    def test_turn_stays_compact(self):
        text = render_turn("find positions", "positions: 01.01", {
            "url": "https://tender.example/", "title": "LV",
            "nodes": [{"i": 1, "role": "button", "name": "Weiter"}],
            "text": "01.01 10 Stk",
        }, "")
        self.assertIn("[1] button Weiter", text)
        self.assertIn("KNOWN FACTS", text)
        self.assertNotIn("kyora", text)
        self.assertNotIn("Lastenheft", text)


if __name__ == "__main__":
    unittest.main()
