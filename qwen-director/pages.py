"""Page heuristics for the director. No model imports."""
from __future__ import annotations

import json
import re

from actions import parse_action, parse_json

FORK_OPS = {"click", "fill", "extract", "done"}
USER_NAMES = ("user", "benutzer", "e-mail", "email", "kennung")
SUBMIT_NAMES = ("anmelden", "einloggen", "log in", "sign in", "login", "submit", "weiter")
COOKIE_NAMES = ("akzeptieren", "accept all", "accept", "einwilligung", "verstanden", "allow all")
COOKIE_SKIP = ("fortfahr", "umfrage", "survey")
TASK_LINKS = {
    "positions": ("leistungsverzeichnis", "positionen", "bill of quantities", "mengenermittlung",
                  "vergabeunterlagen", "verfahrensakte", "ausschreibungsunterlagen"),
    "qa": ("bieterfrage", "fragen und antworten", "q&a", "clarification", "verfahrensakte"),
    "revisions": ("spezifikation", "anforderung", "revision", "lastenheft", "requirement"),
    "mail": ("posteingang", "postfach", "inbox", "nachrichten", "archiv"),
    "erp": ("katalog", "artikelsuche"),
}
SKIP_LINKS = ("umfrage", "survey", "logout", "abmelden", "hilfe", "help", "impressum",
              "datenschutz", "übersicht", "teilnahme")
SPECIFIC_LINKS = {
    "positions": ("leistungsverzeichnis", "unterlagen", "vergabeunterlagen"),
    "qa": ("bieterfrage", "fragen und antworten", "q&a", "rückfrage"),
    "revisions": ("spezifikation", "anforderung", "lastenheft"),
    "mail": ("posteingang", "archiv", "inbox"),
    "erp": ("katalog", "artikelsuche"),
}


def looks_login(snapshot: dict) -> bool:
    nodes = snapshot.get("nodes") or []
    types = [n.get("type") for n in nodes]
    names = " ".join((n.get("name") or "") for n in nodes).lower()
    return "password" in types or ("passwort" in names and any(w in names for w in USER_NAMES + ("login",)))


def looks_document(snapshot: dict, kind: str | None = None) -> bool:
    if looks_login(snapshot):
        return False
    text = snapshot.get("text") or ""
    if len(text) < 280:
        return False
    folded = text.casefold()
    names = " ".join((n.get("name") or "") for n in (snapshot.get("nodes") or [])).casefold()
    blob = f"{folded} {names}"
    if kind == "mail" and (blob.count("@") >= 2 or "betreff" in blob):
        return True
    if kind == "qa" and "rückfrage" in blob:
        return True
    marks = 0
    if len(re.findall(r"position\s*\d+", folded)) >= 2:
        marks += 2
    if folded.count("anforderung") >= 2 or folded.count("revision") >= 2:
        marks += 1
    if "betreff" in folded and ("von:" in folded or "absender" in folded):
        marks += 2
    if re.search(r"\d+[.,]\d{2}", text) and text.count("\n") >= 12:
        marks += 1
    return marks >= 2


def pick_login_fields(nodes: list[dict]) -> tuple[int | None, int | None]:
    """Username and password control ids. Never treat a submit/Anmelden control as the username box."""
    user_id = password_id = None
    for node in nodes:
        typ = (node.get("type") or "").lower()
        name = (node.get("name") or "").lower()
        role = (node.get("role") or "").lower()
        if typ == "password":
            password_id = node["i"]
            continue
        if typ == "submit" or role in {"button", "link"}:
            continue
        if any(word in name for word in SUBMIT_NAMES):
            continue
        if typ in {"text", "email"} or any(word in name for word in USER_NAMES):
            user_id = node["i"]
    return user_id, password_id


def login_submit_ids(nodes: list[dict]) -> list[int]:
    found = []
    for node in nodes:
        typ = (node.get("type") or "").lower()
        name = (node.get("name") or "").lower()
        role = (node.get("role") or "").lower()
        if typ == "password":
            continue
        if typ == "submit":
            found.append(node["i"])
            continue
        if any(word in name for word in SUBMIT_NAMES):
            found.append(node["i"])
            continue
        if role == "button" and any(word in name for word in SUBMIT_NAMES):
            found.append(node["i"])
    return found


CLOSE_NAMES = ("schließen", "schliessen", "close")


def close_ids(nodes: list[dict]) -> list[int]:
    found = []
    for node in nodes:
        name = (node.get("name") or "").strip().lower()
        if name in CLOSE_NAMES:
            found.append(node["i"])
    return found


def cookie_ids(nodes: list[dict]) -> list[int]:
    found = []
    for node in nodes:
        name = (node.get("name") or "").lower()
        if any(word in name for word in COOKIE_SKIP):
            continue
        if any(word in name for word in COOKIE_NAMES):
            found.append(node["i"])
    return found


_CLICK = re.compile(r"^\s*click\s+(\d+)\s*$", re.I)
_DONE = re.compile(r"^\s*(extract|done)\s*$", re.I)
_FILL = re.compile(r"^\s*fill\s+(\d+)\s+(.+)\s*$", re.I)
_OP = re.compile(r'"op"\s*:\s*"(click|extract|done|fill)"', re.I)
_TARGET = re.compile(r'"target"\s*:\s*(\d+)')


def skip_link(node: dict) -> bool:
    blob = f"{node.get('name') or ''} {node.get('href') or ''}".casefold()
    return any(word in blob for word in SKIP_LINKS)


def case_link_ids(snapshot: dict, reference: str) -> list[int]:
    needle = (reference or "").strip().casefold()
    if len(needle) < 4:
        return []
    found = []
    for node in snapshot.get("nodes") or []:
        if skip_link(node):
            continue
        blob = f"{node.get('name') or ''} {node.get('href') or ''}".casefold()
        if needle in blob:
            found.append(node["i"])
    return found


def task_link_ids(snapshot: dict, kind: str) -> list[int]:
    words = TASK_LINKS.get(kind, ())
    specific_words = SPECIFIC_LINKS.get(kind, ())
    specific, general = [], []
    for node in snapshot.get("nodes") or []:
        if skip_link(node):
            continue
        name = (node.get("name") or "").lower()
        href = (node.get("href") or "").lower()
        blob = f"{name} {href}"
        if any(word in blob for word in specific_words) or (
                kind == "positions" and name.strip() in {"lv", "boq"}):
            specific.append(node["i"])
        elif any(word in blob for word in words):
            general.append(node["i"])
    return specific + general


def search_box_ids(snapshot: dict) -> list[int]:
    found = []
    for node in snapshot.get("nodes") or []:
        typ = (node.get("type") or "").lower()
        name = (node.get("name") or "").lower()
        if typ in {"search", "text"} or "such" in name:
            if skip_link(node):
                continue
            found.append(node["i"])
    return found


def parse_fork(text: str):
    parsed = None
    for line in (text or "").splitlines():
        first = line.strip().strip("`")
        if match := _CLICK.match(first):
            parsed = ("other", parse_action(
                json.dumps({"op": "click", "target": int(match.group(1))}), FORK_OPS))
        elif match := _DONE.match(first):
            parsed = ("other", parse_action(
                json.dumps({"op": match.group(1).lower()}), FORK_OPS))
        elif match := _FILL.match(first):
            parsed = ("other", parse_action(
                json.dumps({"op": "fill", "target": int(match.group(1)),
                            "text": match.group(2).strip()}), FORK_OPS))
    if parsed:
        return parsed
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        raise ValueError("empty fork")
    try:
        data = parse_json(text)
        page = data.get("page") if data.get("page") in {
            "login", "blocker", "list", "document", "search", "other"} else "other"
        return page, parse_action(json.dumps(data), FORK_OPS)
    except ValueError:
        op = _OP.search(text or "")
        if not op:
            raise
        target = _TARGET.search(text or "")
        return "other", parse_action(
            json.dumps({"op": op.group(1).lower(),
                        "target": int(target.group(1)) if target else None}),
            FORK_OPS)
