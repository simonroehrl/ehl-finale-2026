"""Code read of one page the model just opened. No second model. Invented layouts only in tests."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_POS = re.compile(
    r"(?m)^\s*(\d{1,2}\.\d{2})\s+(\d+(?:[.,]\d+)?)\s+(Stk|Stück|Stck|Satz|pcs|pc)\b(?:\s+(\S.+))?",
    re.I,
)
_MM = re.compile(r"(?i)(?:bohrung|bore)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*mm")
_STROKE = re.compile(r"(?i)(?:hub|stellweg|stroke|nutzbarer weg)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*mm")
_BAR = re.compile(r"(?i)(?:druck|pressure)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*bar")
_MOUNT = re.compile(r"(?i)(?:befestigung|mounting)\s*[:=]\s*([^\n,;]+)")
_CERT = re.compile(r"(?i)(?:zertifiz(?:ierung)?|certif(?:ication)?)\s*[:=]\s*([^\n,;]+)")
_GROUP = re.compile(r"(?i)(?:kataloggruppe|catalog(?:ue)?\s*group)\s*[:=]\s*([^\n,;]+)")
_REQ = re.compile(r"(?i)(?:anforderung|requirement)\s*[:=]\s*([A-Z][-A-Z0-9./]{1,20})")
_REV = re.compile(r"(?i)(?:revision|fassung|rev(?:ision)?)\s*[:=]?\s*([A-Z0-9][-A-Z0-9./]{1,20})")
_FROM = re.compile(r"(?im)^(?:von|from|absender)\s*:\s*(.+)$")
_SUBJ = re.compile(r"(?im)^(?:betreff|subject)\s*:\s*(.+)$")
_WHEN = re.compile(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?")
_MAT = re.compile(r"(?im)^(?:material|artikel|mat(?:erial)?(?:-?nr\.?)?)\s*[:=]\s*([A-Z0-9][-A-Z0-9./]{2,})$")
_STOCK = re.compile(r"(?i)(?:lagerbestand|stock|bestand)\s*[:=]?\s*(\d+)")
_LEAD_STOCK = re.compile(r"(?i)(?:lieferzeit\s*(?:lager|stock)|stock\s*lead)\s*[:=]?\s*(\d+)")
_LEAD_PROD = re.compile(r"(?i)(?:lieferzeit\s*(?:fertigung|produktion)|production\s*lead)\s*[:=]?\s*(\d+)")
_SET = re.compile(r"(?i)(?:stücke\s*pro\s*satz|pieces\s*per\s*set)\s*[:=]?\s*(\d+)")
_PCT = re.compile(r"(?i)(?:rabatt|discount|genehmigt)\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*%")
_MIN_ORDER = re.compile(r"(?i)(?:mindest(?:bestell|auftrag)|min(?:imum)?\s*order)\D{0,20}(\d+(?:[.,]\d+)?)")
_TAN = re.compile(r"(?i)\b(?:TAN|transaktionsnummer)\s*[:=]\s*([A-Z0-9]{4,12})\b")
_SCALE = re.compile(r"(?i)(?:ab|from|min)?\s*(\d{1,5})\s*(?:stk|stück|pcs)?\s*[:\t]\s*(\d+[.,]\d{2})")
_RESERVE = re.compile(r"(?i)(?:endreserve|reserve)\D{0,24}(\d+(?:[.,]\d+)?)\s*mm")
_LOGIN = ("passwort", "password", "benutzername", "username")
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
    "revision": ("revision", "fassung", "dokument"),
    "marking": ("kennung", "gültigkeit", "marking", "status"),
    "catalog": ("kataloggruppe", "catalog"),
}


def dec(value) -> Decimal | None:
    raw = str(value).strip()
    text = raw.replace(" ", "")
    if "," in text and text.count(",") == 1:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def worth_reading(text: str) -> bool:
    body = (text or "").strip()
    if len(body) < 80:
        return False
    low = body.lower()
    if len(body) < 2000 and all(w in low for w in _LOGIN[:2]):
        return False
    return True


def _marking(text: str) -> str:
    low = " ".join(text.lower().split())
    for token in ("ungültig", "ungueltig", "ersetzt", "superseded"):
        if token in low:
            return token
    for token in ("gültig", "gueltig", "current", "aktuell"):
        if token in low:
            return token
    return ""


def _headers(row: list[str]) -> dict[str, int]:
    found = {}
    for index, cell in enumerate(row):
        low = cell.lower().strip()
        for name, words in _COL.items():
            if name not in found and any(word in low for word in words):
                found[name] = index
    return found


def _cell(row: list[str], headers: dict[str, int], name: str) -> str:
    index = headers.get(name)
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def from_tables(tables: list[list[list[str]]]) -> dict:
    facts: dict = {"positions": [], "revisions": [], "materials": [], "mails": []}
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
                if qty is None:
                    continue
                facts["positions"].append({
                    "position": _cell(row, headers, "position"),
                    "quantity": str(qty),
                    "unit_raw": _cell(row, headers, "unit") or "Stk",
                    "requirement": _cell(row, headers, "requirement") or None,
                })
            if "revision" in headers:
                ident = _cell(row, headers, "revision")
                if ident:
                    facts["revisions"].append({
                        "id": ident,
                        "marking": _cell(row, headers, "marking") or "unknown",
                    })
            if "material" in headers:
                number = _cell(row, headers, "material")
                if number:
                    status = _cell(row, headers, "status")
                    facts["materials"].append({
                        "material": number,
                        "status": status,
                        "kataloggruppe": _cell(row, headers, "catalog") or None,
                    })
            if "subject" in headers and "sender" in headers:
                sender = _cell(row, headers, "sender")
                subject = _cell(row, headers, "subject")
                if sender and subject:
                    facts["mails"].append({
                        "sender": sender,
                        "subject": subject,
                        "date": _cell(row, headers, "date"),
                    })
    return {key: value for key, value in facts.items() if value}


def _attrs(text: str) -> dict:
    attrs = {}
    if (match := _MM.search(text)):
        attrs["bore_mm"] = str(dec(match.group(1)) or match.group(1))
    if (match := _STROKE.search(text)):
        attrs["stroke_mm"] = str(dec(match.group(1)) or match.group(1))
    if (match := _BAR.search(text)):
        attrs["pressure_bar"] = str(dec(match.group(1)) or match.group(1))
    if (match := _MOUNT.search(text)):
        attrs["mounting"] = match.group(1).strip()
    if (match := _CERT.search(text)):
        attrs["certification"] = match.group(1).strip()
    if re.search(r"nutzbarer weg|usable stroke", text, re.I):
        attrs["usable_stroke"] = True
    return attrs


def _scales(text: str) -> list[dict]:
    if not re.search(r"staffel|scale|preis|price", text, re.I):
        return []
    out = []
    for match in _SCALE.finditer(text):
        price = dec(match.group(2))
        if price is None:
            continue
        out.append({"min_quantity": int(match.group(1)), "price": str(price)})
    return out


def from_text(text: str) -> dict:
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
            "requirement": req.group(1) if req else (rest or None),
        })
    if positions:
        facts["positions"] = positions

    attrs = _attrs(text)
    group = _GROUP.search(text)
    req = _REQ.search(text)
    rev = _REV.search(text)
    pieces = _SET.search(text)
    mark = _marking(text)
    if req and attrs.get("bore_mm"):
        requirement = {"id": req.group(1), **attrs}
        if group:
            requirement["kataloggruppe"] = group.group(1).strip().split()[0]
        if pieces:
            requirement["pieces_per_set"] = int(pieces.group(1))
        facts.setdefault("requirements", []).append(requirement)
        facts.setdefault("catalog_groups", [])
        if group and group.group(1).strip().split()[0] not in facts["catalog_groups"]:
            facts["catalog_groups"].append(group.group(1).strip().split()[0])
    if rev:
        facts.setdefault("revisions", []).append({
            "id": rev.group(1),
            "marking": mark or "unknown",
            "marking_raw": mark or None,
        })

    sender = (_FROM.search(text) or [None, ""])[1].strip()
    subject = (_SUBJ.search(text) or [None, ""])[1].strip()
    if sender and subject:
        mail = {"sender": sender, "subject": subject, "body": text[:4000],
                "date": (_WHEN.search(text).group(0) if _WHEN.search(text) else "")}
        if (pct := _PCT.search(text)):
            mail["discount_percent"] = str(dec(pct.group(1)) or pct.group(1))
        if (floor := _MIN_ORDER.search(text)):
            mail["min_order_value"] = str(dec(floor.group(1)) or floor.group(1))
        if (tan := _TAN.search(text)):
            mail["tan"] = tan.group(1)
        if (reserve := _RESERVE.search(text)):
            mail["reserve_mm_per_end"] = str(dec(reserve.group(1)) or reserve.group(1))
        facts.setdefault("mails", []).append(mail)

    if (mat := _MAT.search(text)) and attrs:
        material = {"material": mat.group(1), **attrs,
                    "status": "blocked" if re.search(r"gesperrt|blocked|inaktiv", text, re.I) else "active"}
        if group:
            material["kataloggruppe"] = group.group(1).strip().split()[0]
        if (stock := _STOCK.search(text)):
            material["stock"] = int(stock.group(1))
        if (lead := _LEAD_STOCK.search(text)):
            material["stock_lead_time_weeks"] = int(lead.group(1))
        if (lead := _LEAD_PROD.search(text)):
            material["production_lead_time_weeks"] = int(lead.group(1))
        scales = _scales(text)
        if scales:
            material["price_condition"] = {"currency": "EUR", "price_unit": 1, "eur_per_unit": 1, "scales": scales}
        facts.setdefault("materials", []).append(material)
    return facts


def yaml_tables(text: str) -> list[list[list[str]]]:
    """Rows from a Playwright snapshot: lines that look like a table row with a ref."""
    rows = []
    for line in (text or "").splitlines():
        if "[ref=" not in line:
            continue
        cells = [part.strip() for part in re.split(r"\s{2,}|\t|\s·\s", re.sub(r"\[ref=e\d+\]", "", line)) if part.strip()]
        if len(cells) >= 2:
            rows.append(cells)
    if len(rows) < 2:
        return []
    return [rows]


def read_page(text: str) -> dict:
    """Structured records from one selected page. Empty when the page is a shell."""
    if not worth_reading(text or ""):
        return {}
    return merge_dicts(from_tables(yaml_tables(text)), from_text(text))


def merge_dicts(left: dict, right: dict) -> dict:
    out = dict(left or {})
    for key, value in (right or {}).items():
        if key not in out:
            out[key] = value
        elif isinstance(out[key], list) and isinstance(value, list):
            out[key] = out[key] + value
        elif isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = merge_dicts(out[key], value)
        elif value not in (None, "", [], {}):
            out[key] = value
    return out
