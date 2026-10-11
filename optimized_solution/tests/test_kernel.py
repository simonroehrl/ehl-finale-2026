"""Bid rules against invented numbers. Nothing here comes from the challenge websites."""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal as D
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kernel import allocate_cents, project_bonus, resolve, validate_answer
from records import Ledger


def priced(**extra):
    ledger = Ledger()
    ledger.merge({
        "case": {"tender_reference": "T-1", "scenario_date": "2026-06-01", "bidder_mail_domain": "example.test"},
        "positions": [{"position": "1", "quantity": "10", "unit_raw": "Stück", "requirement_id": "R1"}],
        "revisions": [{
            "id": "S1", "marking": "gültig", "requirements": [{
                "id": "R1", "catalog_group": "G1", "bore_mm": "10", "stroke_mm": "20",
                "pressure_bar": "30", "mounting": "Flansch", "certification": "keine",
            }],
        }],
        "materials": [{
            "number": "M-1", "status": "frei", "catalog_group": "G1",
            "attributes": {"bore_mm": "10", "stroke_mm": "20", "pressure_bar": "30", "mounting": "Flansch", "certification": "keine"},
            "stock_pieces": 100, "stock_lead_time_weeks": 1, "production_lead_time_weeks": 6,
            "price_conditions": [{"valid_from": "2026-01-01", "currency": "EUR", "price_unit": 1, "scales": [{"min_quantity": 1, "price": "100"}]}],
        }],
        "searches": [{"catalog_group": "G1", "material_numbers": ["M-1"], "complete": True}],
    })
    ledger.merge(extra)
    ledger.ev.mail_complete = True
    return ledger


class Arithmetic(unittest.TestCase):
    def test_simple_bid_uses_stock_lead_time(self):
        answer = resolve(priced().ev).answer
        self.assertEqual(answer["decision"], "bid")
        self.assertEqual(answer["discount_percent"], 0.0)
        self.assertEqual(answer["positions"][0]["unit_price"], 100.0)
        self.assertEqual(answer["positions"][0]["quantity"], 10)
        self.assertEqual(answer["positions"][0]["lead_time_weeks"], 1)
        self.assertEqual(answer["total_net"], 1000.0)
        self.assertEqual(validate_answer(answer), [])

    def test_scale_price_unit_and_fx_round_half_up(self):
        ledger = priced()
        ledger.ev.materials[0].price_conditions[0].currency = "CHF"
        ledger.ev.materials[0].price_conditions[0].fx_key = "CHF-1"
        ledger.ev.materials[0].price_conditions[0].price_unit = 100
        ledger.ev.materials[0].price_conditions[0].scales[0].price = D("1234.5")
        ledger.merge({"messages": [{
            "id": "fx-1", "sender": "finanzen@example.test", "date": "2026-05-01T00:00:00+00:00",
            "reference": "T-1", "kind": "fx_fixing",
            "fx_rates": [{"key": "CHF-1", "currency": "CHF", "eur_per_unit": "1.05"}],
        }]})
        position = resolve(ledger.ev).answer["positions"][0]
        self.assertEqual(position["base_unit_price"], 12.96)  # 12.345 * 1.05 = 12.96225

    def test_highest_scale_and_threshold(self):
        ledger = priced()
        ledger.ev.materials[0].price_conditions[0].scales = [
            type(ledger.ev.materials[0].price_conditions[0].scales[0])(1, D("10")),
            type(ledger.ev.materials[0].price_conditions[0].scales[0])(50, D("8")),
        ]
        ledger.ev.positions[0].quantity = D(50)
        ledger.merge({"messages": [{
            "id": "d1", "sender": "Leitung <leitung@example.test>", "date": "2026-05-02T00:00:00+00:00",
            "reference": "T-1", "kind": "discount_approval", "discount_percent": "5", "min_order_value_eur": "400.00",
        }]})
        reached = resolve(ledger.ev).answer
        self.assertEqual(reached["discount_percent"], 5.0)
        self.assertEqual(reached["positions"][0]["base_unit_price"], 8.0)
        ledger.ev.messages[0].min_order_value_eur = D("400.01")
        missed = resolve(ledger.ev).answer
        self.assertEqual(missed["discount_percent"], 0.0)

    def test_discount_scopes_do_not_touch_setup(self):
        ledger = priced()
        ledger.ev.positions = []
        for number, group in (("1", None), ("2", "BOTH"), ("3", "NONE")):
            ledger.merge({"positions": [{"position": number, "quantity": "10", "unit_raw": "Stk", "requirement_id": "R1"}]})
        ledger.merge({"qa": [{
            "number": "1", "date": "2026-05-01T00:00:00+00:00", "reference": "T-1", "text": "keys",
            "assignments": [
                {"position": "2", "condition_group": "BOTH", "setup_fee_eur": "0"},
                {"position": "3", "condition_group": "NONE"},
            ],
        }], "messages": [
            {"id": "d", "sender": "leitung@example.test", "date": "2026-05-02T00:00:00+00:00", "reference": "T-1",
             "kind": "discount_approval", "discount_percent": "10"},
            {"id": "c", "sender": "kalkulation@example.test", "date": "2026-05-01T00:00:00+00:00", "reference": "T-1",
             "kind": "condition_rules", "condition_rules": [
                 {"key": "BOTH", "scope": "base_and_surcharge", "text": "Grundpreis und Massenzuschlag"},
                 {"key": "NONE", "scope": "none", "text": "kein Rabatt"},
             ]},
        ]})
        ledger.ev.materials[0].surcharge_mass_kg = D("1")
        ledger.ev.materials[0].metal_index = "ALU"
        ledger.merge({"messages": [{
            "id": "metal", "sender": "kalkulation@example.test", "date": "2026-05-03T00:00:00+00:00", "reference": "T-1",
            "kind": "metal_index", "metal_index_rates": [{"code": "ALU", "eur_per_kg": "10"}],
        }]})
        prices = [row["unit_price"] for row in resolve(ledger.ev).answer["positions"]]
        self.assertEqual(prices, [100.0, 99.0, 110.0])

    def test_shared_stock_and_commitment(self):
        ledger = priced()
        ledger.ev.materials[0].stock_pieces = 15
        ledger.merge({"positions": [{"position": "2", "quantity": "10", "unit_raw": "Stk", "requirement_id": "R1"}]})
        short = resolve(ledger.ev).answer
        self.assertEqual([row["lead_time_weeks"] for row in short["positions"]], [6, 6])
        ledger.ev.materials[0].stock_pieces = 20
        covered = resolve(ledger.ev).answer
        self.assertEqual([row["lead_time_weeks"] for row in covered["positions"]], [1, 1])
        ledger.merge({"qa": [{
            "number": "9", "date": "2026-05-01T00:00:00+00:00", "reference": "T-1",
            "assignments": [{"position": "1", "delivery_commitment": "FAST"}],
        }], "messages": [{
            "id": "term", "sender": "logistik@example.test", "date": "2026-05-01T00:00:00+00:00", "reference": "T-1",
            "kind": "delivery_commitments",
            "delivery_commitments": [{"key": "FAST", "material": "M-1", "max_pieces": 20, "lead_time_weeks": 3}],
        }]})
        mixed = resolve(ledger.ev).answer
        by_position = {row["position"]: row["lead_time_weeks"] for row in mixed["positions"]}
        self.assertEqual(by_position, {"1": 3, "2": 1})

    def test_packaging_setup_and_final_rounding(self):
        ledger = priced()
        ledger.ev.positions[0].quantity = D(3)
        ledger.ev.materials[0].pieces_per_carton = 2
        ledger.merge({
            "qa": [{"number": "1", "date": "2026-05-01T00:00:00+00:00", "reference": "T-1", "assignments": [
                {"position": "1", "packaging_tariff": "K", "setup_fee_eur": "10"},
            ]}],
            "messages": [
                {"id": "d", "sender": "leitung@example.test", "date": "2026-05-02T00:00:00+00:00", "reference": "T-1",
                 "kind": "discount_approval", "discount_percent": "10"},
                {"id": "p", "sender": "logistik@example.test", "date": "2026-05-01T00:00:00+00:00", "reference": "T-1",
                 "kind": "packaging_tariffs", "packaging_tariffs": [{"key": "K", "eur_per_carton": "5"}]},
            ],
        })
        answer = resolve(ledger.ev).answer
        position = answer["positions"][0]
        self.assertEqual(position["quantity"], 3)
        self.assertEqual(position["unit_price"], 96.67)
        self.assertEqual(position["packaging_per_unit"], 3.33)
        self.assertEqual(position["surcharge_per_unit"], 3.33)
        self.assertEqual(answer["total_net"], 290.01)

    def test_pool_and_bonus_use_largest_remainder(self):
        self.assertEqual(allocate_cents(D("0.10"), {"2": D(1), "1": D(1), "3": D(1)}),
                         {"1": D("0.04"), "2": D("0.03"), "3": D("0.03")})
        self.assertEqual(project_bonus([(D(0), D(1000), D(1)), (D(1000), None, D(2))], D("1500")), D("20.00"))


class Rules(unittest.TestCase):
    def test_withdrawn_answer_does_not_change_quantity_and_its_withdrawal_dies_with_it(self):
        ledger = priced()
        ledger.merge({"qa": [
            {"number": "B", "date": "2026-05-01T00:00:00+00:00", "reference": "T-1",
             "quantity_changes": [{"position": "1", "quantity": "99", "unit_raw": "Stk"}]},
            {"number": "A", "date": "2026-05-02T00:00:00+00:00", "reference": "T-1", "withdraws": ["B"],
             "quantity_changes": [{"position": "1", "quantity": "20", "unit_raw": "Stk"}]},
            {"number": "C", "date": "2026-05-03T00:00:00+00:00", "reference": "T-1", "withdraws": ["A"]},
        ]})
        self.assertEqual(resolve(ledger.ev).answer["positions"][0]["quantity"], 99)

    def test_set_conversion_and_usable_stroke(self):
        ledger = priced()
        ledger.ev.positions[0].unit_kind = "set"
        ledger.ev.positions[0].quantity = D(4)
        ledger.ev.revisions[0].requirements[0].pieces_per_set = 3
        ledger.ev.revisions[0].requirements[0].usable_stroke = True
        ledger.ev.revisions[0].requirements[0].end_reserve_key = "E"
        ledger.ev.revisions[0].requirements[0].attributes.stroke_mm = D(100)
        ledger.ev.materials[0].attributes.stroke_mm = D(110)
        ledger.merge({"messages": [{
            "id": "rel", "sender": "technik@example.test", "date": "2026-05-01T00:00:00+00:00", "reference": "T-1",
            "kind": "technical_release", "technical_releases": [{"key": "E", "revision": "S1", "reserve_mm_per_end": "5"}],
        }]})
        answer = resolve(ledger.ev).answer
        self.assertEqual(answer["positions"][0]["quantity"], 12)
        self.assertEqual(answer["positions"][0]["material_number"], "M-1")

    def test_newer_release_for_another_revision_does_not_revive_the_old_one(self):
        ledger = priced()
        ledger.ev.revisions[0].requirements[0].usable_stroke = True
        ledger.ev.revisions[0].requirements[0].end_reserve_key = "E"
        ledger.merge({"messages": [
            {"id": "old", "sender": "technik@example.test", "date": "2026-04-01T00:00:00+00:00", "reference": "T-1",
             "kind": "technical_release", "technical_releases": [{"key": "E", "revision": "S1", "reserve_mm_per_end": "5"}]},
            {"id": "new", "sender": "technik@example.test", "date": "2026-05-01T00:00:00+00:00", "reference": "T-1",
             "kind": "technical_release", "technical_releases": [{"key": "E", "revision": "OTHER", "reserve_mm_per_end": "9"}]},
        ]})
        resolution = resolve(ledger.ev)
        self.assertIsNone(resolution.answer)
        self.assertTrue(any("OTHER" in gap.question for gap in resolution.gaps))

    def test_metal_index_uses_the_window_not_the_last_row(self):
        ledger = priced()
        ledger.ev.materials[0].surcharge_mass_kg = D("2")
        ledger.ev.materials[0].metal_index = "ALU"
        ledger.merge({"messages": [{
            "id": "metal", "sender": "kalkulation@example.test", "date": "2026-05-01T00:00:00+00:00", "reference": "T-1",
            "kind": "metal_index", "metal_index_rates": [
                {"code": "ALU", "eur_per_kg": "1.00", "valid_from": "2026-01-01", "valid_to": "2026-03-31"},
                {"code": "ALU", "eur_per_kg": "2.50", "valid_from": "2026-04-01", "valid_to": "2026-06-30"},
                {"code": "ALU", "eur_per_kg": "9.00", "valid_from": "2026-07-01", "valid_to": "2026-12-31"},
            ],
        }]})
        self.assertEqual(resolve(ledger.ev).answer["positions"][0]["surcharge_per_unit"], 5.0)

    def test_amendment_overrides_only_the_named_attribute(self):
        ledger = priced()
        ledger.ev.materials[0].attributes.pressure_bar = D(40)
        ledger.merge({"qa": [{
            "number": "7", "date": "2026-05-04T00:00:00+00:00", "reference": "T-1",
            "amendments": [{"revision": "S1", "requirement_id": "R1", "attributes": {"pressure_bar": "40"}}],
        }]})
        self.assertEqual(resolve(ledger.ev).answer["positions"][0]["material_number"], "M-1")

    def test_complete_search_without_a_match_is_no_bid(self):
        ledger = priced()
        ledger.ev.materials[0].attributes.bore_mm = D(99)
        answer = resolve(ledger.ev).answer
        self.assertEqual(answer["decision"], "no_bid")
        self.assertEqual(answer["positions"], [])
        self.assertEqual(answer["total_net"], 0)
        self.assertEqual(answer["discount_percent"], 0)

    def test_unfinished_mail_search_does_not_silently_price_zero_discount(self):
        ledger = priced()
        ledger.ev.mail_complete = False
        resolution = resolve(ledger.ev, strict=True)
        self.assertIsNone(resolution.answer)
        self.assertTrue(any(gap.site == "mail" for gap in resolution.gaps))
        lenient = resolve(ledger.ev, strict=False).answer
        self.assertEqual(lenient["discount_percent"], 0.0)

    def test_later_partial_extract_does_not_wipe_a_unit(self):
        ledger = priced()
        ledger.merge({"positions": [{"position": "1", "quantity": "10", "unit_raw": ""}]})
        self.assertEqual(ledger.ev.positions[0].unit_kind, "piece")


if __name__ == "__main__":
    unittest.main()
