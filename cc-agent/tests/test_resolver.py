"""resolver.py on invented evidence. All references, keys and numbers are made up, none come from the challenge sites."""
import copy
import sys
import unittest
from decimal import Decimal as D
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

from evidence import Case, Evidence, merge  # noqa: E402
from resolver import resolve  # noqa: E402

DOMAIN = "firma.example"
REF = "AB-1000"


def mail(id, sender, date, kind, reference=REF, **payload):
    return {"id": id, "sender": f"{sender}@{DOMAIN}", "date": date, "subject": f"{kind} {reference}",
            "reference": reference, "kind": kind, **payload}


def qa(number, date, **effects):
    return {"number": number, "date": date, "text": "...", **effects}


def material(number, bore=50, stroke=100, pressure=200, mounting="Flansch", cert="EN 1", **kw):
    m = {"number": number, "status": "active", "catalog_group": "G-1",
         "attributes": {"bore_mm": bore, "stroke_mm": stroke, "pressure_bar": pressure, "mounting": mounting,
                        "certification": cert},
         "stock_pieces": 1000, "stock_lead_time_weeks": 2, "production_lead_time_weeks": 8,
         "price_conditions": [{"valid_from": "2030-01-01", "valid_to": "2030-12-31",
                               "scales": [{"min_quantity": 1, "price": "100.00"}]}]}
    m.update(kw)
    return m


def base() -> dict:
    """One position, one requirement, one exactly matching material at 100.00 EUR, no discount."""
    return {
        "case": {"tender_reference": REF, "scenario_date": "2030-03-10T09:00:00Z", "bidder_mail_domain": DOMAIN},
        "positions": [{"position": "1", "quantity": 10, "unit_raw": "Stk", "unit_kind": "piece",
                       "requirement_id": "R-1"}],
        "revisions": [
            {"id": "REV-OLD", "marking": "superseded", "requirements": [
                {"id": "R-1", "bore_mm": 99, "stroke_mm": 100, "pressure_bar": 200, "mounting": "Flansch",
                 "certification": "EN 1", "catalog_group": "G-1"}]},
            {"id": "REV-2", "marking": "current", "requirements": [
                {"id": "R-1", "bore_mm": 50, "stroke_mm": 100, "pressure_bar": 200, "mounting": "Flansch",
                 "certification": "EN 1", "catalog_group": "G-1"}]}],
        "materials": [material("M-1")],
        "erp_searches": [{"catalog_group": "G-1", "material_numbers": ["M-1"], "complete": True}],
        "qa": [], "messages": [],
    }


def run(data):
    return resolve(Evidence.model_validate(data))


def position(result, p="1"):
    return next(x for x in result.answer["positions"] if x["position"] == p)


class Basics(unittest.TestCase):
    def test_plain_bid(self):
        r = run(base())
        self.assertTrue(r.complete, r.gaps)
        self.assertEqual(r.answer["decision"], "bid")
        self.assertEqual(position(r)["material_number"], "M-1")
        self.assertEqual(position(r)["unit_price"], 100.0)
        self.assertEqual(position(r)["lead_time_weeks"], 2)
        self.assertEqual(r.answer["total_net"], 1000.0)

    def test_schema_for_the_model(self):
        schema = Evidence.model_json_schema()
        self.assertIn("QAAnswer", schema["$defs"])

    def test_naive_dates_are_utc(self):
        data = base()
        data["case"]["scenario_date"] = "2030-03-10T09:00:00"
        data["qa"] = [qa("7", "2030-03-01T00:00:00", quantity_changes=[
            {"position": "1", "quantity": 12, "unit_kind": "piece"}])]
        self.assertEqual(position(run(data))["quantity"], 12)


class Quantities(unittest.TestCase):
    def test_latest_change_wins_and_future_is_ignored(self):
        data = base()
        data["qa"] = [
            qa("1", "2030-03-01T10:00:00Z", quantity_changes=[{"position": "1", "quantity": 20, "unit_kind": "piece"}]),
            qa("2", "2030-03-05T10:00:00Z", quantity_changes=[{"position": "1", "quantity": 15, "unit_kind": "piece"}]),
            qa("3", "2030-03-11T10:00:00Z", quantity_changes=[{"position": "1", "quantity": 99, "unit_kind": "piece"}]),
        ]
        self.assertEqual(position(run(data))["quantity"], 15)

    def test_withdrawn_answer_is_ignored(self):
        data = base()
        data["qa"] = [
            qa("1", "2030-03-01T10:00:00Z", quantity_changes=[{"position": "1", "quantity": 20, "unit_kind": "piece"}]),
            qa("2", "2030-03-05T10:00:00Z", quantity_changes=[{"position": "1", "quantity": 30, "unit_kind": "piece"}]),
            qa("3", "2030-03-06T10:00:00Z", withdraws=["2"]),
        ]
        self.assertEqual(position(run(data))["quantity"], 20)

    def test_sets_convert_with_the_governing_revision(self):
        data = base()
        data["positions"][0].update(quantity=3, unit_raw="Satz", unit_kind="set")
        data["revisions"][1]["requirements"][0]["pieces_per_set"] = 4
        self.assertEqual(position(run(data))["quantity"], 12)

    def test_sets_without_pieces_per_set_are_a_gap(self):
        data = base()
        data["positions"][0].update(unit_raw="Satz", unit_kind="set")
        r = run(data)
        self.assertEqual(r.omitted, ["1"])
        self.assertIn("pieces per set", r.gaps[0].question)


class Requirements(unittest.TestCase):
    def test_no_current_revision_is_a_gap(self):
        data = base()
        data["revisions"][1]["marking"] = "unknown"
        r = run(data)
        self.assertIsNone(r.answer)
        self.assertEqual(r.gaps[0].site, "customer-portal")

    def test_latest_amendment_per_attribute_for_the_governing_revision_only(self):
        data = base()
        data["materials"] = [material("M-1"), material("M-2", stroke=120, pressure=250)]
        data["erp_searches"][0]["material_numbers"] = ["M-1", "M-2"]
        data["qa"] = [
            qa("1", "2030-03-01T10:00:00Z", amendments=[
                {"revision": "REV-2", "requirement_id": "R-1", "attributes": {"stroke_mm": 110, "pressure_bar": 250}}]),
            qa("2", "2030-03-02T10:00:00Z", amendments=[
                {"revision": "REV-2", "requirement_id": "R-1", "attributes": {"stroke_mm": 120}}]),
            qa("3", "2030-03-03T10:00:00Z", amendments=[
                {"revision": "REV-OLD", "requirement_id": "R-1", "attributes": {"stroke_mm": 999}}]),
        ]
        self.assertEqual(position(run(data))["material_number"], "M-2")

    def test_usable_stroke_adds_the_revision_specific_reserve(self):
        data = base()
        data["revisions"][1]["requirements"][0].update(usable_stroke=True, end_reserve_key="ER-1")
        data["materials"] = [material("M-1"), material("M-3", stroke=110)]
        data["messages"] = [
            mail("t1", "technik", "2030-03-01T10:00:00Z", "technical_release",
                 technical_releases=[{"key": "ER-1", "revision": "REV-2", "reserve_mm_per_end": 5}]),
            mail("t2", "technik", "2030-03-02T10:00:00Z", "technical_release",
                 technical_releases=[{"key": "ER-1", "revision": "REV-OLD", "reserve_mm_per_end": 7}]),
        ]
        self.assertEqual(position(run(data))["material_number"], "M-3")

    def test_usable_stroke_without_release_is_a_gap(self):
        data = base()
        data["revisions"][1]["requirements"][0].update(usable_stroke=True, end_reserve_key="ER-1")
        r = run(data)
        self.assertEqual(r.gaps[0].site, "mail")


class Materials(unittest.TestCase):
    def test_blocked_and_inexact_materials_do_not_qualify(self):
        data = base()
        data["materials"] = [material("M-1", status="blocked"), material("M-2", mounting="Gelenkauge"),
                             material("M-3", mounting="  flansch ")]
        self.assertEqual(position(run(data))["material_number"], "M-3")

    def test_certification_none_spellings_match(self):
        data = base()
        data["revisions"][1]["requirements"][0]["certification"] = "keine gefordert"
        data["materials"] = [material("M-1", cert="Keine")]
        self.assertEqual(position(run(data))["material_number"], "M-1")

    def test_searched_group_without_match_is_no_bid(self):
        data = base()
        data["materials"] = [material("M-1", bore=63)]
        r = run(data)
        self.assertEqual(r.answer["decision"], "no_bid")
        self.assertEqual(r.answer["positions"], [])
        self.assertEqual(r.answer["discount_percent"], 0)

    def test_unsearched_group_is_a_gap_not_no_bid(self):
        data = base()
        data["positions"].append({"position": "2", "quantity": 5, "unit_raw": "Stk", "unit_kind": "piece",
                                  "requirement_id": "R-2"})
        data["revisions"][1]["requirements"].append(
            {"id": "R-2", "bore_mm": 80, "stroke_mm": 100, "pressure_bar": 200, "mounting": "Flansch",
             "certification": "EN 1", "catalog_group": "G-2"})
        r = run(data)
        self.assertEqual(r.answer["decision"], "bid")
        self.assertEqual(r.omitted, ["2"])
        self.assertIn("G-2", r.gaps[0].question)
        self.assertEqual([p["position"] for p in r.answer["positions"]], ["1"])

    def test_disagreeing_agent_pick_is_reported(self):
        data = base()
        data["picks"] = {"materials": {"1": "M-9"}}
        self.assertIn("M-9", run(data).warnings[0])


class Discount(unittest.TestCase):
    def test_only_the_latest_exact_authorized_past_approval_counts(self):
        data = base()
        data["messages"] = [
            mail("a1", "leitung", "2030-03-01T10:00:00Z", "discount_approval", discount_percent=4),
            mail("a2", "leitung", "2030-03-02T10:00:00Z", "discount_approval", discount_percent=10),
            mail("a3", "leitung", "2030-03-03T10:00:00Z", "discount_approval", reference=REF + "-A",
                 discount_percent=20),
            mail("a4", "vertrieb", "2030-03-04T10:00:00Z", "discount_approval", discount_percent=30),
            mail("a5", "leitung", "2030-03-11T10:00:00Z", "discount_approval", discount_percent=40),
            mail("a6", "leitung", "2030-03-05T10:00:00Z", "other"),
        ]
        r = run(data)
        self.assertEqual(r.answer["discount_percent"], 10.0)
        self.assertEqual(position(r)["unit_price"], 90.0)

    def test_invisible_characters_in_the_reference(self):
        data = base()
        data["messages"] = [mail("a1", "leitung", "2030-03-01T10:00:00Z", "discount_approval",
                                 reference="AB-​1000", discount_percent=10)]
        self.assertEqual(run(data).answer["discount_percent"], 10.0)

    def test_failed_threshold_does_not_revive_an_older_approval(self):
        data = base()
        data["messages"] = [
            mail("a1", "leitung", "2030-03-01T10:00:00Z", "discount_approval", discount_percent=4),
            mail("a2", "leitung", "2030-03-02T10:00:00Z", "discount_approval", discount_percent=10,
                 min_order_value_eur="1000.01"),
        ]
        self.assertEqual(run(data).answer["discount_percent"], 0.0)


class Pricing(unittest.TestCase):
    def surcharge_case(self, scope):
        data = base()
        data["materials"] = [material("M-1", surcharge_mass_kg="2", metal_index="MET-1")]
        data["qa"] = [qa("1", "2030-03-01T10:00:00Z", assignments=[{"position": "1", "condition_group": "K-1"}])]
        data["messages"] = [
            mail("a1", "leitung", "2030-03-01T10:00:00Z", "discount_approval", discount_percent=10),
            mail("i1", "kalkulation", "2030-03-01T10:00:00Z", "metal_index", metal_index_rates=[
                {"code": "MET-1", "eur_per_kg": 9}]),
            mail("i2", "kalkulation", "2030-03-02T10:00:00Z", "metal_index", metal_index_rates=[
                {"code": "MET-1", "eur_per_kg": 2, "valid_from": "2030-01-01", "valid_to": "2030-03-09"},
                {"code": "MET-1", "eur_per_kg": 3, "valid_from": "2030-03-10", "valid_to": "2030-04-30"},
                {"code": "MET-1", "eur_per_kg": 5, "valid_from": "2030-05-01"}]),
            mail("k1", "kalkulation", "2030-03-01T10:00:00Z", "condition_rules", condition_rules=[
                {"key": "K-1", "scope": scope, "text": "..."}]),
        ]
        return run(data)

    def test_discount_on_base_and_surcharge_with_the_date_selected_index(self):
        r = self.surcharge_case("base_and_surcharge")
        self.assertEqual(position(r)["surcharge_per_unit"], 6.0)
        self.assertEqual(position(r)["unit_price"], 95.4)

    def test_fixed_price_group_takes_no_discount(self):
        self.assertEqual(position(self.surcharge_case("none"))["unit_price"], 106.0)

    def test_foreign_currency_converts_with_the_fixing(self):
        data = base()
        data["materials"][0]["price_conditions"] = [{
            "valid_from": "2030-01-01", "currency": "USD", "fx_key": "FX-1", "price_unit": 100,
            "scales": [{"min_quantity": 1, "price": "200"}]}]
        data["messages"] = [mail("f1", "finanzen", "2030-03-01T10:00:00Z", "fx_fixing", fx_rates=[
            {"key": "FX-1", "currency": "USD", "eur_per_unit": "1.1"}])]
        self.assertEqual(position(run(data))["base_unit_price"], 2.2)

    def test_missing_fixing_is_a_gap(self):
        data = base()
        data["materials"][0]["price_conditions"][0].update(currency="USD", fx_key="FX-1")
        self.assertEqual(run(data).gaps[0].site, "mail")

    def test_commitment_within_ceiling_and_shared_stock(self):
        data = base()
        data["positions"].append({"position": "2", "quantity": 15, "unit_raw": "Stk", "unit_kind": "piece",
                                  "requirement_id": "R-1"})
        data["materials"][0]["stock_pieces"] = 20  # 10 + 15 pooled: stock does not cover
        data["qa"] = [qa("1", "2030-03-01T10:00:00Z", assignments=[{"position": "1", "delivery_commitment": "TZ-1"}])]
        data["messages"] = [mail("d1", "logistik", "2030-03-01T10:00:00Z", "delivery_commitments",
                                 delivery_commitments=[{"key": "TZ-1", "material": "M-1", "max_pieces": 25,
                                                        "lead_time_weeks": 3}])]
        r = run(data)
        self.assertEqual(position(r, "1")["lead_time_weeks"], 3)
        self.assertEqual(position(r, "2")["lead_time_weeks"], 8)

    def test_packaging_pool_and_bonus(self):
        data = base()
        data["materials"][0].update(pieces_per_carton=4, allocation_mass_g=500)
        data["qa"] = [qa("1", "2030-03-01T10:00:00Z", assignments=[{
            "position": "1", "packaging_tariff": "VP-1", "setup_pool": "RP-1", "bonus_plan": "PB-1",
            "setup_fee_eur": "20"}])]
        largest = "floor_largest_remainder_ascending_position"
        data["messages"] = [
            mail("v1", "logistik", "2030-03-01T10:00:00Z", "packaging_tariffs",
                 packaging_tariffs=[{"key": "VP-1", "eur_per_carton": "5"}]),
            mail("r1", "kalkulation", "2030-03-01T10:00:00Z", "setup_pools",
                 setup_pools=[{"key": "RP-1", "fee_eur": "30", "allocation": largest}]),
            mail("b1", "finanzen", "2030-03-01T10:00:00Z", "project_bonus", bonus_plans=[{
                "key": "PB-1", "allocation": largest,
                "bands": [{"lower_eur": 0, "upper_eur": 500, "percent": 0}, {"lower_eur": 500, "percent": 2}]}]),
        ]
        r = run(data)
        self.assertTrue(r.complete, r.gaps)
        p = position(r)
        self.assertEqual(p["packaging_per_unit"], 1.5)   # 3 cartons x 5.00 / 10
        self.assertEqual(p["surcharge_per_unit"], 5.0)   # (20 setup + 30 pool) / 10
        self.assertEqual(p["rebate_total"], 10.0)        # 2 % of the 500 above 500
        self.assertEqual(p["unit_price"], 105.5)         # 100 + (20 + 30 + 15 - 10) / 10


class Merge(unittest.TestCase):
    def test_parts_from_several_agents(self):
        data = base()
        case = Case.model_validate(data["case"])
        tender = {"positions": data["positions"], "qa": [qa("1", "2030-03-01T10:00:00Z")]}
        spec = {"revisions": data["revisions"], "picks": {"governing_revision": "REV-2"}}
        erp = {"materials": [material("M-1", stock_pieces=1)], "erp_searches": data["erp_searches"]}
        erp_again = {"materials": data["materials"]}
        evidence = merge(case, tender, spec, erp, erp_again)
        self.assertEqual(evidence.picks.governing_revision, "REV-2")
        self.assertEqual(len(evidence.materials), 1)
        self.assertEqual(evidence.materials[0].stock_pieces, 1000)
        self.assertTrue(resolve(evidence).complete)

    def test_input_is_not_modified(self):
        data = base()
        before = copy.deepcopy(data)
        run(data)
        self.assertEqual(data, before)


if __name__ == "__main__":
    unittest.main()
