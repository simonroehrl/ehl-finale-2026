"""Short-context extract from one page. The browsing loop does not rewrite Evidence or keep page text."""
from __future__ import annotations

import json
import re

from evidence import facts_index, merge_dicts

PAGE_CHARS = 12000
SCHEMA_CHARS = 5000

SURVEY = (
    "Extract only list-level records visible on this page: identifiers, dates, quantities, kinds, catalogue "
    "keys, numeric attributes. Omit long prose and nested price scales unless a single line states them."
)
DEEP = (
    "Extract every structured field visible on this page for the records that were asked for, including nested "
    "lists. Keep identifiers exactly as written."
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
    instruction = SURVEY if mode == "survey" else DEEP
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
