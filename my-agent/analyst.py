"""The analyst: the model reads every site agent's facts with the brief and decides each rule question (which
revision is current, which clarification and approval apply, which material matches). It returns a structured
order; pricing.py does the arithmetic. The model makes the decisions, the code only checks their shape.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal

import pricing
from llm_agent import qwen_client
from match import recover_line

ORDER_SHAPE = """{
  "tender_reference": "exact reference",
  "scenario_date": "from the brief",
  "decision": "bid" or "no_bid",
  "decision_notes": "why",
  "approval": {"percent": 0, "min_order_value": null, "source": "which mail and why it governs"} or null,
  "setup_pool_fee": 0,
  "bonus_bands": [{"lower": 0, "upper": null, "percent": 0}],
  "lines": [{
    "position": "as in the tender",
    "quantity_pieces": 0,
    "quantity_reasoning": "tender quantity, applied clarifications, withdrawals, unit conversion",
    "requirement": "governing attributes after amendments",
    "material": "material number, or null if no active material matches exactly",
    "material_reasoning": "why it matches; for null, why every candidate fails",
    "price_condition": {"currency": "EUR", "price_unit": 1, "eur_per_unit": 1,
                        "scales": [{"min_quantity": 1, "price": 0}]},
    "stock": 0, "stock_lead_time_weeks": 0, "production_lead_time_weeks": 0,
    "commitment": {"material": "", "ceiling": 0, "lead_time_weeks": 0} or null,
    "mass_surcharge_per_unit": 0,
    "setup_fee": 0,
    "discount_scope": "base" | "base_and_surcharge" | "none",
    "carton_size": null, "carton_price": 0,
    "setup_pool": false, "allocation_mass": 0,
    "bonus_eligible": false
  }]
}"""

SYSTEM = f"""You are the bid analyst. Colleagues collected raw facts from four websites. Apply the brief's rules to
them, exactly and literally, and decide every value of the order below. Parse dates by their format hints and
numbers by their locale hints. Ignore invisible characters when comparing references. Use only records that the
rules allow: exact tender reference, authorized sender, dated no later than the scenario date, latest wins,
withdrawn answers ignored. Quantities are final pieces. Prices in price_condition are as the ERP states them (per
price_unit pieces, in their currency, eur_per_unit from the governing FX record); do not apply discounts yourself:
the arithmetic is done afterwards from your values. Use 0, null or false where something does not apply.

Record attributes as the pages state them, including whether a stroke is usable travel and any per-end reserve.
Do not compare millimetres yourself; code checks the exact match after the brief's derivation. Prefer a bid for
every position that has an active matching material and a price condition. Omit positions that still have none.
no_bid is only for when no position at all can be priced. Unread documents are not no_bid.

Reply with one JSON object and nothing else, in this shape:
{ORDER_SHAPE}"""


KEEP_FACT_KEYS = (
    "tender", "positions", "clarifications", "revisions", "requirements", "catalog_groups",
    "materials", "mails", "messages", "approval", "tender_reference", "reference",
)


def compact_facts(facts: dict | None, body_limit: int = 1200) -> dict:
    """Structured records only — the full research dump overflows the analyst."""
    def trim(node):
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if key in {"notes", "summary", "transcript"}:
                    continue
                if key == "body" and isinstance(value, str):
                    out[key] = value[:body_limit]
                else:
                    out[key] = trim(value)
            return out
        if isinstance(node, list):
            return [trim(item) for item in node[:40]]
        return node

    facts = facts if isinstance(facts, dict) else {}
    keep = {}
    for key, value in facts.items():
        if isinstance(value, dict):
            inner = {k: trim(v) for k, v in value.items() if k in KEEP_FACT_KEYS or k in value}
            keep[key] = {k: v for k, v in inner.items() if k in KEEP_FACT_KEYS or isinstance(v, (list, dict))}
        elif key in KEEP_FACT_KEYS:
            keep[key] = trim(value)
    return keep


async def analyse(brief: str, facts: dict, timeout: float = 25) -> dict:
    client = qwen_client()
    payload = compact_facts(facts)
    user = f"<brief>\n{brief}\n</brief>\n\n<facts>\n{json.dumps(payload, ensure_ascii=False)}\n</facts>"
    response = await client.with_options(max_retries=0).chat.completions.create(
        model="qwen", temperature=0, timeout=timeout,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
    return parse_json(response.choices[0].message.content or "")


def find_reference(facts: dict | None, brief: str = "") -> str:
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in {"tender_reference", "reference", "tender"} and isinstance(value, str):
                    if re.search(r"[A-Z]{2,}-\d{4}-\d+", value):
                        found.append(re.search(r"[A-Z]{2,}-\d{4}-\d+", value).group(0))
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            match = re.search(r"\b([A-Z]{2,}-\d{4}-\d+)\b", node)
            if match:
                found.append(match.group(1))

    walk(facts)
    if found:
        return found[0]
    match = re.search(r"\b([A-Z]{2,}-\d{4}-\d+)\b", brief or "")
    return match.group(1) if match else ""


def requirement_for(position: dict, facts: dict | None) -> dict:
    rid = position.get("requirement") or position.get("requirement_id") or position.get("anforderung")
    if isinstance(rid, dict):
        return rid
    reqs = _list(facts, "requirements") or []
    token = str(rid or "")
    pid = str(position.get("position") or "")
    for row in reqs:
        if not isinstance(row, dict):
            continue
        ident = str(row.get("id") or row.get("requirement") or "")
        if token and ident and (token == ident or token in ident or ident in token):
            return row
        if pid and pid in json.dumps(row, ensure_ascii=False):
            return row
    return rid if isinstance(rid, dict) else {}


def approval_from(facts: dict | None) -> dict | None:
    best = None
    for row in _list(facts, "mails") or _list(facts, "messages") or []:
        if not isinstance(row, dict):
            continue
        pct = row.get("discount_percent") or row.get("percent") or row.get("approval_percent")
        if pct in (None, ""):
            continue
        best = {"percent": pct, "min_order_value": row.get("min_order_value"), "source": row.get("subject") or "mail"}
    return best


def from_facts(facts: dict | None, brief: str = "", seed: dict | None = None) -> dict:
    """Build an order from harvested records. No model. Used when the analyst fails or as the base bid."""
    out = dict(seed or {})
    out["tender_reference"] = out.get("tender_reference") or find_reference(facts, brief)
    if not out.get("approval"):
        found = approval_from(facts)
        if found:
            out["approval"] = found
    lines = list(out.get("lines") or [])
    have = {str(line.get("position")) for line in lines if isinstance(line, dict) and line.get("position")}
    for row in _list(facts, "positions") or []:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("position") or row.get("number") or row.get("pos") or "")
        if not pid or pid in have:
            continue
        lines.append({
            "position": pid,
            "quantity_pieces": row.get("quantity_pieces") or row.get("quantity") or row.get("menge"),
            "requirement": requirement_for(row, facts),
            "material": row.get("material"),
        })
        have.add(pid)
    if not lines:
        for row in _list(facts, "requirements") or []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            pid = str(row.get("position") or row.get("id"))
            if pid in have:
                continue
            lines.append({"position": pid, "quantity_pieces": row.get("quantity") or row.get("pieces_per_set"),
                          "requirement": row, "material": None})
            have.add(pid)
    out["lines"] = lines
    if not out.get("decision"):
        out["decision"] = "bid" if lines else "no_bid"
        out["decision_notes"] = (out.get("decision_notes") or "") + " [code order from harvested pages]"
    return out


def _list(facts: dict | None, *names: str):
    if not isinstance(facts, dict):
        return None
    for name in names:
        value = facts.get(name)
        if isinstance(value, list) and value:
            return value
    for part in facts.values():
        if isinstance(part, dict):
            found = _list(part, *names)
            if found:
                return found
    return None


def _has_material(line: dict) -> bool:
    return line.get("material") not in (None, "", "null")


def named_missing_material(decided: dict) -> bool:
    return any((not _has_material(line)) and line.get("position")
               for line in decided.get("lines") or [])


def _as_quantity(value):
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().replace(".", "").replace(",", ".").split()[0]
    try:
        return int(float(text))
    except ValueError:
        return None


def ensure_lines(lines: list, facts: dict | None) -> list:
    """Keep the analyst's lines and add tender positions it skipped, so code can still match them."""
    out = [dict(line) for line in (lines or []) if isinstance(line, dict)]
    have = {str(line.get("position")) for line in out if line.get("position")}
    for row in _list(facts, "positions") or []:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("position") or row.get("number") or row.get("pos") or "")
        if not pid or pid in have:
            continue
        qty = _as_quantity(row.get("quantity_pieces") or row.get("quantity") or row.get("menge"))
        out.append({"position": pid, "quantity_pieces": qty,
                    "requirement": row.get("requirement") or row.get("anforderung"), "material": None})
        have.add(pid)
    return out


def sanitize_decision(decided: dict, facts: dict | None) -> dict:
    """Price every line that has a material. Official all-or-nothing no_bid is only when nothing is priced."""
    out = dict(decided or {})
    lines = [recover_line(line, facts) for line in ensure_lines(out.get("lines") or [], facts)]
    priced = [line for line in lines if _has_material(line)]
    omitted = [str(line.get("position")) for line in lines if line.get("position") and not _has_material(line)]
    out["omitted_positions"] = omitted
    if priced:
        out["lines"] = priced
        out["decision"] = "bid"
        if omitted:
            note = f" [partial bid: omitting {', '.join(omitted)}]"
            out["decision_notes"] = (out.get("decision_notes") or "") + note
        elif decided.get("decision") == "no_bid":
            out["decision_notes"] = (out.get("decision_notes") or "") + " [code found an exact material match]"
        return out
    out["lines"] = lines
    if out.get("decision") != "no_bid":
        out["decision"] = "no_bid"
        out["decision_notes"] = (out.get("decision_notes") or "") + " [no priced line]"
    positions = _list(facts, "positions")
    requirements = _list(facts, "requirements")
    if not positions or not requirements:
        out["decision_notes"] = (out.get("decision_notes") or "") + " [incomplete retrieval; no_bid not from rule 3]"
    return out


def parse_json(text: str) -> dict:
    """The JSON object in a reply, also inside a ```json fence or after some prose."""
    text = text or ""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        return json.loads(fenced.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("analyst returned no JSON")
    return json.loads(text[start:end + 1])


def _dec(value, default="0") -> Decimal:
    if value in (None, ""):
        return Decimal(default)
    return Decimal(str(value).replace(",", ".")) if isinstance(value, str) else Decimal(str(value))


def to_answer(data: dict, expected_positions: int | None = None) -> dict:
    """Price every line that has a material and scales. A short bid is valid; unmatched positions are omitted."""
    del expected_positions
    order = to_order(data)
    if not order.lines:
        return pricing.no_bid(str(data.get("tender_reference") or ""))
    answer = pricing.price_order(order)
    if data.get("omitted_positions"):
        answer["omitted_positions"] = list(data["omitted_positions"])
    return answer


def to_order(data: dict) -> pricing.Order:
    """The analyst's JSON as a pricing.Order. Lines without a material or scales are skipped, not fatal."""
    lines = []
    for raw in data.get("lines") or []:
        if not _has_material(raw):
            continue
        cond = raw.get("price_condition") or {}
        if isinstance(cond, list) and cond:
            cond = cond[0]
        if not isinstance(cond, dict) or not cond.get("scales"):
            continue
        qty = _as_quantity(raw.get("quantity_pieces"))
        if not qty or qty <= 0:
            continue
        commitment = raw.get("commitment")
        lines.append(pricing.Line(
            position=str(raw["position"]),
            quantity=qty,
            material=str(raw.get("material")),
            condition=pricing.PriceCondition(
                scales=[pricing.PriceScale(int(s["min_quantity"]), _dec(s["price"])) for s in cond.get("scales", [])],
                currency=cond.get("currency") or "EUR",
                price_unit=int(cond.get("price_unit") or 1),
                eur_per_unit=_dec(cond.get("eur_per_unit"), "1")),
            stock=int(raw.get("stock") or 0),
            stock_lead_time_weeks=int(raw.get("stock_lead_time_weeks") or 0),
            production_lead_time_weeks=int(raw.get("production_lead_time_weeks") or 0),
            commitment=pricing.Commitment(str(commitment["material"]), int(commitment["ceiling"]),
                                          int(commitment["lead_time_weeks"]))
            if commitment and commitment.get("material") else None,
            mass_surcharge_per_unit=_dec(raw.get("mass_surcharge_per_unit")),
            setup_fee=_dec(raw.get("setup_fee")),
            discount_scope=raw.get("discount_scope") or pricing.SCOPE_BASE,
            carton_size=int(raw["carton_size"]) if raw.get("carton_size") else None,
            carton_price=_dec(raw.get("carton_price")),
            setup_pool=bool(raw.get("setup_pool")),
            allocation_mass=_dec(raw.get("allocation_mass")),
            bonus_eligible=bool(raw.get("bonus_eligible")),
        ))
    approval = data.get("approval")
    return pricing.Order(
        tender_reference=str(data["tender_reference"]),
        lines=lines,
        approval=pricing.Approval(_dec(approval["percent"]),
                                  _dec(approval["min_order_value"]) if approval.get("min_order_value") else None)
        if approval and approval.get("percent") is not None else None,
        setup_pool_fee=_dec(data.get("setup_pool_fee")),
        bonus_bands=[pricing.BonusBand(_dec(b.get("lower")), _dec(b["upper"]) if b.get("upper") is not None else None,
                                       _dec(b.get("percent"))) for b in data.get("bonus_bands") or []],
    )
