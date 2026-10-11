"""Simple-agent helpers. Invented values only."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

import main  # noqa: E402
from evidence import Case, Evidence, facts_index, merge_dicts  # noqa: E402
from llm_agent import missing_keys  # noqa: E402
from resolver import resolve  # noqa: E402
from test_resolver import base  # noqa: E402


class Helpers(unittest.TestCase):
    def test_site_map(self):
        task = {"access": [{"siteId": i, "url": f"https://{i}.example/",
                            "credentials": {"username": "u", "password": "p"}}
                           for i in ("tender-portal", "customer-portal", "erp", "mail")],
                "brief": "Tender AB-2031-9. 2031-03-10T09:00:00Z. vertrieb@firma.example."}
        mapped = main.site_map(task)
        self.assertEqual(mapped["spec"]["siteId"], "customer-portal")

    def test_case_from_brief(self):
        case = main.case_from_brief(
            "Tender AB-2031-9 (AB-2031-9). Scenario date is 2031-03-10T09:00:00Z. "
            "Mailbox vertrieb@firma.example. Discount from leitung@firma.example.")
        self.assertEqual(case.tender_reference, "AB-2031-9")
        self.assertEqual(case.bidder_mail_domain, "firma.example")

    def test_empty_complete_search_is_not_no_bid(self):
        data = base()
        data["materials"] = []
        data["erp_searches"] = [{"catalog_group": "G-1", "material_numbers": ["M-9"], "complete": True}]
        ev = main.sanitize_erp(Evidence.model_validate(data))
        self.assertFalse(ev.erp_searches[0].complete)
        self.assertIsNone(resolve(ev).answer)

    def test_coerce_keeps_page_marking_words(self):
        fallback = Case(tender_reference="T", scenario_date="2031-01-01T00:00:00Z",
                        bidder_mail_domain="x.example")
        ev = main.coerce_evidence({
            "revisions": [{"id": "R-9", "date": "2031-01-01T00:00:00Z", "marking": "gültig"}],
        }, fallback)
        self.assertEqual(ev.revisions[0].marking, "gültig")

    def test_save_facts_shape_is_kept(self):
        facts = merge_dicts(None, {"positions": [{"position": "1", "quantity": 2, "unit_raw": "Stk",
                                                 "unit_kind": "piece"}]})
        self.assertIn("1", facts_index(facts))
        ev = main.coerce_evidence(facts, Case(tender_reference="T", scenario_date="2031-01-01T00:00:00Z",
                                             bidder_mail_domain="x.example"))
        self.assertEqual(ev.positions[0].position, "1")

    def test_finish_waits_for_required_lists(self):
        self.assertEqual(missing_keys(None, ["positions", "qa"]), ["positions", "qa"])
        self.assertEqual(missing_keys({"positions": [{"position": "1"}]}, ["positions", "qa"]), ["qa"])


if __name__ == "__main__":
    unittest.main()
