"""Submit-browser wiring. Invented CDP URLs only."""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

import main  # noqa: E402


class Browsers(unittest.TestCase):
    def test_pick_cdp_reserves_second(self):
        research, submit = main.pick_cdp(["http://127.0.0.1:9222", "http://127.0.0.1:9223"])
        self.assertEqual(research, "http://127.0.0.1:9222")
        self.assertEqual(submit, "http://127.0.0.1:9223")
        self.assertNotEqual(research, submit)

    def test_pick_cdp_one_browser_reuses(self):
        research, submit = main.pick_cdp(["http://127.0.0.1:9222"])
        self.assertEqual(research, submit)

    def test_pick_cdp_empty(self):
        with self.assertRaises(SystemExit):
            main.pick_cdp([])

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
        self.assertFalse(main.submit_stalled({"result": ""}))

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


if __name__ == "__main__":
    unittest.main()
