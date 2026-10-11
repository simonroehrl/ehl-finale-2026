import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

from loop import Scripted, pursue  # noqa: E402
from store import Store, case_from_brief  # noqa: E402


class FakeDriver:
    def __init__(self, pages: list[dict]):
        self.pages = list(pages)
        self.signed = False
        self.username = "u"
        self.password = "p"
        self.opened_text = ""

    async def snapshot(self) -> dict:
        return self.pages[0]

    async def reading(self):
        page = self.pages[0]
        return page.get("text") or "", page.get("tables") or []

    async def perform(self, action) -> str:
        if action.op == "back" and getattr(self, "_prev", None) is not None:
            self.pages.insert(0, self._prev)
            self._prev = None
            return "navigated"
        if action.op == "click" and len(self.pages) > 1:
            self._prev = self.pages[0]
            self.pages.pop(0)
            return "navigated"
        if action.op == "fill":
            return "filled"
        return "navigated"


class Loop(unittest.IsolatedAsyncioTestCase):
    async def test_model_opens_a_page_and_code_reads_it(self):
        store = Store(case_from_brief(
            "Tender AB-2031-9 (AB-2031-9). Scenario date is 2031-03-10T09:00:00Z. "
            "Mailbox vertrieb@firma.example."))
        register = {
            "url": "https://spec.example/list", "title": "Docs",
            "nodes": [{"i": 1, "role": "link", "name": "Open R-3"}],
            "text": "Register\nR-3 2031-01-01\n",
        }
        document = {
            "url": "https://spec.example/r-3", "title": "R-3",
            "nodes": [{"i": 2, "role": "button", "name": "Back"}],
            "text": (
                "Revision R-3\nKennzeichnung: gültig\nAnforderung A-1\n"
                "Bohrung: 50 mm\nHub: 100 mm\nDruck: 200 bar\n"
                "Befestigung: Flansch\nZertifizierung: EN 1\nKataloggruppe: Hydraulikzylinder\n"
            ) * 2,
        }
        driver = FakeDriver([register, document])
        model = Scripted([
            '{"actions":[{"op":"click","target":1}]}',
            '{"actions":[{"op":"done"}]}',
        ])
        status = await pursue(driver, model, store, "Open each revision document.",
                              role="spec", max_steps=6, deadline=__import__("time").monotonic() + 30)
        self.assertEqual(status, "done")
        self.assertEqual(store.evidence.revisions[0].id, "R-3")
        self.assertEqual(store.evidence.revisions[0].marking_raw, "gültig")
        self.assertEqual(store.evidence.revisions[0].requirements[0].catalog_group, "Hydraulikzylinder")

    async def test_helper_opens_listed_rows_the_model_only_landed_on(self):
        store = Store(case_from_brief(
            "Tender AB-2031-9 (AB-2031-9). Scenario date is 2031-03-10T09:00:00Z. "
            "Mailbox vertrieb@firma.example."))
        document = (
            "Revision R-3\nKennzeichnung: gültig\nAnforderung A-1\n"
            "Bohrung: 50 mm\nHub: 100 mm\nDruck: 200 bar\n"
            "Befestigung: Flansch\nZertifizierung: EN 1\nKataloggruppe: Hydraulikzylinder\n"
        ) * 2
        register = {
            "url": "https://spec.example/list", "title": "Docs",
            "nodes": [{"i": 3, "role": "button", "name": "Aktionen", "row": "R-3 2031-01-01 Aktionen"}],
            "text": "Document register\n" + ("listed row R-3 dated 2031-01-01\n" * 6),
            "tables": [[["Dokument", "Datum"], ["R-3", "2031-01-01"]]],
        }
        opened = {
            "url": "https://spec.example/r-3", "title": "R-3",
            "nodes": [{"i": 9, "role": "button", "name": "Zurück"}],
            "text": document,
        }
        driver = FakeDriver([register, opened])
        model = Scripted(['{"actions":[{"op":"done"}]}'])
        status = await pursue(driver, model, store, "Find revision documents.",
                              role="spec", max_steps=4, deadline=__import__("time").monotonic() + 30)
        self.assertEqual(status, "done")
        self.assertEqual(store.evidence.revisions[0].marking_raw, "gültig")
        self.assertTrue(store.evidence.revisions[0].requirements)


if __name__ == "__main__":
    unittest.main()
