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

Reply with one JSON object and nothing else, in this shape:
{ORDER_SHAPE}"""


async def analyse(brief: str, facts: dict, timeout: float = 300) -> dict:
    client = qwen_client()
    user = f"<brief>\n{brief}\n</brief>\n\n<facts>\n{json.dumps(facts, ensure_ascii=False)}\n</facts>"
    response = await client.with_options(max_retries=0).chat.completions.create(
        model="qwen", temperature=0, timeout=timeout,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])
    return parse_json(response.choices[0].message.content or "")


def parse_json(text: str) -> dict:
    """The JSON object in a reply, also inside a ```json fence or after some prose."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidate = fenced.group(1) if fenced else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(candidate)


def _dec(value, default="0") -> Decimal:
    if value in (None, ""):
        return Decimal(default)
    return Decimal(str(value).replace(",", ".")) if isinstance(value, str) else Decimal(str(value))


def to_answer(data: dict, expected_positions: int | None = None) -> dict:
    """The analyst's decision priced: the official no_bid answer, or a bid with one entry per tender position."""
    if data.get("decision") == "no_bid":
        return pricing.no_bid(str(data["tender_reference"]))
    order = to_order(data)
    if not order.lines:
        raise ValueError("decision bid without any position")
    if expected_positions and len(order.lines) != expected_positions:
        raise ValueError(f"{len(order.lines)} priced positions, the tender has {expected_positions}")
    return pricing.price_order(order)


def to_order(data: dict) -> pricing.Order:
    """The analyst's JSON as a pricing.Order; raises ValueError on a shape the arithmetic can't use."""
    lines = []
    for raw in data["lines"]:
        cond = raw.get("price_condition") or {}
        commitment = raw.get("commitment")
        lines.append(pricing.Line(
            position=str(raw["position"]),
            quantity=int(raw["quantity_pieces"]),
            material=raw.get("material") or None,
            condition=pricing.PriceCondition(
                scales=[pricing.PriceScale(int(s["min_quantity"]), _dec(s["price"])) for s in cond.get("scales", [])],
                currency=cond.get("currency") or "EUR",
                price_unit=int(cond.get("price_unit") or 1),
                eur_per_unit=_dec(cond.get("eur_per_unit"), "1")) if cond.get("scales") else None,
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
    for line in lines:
        if line.material and line.condition is None:
            raise ValueError(f"position {line.position}: a material but no price condition")
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
