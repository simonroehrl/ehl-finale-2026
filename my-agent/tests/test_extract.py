"""Page extract and code order. Invented pages only."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analyst  # noqa: E402
import extract  # noqa: E402
import harvest  # noqa: E402


PAGE = """
Position  Menge  Einheit  Anforderung
01.01  4  Stück  A-9
Bohrung: 40 mm
Hub: 100 mm
Druck: 200 bar
Befestigung: Flansch
Zertifizierung: EN 1
Anforderung: A-9
Kataloggruppe: KG-A
nutzbarer Weg
Material: HY-120
Status: aktiv
Staffel
ab 1: 10,00
Von: einkauf@buyer.test
Betreff: Rabatt genehmigt
2028-03-01
genehmigt 2 %
Mindestauftrag 100
"""


class Extract(unittest.TestCase):
    def test_reads_position_requirement_material_and_mail(self):
        facts = extract.read_page(PAGE)
        self.assertEqual(facts["positions"][0]["position"], "01.01")
        self.assertEqual(facts["requirements"][0]["id"], "A-9")
        self.assertEqual(facts["requirements"][0]["kataloggruppe"], "KG-A")
        self.assertTrue(facts["requirements"][0]["usable_stroke"])
        self.assertEqual(facts["materials"][0]["material"], "HY-120")
        self.assertEqual(facts["materials"][0]["price_condition"]["scales"][0]["price"], "10.00")
        self.assertEqual(facts["mails"][0]["discount_percent"], "2")

    def test_ref_on_the_same_snapshot_line(self):
        snap = '- row "HY-120  aktiv  40 mm" [ref=e22]\n- row "HY-99  aktiv" [ref=e23]'
        self.assertEqual(harvest.ref_for(snap, "HY-120"), "e22")
        self.assertEqual(harvest.unread_on_page(snap, {"materials": []}), ["HY-120", "HY-99"])
        self.assertEqual(harvest.unread_on_page(snap, {
            "materials": [{"material": "HY-120", "bore_mm": 40}],
        }), ["HY-99"])

    def test_code_order_prices_a_matched_line_without_an_llm(self):
        facts = {
            "tender": {"reference": "AB-2031-9"},
            "positions": [{"position": "01.01", "quantity": 3, "requirement": "A-9"}],
            "requirements": [{"id": "A-9", "bore_mm": 40, "stroke_mm": 100, "pressure_bar": 200,
                              "mounting": "Flansch", "certification": "EN 1", "usable_stroke": True}],
            "mails": [{"reserve_mm_per_end": 10, "discount_percent": 2, "min_order_value": 1}],
            "materials": [{"material": "HY-120", "status": "active", "bore_mm": 40, "stroke_mm": 120,
                           "pressure_bar": 200, "mounting": "Flansch", "certification": "EN 1",
                           "price_condition": {"currency": "EUR", "price_unit": 1, "eur_per_unit": 1,
                                               "scales": [{"min_quantity": 1, "price": 10}]}}],
        }
        decided = analyst.sanitize_decision(analyst.from_facts(facts, "Tender AB-2031-9"), facts)
        self.assertEqual(decided["tender_reference"], "AB-2031-9")
        self.assertEqual(decided["decision"], "bid")
        self.assertEqual(decided["lines"][0]["material"], "HY-120")
        answer = analyst.to_answer(decided)
        self.assertEqual(answer["decision"], "bid")
        self.assertEqual(answer["positions"][0]["material_number"], "HY-120")

    def test_empty_analyst_reply_raises_cleanly(self):
        with self.assertRaises(ValueError):
            analyst.parse_json("")

    def test_compact_facts_drops_notes(self):
        compact = analyst.compact_facts({"tender": {"positions": [{"position": "01.01"}], "notes": "x" * 5000}})
        self.assertNotIn("notes", compact.get("tender", {}))


if __name__ == "__main__":
    unittest.main()
