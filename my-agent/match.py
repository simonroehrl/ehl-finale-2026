"""Exact material match after the brief's derivation. The model records; this only does the last arithmetic.

If a requirement is usable travel (nutzbarer Weg), nominal stroke is stated stroke plus twice the per-end reserve.
Then bore, stroke, pressure, mounting and certification must match exactly. No site-specific numbers.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

ATTRS = ("bore_mm", "stroke_mm", "pressure_bar", "mounting", "certification")
_ALIASES = {
    "bore_mm": ("bore_mm", "bore", "bohrung", "bohrung_mm"),
    "stroke_mm": ("stroke_mm", "stroke", "hub", "hub_mm", "stellweg", "stellweg_mm"),
    "pressure_bar": ("pressure_bar", "pressure", "druck", "druck_bar"),
    "mounting": ("mounting", "befestigung"),
    "certification": ("certification", "zertifizierung", "zertifikat"),
}


def D(value) -> Decimal | None:
    if value in (None, "", [], {}):
        return None
    text = str(value).strip()
    text = re_num(text)
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def re_num(text: str) -> str:
    text = text.replace("mm", "").replace("bar", "").strip()
    if "," in text and text.count(",") == 1:
        return text.replace(".", "").replace(",", ".")
    return text.replace(" ", "")


def text(value) -> str:
    return " ".join(str(value or "").lower().split())


def certification(value) -> str:
    t = text(value).replace(" ", "")
    return "none" if t in {"none", "keine", "kein", "-", "n/a"} else t


def same(name: str, left, right) -> bool:
    if left is None or right is None:
        return False
    if name == "mounting":
        return text(left) == text(right)
    if name == "certification":
        return certification(left) == certification(right)
    a, b = D(left), D(right)
    return a is not None and b is not None and a == b


def pick(record: dict, *names: str):
    if not isinstance(record, dict):
        return None
    lower = {str(k).lower(): v for k, v in record.items()}
    for name in names:
        if name.lower() in lower and lower[name.lower()] not in (None, ""):
            return lower[name.lower()]
    return None


def attributes(record: dict | None) -> dict:
    record = record if isinstance(record, dict) else {}
    nested = record.get("attributes") if isinstance(record.get("attributes"), dict) else {}
    blob = {**nested, **record}
    out = {}
    for attr, names in _ALIASES.items():
        out[attr] = pick(blob, *names)
    return out


def is_usable_stroke(record: dict | None) -> bool:
    record = record if isinstance(record, dict) else {}
    if record.get("usable_stroke") in (True, "true", "yes", 1):
        return True
    blob = text(json_blob(record))
    return "nutzbarer weg" in blob or "usable stroke" in blob or ("nutzbar" in blob and "weg" in blob)


def json_blob(value) -> str:
    if isinstance(value, dict):
        return " ".join(json_blob(v) for v in value.values())
    if isinstance(value, list):
        return " ".join(json_blob(v) for v in value)
    return str(value or "")


def reserve_per_end(facts) -> Decimal | None:
    """Any recorded per-end reserve in millimetres. Latest numeric value wins; no named key is assumed."""
    found: list[Decimal] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                low = str(key).lower()
                if any(w in low for w in ("reserve_mm", "endreserve", "reserve_per_end", "mm_per_end")):
                    number = D(value)
                    if number is not None:
                        found.append(number)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(facts)
    return found[-1] if found else None


def nominal_stroke(stated, usable: bool, reserve) -> Decimal | None:
    stroke = D(stated)
    if stroke is None:
        return None
    if usable and reserve is not None:
        return stroke + (D(reserve) * 2)
    return stroke


def walk_materials(facts) -> list[dict]:
    out = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        number = pick(node, "number", "material", "materialnummer", "material_number", "mat")
        attrs = attributes(node)
        if number and any(attrs.values()):
            status = text(pick(node, "status", "status_raw") or "")
            blocked = any(w in status for w in ("blocked", "gesperrt", "inaktiv", "inactive"))
            out.append({"number": str(number), "blocked": blocked, "attributes": attrs, "raw": node})
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value)

    walk(facts)
    return out


def matches(need: dict, material: dict) -> bool:
    got = material.get("attributes") or {}
    return all(same(name, need.get(name), got.get(name)) for name in ATTRS)


def first_match(need: dict, materials: list[dict]) -> dict | None:
    for material in materials:
        if material.get("blocked"):
            continue
        if matches(need, material):
            return material
    return None


def requirement_record(line: dict, facts: dict | None) -> dict:
    req = line.get("requirement")
    if isinstance(req, dict) and attributes(req).get("bore_mm") is not None:
        return req
    blob = f"{req or ''} {line.get('position') or ''}"
    found = {}

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return
        ident = pick(node, "id", "requirement", "requirement_id", "anforderung")
        if ident and str(ident) in blob and attributes(node).get("bore_mm") is not None:
            found.update(node)
        for value in node.values():
            if isinstance(value, (dict, list)):
                walk(value)

    walk(facts)
    return found or (req if isinstance(req, dict) else {})


def _find_price_condition(node):
    if isinstance(node, dict):
        if node.get("scales"):
            return node
        for key in ("price_condition", "price_conditions", "konditionen", "preise"):
            hit = _find_price_condition(node.get(key))
            if hit:
                return hit
        for value in node.values():
            if isinstance(value, (dict, list)):
                hit = _find_price_condition(value)
                if hit:
                    return hit
    elif isinstance(node, list):
        for item in node:
            hit = _find_price_condition(item)
            if hit:
                return hit
    return None


def attach_prices(line: dict, facts: dict | None) -> dict:
    """Copy scale prices from the recorded material when the analyst omitted them."""
    out = dict(line)
    existing = out.get("price_condition")
    if isinstance(existing, dict) and existing.get("scales"):
        return out
    number = str(out.get("material") or "")
    if not number:
        return out
    for material in walk_materials(facts):
        if str(material.get("number")) != number:
            continue
        cond = _find_price_condition(material.get("raw") or {})
        if cond:
            out["price_condition"] = cond
        raw = material.get("raw") or {}
        for key in ("stock", "stock_lead_time_weeks", "production_lead_time_weeks", "carton_size",
                    "carton_price", "mass_surcharge_per_unit", "allocation_mass"):
            if out.get(key) in (None, "", 0) and raw.get(key) not in (None, ""):
                out[key] = raw[key]
        break
    return out


def material_fits(number, need: dict, materials: list[dict]) -> bool:
    for material in materials:
        if str(material.get("number")) != str(number) or material.get("blocked"):
            continue
        if matches(need, material):
            return True
    return False


def recover_line(line: dict, facts: dict | None) -> dict:
    """Fill or replace the line's material after usable-stroke derivation. A named miss is not kept."""
    out = dict(line)
    req = requirement_record(out, facts)
    need = attributes(req)
    reserve = reserve_per_end(facts)
    need["stroke_mm"] = nominal_stroke(need.get("stroke_mm"), is_usable_stroke(req), reserve)
    materials = walk_materials(facts)
    current = out.get("material")
    named = current not in (None, "", "null")
    if named and (need.get("stroke_mm") is None or need.get("bore_mm") is None):
        return attach_prices(out, facts)
    if named and material_fits(current, need, materials):
        return attach_prices(out, facts)
    if not need.get("stroke_mm") or need.get("bore_mm") is None:
        return out
    hit = first_match(need, materials)
    if hit:
        out["material"] = hit["number"]
        out["requirement"] = req
        note = " [replaced: named material failed exact match after stroke derivation]" if named else (
            " [code match after stroke derivation]")
        out["material_reasoning"] = (out.get("material_reasoning") or "") + note
        return attach_prices(out, facts)
    if named:
        out["material"] = None
        out["material_reasoning"] = (out.get("material_reasoning") or "") + (
            " [named material failed exact match; omitted]")
    return out


def catalog_groups(facts) -> list[str]:
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                low = str(key).lower()
                if any(word in low for word in ("kataloggruppe", "catalog_group", "cataloggroup")):
                    if isinstance(value, (str, int)) and str(value).strip():
                        token = str(value).strip().split()[0]
                        if token not in found:
                            found.append(token)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(facts)
    return found


def covered_groups(facts) -> set[str]:
    covered = set()
    for material in walk_materials(facts):
        raw = material.get("raw") or {}
        token = pick(raw, "catalog_group", "kataloggruppe", "found_for_kataloggruppe", "found_for")
        if token:
            covered.add(str(token).strip().split()[0])
    return covered
