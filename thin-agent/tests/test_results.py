import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

from results import find_controls, ids_on_snapshot, listed_from, opened_from  # noqa: E402


class Results(unittest.TestCase):
    def test_aktionen_button_matches_via_row_text(self):
        snapshot = {"nodes": [
            {"i": 1, "role": "button", "name": "Menü"},
            {"i": 3, "role": "button", "name": "Aktionen", "row": "R-3 2031-01-01 Aktionen"},
        ]}
        self.assertEqual(find_controls(snapshot, "R-3"), [3])
        self.assertEqual(ids_on_snapshot(snapshot, {"AB-2031-9"}), ["R-3"])

    def test_register_row_is_listed_not_opened(self):
        facts = {"revisions": [{"id": "R-3", "marking": "unknown"}]}
        self.assertEqual(listed_from(facts, "spec"), ["R-3"])
        self.assertEqual(opened_from(facts, "spec"), [])
        facts["revisions"][0]["marking_raw"] = "gültig"
        facts["revisions"][0]["requirements"] = [{"id": "A-1", "catalog_group": "G"}]
        self.assertEqual(opened_from(facts, "spec"), ["R-3"])

    def test_tender_reference_is_not_a_list_id(self):
        snapshot = {"nodes": [
            {"i": 1, "role": "link", "name": "AB-2031-9", "row": "tender AB-2031-9"},
            {"i": 2, "role": "link", "name": "Open R-3"},
        ]}
        self.assertEqual(ids_on_snapshot(snapshot, {"AB-2031-9"}), ["R-3"])
