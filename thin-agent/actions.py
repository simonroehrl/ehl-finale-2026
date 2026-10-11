"""Turn a short model reply into actions, and format bid values the model already chose by key."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

OPS = {"click", "fill", "press", "scroll", "back", "wait", "collect_pages", "open_archive", "done", "need_tan"}
STOP_AFTER = {"click", "press", "scroll", "back", "collect_pages", "open_archive", "done", "need_tan"}
SIGN_WORDS = ("einreichen", "unterschreiben", "signieren", "verbindlich absenden", "sign and submit", "angebot absenden")


@dataclass
class Action:
    op: str
    target: int | None = None
    text: str | None = None
    times: int | None = None
    file: str | None = None
    value_key: str | None = None
    number_format: str = "auto"

    def signature(self) -> str:
        return f"{self.op}:{self.target}:{self.value_key}:{self.file}"


def parse_json(text: str) -> dict:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object")
    data = json.loads(raw[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON was not an object")
    return data


def parse_actions(text: str) -> list[Action]:
    data = parse_json(text)
    rows = data.get("actions")
    if rows is None and data.get("op"):
        rows = [data]
    if not isinstance(rows, list):
        raise ValueError("actions must be a list")
    actions = []
    for row in rows[:4]:
        if not isinstance(row, dict) or row.get("op") not in OPS:
            continue
        target = row.get("target")
        actions.append(Action(
            op=row["op"],
            target=int(target) if isinstance(target, int) or (isinstance(target, str) and str(target).isdigit()) else None,
            text=None if row.get("text") is None else str(row.get("text")),
            times=int(row["times"]) if isinstance(row.get("times"), int) else None,
            file=None if row.get("file") is None else str(row.get("file")),
            value_key=None if row.get("value_key") is None else str(row.get("value_key")),
            number_format=row.get("format") if row.get("format") in {"auto", "plain", "de"} else "auto",
        ))
    return actions


def batch_until_navigation(actions: list[Action]) -> list[Action]:
    kept = []
    for action in actions:
        kept.append(action)
        if action.op in STOP_AFTER:
            break
    return kept


def german_money(value) -> str:
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if amount < 0 else ""
    whole, cent = divmod(int(abs(amount) * 100), 100)
    grouped = f"{whole:,}".replace(",", ".")
    return f"{sign}{grouped},{cent:02d}"


def format_value(key: str, value, number_format: str = "auto") -> str:
    field_name = key.rsplit(".", 1)[-1]
    if field_name in {"quantity", "lead_time_weeks", "material_number", "position"} or key in {
        "tender_reference", "decision", "currency", "tan",
    }:
        return str(value)
    if number_format == "plain":
        return str(value)
    return german_money(value)


def lookup_value(answer: dict, tan: str | None, key: str, number_format: str = "auto") -> str | None:
    if key == "tan":
        return None if not tan else str(tan)
    if key in answer and not isinstance(answer[key], (list, dict)):
        return format_value(key, answer[key], number_format)
    match = re.fullmatch(r"position\.(.+)\.([A-Za-z_]+)", key)
    if not match:
        return None
    position_id, field_name = match.group(1), match.group(2)
    for position in answer.get("positions") or []:
        if str(position.get("position")) == position_id and field_name in position:
            return format_value(field_name, position[field_name], number_format)
    return None


def is_sign_control(name: str) -> bool:
    return any(word in (name or "").casefold() for word in SIGN_WORDS)


def render_observation(snapshot: dict) -> str:
    lines = [f"URL: {snapshot.get('url', '')}", f"Title: {snapshot.get('title', '')}"]
    if snapshot.get("opened_document"):
        lines.append("The text below is the document that was just opened.")
    files = snapshot.get("archive_files") or []
    if files:
        lines.append("Archive files: " + ", ".join(files))
    lines.append("Elements:")
    for node in (snapshot.get("nodes") or [])[:60]:
        href = f" href={node['href']}" if node.get("href") else ""
        lines.append(f"[{node.get('i')}] {node.get('role', '')} {node.get('name', '')}{href}")
    lines.append("Visible text:")
    lines.append((snapshot.get("text") or "")[:2800])
    if snapshot.get("truncated"):
        lines.append("[visible text truncated; scroll or open the document]")
    return "\n".join(lines)


def render_turn(goal: str, summary: str, snapshot: dict, hint: str) -> str:
    parts = [f"GOAL:\n{goal}", f"KNOWN FACTS:\n{summary or 'nothing yet'}", render_observation(snapshot)]
    if hint:
        parts.append(f"HINT:\n{hint}")
    return "\n\n".join(parts)
