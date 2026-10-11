"""Short-context extract from one page. The browsing loop does not rewrite Evidence or keep page text."""
from __future__ import annotations

import asyncio
import json
import re

from evidence import facts_index, merge_dicts

PAGE_CHARS = 8000
SCHEMA_CHARS = 2500
EXTRACT_TIMEOUT_S = 18
MIN_PAGE_CHARS = 280
PAGE_TOOLS = {"browser_snapshot", "browser_evaluate", "read_file"}
ACTION_TOOLS = {"browser_navigate", "browser_click"}
_LOGIN_SECRET = ("passwort", "password", "kennwort")
_LOGIN_ID = ("benutzername", "username", "anmelden", "einloggen", "sign in", "log in", "login")
_COOKIE = ("cookie", "akzeptieren", "accept all", "einwilligung", "consent")
_DIAGNOSTIC = re.compile(
    r"^(iframes:|rows=\d|tbodies=|tbodyhtml=|len=\d|has[a-z0-9]+=|document\.title\b)",
    re.I | re.M,
)

SURVEY = (
    "Extract only list-level records visible on this page: identifiers, dates, quantities, kinds, catalogue "
    "keys, numeric attributes. Omit long prose and nested price scales unless a single line states them."
)
DEEP = (
    "Extract every structured field visible on this page for the records that were asked for, including nested "
    "lists. Keep identifiers exactly as written."
)
MARKING = (
    "Extract every revision on this page: id, date if shown, and the marking exactly as the page writes it. "
    "Copy those words into marking. Do not infer current from the date or from list order."
)


def parse_json_object(text: str) -> dict:
    """Best-effort object from a model reply (fences and leading prose allowed)."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        raw = re.sub(r"^json", "", raw, flags=re.I).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON is not an object")
    return data.get("facts", data) if isinstance(data.get("facts"), dict) else data


def extract_messages(schema: dict | None, facts: dict | None, page: str, mode: str, note: str = "") -> list[dict]:
    instruction = {"survey": SURVEY, "deep": DEEP, "marking": MARKING}.get(mode, SURVEY)
    shape = json.dumps(schema or {"type": "object"}, ensure_ascii=False)[:SCHEMA_CHARS]
    system = (
        "You extract JSON records from one web page. Output one JSON object, no markdown. "
        "Only what the page states. Same id as an already-recorded row means an update.\n"
        f"Shape:\n{shape}"
    )
    user = (
        f"{instruction}\n"
        f"{('Focus: ' + note + chr(10)) if note else ''}"
        f"Already recorded:\n{facts_index(facts)}\n\n"
        f"Page:\n{(page or '')[:PAGE_CHARS]}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def apply_extract(base: dict | None, text: str) -> dict:
    return merge_dicts(base, parse_json_object(text))


def tool_name(name: str) -> str:
    return name.partition("__")[2] or name


def looks_login(text: str) -> bool:
    low = text.lower()
    return any(w in low for w in _LOGIN_SECRET) and any(w in low for w in _LOGIN_ID)


def looks_cookie_wall(text: str) -> bool:
    low = text.lower()
    hits = sum(low.count(w) for w in _COOKIE)
    return hits >= 3 and len(text) < 2000


def looks_diagnostic(text: str) -> bool:
    """JS probe output (`rows=…`, `iframes:…`), not a page the extract model should read."""
    return bool(_DIAGNOSTIC.search(text[:500]))


def page_for_extract(name: str, result: str | None) -> str | None:
    """Page text worth a short extract call. Shell pages, clicks, errors and file listings are skipped."""
    text = (result or "").strip()
    if not text or text.startswith("Error:"):
        return None
    tool = tool_name(name)
    if tool == "read_file":
        if text.startswith(("Downloaded files:", "No files", "No downloaded")):
            return None
        return text if len(text) >= 80 else None
    if tool in PAGE_TOOLS:
        if len(text) < MIN_PAGE_CHARS:
            return None
    elif tool in ACTION_TOOLS and len(text) >= 800:
        pass
    else:
        return None
    if looks_diagnostic(text) or looks_login(text) or looks_cookie_wall(text):
        return None
    return text


async def extract_page(client, model: str, schema: dict | None, facts: dict | None,
                       page: str, mode: str = "survey", note: str = "",
                       timeout: float = EXTRACT_TIMEOUT_S):
    """One page → (merged facts, model response). Caller counts usage."""
    messages = extract_messages(schema, facts, page, mode, note)
    response = await asyncio.wait_for(
        client.chat.completions.create(model=model, messages=messages, temperature=0),
        timeout=max(4.0, timeout),
    )
    raw = (response.choices[0].message.content or "") if response.choices else ""
    return apply_extract(facts, raw), response
