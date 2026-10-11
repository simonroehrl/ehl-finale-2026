"""Submit helpers. Invented references and codes only."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

from submit_helpers import (bid_values, extract_tan, german, is_sign_label,  # noqa: E402
                            phase_deadlines, remaining_credit, require_credit)


class GermanMoney(unittest.TestCase):
    def test_thousands_and_cents(self):
        self.assertEqual(german("1234.5"), "1.234,50")
        self.assertEqual(german(0), "0,00")
        self.assertEqual(german("356949.62"), "356.949,62")


class BidValues(unittest.TestCase):
    def test_types_exact_strings(self):
        values = bid_values({
            "tender_reference": "AB-2031-9",
            "decision": "bid",
            "discount_percent": 5,
            "total_net": "1200.5",
            "positions": [{
                "position": "01.01",
                "material_number": "M-1",
                "quantity": 4,
                "lead_time_weeks": 2,
                "unit_price": "10.5",
                "base_unit_price": "11",
                "surcharge_per_unit": "0.25",
                "packaging_per_unit": "0.1",
                "rebate_total": 0,
            }],
        })
        self.assertEqual(values["discount_percent"], "5,00")
        self.assertEqual(values["total_net"], "1.200,50")
        self.assertEqual(values["positions"][0]["unit_price"], "10,50")
        self.assertEqual(values["positions"][0]["quantity"], "4")


class TanParse(unittest.TestCase):
    def test_labelled_code_wins(self):
        text = "Ausschreibung AB-2031-9\nSignatur-TAN: K7M2Q9PL\nGültig 5 Minuten"
        self.assertEqual(extract_tan(text), "K7M2Q9PL")

    def test_newest_labelled_code(self):
        text = "TAN ABC111\nneuere Nachricht\nTAN XYZ99999"
        self.assertEqual(extract_tan(text), "XYZ99999")

    def test_hex_fallback(self):
        self.assertEqual(extract_tan("Ihr Code 0ADB12EF ist 5 Minuten gültig"), "0ADB12EF")

    def test_empty(self):
        self.assertIsNone(extract_tan(""))
        self.assertIsNone(extract_tan("keine Freigabe in dieser Nachricht"))


class SignLabel(unittest.TestCase):
    def test_binding_submit(self):
        self.assertTrue(is_sign_label("Angebot verbindlich abgeben"))
        self.assertFalse(is_sign_label("Weiter"))


class Credit(unittest.TestCase):
    def test_remaining_is_limit_minus_usage(self):
        self.assertAlmostEqual(remaining_credit({"limit": 20, "usage": 19.7}), 0.3)

    def test_refuses_a_key_that_cannot_pay_for_a_run(self):
        with self.assertRaises(SystemExit) as raised:
            require_credit({"limit": 20, "usage": 19.7}, need=3)
        self.assertIn("$0.30", str(raised.exception))

    def test_allows_enough_credit(self):
        require_credit({"limit": 20, "usage": 10}, need=3)


class Deadlines(unittest.TestCase):
    def test_research_leaves_submit_reserve(self):
        now = 1000.0
        task = {"expires_at": None}
        research_by, submit_by = phase_deadlines(task, now=now, submit_s=270, safety_s=45)
        self.assertEqual(submit_by, now + 20 * 60 - 45)
        self.assertEqual(research_by, submit_by - 270)
        self.assertGreater(submit_by - research_by, 200)

    def test_short_wall_still_gives_research_a_minute(self):
        now = 50.0
        with patch("submit_helpers.contract.seconds_left", return_value=80.0):
            research_by, submit_by = phase_deadlines({"expires_at": 1}, now=now, submit_s=270, safety_s=45)
        self.assertGreaterEqual(research_by, now + 60)
        self.assertGreaterEqual(submit_by, now + 90)


if __name__ == "__main__":
    unittest.main()
