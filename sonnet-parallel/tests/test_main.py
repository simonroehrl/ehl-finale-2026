"""Submit goal, idle kill, checkpoint — the sonnet-100 pieces this folder keeps."""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

import main  # noqa: E402
from board import assign_slots  # noqa: E402


class Submit(unittest.TestCase):
    def test_submit_stalled_detects_mcp_init(self):
        self.assertTrue(main.submit_stalled({
            "result": "TimeoutError: async initializeServer. The browser tools never connected.",
        }))
        self.assertTrue(main.submit_stalled({
            "result": "the browser server never finished starting up",
        }))
        self.assertFalse(main.submit_stalled({
            "result": "Angebot verbindlich abgegeben. Vorgangsnummer 1",
        }))

    def test_submit_succeeded_is_the_portal_goal(self):
        self.assertTrue(main.submit_succeeded({
            "result": "Angebot verbindlich abgegeben. Vielen Dank.",
        }))
        self.assertTrue(main.submit_succeeded({
            "result": "The portal confirmation: bid submitted and signed.",
        }))
        self.assertFalse(main.submit_succeeded({
            "result": "confirm Angebot verbindlich abgeben in the dialog",
        }))
        self.assertFalse(main.submit_succeeded({
            "is_error": True, "result": "idle after 25s",
        }))
        self.assertFalse(main.submit_succeeded({
            "result": "I couldn't submit the bid: the browser tools never connected.",
        }))

    def test_submit_browsers_are_tender_and_mail(self):
        found = assign_slots(
            [{"siteId": "tender-portal"}, {"siteId": "customer-portal"},
             {"siteId": "erp"}, {"siteId": "mail"}],
            [f"http://127.0.0.1:922{n}" for n in range(2, 6)])
        browsers = main.submit_browsers(found)
        self.assertEqual(set(browsers), {"tender-portal", "mail"})
        self.assertNotIn("erp", browsers)

    def test_checkpoint_saves_a_killed_session(self):
        with tempfile.TemporaryDirectory() as raw:
            session = Path(raw)
            (session / "evidence.json").write_text(
                '{"case": {"tender_reference": "T-TEST-1", "scenario_date": "2026-01-01T00:00:00Z",'
                ' "bidder_mail_domain": "example.test"},'
                ' "positions": [{"position": "10", "quantity": "4", "unit_raw": "Stk",'
                ' "unit_kind": "piece"}]}',
                encoding="utf-8")
            found = main.evidence_from({"is_error": True, "result": "402 payment required"}, session)
            self.assertIsNotNone(found)
            self.assertEqual(len(found.positions), 1)

    def test_evidence_of_explains_empty_error(self):
        with self.assertRaises(SystemExit):
            main.evidence_of({"is_error": True, "result": "402 payment required"})
        with self.assertRaises(SystemExit):
            main.evidence_of({"result": ""})

    def test_wait_budget_kills_a_silent_tool(self):
        left, why = main.wait_budget(now=100.0, started=0.0, last_progress=70.0, limit_s=420, idle_s=25)
        self.assertEqual(why, "idle")
        self.assertEqual(left, 0.0)

    def test_known_text_lists_what_colleagues_found(self):
        from evidence import Evidence
        empty = main.known_text(None)
        self.assertIn("nothing yet", empty)
        found = Evidence.model_validate({
            "case": {"tender_reference": "AB-1000", "scenario_date": "2030-03-10T09:00:00Z",
                     "bidder_mail_domain": "firma.example"},
            "positions": [{"position": "1", "quantity": 10, "unit_raw": "Stk", "unit_kind": "piece"}],
            "erp_searches": [{"catalog_group": "G-1", "material_numbers": ["M-1"], "complete": True}],
        })
        text = main.known_text(found)
        self.assertIn("1 positions", text)
        self.assertIn("G-1", text)


if __name__ == "__main__":
    unittest.main()
