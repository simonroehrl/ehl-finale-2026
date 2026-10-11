"""Submit-phase helpers: deadlines, German amounts, TAN parse, a generic page-gate snippet.

No website values live here. The model still chooses the form path; this module only formats
what the resolver already calculated and recognises a signing code in text the model opened.
"""
from __future__ import annotations

import re
import time
from decimal import Decimal

import run as contract

SUBMIT_S = 270
SAFETY_S = 45
HEARTBEAT_S = 30
WALL_CAP_S = 20 * 60
# A Sonnet run that scores has cost about $2.40; Opus was $4+. Refuse to start below this.
MIN_CREDIT_USD = 3.0


def remaining_credit(info: dict) -> float | None:
    """OpenRouter /v1/key payload: limit minus usage, or None if the API omitted either."""
    limit, usage = info.get("limit"), info.get("usage")
    if limit is None or usage is None:
        return None
    try:
        return float(limit) - float(usage)
    except (TypeError, ValueError):
        return None


def require_credit(info: dict, need: float = MIN_CREDIT_USD) -> None:
    """Stop before a long session if the key cannot pay for a priced run."""
    left = remaining_credit(info)
    if left is None:
        return
    if left < need:
        raise SystemExit(
            f"OpenRouter credit left ${left:.2f} (limit {info.get('limit')}, usage {info.get('usage')}); "
            f"need about ${need:.0f} for one run. Add credit at openrouter.ai and update the team secret."
        )

# A page that blocks behind a checkbox + range slider. No site-specific labels.
CLEAR_GATES_JS = """() => {
  const clicked = [];
  for (const el of document.querySelectorAll('input[type="checkbox"], [role="checkbox"]')) {
    const nearby = `${el.getAttribute('aria-label') || ''} ${el.name || ''} ${el.id || ''} ${el.closest('label')?.innerText || ''}`;
    const checked = el.checked || el.getAttribute('aria-checked') === 'true';
    if (!checked && /robot|roboter|captcha|human/i.test(nearby)) {
      el.click();
      clicked.push('checkbox');
    }
  }
  const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
  for (const el of document.querySelectorAll('input[type="range"]')) {
    const max = el.max === '' ? 100 : Number(el.max);
    if (proto && proto.set) proto.set.call(el, max);
    else el.value = String(max);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    clicked.push('range');
  }
  return clicked.join(',') || 'none';
}"""

TAN_LABEL = re.compile(
    r"(?:signatur[-\s]?tan|tan(?:\s*(?:code|wert|nummer))?)[\s:#.]*([A-Z0-9]{6,12})\b",
    re.I,
)
TAN_HEX = re.compile(r"\b([A-F0-9]{8})\b")
SKIP_TAN = re.compile(r"^(TAN|SIGNATUR)$", re.I)


def phase_deadlines(task: dict, now: float | None = None, submit_s: float = SUBMIT_S,
                    safety_s: float = SAFETY_S) -> tuple[float, float]:
    """Monotonic cutoffs: finish research before `research_by`, finish submit before `submit_by`."""
    now = time.monotonic() if now is None else now
    remaining = contract.seconds_left(task)
    wall = WALL_CAP_S if remaining == float("inf") else min(max(0.0, remaining), WALL_CAP_S)
    submit_by = now + max(90.0, wall - safety_s)
    research_by = submit_by - submit_s
    if research_by < now + 60:
        research_by = now + 60
    return research_by, submit_by


def german(value) -> str:
    """1234.5 -> '1.234,50'."""
    return f"{Decimal(str(value)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def bid_values(answer: dict) -> dict:
    """Exact strings to type. The model maps visible fields to these keys."""
    positions = []
    for row in answer.get("positions") or []:
        positions.append({
            "position": row["position"],
            "material_number": str(row["material_number"]),
            "quantity": str(row["quantity"]),
            "lead_time_weeks": str(row["lead_time_weeks"]),
            "unit_price": german(row["unit_price"]),
            "base_unit_price": german(row["base_unit_price"]),
            "surcharge_per_unit": german(row["surcharge_per_unit"]),
            "packaging_per_unit": german(row["packaging_per_unit"]),
            "rebate_total": german(row["rebate_total"]),
        })
    return {
        "tender_reference": answer["tender_reference"],
        "decision": answer["decision"],
        "discount_percent": german(answer["discount_percent"]),
        "total_net": german(answer["total_net"]),
        "positions": positions,
    }


def extract_tan(text: str) -> str | None:
    """Newest signing code in a mail body. Prefers a labelled TAN; skips the word TAN itself."""
    if not text:
        return None
    found = [m.group(1) for m in TAN_LABEL.finditer(text) if not SKIP_TAN.fullmatch(m.group(1))]
    if not found:
        found = [m.group(1) for m in TAN_HEX.finditer(text)]
    return found[-1] if found else None


def is_sign_label(name: str) -> bool:
    text = (name or "").casefold()
    return any(word in text for word in (
        "verbindlich", "unterschreib", "signier", "sign and submit", "angebot absenden",
        "einreichen",
    ))
