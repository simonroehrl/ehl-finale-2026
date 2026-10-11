"""Analyst guards. Invented facts only."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import analyst  # noqa: E402
import match  # noqa: E402
from llm_agent import host_block, missing_keys, unread_records  # noqa: E402


class DecisionGuard(unittest.TestCase):
    def test_no_bid_without_a_named_gap_is_rejected_when_lines_exist(self):
        decided = analyst.sanitize_decision({
            "decision": "no_bid",
            "tender_reference": "AB-2031-9",
            "lines": [{"position": "01.01", "material": "HY-1", "quantity_pieces": 3}],
        }, {"tender": {"positions": [{"position": "01.01"}]}, "requirements": [{"id": "A-1"}]})
        self.assertEqual(decided["decision"], "bid")

    def test_no_bid_stays_when_no_line_can_be_priced(self):
        decided = analyst.sanitize_decision({
            "decision": "no_bid",
            "lines": [{"position": "01.01", "material": None}],
        }, {"positions": [{"position": "01.01"}]})
        self.assertEqual(decided["decision"], "no_bid")

    def test_partial_bid_when_some_positions_have_material(self):
        decided = analyst.sanitize_decision({
            "decision": "no_bid",
            "tender_reference": "AB-2031-9",
            "lines": [
                {"position": "01.01", "material": "HY-1", "quantity_pieces": 3,
                 "price_condition": {"currency": "EUR", "price_unit": 1, "eur_per_unit": 1,
                                     "scales": [{"min_quantity": 1, "price": 10}]}},
                {"position": "01.02", "material": None, "quantity_pieces": 2},
            ],
        }, {"tender": {"positions": [{"position": "01.01"}, {"position": "01.02"}]}})
        self.assertEqual(decided["decision"], "bid")
        self.assertEqual([line["position"] for line in decided["lines"]], ["01.01"])
        self.assertEqual(decided["omitted_positions"], ["01.02"])
        answer = analyst.to_answer(decided, expected_positions=2)
        self.assertEqual(answer["decision"], "bid")
        self.assertEqual(len(answer["positions"]), 1)
        self.assertEqual(answer["positions"][0]["material_number"], "HY-1")
        self.assertEqual(answer["omitted_positions"], ["01.02"])

    def test_usable_stroke_plus_reserve_matches_nominal_material(self):
        facts = {
            "requirements": [{"id": "A-9", "bore_mm": 40, "stroke_mm": 100, "pressure_bar": 200,
                              "mounting": "Flansch", "certification": "EN 1", "usable_stroke": True}],
            "mails": [{"technical_releases": [{"key": "ER-1", "reserve_mm_per_end": 10}]}],
            "materials": [{"material": "HY-120", "status": "active", "bore_mm": 40, "stroke_mm": 120,
                           "pressure_bar": 200, "mounting": "Flansch", "certification": "EN 1"}],
        }
        decided = analyst.sanitize_decision({
            "decision": "no_bid",
            "tender_reference": "AB-2031-9",
            "lines": [{"position": "01.01", "material": None, "quantity_pieces": 2,
                       "requirement": facts["requirements"][0]}],
        }, facts)
        self.assertEqual(decided["decision"], "bid")
        self.assertEqual(decided["lines"][0]["material"], "HY-120")
        self.assertEqual(match.nominal_stroke(100, True, 10), 120)

    def test_wrong_named_material_is_replaced_after_stroke_derivation(self):
        facts = {
            "requirements": [{"id": "A-9", "bore_mm": 40, "stroke_mm": 100, "pressure_bar": 200,
                              "mounting": "Flansch", "certification": "EN 1", "usable_stroke": True}],
            "mails": [{"technical_releases": [{"key": "ER-1", "reserve_mm_per_end": 10}]}],
            "materials": [
                {"material": "HY-100", "status": "active", "bore_mm": 40, "stroke_mm": 100,
                 "pressure_bar": 200, "mounting": "Flansch", "certification": "EN 1"},
                {"material": "HY-120", "status": "active", "bore_mm": 40, "stroke_mm": 120,
                 "pressure_bar": 200, "mounting": "Flansch", "certification": "EN 1",
                 "price_condition": {"currency": "EUR", "price_unit": 1, "eur_per_unit": 1,
                                     "scales": [{"min_quantity": 1, "price": 8}]}},
            ],
        }
        decided = analyst.sanitize_decision({
            "decision": "bid",
            "tender_reference": "AB-2031-9",
            "lines": [{"position": "01.01", "material": "HY-100", "quantity_pieces": 2,
                       "requirement": facts["requirements"][0]}],
        }, facts)
        self.assertEqual(decided["lines"][0]["material"], "HY-120")

    def test_host_lock_rejects_a_different_site(self):
        self.assertEqual(host_block("portal.example", "https://erp.example/mm03"),
                         "Error: this browser stays on portal.example. Do not open erp.example.")
        self.assertEqual(host_block("portal.example", "https://portal.example/docs/1"), "")
        self.assertEqual(host_block("portal.example", "/relative"), "")

    def test_empty_lists_are_still_missing(self):
        self.assertEqual(missing_keys({"positions": []}, ["positions", "clarifications"]),
                         ["positions", "clarifications"])

    def test_register_without_document_blocks_finish(self):
        hint = unread_records({"revisions": [{"id": "R-9", "marking": "unknown"}], "requirements": []})
        self.assertIn("document", hint.lower())
        self.assertIn("register", hint.lower())
        self.assertEqual(unread_records({"revisions": [{"id": "R-9", "marking": "gültig"}],
                                         "requirements": [{"id": "A-1"}]}), "")

    def test_unsearched_catalog_group_blocks_finish(self):
        hint = unread_records({
            "catalog_groups": ["KG-A", "KG-B"],
            "searched_groups": ["KG-A"],
            "materials": [{"material": "HY-1", "bore_mm": 40, "kataloggruppe": "KG-A"}],
        })
        self.assertIn("KG-B", hint)
        self.assertEqual(unread_records({
            "catalog_groups": ["KG-A", "KG-B"],
            "searched_groups": ["KG-A", "KG-B"],
            "materials": [
                {"material": "HY-1", "bore_mm": 40, "kataloggruppe": "KG-A"},
                {"material": "HY-2", "bore_mm": 50, "kataloggruppe": "KG-B"},
            ],
        }), "")
        self.assertIn("KG-B", unread_records(
            {"catalog_groups": ["KG-A"], "searched_groups": ["KG-A"],
             "materials": [{"material": "HY-1", "bore_mm": 40, "kataloggruppe": "KG-A"}]},
            must_search=["KG-A", "KG-B"]))


if __name__ == "__main__":
    unittest.main()
