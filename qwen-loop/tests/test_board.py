"""Task board: seeds, site routing, merge-then-refresh. Invented records only."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

from board import Board, Task, assign_slots, slot_for_gap, task_for_slot  # noqa: E402
from evidence import Evidence  # noqa: E402

ACCESS = [
    {"siteId": "tender-portal", "url": "https://tender.example.test"},
    {"siteId": "customer-portal", "url": "https://spec.example.test"},
    {"siteId": "erp", "url": "https://erp.example.test"},
    {"siteId": "mail", "url": "https://mail.example.test"},
]
CDPS = [f"http://127.0.0.1:922{n}" for n in range(2, 6)]


def slots():
    return assign_slots(ACCESS, CDPS)


def case_only() -> Evidence:
    return Evidence.model_validate({
        "case": {"tender_reference": "AB-1000", "scenario_date": "2030-03-10T09:00:00Z",
                 "bidder_mail_domain": "firma.example"},
    })


def with_positions() -> Evidence:
    data = case_only().model_dump(mode="json")
    data["positions"] = [{"position": "1", "quantity": 10, "unit_raw": "Stk", "unit_kind": "piece",
                          "requirement_id": "R-1"}]
    return Evidence.model_validate(data)


def with_revision() -> Evidence:
    data = with_positions().model_dump(mode="json")
    data["revisions"] = [{
        "id": "REV-2", "marking": "current",
        "requirements": [{"id": "R-1", "bore_mm": 50, "stroke_mm": 100, "pressure_bar": 200,
                          "mounting": "Flansch", "certification": "EN 1", "catalog_group": "G-1"}],
    }]
    return Evidence.model_validate(data)


class Routing(unittest.TestCase):
    def test_one_slot_per_website(self):
        found = slots()
        self.assertEqual([s["name"] for s in found],
                         ["tender-portal", "customer-portal", "erp", "mail"])
        self.assertEqual(slot_for_gap(found, "erp")["name"], "erp")
        self.assertIs(task_for_slot(found, Task("mail", "anything")), found[3])


class Seeds(unittest.IsolatedAsyncioTestCase):
    def test_seeds_skip_erp(self):
        board = Board(slots())
        self.assertEqual({t.site for t in board.pending.values()},
                         {"tender-portal", "customer-portal", "mail"})

    async def test_claim_is_exclusive(self):
        board = Board(slots())
        tender = slot_for_gap(board.slots, "tender-portal")
        first, second = await asyncio.gather(board.claim(tender), board.claim(tender))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.key, second.key)

    async def test_publish_opens_erp_while_spec_still_runs(self):
        board = Board(slots())
        spec = slot_for_gap(board.slots, "customer-portal")
        task = await board.claim(spec)
        self.assertIsNotNone(task)
        await board.publish(with_revision())
        self.assertIn(task.key, board.inflight)
        opened = board.open_for(slot_for_gap(board.slots, "erp"))
        self.assertTrue(opened)

    async def test_revision_opens_an_erp_search(self):
        board = Board(slots())
        spec = slot_for_gap(board.slots, "customer-portal")
        task = await board.claim(spec)
        await board.absorb(task, with_revision())
        opened = board.open_for(slot_for_gap(board.slots, "erp"))
        self.assertTrue(opened)

    async def test_leitung_mail_fills_pending_domain(self):
        board = Board(slots())
        mail = Evidence.model_validate({
            "case": {"tender_reference": "AB-1000", "scenario_date": "2030-03-10T09:00:00Z",
                     "bidder_mail_domain": "pending.local"},
            "messages": [{"id": "m1", "sender": "leitung@firma.example", "date": "2030-03-01T10:00:00Z",
                          "subject": "ok", "reference": None, "kind": "other"}],
        })
        spec = slot_for_gap(board.slots, "mail")
        task = await board.claim(spec)
        await board.absorb(task, mail)
        self.assertEqual(board.evidence.case.bidder_mail_domain, "firma.example")

    async def test_board_json_updates_after_merge(self):
        board = Board(slots())
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "board.json"
            tender = slot_for_gap(board.slots, "tender-portal")
            task = await board.claim(tender)
            await board.absorb(task, with_positions(), path)
            self.assertIn('"position": "1"', path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
