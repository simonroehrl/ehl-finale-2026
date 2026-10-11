"""Helpers for Claude Code tool flags. Invented paths only."""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

import main  # noqa: E402

CASE = {
    "tender_reference": "T-TEST-1",
    "scenario_date": "2026-01-01T00:00:00Z",
    "bidder_mail_domain": "example.test",
}
POSITION = {"position": "10", "quantity": "4", "unit_raw": "Stk", "unit_kind": "piece"}
EVIDENCE = {"case": CASE, "positions": [POSITION]}


class ToolFlags(unittest.TestCase):
    def test_retrieval_keeps_js_tools(self):
        flags = main.claude_tool_flags({"browser": "http://127.0.0.1:9301"}, allow_js=True)
        self.assertIn("mcp__browser", flags)
        self.assertIn("Write", flags)
        self.assertNotIn("browser_run_code_unsafe", flags)

    def test_submit_disallows_page_js(self):
        flags = main.claude_tool_flags({"tender": "http://127.0.0.1:9301", "mail": "http://127.0.0.1:9302"},
                                       allow_js=False)
        self.assertIn("mcp__tender", flags)
        self.assertIn("mcp__mail", flags)
        self.assertIn("browser_run_code_unsafe", flags)
        self.assertIn("browser_evaluate", flags)

    def test_evidence_of_explains_empty_session(self):
        with self.assertRaises(SystemExit):
            main.evidence_of({"is_error": True, "result": "402 payment required"})
        with self.assertRaises(SystemExit):
            main.evidence_of({"result": "no structured output"})

    def test_evidence_of_keeps_structured_output_after_kill(self):
        found = main.evidence_of({"is_error": True, "result": "deadline after 720s",
                                  "structured_output": EVIDENCE})
        self.assertEqual(found.case.tender_reference, "T-TEST-1")
        self.assertEqual(len(found.positions), 1)

    def test_mcp_names_match_cdp_keys(self):
        cfg = main.mcp_config({"tender": "http://example.invalid:1", "mail": "http://example.invalid:2"},
                              Path("/tmp/dl"))
        self.assertEqual(set(cfg["mcpServers"]), {"tender", "mail"})
        self.assertIn("15000", cfg["mcpServers"]["tender"]["args"])

    def test_try_evidence_none_on_error(self):
        self.assertIsNone(main.try_evidence({"is_error": True, "result": "idle"}))

    def test_wait_budget_idle_ignores_deadline_slack(self):
        left, why = main.wait_budget(now=100.0, started=0.0, last_progress=10.0, limit_s=720, idle_s=90)
        self.assertEqual(why, "idle")
        self.assertEqual(left, 0.0)

    def test_wait_budget_deadline_before_idle(self):
        left, why = main.wait_budget(now=720.0, started=0.0, last_progress=700.0, limit_s=720, idle_s=90)
        self.assertEqual(why, "deadline")
        self.assertEqual(left, 0.0)

    def test_progress_event_ignores_noise(self):
        self.assertTrue(main.progress_event({"type": "assistant"}))
        self.assertTrue(main.progress_event({"type": "stream_event"}))
        self.assertFalse(main.progress_event({"type": "system"}))
        self.assertFalse(main.progress_event({"type": "keepalive"}))

    def test_checkpoint_used_when_session_is_killed(self):
        with tempfile.TemporaryDirectory() as raw:
            session = Path(raw)
            (session / "evidence.json").write_text(
                '{"case": {"tender_reference": "T-TEST-1", "scenario_date": "2026-01-01T00:00:00Z",'
                ' "bidder_mail_domain": "example.test"},'
                ' "positions": [{"position": "10", "quantity": "4", "unit_raw": "Stk",'
                ' "unit_kind": "piece"}]}',
                encoding="utf-8")
            found = main.evidence_from({"is_error": True, "result": "idle after 90s"}, session)
            self.assertIsNotNone(found)
            self.assertEqual(len(found.positions), 1)

    def test_load_checkpoint_rejects_garbage(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "evidence.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(main.load_checkpoint(path))
            self.assertIsNone(main.load_checkpoint(Path(raw) / "missing.json"))

    def test_assign_slots_pairs_access_order_with_browsers(self):
        access = [{"siteId": "alpha-site", "url": "https://a.example"},
                  {"siteId": "beta site!", "url": "https://b.example"},
                  {"siteId": "gamma", "url": "https://c.example"}]
        slots = main.assign_slots(access, ["http://127.0.0.1:1", "http://127.0.0.1:2"])
        self.assertEqual([s["name"] for s in slots], ["alpha-site", "beta-site"])
        self.assertEqual(slots[0]["cdp"], "http://127.0.0.1:1")
        self.assertEqual(slots[1]["site"]["siteId"], "beta site!")

    def test_slot_for_gap_matches_id_then_hint(self):
        slots = main.assign_slots(
            [{"siteId": "portal-a", "url": "https://a.example"},
             {"siteId": "werkpost", "url": "https://m.example"},
             {"siteId": "erp-prod", "url": "https://e.example"}],
            ["http://127.0.0.1:1", "http://127.0.0.1:2", "http://127.0.0.1:3"])
        self.assertEqual(main.slot_for_gap(slots, "erp-prod")["name"], "erp-prod")
        self.assertEqual(main.slot_for_gap(slots, "mail")["name"], "werkpost")
        self.assertIsNone(main.slot_for_gap(slots, "unknown-role"))

    def test_group_gaps_runs_only_needed_sites(self):
        from resolver import Gap
        slots = main.assign_slots(
            [{"siteId": "tender-portal", "url": "https://t.example"},
             {"siteId": "erp", "url": "https://e.example"},
             {"siteId": "mail", "url": "https://m.example"}],
            ["http://127.0.0.1:1", "http://127.0.0.1:2", "http://127.0.0.1:3"])
        jobs = main.group_gaps(slots, [Gap("mail", "Find the index mail."),
                                       Gap("erp", "Search the group.")])
        names = {slot["name"] for slot, _ in jobs}
        self.assertEqual(names, {"mail", "erp"})
        self.assertNotIn("tender-portal", names)

    def test_merge_all_joins_site_parts(self):
        from evidence import Evidence
        tender = Evidence.model_validate(EVIDENCE)
        mail = Evidence.model_validate({
            "case": CASE,
            "messages": [{"id": "m1", "sender": "leitung@example.test", "date": "2026-01-01T00:00:00Z",
                          "subject": "ok", "reference": "T-TEST-1", "kind": "other"}],
        })
        merged = main.merge_all([tender, mail])
        self.assertEqual(len(merged.positions), 1)
        self.assertEqual(len(merged.messages), 1)


if __name__ == "__main__":
    unittest.main()
