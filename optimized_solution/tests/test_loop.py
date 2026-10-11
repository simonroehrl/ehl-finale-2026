"""The investigation loop against a fake page and a scripted model. No network."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from loop import ScriptedModel, pursue
from metrics import Metrics
from planning import Action
from records import Ledger


class FakeDriver:
    def __init__(self):
        self.signed = False
        self.stage = 0
        self.ops = []

    async def snapshot(self):
        if self.stage == 0:
            return {
                "url": "https://example.test/login", "title": "Login",
                "nodes": [
                    {"i": 0, "role": "input", "name": "Benutzer"},
                    {"i": 1, "role": "input", "name": "Passwort"},
                    {"i": 2, "role": "button", "name": "Anmelden"},
                ],
                "text": "Anmeldung",
            }
        return {
            "url": "https://example.test/tender", "title": "Tender",
            "nodes": [
                {"i": 0, "role": "button", "name": "Positionen"},
                {"i": 1, "role": "button", "name": "Verbindlich einreichen"},
            ],
            "text": "Ausschreibung angenommen unter Beleg 100",
        }

    async def perform(self, action: Action):
        self.ops.append(action.op)
        if action.op == "click":
            self.stage = 1
            return "navigated"
        return "filled"

    async def reading(self):
        return "Position 1 Menge 4 Stück Anforderung R9"


class Loop(unittest.IsolatedAsyncioTestCase):
    async def test_model_chooses_the_click_and_extraction_is_merged(self):
        driver = FakeDriver()
        ledger = Ledger()
        metrics = Metrics()
        model = ScriptedModel([
            '{"actions":[{"op":"fill","target":0,"text":"user"},{"op":"click","target":2}]}',
            '{"actions":[{"op":"extract"}]}',
            '{"case":{"tender_reference":"T-9"},"positions":[{"position":"1","quantity":"4","unit_raw":"Stück","requirement_id":"R9"}]}',
            '{"actions":[{"op":"done"}]}',
        ], metrics)
        status = await pursue(driver, model, ledger, "Log in and read the tender.", role="tender", metrics=metrics, max_steps=6, deadline=1e12)
        self.assertEqual(status, "done")
        self.assertEqual(driver.ops, ["fill", "click"])
        self.assertEqual(ledger.ev.reference(), "T-9")
        self.assertEqual(ledger.ev.positions[0].quantity, 4)
        self.assertGreaterEqual(metrics.llm_calls, 3)

    async def test_a_second_signature_click_is_refused(self):
        driver = FakeDriver()
        driver.stage = 1
        driver.signed = True
        ledger = Ledger()
        metrics = Metrics()
        model = ScriptedModel([
            '{"actions":[{"op":"click","target":1}]}',
            '{"actions":[{"op":"done","text":"Beleg 100"}]}',
        ], metrics)
        answer = {"tender_reference": "T-9", "decision": "bid", "currency": "EUR", "discount_percent": 0,
                  "positions": [], "total_net": 0}
        status = await pursue(
            driver, model, ledger, "Sign once.", role="submit", metrics=metrics, max_steps=4,
            deadline=1e12, mode="submit", answer=answer,
        )
        self.assertEqual(status, "done")
        self.assertEqual(driver.ops, [])
        self.assertEqual(metrics.browser_actions, 0)


if __name__ == "__main__":
    unittest.main()
