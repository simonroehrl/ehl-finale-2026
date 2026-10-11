"""Action parsing and bid-field lookup. Invented numbers only."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

from actions import (format_value, lookup_value, parse_action, parse_json, portal_confirmed,  # noqa: E402
                     value_keys)
from extract import evidence_from, infer_unit, kind_for, parse_case, parse_extract  # noqa: E402
from board import Task  # noqa: E402
from evidence import Case  # noqa: E402

ANSWER = {
    "tender_reference": "AB-1000",
    "decision": "bid",
    "discount_percent": 3,
    "total_net": 1234.5,
    "positions": [{"position": "10", "material_number": "M-1", "quantity": 4,
                   "unit_price": 100.0, "lead_time_weeks": 2, "base_unit_price": 90.0,
                   "surcharge_per_unit": 8.0, "packaging_per_unit": 2.0, "rebate_total": 12.0}],
}


class Parse(unittest.TestCase):
    def test_fenced_json(self):
        self.assertEqual(parse_json("```json\n{\"op\":\"click\",\"target\":3}\n```")["op"], "click")

    def test_one_action_from_batch(self):
        action = parse_action('{"actions":[{"op":"extract"},{"op":"click","target":1}]}',
                              {"extract", "click"})
        self.assertEqual(action.op, "extract")

    def test_rejects_unknown_op(self):
        with self.assertRaises(ValueError):
            parse_action('{"op":"explode"}', {"click"})


class BidFields(unittest.TestCase):
    def test_german_amount_is_typed_by_code(self):
        self.assertEqual(lookup_value(ANSWER, None, "total_net"), "1.234,50")
        self.assertEqual(lookup_value(ANSWER, None, "position.10.unit_price"), "100,00")
        self.assertEqual(lookup_value(ANSWER, None, "position.10.quantity"), "4")
        self.assertEqual(lookup_value(ANSWER, "991122", "tan"), "991122")
        self.assertIsNone(lookup_value(ANSWER, None, "position.99.unit_price"))

    def test_plain_format(self):
        self.assertEqual(format_value("discount_percent", 3, "plain"), "3")

    def test_value_keys_list_positions(self):
        text = "\n".join(value_keys(ANSWER, None))
        self.assertIn("position.10.unit_price", text)
        self.assertIn("tan is not known yet", text)

    def test_portal_goal(self):
        self.assertTrue(portal_confirmed("Angebot verbindlich abgegeben. Vielen Dank."))
        self.assertFalse(portal_confirmed("confirm Angebot verbindlich abgeben in the dialog"))


class BriefCase(unittest.TestCase):
    def test_labeled_fields_from_invented_brief(self):
        brief = (
            "Prepare one bid for Example GmbH (AB-1000). "
            "The scenario date is 2030-03-10T09:00:00Z. "
            "Approvals come from leitung@firma.example."
        )
        found = parse_case(brief)
        self.assertEqual(found["tender_reference"], "AB-1000")
        self.assertEqual(found["scenario_date"], "2030-03-10T09:00:00Z")
        self.assertEqual(found["bidder_mail_domain"], "firma.example")

    def test_label_form(self):
        brief = "Tender reference: XY-9. The scenario date is 2031-04-01T08:00:00+02:00. Mailbox domain: mail.test"
        found = parse_case(brief)
        self.assertEqual(found["tender_reference"], "XY-9")
        self.assertEqual(found["bidder_mail_domain"], "mail.test")


class Extract(unittest.TestCase):
    def test_kind_from_task(self):
        self.assertEqual(kind_for(Task("tender-portal", "Record every bidder Q&A answer.")), "qa")
        self.assertEqual(kind_for(Task("erp", "Search Kataloggruppe G-1.")), "erp")

    def test_infer_set_unit(self):
        self.assertEqual(infer_unit("Satz"), "set")
        self.assertEqual(infer_unit("Stk"), "piece")

    def test_positions_drop_incomplete_rows(self):
        case = Case.model_validate({
            "tender_reference": "AB-1000", "scenario_date": "2030-03-10T09:00:00Z",
            "bidder_mail_domain": "firma.example"})
        data = parse_extract('{"positions":[{"position":"1","quantity":"10","unit_raw":"Stk"},{"quantity":"2"}]}')
        found = evidence_from(case, data, "positions")
        self.assertIsNotNone(found)
        self.assertEqual(len(found.positions), 1)
        self.assertEqual(found.positions[0].unit_kind, "piece")

    def test_empty_extract_is_none(self):
        case = Case.model_validate({
            "tender_reference": "AB-1000", "scenario_date": "2030-03-10T09:00:00Z",
            "bidder_mail_domain": "firma.example"})
        self.assertIsNone(evidence_from(case, {"positions": []}, "positions"))


if __name__ == "__main__":
    unittest.main()
