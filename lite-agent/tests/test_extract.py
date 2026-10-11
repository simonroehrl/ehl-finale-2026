"""Page extract helpers. Invented page text only."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

import extract  # noqa: E402
from evidence import merge_dicts  # noqa: E402


PAGE = (
    "Bill of quantities\n"
    "01.01  10 Stk  requirement A-1\n"
    "01.02  4 Satz requirement A-2\n"
) * 8


class PageGate(unittest.TestCase):
    def test_skips_errors_clicks_and_file_listings(self):
        self.assertIsNone(extract.page_for_extract("tender__browser_click", "Clicked"))
        self.assertIsNone(extract.page_for_extract("erp__browser_evaluate", "Error: timeout"))
        self.assertIsNone(extract.page_for_extract("read_file", "Downloaded files:\n a.pdf"))
        self.assertIsNone(extract.page_for_extract("spec__browser_snapshot", "short"))

    def test_keeps_snapshots_evaluate_and_documents(self):
        self.assertTrue(extract.page_for_extract("spec__browser_snapshot", PAGE))
        self.assertTrue(extract.page_for_extract("erp__browser_evaluate", PAGE))
        self.assertTrue(extract.page_for_extract("read_file", "PDF text about a revision marking gültig " * 4))
        self.assertTrue(extract.page_for_extract("tender__browser_navigate", PAGE + "\n" + "x" * 200))

    def test_skips_login_cookie_and_js_probes(self):
        login = (
            "- heading Login\n- textbox Benutzername\n- textbox Passwort\n- button Anmelden\n"
            "- text Bitte anmelden um fortzufahren\n"
        ) * 8
        cookie = ("Wir verwenden Cookies.\n- button Alle akzeptieren\n- button Nur notwendige\n"
                  "Einwilligung und Consent verwalten.\n") * 4
        probe = "iframes:0\nrows=12\nlen=4400\nhas011=true\n" + ("debug " * 40)
        self.assertIsNone(extract.page_for_extract("spec__browser_snapshot", login))
        self.assertIsNone(extract.page_for_extract("tender__browser_navigate", cookie))
        self.assertIsNone(extract.page_for_extract("spec__browser_evaluate", probe))
        self.assertTrue(extract.page_for_extract(
            "mail__browser_evaluate",
            "Von: desk@firma.example\nBetreff: Freigabe\n" + PAGE,
        ))


class Apply(unittest.TestCase):
    def test_fenced_json_merges_by_id(self):
        base = {"positions": [{"position": "1", "quantity": 3}]}
        out = extract.apply_extract(base, '```json\n{"positions": [{"position": "1", "unit_kind": "piece"}]}\n```')
        self.assertEqual(out["positions"][0]["quantity"], 3)
        self.assertEqual(out["positions"][0]["unit_kind"], "piece")

    def test_extract_messages_include_the_page_and_mode(self):
        messages = extract.extract_messages({"type": "object"}, None, PAGE, "marking")
        blob = "\n".join(m["content"] for m in messages)
        self.assertIn("marking exactly as the page writes it", blob)
        self.assertIn("01.01", blob)


class ExtractCall(unittest.IsolatedAsyncioTestCase):
    async def test_extract_page_merges_model_json(self):
        class Fake:
            async def create(self, **_kw):
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(
                        content='{"materials": [{"number": "M-1", "status": "active"}]}'))],
                    usage=None,
                )

        client = SimpleNamespace(chat=SimpleNamespace(completions=Fake()))
        facts, response = await extract.extract_page(
            client, "qwen", {"type": "object"}, None, PAGE, "survey")
        self.assertEqual(facts["materials"][0]["number"], "M-1")
        self.assertTrue(response.choices)

    def test_merge_dicts_used_by_extract_keeps_old_fields(self):
        out = merge_dicts({"messages": [{"id": "m1", "kind": "other"}]},
                          {"messages": [{"id": "m1", "subject": "idx"}]})
        self.assertEqual(out["messages"][0]["kind"], "other")
        self.assertEqual(out["messages"][0]["subject"], "idx")


if __name__ == "__main__":
    unittest.main()
