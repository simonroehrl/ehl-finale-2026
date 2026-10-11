"""Parse one small JSON action and map bid fields the model pointed at to values code will type."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

RESEARCH_OPS = {"click", "fill", "press", "scroll", "back", "wait", "extract", "collect_pages", "open_archive", "done"}
SUBMIT_OPS = {"click", "fill", "press", "scroll", "back", "wait", "extract_tan", "done"}
SIGN_WORDS = ("einreichen", "unterschreiben", "signieren", "verbindlich absenden", "sign and submit",
              "angebot absenden", "verbindlich abgeben")


@dataclass
class Action:
    op: str
    target: int | None = None
    text: str | None = None
    times: int | None = None
    file: str | None = None
    value_key: str | None = None
    number_format: str = "auto"
    browser: str | None = None

    def signature(self) -> str:
        return f"{self.op}:{self.target}:{self.value_key}:{self.file}:{self.browser}"


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


def parse_action(text: str, allowed: set[str]) -> Action:
    data = parse_json(text)
    row = data
    if isinstance(data.get("actions"), list) and data["actions"]:
        row = data["actions"][0]
    if not isinstance(row, dict) or row.get("op") not in allowed:
        raise ValueError("unknown op")
    target = row.get("target")
    return Action(
        op=row["op"],
        target=int(target) if isinstance(target, int) or (isinstance(target, str) and str(target).isdigit()) else None,
        text=None if row.get("text") is None else str(row.get("text")),
        times=int(row["times"]) if isinstance(row.get("times"), int) else None,
        file=None if row.get("file") is None else str(row.get("file")),
        value_key=None if row.get("value_key") is None else str(row.get("value_key")),
        number_format=row.get("format") if row.get("format") in {"auto", "plain", "de"} else "auto",
        browser=row.get("browser") if row.get("browser") in {"tender", "mail"} else None,
    )


def german_money(value) -> str:
    amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if amount < 0 else ""
    whole, cent = divmod(int(abs(amount) * 100), 100)
    return f"{sign}{whole:,}".replace(",", ".") + f",{cent:02d}"


def format_value(key: str, value, number_format: str = "auto") -> str:
    field = key.rsplit(".", 1)[-1]
    if field in {"quantity", "lead_time_weeks", "material_number", "position"} or key in {
            "tender_reference", "decision", "currency", "tan"}:
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


def value_keys(answer: dict, tan: str | None) -> list[str]:
    lines = []
    for key in ("tender_reference", "decision", "discount_percent", "total_net"):
        lines.append(f"- {key} = {lookup_value(answer, tan, key)}")
    for position in answer.get("positions") or []:
        pid = position["position"]
        for field in ("material_number", "quantity", "unit_price", "lead_time_weeks", "base_unit_price",
                      "surcharge_per_unit", "packaging_per_unit", "rebate_total"):
            lines.append(f"- position.{pid}.{field} = {lookup_value(answer, tan, f'position.{pid}.{field}')}")
    if tan:
        lines.append(f"- tan = {tan}")
    else:
        lines.append("- tan is not known yet. Use extract_tan on the mailbox when the form is ready.")
    return lines


def is_sign_control(name: str) -> bool:
    text = (name or "").casefold()
    return any(word in text for word in SIGN_WORDS)


def portal_confirmed(text: str) -> bool:
    low = (text or "").lower()
    if any(marker in low for marker in ("couldn't submit", "could not submit", "did not submit")):
        return False
    signed = "verbindlich" in low and any(word in low for word in ("abgegeben", "bestätigt", "angenommen"))
    confirmed = "confirmation" in low and any(word in low for word in ("submitted", "signed", "accepted"))
    return signed or confirmed
