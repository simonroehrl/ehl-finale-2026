"""Code read of one page the model just opened. No second model. Invented layouts only in tests."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from resolver import CURRENT_MARKINGS, SUPERSEDED_MARKINGS

_LOGIN_SECRET = ("passwort", "password", "kennwort")
_LOGIN_ID = ("benutzername", "username", "anmelden", "einloggen", "sign in", "log in", "login")
_COOKIE = ("cookie", "akzeptieren", "accept all", "einwilligung", "consent")
_POS = re.compile(
    r"(?m)^\s*(\d{1,2}\.\d{2})\s+(\d+(?:[.,]\d+)?)\s+(Stk|Stück|Stck|Satz|pcs|pc)\b(?:\s+(\S.+))?",
    re.I,
)
_WHEN = re.compile(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)?")
_MM = re.compile(r"(?i)(?:bohrung|bore)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*mm")
_STROKE = re.compile(r"(?i)(?:hub|stellweg|stroke|nutzbarer weg)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*mm")
_BAR = re.compile(r"(?i)(?:druck|pressure)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*bar")
_MOUNT = re.compile(r"(?i)(?:befestigung|mounting)\s*[:=]\s*([^\n,;]+)")
_CERT = re.compile(r"(?i)(?:zertifiz(?:ierung)?|certif(?:ication)?)\s*[:=]\s*([^\n,;]+)")
_GROUP = re.compile(r"(?i)(?:kataloggruppe|catalog(?:ue)?\s*group)\s*[:=]\s*([^\n,;]+)")
_REQ = re.compile(r"(?i)(?:anforderung|requirement|forderung)\s*[:=]?\s*([A-Z0-9][-A-Z0-9./]{1,20})")
_REV = re.compile(r"(?i)(?:revision|fassung|rev(?:ision)?)\s*[:=]?\s*([A-Z0-9][-A-Z0-9./]{1,20})")
_FROM = re.compile(r"(?im)^(?:von|from|absender)\s*:\s*(.+)$")
_SUBJ = re.compile(r"(?im)^(?:betreff|subject)\s*:\s*(.+)$")
_REF = re.compile(r"(?im)^(?:referenz|reference|az)\s*:\s*([A-Z0-9][-A-Z0-9/]{4,})$")
_PCT = re.compile(r"(?i)(?:rabatt|discount|genehmigt)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*%")
_TAN = re.compile(r"(?i)\b(?:TAN|transaktionsnummer)\s*[:=]\s*([A-Z0-9]{4,12})\b")
_MAT = re.compile(r"(?im)^(?:material|artikel|mat(?:erial)?(?:-?nr\.?)?)\s*[:=]\s*([A-Z0-9][-A-Z0-9./]{2,})$")
_STOCK = re.compile(r"(?i)(?:lagerbestand|stock|bestand)\s*[:=]?\s*(\d+)")
_LEAD_STOCK = re.compile(r"(?i)(?:lieferzeit\s*(?:lager|stock)|stock\s*lead)\s*[:=]?\s*(\d+)")
_LEAD_PROD = re.compile(r"(?i)(?:lieferzeit\s*(?:fertigung|produktion)|production\s*lead)\s*[:=]?\s*(\d+)")
_SET = re.compile(r"(?i)(?:stücke\s*pro\s*satz|pieces\s*per\s*set)\s*[:=]?\s*(\d+)")
_KIND_HINTS = {
    "discount_approval": ("leitung", "rabatt", "discount", "sales director"),
    "condition_rules": ("kalkulation", "konditionsregeln"),
    "metal_index": ("preisindizes", "metal index"),
    "technical_release": ("technik", "technikfreigabe"),
    "packaging_tariffs": ("verpackungstarife", "packaging"),
    "delivery_commitments": ("terminfreigabe",),
    "fx_fixing": ("devisenfixing", "fx"),
    "setup_pools": ("rüstpool", "ruestpool"),
    "project_bonus": ("projektbonus",),
    "signing_tan": ("tan", "transaktionsnummer"),
}
_COL = {
    "position": ("position", "pos.", "pos", "lv-pos"),
    "quantity": ("menge", "quantity", "anzahl"),
    "unit": ("einheit", "unit", "me"),
    "requirement": ("anforderung", "requirement", "forderung"),
    "material": ("material", "artikel", "mat."),
    "status": ("status", "statuscode"),
    "subject": ("betreff", "subject"),
    "sender": ("von", "from", "absender"),
    "date": ("datum", "date"),
    "revision": ("revision", "fassung", "dokument", "id"),
    "marking": ("kennung", "gültigkeit", "marking"),
    "catalog": ("kataloggruppe", "catalog"),
}


def dec(value) -> Decimal | None:
    text = str(value).strip().replace(" ", "").replace(".", "").replace(",", ".") if "," in str(value) and str(value).count(",") == 1 else str(value).strip().replace(",", "")
    if re.fullmatch(r"\d+[.,]\d{3}", str(value).strip()) and "," not in str(value):
        text = str(value).strip()
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        try:
            return Decimal(str(value).replace(",", "."))
        except (InvalidOperation, ValueError):
            return None


def unit_kind(raw: str) -> str:
    low = (raw or "").lower()
    if "satz" in low:
        return "set"
    if any(w in low for w in ("stk", "stück", "stck", "pcs", "pc", "stück")):
        return "piece"
    return "other"


def looks_login(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in _LOGIN_SECRET) and any(w in low for w in _LOGIN_ID)


def looks_cookie_wall(text: str) -> bool:
    low = text.lower()
    return sum(low.count(w) for w in _COOKIE) >= 3 and len(text) < 2000


def worth_reading(text: str) -> bool:
    body = (text or "").strip()
    if len(body) < 80 or looks_login(body) or looks_cookie_wall(body):
        return False
    return True


def _marking_words(text: str) -> str | None:
    low = " ".join(text.lower().split())
    if any(token in low for token in SUPERSEDED_MARKINGS):
        for token in SUPERSEDED_MARKINGS:
            if token in low:
                return token
    if any(token in low for token in CURRENT_MARKINGS):
        for token in CURRENT_MARKINGS:
            if token in low:
                return token
    return None


def _kind(sender: str, subject: str, body: str, senders: dict[str, str] | None) -> str:
    blob = f"{sender} {subject} {body}".lower()
    if _TAN.search(body) or "tan" in subject.lower():
        return "signing_tan"
    local = sender.split("@", 1)[0].lower() if "@" in sender else ""
    for kind, default in (senders or {}).items():
        if local == default.lower():
            return kind
    for kind, words in _KIND_HINTS.items():
        if any(w in blob for w in words):
            return kind
    return "other"


def _headers(row: list[str]) -> dict[str, int]:
    found = {}
    for index, cell in enumerate(row):
        low = cell.lower().strip()
        for name, words in _COL.items():
            if name not in found and any(w in low for w in words):
                found[name] = index
    return found


def _cell(row: list[str], headers: dict[str, int], name: str) -> str:
    index = headers.get(name)
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def from_tables(tables: list[list[list[str]]]) -> dict:
    facts: dict = {"positions": [], "revisions": [], "materials": [], "messages": []}
    for table in tables or []:
        if len(table) < 2:
            continue
        headers = _headers(table[0])
        if not headers:
            continue
        for row in table[1:]:
            if not any(row):
                continue
            if "position" in headers and "quantity" in headers:
                qty = dec(_cell(row, headers, "quantity"))
                unit = _cell(row, headers, "unit") or "Stk"
                if qty is None:
                    continue
                facts["positions"].append({
                    "position": _cell(row, headers, "position"),
                    "quantity": str(qty),
                    "unit_raw": unit,
                    "unit_kind": unit_kind(unit),
                    "requirement_id": _cell(row, headers, "requirement") or None,
                })
            if "revision" in headers:
                ident = _cell(row, headers, "revision")
                if ident:
                    mark = _cell(row, headers, "marking") or "unknown"
                    facts["revisions"].append({
                        "id": ident,
                        "marking": mark if mark in ("current", "superseded", "unknown") else "unknown",
                        "marking_raw": None if mark in ("current", "superseded", "unknown") else mark or None,
                    })
            if "material" in headers:
                number = _cell(row, headers, "material")
                if number:
                    status_raw = _cell(row, headers, "status")
                    low = status_raw.lower()
                    status = "blocked" if any(w in low for w in ("gesperrt", "blocked", "inaktiv")) else (
                        "active" if any(w in low for w in ("aktiv", "active", "frei")) else "other")
                    facts["materials"].append({
                        "number": number,
                        "status": status,
                        "status_raw": status_raw or None,
                        "catalog_group": _cell(row, headers, "catalog") or None,
                        "attributes": {},
                    })
            if "subject" in headers and "sender" in headers:
                sender = _cell(row, headers, "sender")
                subject = _cell(row, headers, "subject")
                when = _WHEN.search(_cell(row, headers, "date")) 
                if sender and subject and when:
                    facts["messages"].append({
                        "id": f"{sender}:{when.group(0)}:{subject[:40]}",
                        "sender": sender,
                        "date": when.group(0),
                        "subject": subject,
                        "kind": "other",
                    })
    for name in list(facts):
        if not facts[name]:
            del facts[name]
    return facts


def from_text(text: str, senders: dict[str, str] | None = None) -> dict:
    facts: dict = {}
    positions = []
    for match in _POS.finditer(text):
        qty = dec(match.group(2))
        if qty is None:
            continue
        rest = (match.group(4) or "").strip()
        req = _REQ.search(rest)
        positions.append({
            "position": match.group(1),
            "quantity": str(qty),
            "unit_raw": match.group(3),
            "unit_kind": unit_kind(match.group(3)),
            "requirement_id": req.group(1) if req else (rest or None),
        })
    if positions:
        facts["positions"] = positions

    attrs = {}
    if (m := _MM.search(text)):
        attrs["bore_mm"] = str(dec(m.group(1)) or m.group(1))
    if (m := _STROKE.search(text)):
        attrs["stroke_mm"] = str(dec(m.group(1)) or m.group(1))
    if (m := _BAR.search(text)):
        attrs["pressure_bar"] = str(dec(m.group(1)) or m.group(1))
    if (m := _MOUNT.search(text)):
        attrs["mounting"] = m.group(1).strip()
    if (m := _CERT.search(text)):
        attrs["certification"] = m.group(1).strip()
    group = _GROUP.search(text)
    req = _REQ.search(text)
    rev = _REV.search(text)
    pieces = _SET.search(text)
    mark = _marking_words(text)
    if rev or (attrs and req):
        requirement = None
        if req:
            requirement = {"id": req.group(1), **attrs}
            if group:
                requirement["catalog_group"] = group.group(1).strip()
            if pieces:
                requirement["pieces_per_set"] = int(pieces.group(1))
            if re.search(r"nutzbarer weg", text, re.I):
                requirement["usable_stroke"] = True
        revision = {
            "id": rev.group(1) if rev else (req.group(1) if req else "doc"),
            "marking": "unknown",
            "requirements": [requirement] if requirement else [],
        }
        if mark:
            revision["marking_raw"] = mark
        if (when := _WHEN.search(text)):
            revision["date"] = when.group(0)
        facts.setdefault("revisions", []).append(revision)

    sender = (_FROM.search(text) or [None, ""])[1].strip()
    subject = (_SUBJ.search(text) or [None, ""])[1].strip()
    if sender and subject:
        when = _WHEN.search(text)
        if when:
            kind = _kind(sender, subject, text, senders)
            msg = {
                "id": f"{sender}:{when.group(0)}:{subject[:40]}",
                "sender": sender,
                "date": when.group(0),
                "subject": subject,
                "kind": kind,
            }
            if (ref := _REF.search(text)):
                msg["reference"] = ref.group(1)
            if (pct := _PCT.search(text)):
                msg["discount_percent"] = str(dec(pct.group(1)) or pct.group(1))
            if (tan := _TAN.search(text)):
                msg["notes"] = f"tan {tan.group(1)}"
            facts.setdefault("messages", []).append(msg)

    if (mat := _MAT.search(text)) and attrs:
        status_raw = "aktiv" if re.search(r"\b(aktiv|active|frei)\b", text, re.I) else (
            "gesperrt" if re.search(r"\b(gesperrt|blocked)\b", text, re.I) else "")
        material = {
            "number": mat.group(1),
            "status": "blocked" if "gesperrt" in status_raw or "blocked" in status_raw else (
                "active" if status_raw else "other"),
            "status_raw": status_raw or None,
            "attributes": attrs,
        }
        if group:
            material["catalog_group"] = group.group(1).strip()
        if (stock := _STOCK.search(text)):
            material["stock_pieces"] = int(stock.group(1))
        if (lead := _LEAD_STOCK.search(text)):
            material["stock_lead_time_weeks"] = int(lead.group(1))
        if (lead := _LEAD_PROD.search(text)):
            material["production_lead_time_weeks"] = int(lead.group(1))
        facts.setdefault("materials", []).append(material)
    return facts


def read_page(text: str, tables: list[list[list[str]]] | None = None,
              senders: dict[str, str] | None = None) -> dict:
    """Structured records from one selected page. Empty when the page is a shell."""
    if not worth_reading(text or ""):
        return {}
    from evidence import merge_dicts
    return merge_dicts(from_tables(tables or []), from_text(text, senders))
