"""Code extract on invented pages only."""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent)]

import extract  # noqa: E402


BOQ = """
Leistungsverzeichnis
01.01  10 Stk  A-1
01.02  4 Satz A-2
""" * 3

DOC = """
Revision R-3
Kennzeichnung: gültig
Anforderung A-1
Bohrung: 50 mm
Hub: 100 mm
Druck: 200 bar
Befestigung: Flansch
Zertifizierung: EN 1
Kataloggruppe: Hydraulikzylinder
Stücke pro Satz: 2
"""

MAIL = """
Von: leitung@firma.example
Datum: 2031-03-01T10:00:00Z
Betreff: Rabattfreigabe AB-2031-9
Referenz: AB-2031-9
Genehmigt: 3,5 %
"""

ERP = """
Material: HY-50
Status: aktiv
Bohrung: 50 mm
Hub: 100 mm
Druck: 200 bar
Befestigung: Flansch
Zertifizierung: EN 1
Kataloggruppe: Hydraulikzylinder
Lagerbestand 20
Lieferzeit Lager 2 Wochen
Lieferzeit Fertigung 8 Wochen
"""

LOGIN = ("- heading Login\n- textbox Benutzername\n- textbox Passwort\n- button Anmelden\n") * 8


class ReadPage(unittest.TestCase):
    def test_skips_login(self):
        self.assertEqual(extract.read_page(LOGIN), {})

    def test_positions_from_prose(self):
        facts = extract.read_page(BOQ)
        numbers = {row["position"] for row in facts["positions"]}
        self.assertEqual(numbers, {"01.01", "01.02"})
        self.assertEqual(facts["positions"][1]["unit_kind"], "set")

    def test_revision_document_keeps_marking_words(self):
        facts = extract.read_page(DOC)
        rev = facts["revisions"][0]
        self.assertEqual(rev["id"], "R-3")
        self.assertEqual(rev["marking_raw"], "gültig")
        self.assertEqual(rev["requirements"][0]["catalog_group"], "Hydraulikzylinder")
        self.assertEqual(rev["requirements"][0]["bore_mm"], "50")

    def test_mail_and_material(self):
        mail = extract.read_page(MAIL, senders={"discount_approval": "leitung"})
        self.assertEqual(mail["messages"][0]["kind"], "discount_approval")
        self.assertEqual(mail["messages"][0]["discount_percent"], "3.5")
        mat = extract.read_page(ERP)
        self.assertEqual(mat["materials"][0]["number"], "HY-50")
        self.assertEqual(mat["materials"][0]["status"], "active")
        self.assertEqual(mat["materials"][0]["stock_pieces"], 20)

    def test_table_positions(self):
        table = [[
            ["Pos", "Menge", "Einheit", "Anforderung"],
            ["01.01", "10", "Stk", "A-1"],
            ["01.02", "4", "Satz", "A-2"],
        ]]
        facts = extract.read_page("Leistungsverzeichnis\n" + ("zeile " * 20), table)
        self.assertEqual(len(facts["positions"]), 2)
        self.assertEqual(facts["positions"][0]["requirement_id"], "A-1")


if __name__ == "__main__":
    unittest.main()
