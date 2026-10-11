"""Pipeline helpers: site map, brief case, forgiving evidence. Invented values only."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

import extract  # noqa: E402
import main  # noqa: E402
from llm_agent import fact_guidance, missing_keys  # noqa: E402
from evidence import Case, facts_index, merge, merge_dicts, schema_for  # noqa: E402
from resolver import Gap  # noqa: E402


def task(*ids):
    return {"access": [{"siteId": i, "url": f"https://{i}.example/",
                        "credentials": {"username": "u", "password": "p"}} for i in ids],
            "brief": "Tender AB-2031-9 (AB-2031-9). Scenario date is 2031-03-10T09:00:00Z. "
                     "Mailbox vertrieb@firma.example. Discount from leitung@firma.example.",
            "expires_at": None}


class Helpers(unittest.TestCase):
    def test_known_and_unknown_site_ids(self):
        known = main.site_map(task("tender-portal", "customer-portal", "erp", "mail"))
        self.assertEqual(known["spec"]["siteId"], "customer-portal")
        unknown = main.site_map(task("a", "b", "c", "d"))
        self.assertEqual([unknown[r]["siteId"] for r in ("tender", "spec", "erp", "mail")], list("abcd"))

    def test_case_from_brief_uses_formats_not_website_values(self):
        case = main.case_from_brief(task()["brief"])
        self.assertEqual(case.tender_reference, "AB-2031-9")
        self.assertEqual(case.bidder_mail_domain, "firma.example")
        self.assertEqual(case.authorized_senders["discount_approval"], "leitung")

    def test_listed_revisions_without_marking_words_are_steered_not_finished(self):
        hint = fact_guidance({"revisions": [
            {"id": "R-1", "marking": "unknown"},
            {"id": "R-2", "marking": "unknown"},
        ]})
        self.assertIn("R-1", hint)
        self.assertIn("document", hint.lower())
        self.assertNotIn("finish", hint.lower())
        self.assertEqual(fact_guidance({"revisions": [{"id": "R-1", "marking": "gültig"}]}), "")
        self.assertEqual(fact_guidance({"revisions": [{"id": "R-1", "marking": "current"}]}), "")

    def test_submitted_false_is_still_missing(self):
        self.assertEqual(missing_keys({"submitted": False}, ["submitted"]), ["submitted"])
        self.assertEqual(missing_keys({"submitted": True}, ["submitted"]), [])

    def test_coerce_empty_facts_keeps_the_brief_case(self):
        fallback = Case(tender_reference="T", scenario_date="2031-01-01T00:00:00Z",
                        bidder_mail_domain="x.example")
        ev = main.coerce_evidence({}, fallback)
        self.assertEqual(ev.case.tender_reference, "T")
        self.assertEqual(ev.positions, [])

    def test_coerce_defaults_revision_marking_to_unknown(self):
        fallback = Case(tender_reference="T", scenario_date="2031-01-01T00:00:00Z",
                        bidder_mail_domain="x.example")
        ev = main.coerce_evidence({
            "revisions": [{"id": "R-9", "date": "2031-01-01T00:00:00Z"}],
        }, fallback)
        self.assertEqual(ev.revisions[0].marking, "unknown")

    def test_coerce_keeps_a_revision_when_one_requirement_is_broken(self):
        fallback = Case(tender_reference="T", scenario_date="2031-01-01T00:00:00Z",
                        bidder_mail_domain="x.example")
        ev = main.coerce_evidence({
            "revisions": [{"id": "R-1", "date": "2031-01-01T00:00:00Z", "marking": "gültig",
                           "requirements": [
                               {"id": "A-1", "catalog_group": "GR-1", "bore_mm": 40},
                               {"id": "A-2", "bore_mm": "not-a-number"},
                           ]}],
        }, fallback)
        self.assertEqual(len(ev.revisions), 1)
        self.assertEqual(ev.revisions[0].marking, "gültig")
        self.assertEqual([req.id for req in ev.revisions[0].requirements], ["A-1"])

    def test_coerce_keeps_page_marking_words(self):
        fallback = Case(tender_reference="T", scenario_date="2031-01-01T00:00:00Z",
                        bidder_mail_domain="x.example")
        ev = main.coerce_evidence({
            "revisions": [{"id": "R-9", "date": "2031-01-01T00:00:00Z", "marking": "gültig"}],
        }, fallback)
        self.assertEqual(ev.revisions[0].marking, "gültig")
        self.assertEqual(ev.revisions[0].marking_raw, "gültig")

    def test_role_prompt_uses_brief_objects_not_portal_paths(self):
        text = main.role_prompt("spec", task())
        self.assertIn("AB-2031-9", text)
        self.assertIn("firma.example", text)
        self.assertIn("Goal:", text)
        self.assertIn("document", text.lower())
        self.assertNotIn("kyora", text)
        self.assertNotIn("Lastenheft", text)
        self.assertNotIn("Angebotsabgabe", text)
        tender = main.role_prompt("tender", task())
        self.assertIn("bill of quantities", tender.lower())

    def test_follow_up_keeps_the_same_session(self):
        text = main.follow_up_prompt("- open R-1")
        self.assertIn("Same browser session", text)
        self.assertIn("open R-1", text)

    def test_coerce_empty_facts_still_returns_the_brief_case(self):
        fallback = Case(tender_reference="T", scenario_date="2031-01-01T00:00:00Z",
                        bidder_mail_domain="x.example")
        ev = main.coerce_evidence(None, fallback)
        self.assertEqual(ev.case.tender_reference, "T")
        self.assertEqual(ev.positions, [])

    def test_coerce_stringifies_object_notes(self):
        fallback = Case(tender_reference="T", scenario_date="2031-01-01T00:00:00Z",
                        bidder_mail_domain="x.example")
        ev = main.coerce_evidence({
            "positions": [{"position": "1", "quantity": 3, "unit_raw": "Stk", "unit_kind": "piece"}],
            "notes": [{"why": "extract"}],
        }, fallback)
        self.assertEqual(len(ev.positions), 1)
        self.assertTrue(any("extract" in n for n in ev.notes))

    def test_coerce_keeps_good_rows_and_drops_bad(self):
        fallback = Case(tender_reference="T", scenario_date="2031-01-01T00:00:00Z",
                        bidder_mail_domain="x.example")
        ev = main.coerce_evidence({
            "positions": [{"position": "1", "quantity": 3, "unit_raw": "Stk", "unit_kind": "piece"}],
            "qa": [{"number": "x", "broken": True}],
            "notes": "see qa",
        }, fallback)
        self.assertEqual(len(ev.positions), 1)
        self.assertEqual(ev.qa, [])
        self.assertTrue(any("dropped qa" in n for n in ev.notes))

    def test_gap_routing(self):
        mapping = main.site_map(task("tender-portal", "customer-portal", "erp", "mail"))
        self.assertEqual(main.role_for_gap(Gap("customer-portal", "rev"), mapping), "spec")
        self.assertEqual(main.role_for_gap(Gap("erp", "mat"), mapping), "erp")

    def test_schema_for_keeps_only_the_role_lists(self):
        schema = schema_for("positions", "qa")
        self.assertIn("positions", schema["properties"])
        self.assertIn("qa", schema["properties"])
        self.assertIn("notes", schema["properties"])
        self.assertNotIn("materials", schema["properties"])
        self.assertNotIn("messages", schema["properties"])
        self.assertNotIn("Material", schema.get("$defs", {}))
        self.assertIn("TenderPosition", schema.get("$defs", {}))

    def test_phase_deadlines_reserve_gap_and_submit(self):
        now = 1_000.0
        research, gap, submit = main.phase_deadlines(task(), now=now)
        self.assertAlmostEqual(submit - now, 20 * 60 - main.SAFETY_S)
        self.assertAlmostEqual(gap, submit - main.SUBMIT_S)
        self.assertAlmostEqual(research, gap - main.GAP_S)
        self.assertGreaterEqual(submit - research, main.SUBMIT_S + main.GAP_S)

    def test_role_until_uses_the_earlier_cutoff(self):
        self.assertEqual(main.role_until(10_000, cap_s=240, now=100), 340)
        self.assertEqual(main.role_until(200, cap_s=240, now=100), 200)

    def test_needs_mail_harvest_when_inbox_is_empty(self):
        fallback = Case(tender_reference="T", scenario_date="2031-01-01T00:00:00Z",
                        bidder_mail_domain="x.example")
        empty = main.coerce_evidence({}, fallback)
        self.assertTrue(main.needs_mail_harvest(empty))
        filled = main.coerce_evidence({
            "messages": [{"id": "m1", "sender": "kalkulation@x.example",
                          "date": "2031-01-02T00:00:00Z", "subject": "idx", "kind": "other",
                          "reference": "AB-1"}],
        }, fallback)
        self.assertEqual(len(filled.messages), 1, filled.notes)
        self.assertFalse(main.needs_mail_harvest(filled))

    def test_next_action_does_not_submit_missing_evidence(self):
        class R:
            answer = None
            gaps = [Gap("customer-portal", "Exactly one specification revision must be marked current (found: none).")]
        self.assertEqual(main.next_action(R(), now=10, research_until=100), "marking")
        self.assertEqual(main.next_action(R(), now=200, research_until=100), "stop")

    def test_next_action_submits_a_priced_bid_or_true_no_bid(self):
        class Bid:
            answer = {"decision": "bid", "positions": [{"position": "1"}]}
            gaps = []
        class NoBid:
            answer = {"decision": "no_bid", "positions": []}
            gaps = []
        class BidWithGaps:
            answer = {"decision": "bid", "positions": [{"position": "1"}]}
            gaps = [Gap("erp", "Search the ERP")]
        self.assertEqual(main.next_action(Bid(), now=10, research_until=100), "submit")
        self.assertEqual(main.next_action(NoBid(), now=10, research_until=100), "submit")
        self.assertEqual(main.next_action(BidWithGaps(), now=10, research_until=100), "gaps")
        self.assertEqual(main.next_action(BidWithGaps(), now=200, research_until=100), "submit")

    def test_sanitize_erp_complete_rejects_empty_extract(self):
        fallback = Case(tender_reference="T", scenario_date="2031-01-01T00:00:00Z",
                        bidder_mail_domain="x.example")
        ev = main.coerce_evidence({
            "erp_searches": [{"catalog_group": "GR-1", "material_numbers": ["M-1", "M-2"],
                              "complete": True}],
        }, fallback)
        self.assertFalse(ev.erp_searches[0].complete)
        self.assertTrue(any("complete ignored" in n for n in ev.notes))

    def test_sanitize_erp_complete_keeps_full_recorded_search(self):
        fallback = Case(tender_reference="T", scenario_date="2031-01-01T00:00:00Z",
                        bidder_mail_domain="x.example")
        ev = main.coerce_evidence({
            "erp_searches": [{"catalog_group": "GR-1", "material_numbers": ["M-1"],
                              "complete": True}],
            "materials": [{"number": "M-1", "status": "active", "catalog_group": "GR-1",
                           "attributes": {"bore_mm": 40, "stroke_mm": 80, "pressure_bar": 200,
                                          "mounting": "Flansch", "certification": "EN 1"}}],
        }, fallback)
        self.assertTrue(ev.erp_searches[0].complete)

    def test_empty_complete_search_is_a_gap_not_no_bid(self):
        from evidence import Evidence
        from resolver import resolve
        from test_resolver import base
        data = base()
        data["materials"] = []
        data["erp_searches"] = [{"catalog_group": "G-1", "material_numbers": ["M-9"], "complete": True}]
        ev = main.sanitize_erp_complete(Evidence.model_validate(data))
        resolution = resolve(ev)
        self.assertIsNone(resolution.answer)
        self.assertTrue(any(g.site == "erp" for g in resolution.gaps))

    def test_pending_catalog_skips_already_searched_groups(self):
        fallback = Case(tender_reference="T", scenario_date="2031-01-01T00:00:00Z",
                        bidder_mail_domain="x.example")
        spec = main.coerce_evidence({
            "revisions": [{"id": "R-1", "date": "2031-01-01T00:00:00Z", "marking": "current",
                           "requirements": [
                               {"id": "A-1", "catalog_group": "GR-1"},
                               {"id": "A-2", "catalog_group": "GR-2"},
                           ]}],
        }, fallback)
        erp = main.coerce_evidence({
            "erp_searches": [{"catalog_group": "GR-1", "material_numbers": ["M-1"], "complete": True}],
            "materials": [{"number": "M-1", "status": "active", "catalog_group": "GR-1",
                           "attributes": {"bore_mm": 40, "stroke_mm": 80, "pressure_bar": 200,
                                          "mounting": "Flansch", "certification": "EN 1"}}],
        }, fallback)
        pending = main.pending_catalog(spec, erp)
        self.assertEqual([row["catalog_group"] for row in pending], ["GR-2"])

    def test_merge_dicts_updates_same_id_and_keeps_old_fields(self):
        base = {"messages": [{"id": "m1", "sender": "a@x.example", "kind": "other", "subject": "hi"}]}
        out = merge_dicts(base, {"messages": [{"id": "m1", "metal_index_rates": [
            {"code": "MET-1", "eur_per_kg": "2.5"}]}]})
        self.assertEqual(out["messages"][0]["sender"], "a@x.example")
        self.assertEqual(out["messages"][0]["metal_index_rates"][0]["code"], "MET-1")

    def test_facts_index_lists_ids_only(self):
        text = facts_index({"positions": [{"position": "01.01", "quantity": 3}],
                            "messages": [{"id": "m1"}]})
        self.assertIn("01.01", text)
        self.assertIn("m1", text)
        self.assertNotIn("quantity", text)

    def test_parse_json_object_allows_fences(self):
        data = extract.parse_json_object('here\n```json\n{"facts": {"positions": [{"position": "2"}]}}\n```')
        self.assertEqual(data["positions"][0]["position"], "2")

    def test_merge_evidence_fills_nested_fields(self):
        fallback = Case(tender_reference="T", scenario_date="2031-01-01T00:00:00Z",
                        bidder_mail_domain="x.example")
        listed = main.coerce_evidence({
            "messages": [{"id": "m1", "sender": "kalkulation@x.example",
                          "date": "2031-01-02T00:00:00Z", "subject": "idx", "kind": "metal_index",
                          "reference": "AB-1"}],
        }, fallback)
        deep = main.coerce_evidence({
            "messages": [{"id": "m1", "sender": "kalkulation@x.example",
                          "date": "2031-01-02T00:00:00Z", "subject": "idx", "kind": "metal_index",
                          "reference": "AB-1",
                          "metal_index_rates": [{"code": "MET-1", "eur_per_kg": "3"}]}],
        }, fallback)
        merged = merge(fallback, listed, deep)
        self.assertEqual(merged.messages[0].metal_index_rates[0].code, "MET-1")
        self.assertEqual(merged.messages[0].subject, "idx")


if __name__ == "__main__":
    unittest.main()
