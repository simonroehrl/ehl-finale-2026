"""Merge harvested page records and find click refs on a snapshot the model already opened."""
from __future__ import annotations

import re

from extract import merge_dicts

_ID = re.compile(r"\b([A-Z]{1,8}[-/][A-Z0-9][-A-Z0-9./]{0,24})\b")
_REF = re.compile(r"\[ref=(e\d+)\]")
_ROW = re.compile(r"(?m)^.*\b([A-Z]{1,8}-[A-Z0-9][-A-Z0-9./]{1,24})\b.*\[ref=(e\d+)\]")


def _key(record: dict) -> str:
    for name in ("position", "id", "material", "number", "subject", "sender"):
        value = record.get(name)
        if value:
            return f"{name}:{value}"
    return ""


def merge_facts(base: dict | None, incoming: dict | None) -> dict:
    """Union list records by a stable key; incoming fills empty fields."""
    out = dict(base or {})
    for key, value in (incoming or {}).items():
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            have = {_key(item): i for i, item in enumerate(out.get(key) or []) if isinstance(item, dict) and _key(item)}
            merged = list(out.get(key) or [])
            for item in value:
                ident = _key(item)
                if ident and ident in have:
                    merged[have[ident]] = merge_dicts(merged[have[ident]], item)
                else:
                    if ident:
                        have[ident] = len(merged)
                    merged.append(item)
            out[key] = merged
        elif isinstance(value, list) and value:
            seen = {str(item) for item in (out.get(key) or [])}
            extra = [item for item in value if str(item) not in seen]
            out[key] = list(out.get(key) or []) + extra
        elif key not in out or out[key] in (None, "", [], {}):
            out[key] = value
    return out


def ids_on_page(text: str) -> list[str]:
    found, seen = [], set()
    for match in _ID.finditer(text or ""):
        token = match.group(1)
        if token not in seen:
            seen.add(token)
            found.append(token)
    return found


def ref_for(text: str, ident: str) -> str:
    """The snapshot ref on the same line as this identifier, if the model is on that list."""
    needle = ident.lower()
    for line in (text or "").splitlines():
        if needle not in line.lower():
            continue
        match = _REF.search(line)
        if match:
            return match.group(1)
    return ""


def rich_ids(facts: dict | None) -> set[str]:
    """Records we already have enough of — do not open them again."""
    facts = facts if isinstance(facts, dict) else {}
    have: set[str] = set()
    for row in facts.get("materials") or []:
        if isinstance(row, dict) and (row.get("bore_mm") or (row.get("attributes") or {}).get("bore_mm")):
            if row.get("material") or row.get("number"):
                have.add(str(row.get("material") or row.get("number")))
    for row in facts.get("requirements") or []:
        if isinstance(row, dict) and row.get("id") and row.get("bore_mm"):
            have.add(str(row["id"]))
    for row in facts.get("revisions") or []:
        if isinstance(row, dict) and row.get("id") and row.get("marking") not in (None, "", "unknown"):
            have.add(str(row["id"]))
    for row in facts.get("mails") or []:
        if isinstance(row, dict) and (row.get("body") or row.get("discount_percent") or row.get("tan")):
            have.add(str(row.get("subject") or row.get("id") or ""))
    return {item for item in have if item}


def unread_on_page(text: str, facts: dict | None) -> list[str]:
    """Identifiers on this result list that we have not opened yet."""
    have = rich_ids(facts)
    unread = []
    for match in _ROW.finditer(text or ""):
        token = match.group(1)
        if token not in have and token not in unread:
            unread.append(token)
    return unread
