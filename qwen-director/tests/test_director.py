"""Director heuristics. Invented snapshots only."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

from pages import case_link_ids, close_ids, looks_document, looks_login, login_submit_ids, parse_fork, pick_login_fields, task_link_ids  # noqa: E402


class PageKind(unittest.TestCase):
    def test_login_has_a_password_box(self):
        self.assertTrue(looks_login({
            "nodes": [{"i": 1, "type": "text", "name": "Benutzername"},
                      {"i": 2, "type": "password", "name": "Passwort"}],
            "text": "Anmelden",
        }))
        self.assertFalse(looks_login({
            "nodes": [{"i": 1, "type": "", "name": "Position 10"}],
            "text": "LV",
        }))

    def test_qa_list_is_a_document(self):
        text = "Bieterfragen\n" + "Rückfrage 12 Eingang 01.02.2027\n" * 10
        self.assertTrue(looks_document({"nodes": [], "text": text}, "qa"))

    def test_mail_list_is_a_document(self):
        text = "Posteingang\n" + "leitung@firma.example Betreff Angebot\n" * 8
        self.assertTrue(looks_document({"nodes": [], "text": text}, "mail"))

    def test_document_needs_substance(self):
        self.assertFalse(looks_document({"nodes": [], "text": "Home"}))
        self.assertTrue(looks_document({
            "nodes": [{"i": 1, "type": "", "name": "weiter"}],
            "text": "Position 10\nPosition 20\n" + "Menge 4 Stueck Preis 1.234,50\n" * 12,
        }))

    def test_anmelden_is_submit_not_username(self):
        nodes = [
            {"i": 1, "type": "text", "name": "Benutzername", "role": "textbox"},
            {"i": 2, "type": "password", "name": "Passwort", "role": "textbox"},
            {"i": 3, "type": "submit", "name": "Anmelden", "role": "button"},
        ]
        user, password = pick_login_fields(nodes)
        self.assertEqual(user, 1)
        self.assertEqual(password, 2)
        self.assertEqual(login_submit_ids(nodes), [3])

    def test_anmelden_button_without_submit_type(self):
        nodes = [
            {"i": 1, "type": "text", "name": "Benutzername"},
            {"i": 2, "type": "password", "name": "Passwort"},
            {"i": 3, "type": "", "name": "Anmelden", "role": "button"},
        ]
        user, password = pick_login_fields(nodes)
        self.assertEqual((user, password), (1, 2))
        self.assertEqual(login_submit_ids(nodes), [3])

    def test_fork_empty(self):
        with self.assertRaises(ValueError):
            parse_fork("")
        with self.assertRaises(ValueError):
            parse_fork("   \n")

    def test_fork_one_line(self):
        page, action = parse_fork("click 12")
        self.assertEqual(action.op, "click")
        self.assertEqual(action.target, 12)
        self.assertEqual(parse_fork("extract")[1].op, "extract")

    def test_fork_uses_last_command(self):
        page, action = parse_fork("We need to choose the next step.\nclick 3\nclick 9")
        self.assertEqual(action.target, 9)

    def test_fork_ignores_example_prose(self):
        with self.assertRaises(ValueError):
            parse_fork("click 12 is the format\nOptions: click extract done")

    def test_close_modal(self):
        self.assertEqual(close_ids([
            {"i": 10, "name": "Akzeptieren und fortfahren"},
            {"i": 11, "name": "Schließen"},
        ]), [11])

    def test_case_link_follows_brief_reference(self):
        snap = {"nodes": [
            {"i": 2, "name": "Open AB-1000", "href": "/t/AB-1000"},
            {"i": 3, "name": "Other", "href": "/x"},
        ]}
        self.assertEqual(case_link_ids(snap, "AB-1000"), [2])
        self.assertEqual(case_link_ids(snap, ""), [])

    def test_task_links(self):
        snap = {"nodes": [
            {"i": 4, "name": "Leistungsverzeichnis", "href": "/lv"},
            {"i": 5, "name": "Home", "href": "/"},
            {"i": 6, "name": "Portalumfrage", "href": "/portal-survey"},
            {"i": 7, "name": "Übersicht"},
            {"i": 8, "name": "Unterlagen"},
            {"i": 9, "name": "Bieterfragen"},
        ]}
        self.assertEqual(task_link_ids(snap, "positions"), [4, 8])
        self.assertEqual(task_link_ids(snap, "qa"), [9])

    def test_fork_truncated_json(self):
        page, action = parse_fork('{"op":"click","target":7,"text":"this will be cut')
        self.assertEqual(action.op, "click")
        self.assertEqual(action.target, 7)

    def test_fork_json(self):
        page, action = parse_fork('{"page":"document","op":"extract","target":0}')
        self.assertEqual(page, "document")
        self.assertEqual(action.op, "extract")


if __name__ == "__main__":
    unittest.main()
