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

    def test_unknown_gap_site_has_no_slot(self):
        self.assertIsNone(slot_for_gap(slots(), "unknown-site"))


class Seeds(unittest.IsolatedAsyncioTestCase):
    def test_seeds_skip_erp(self):
        board = Board(slots())
        self.assertEqual({t.site for t in board.pending.values()},
                         {"tender-portal", "customer-portal", "mail"})
        self.assertFalse(any(t.site == "erp" for t in board.pending.values()))

    async def test_claim_is_exclusive(self):
        board = Board(slots())
        tender = slot_for_gap(board.slots, "tender-portal")
        first, second = await asyncio.gather(board.claim(tender), board.claim(tender))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first.key, second.key)
        self.assertEqual(len(board.open_for(tender)), 0)

    async def test_failed_task_is_retried(self):
        board = Board(slots())
        mail = slot_for_gap(board.slots, "mail")
        task = await board.claim(mail)
        self.assertIsNotNone(task)
        await board.absorb(task, None)
        again = await board.claim(mail)
        self.assertEqual(again.key, task.key)

    async def test_revision_opens_an_erp_search(self):
        board = Board(slots())
        spec = slot_for_gap(board.slots, "customer-portal")
        task = await board.claim(spec)
        await board.absorb(task, with_revision())
        erp = slot_for_gap(board.slots, "erp")
        opened = board.open_for(erp)
        self.assertTrue(opened, board.pending)
        self.assertTrue(any("Kataloggruppe" in t.question or "G-1" in t.question for t in opened))

    async def test_complete_bid_clears_the_board(self):
        board = Board(slots())
        # Enough invented records for resolver to price a bid (same shape as test_resolver).
        priced = Evidence.model_validate({
            "case": {"tender_reference": "AB-1000", "scenario_date": "2030-03-10T09:00:00Z",
                     "bidder_mail_domain": "firma.example"},
            "positions": [{"position": "1", "quantity": 10, "unit_raw": "Stk", "unit_kind": "piece",
                           "requirement_id": "R-1"}],
            "revisions": [{"id": "REV-2", "marking": "current", "requirements": [
                {"id": "R-1", "bore_mm": 50, "stroke_mm": 100, "pressure_bar": 200,
                 "mounting": "Flansch", "certification": "EN 1", "catalog_group": "G-1"}]}],
            "materials": [{"number": "M-1", "status": "active", "catalog_group": "G-1",
                           "attributes": {"bore_mm": 50, "stroke_mm": 100, "pressure_bar": 200,
                                          "mounting": "Flansch", "certification": "EN 1"},
                           "stock_pieces": 1000, "stock_lead_time_weeks": 2,
                           "production_lead_time_weeks": 8,
                           "price_conditions": [{"valid_from": "2030-01-01", "valid_to": "2030-12-31",
                                                 "scales": [{"min_quantity": 1, "price": "100.00"}]}]}],
            "erp_searches": [{"catalog_group": "G-1", "material_numbers": ["M-1"], "complete": True}],
        })
        spec = slot_for_gap(board.slots, "customer-portal")
        task = await board.claim(spec)
        await board.absorb(task, priced)
        self.assertTrue(board.is_complete())
        self.assertFalse(board.has_work())

    async def test_board_json_updates_after_merge(self):
        board = Board(slots())
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "board.json"
            tender = slot_for_gap(board.slots, "tender-portal")
            task = await board.claim(tender)
            await board.absorb(task, with_positions(), path)
            snapshot = board.snapshot()
            self.assertEqual(len(snapshot["known"]["positions"]), 1)
            text = path.read_text(encoding="utf-8")
            self.assertIn('"position": "1"', text)


if __name__ == "__main__":
    unittest.main()
