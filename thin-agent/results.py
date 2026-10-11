"""Unread rows of a model-selected result set. The helper opens those rows; it does not search the site."""
from __future__ import annotations

import re

_ID = re.compile(r"\b([A-Z]{1,8}[-/][A-Z0-9][-A-Z0-9./]{0,24})\b")
_SKIP = (
    "logout", "abmelden", "home", "startseite", "menü", "menu", "cookie",
    "sprache", "language", "hilfe", "help", "suche", "search",
)


def _unique(items: list[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def listed_from(facts: dict, role: str) -> list[str]:
    if role == "spec":
        return _unique([str(r.get("id")) for r in facts.get("revisions") or [] if r.get("id")])
    if role == "erp":
        return _unique([str(m.get("number")) for m in facts.get("materials") or [] if m.get("number")])
    if role == "mail":
        return _unique([str(m.get("subject") or m.get("id")) for m in facts.get("messages") or [] if m.get("id") or m.get("subject")])
    if role == "tender":
        return _unique([str(q.get("number")) for q in facts.get("qa") or [] if q.get("number") and not (q.get("text") or "").strip()])
    return []


def opened_from(facts: dict, role: str) -> list[str]:
    if role == "spec":
        out = []
        for row in facts.get("revisions") or []:
            reqs = row.get("requirements") or []
            if row.get("marking_raw") or any(item.get("bore_mm") or item.get("catalog_group") for item in reqs):
                if row.get("id"):
                    out.append(str(row["id"]))
        return _unique(out)
    if role == "erp":
        out = []
        for row in facts.get("materials") or []:
            attrs = row.get("attributes") or {}
            if attrs.get("bore_mm") and row.get("number"):
                out.append(str(row["number"]))
        return _unique(out)
    if role == "mail":
        out = []
        for row in facts.get("messages") or []:
            rich = row.get("discount_percent") is not None or row.get("kind") not in (None, "other")
            if rich:
                out.append(str(row.get("subject") or row.get("id")))
        return _unique(out)
    return []


def ids_on_snapshot(snapshot: dict, exclude: set[str] | None = None) -> list[str]:
    """Identifiers sitting on the current result list (row text or control name)."""
    skip = {x.lower() for x in (exclude or set())}
    found = []
    for node in snapshot.get("nodes") or []:
        blob = f"{node.get('name') or ''} {node.get('row') or ''} {node.get('href') or ''}"
        for match in _ID.finditer(blob):
            token = match.group(1)
            if token.lower() not in skip:
                found.append(token)
    return _unique(found)


def find_controls(snapshot: dict, ident: str) -> list[int]:
    """Click targets that belong to this list row, including an Actions button whose row holds the id."""
    needle = ident.lower()
    hits = []
    for node in snapshot.get("nodes") or []:
        if (node.get("type") or "").lower() == "password":
            continue
        label = f"{node.get('name') or ''} {node.get('href') or ''} {node.get('row') or ''}".lower()
        if any(word in (node.get("name") or "").lower() for word in _SKIP):
            continue
        if needle not in label:
            continue
        hits.append(int(node["i"]))
    return hits
