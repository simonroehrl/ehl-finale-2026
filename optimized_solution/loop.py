"""Model-directed investigation. The model picks the next controls; code parses, pages and calculates."""

from __future__ import annotations

import time
from pathlib import Path

from planning import Action, batch_until_navigation, is_sign_control, lookup_value, parse_actions, parse_json, render_turn
from records import Ledger

PROMPTS = Path(__file__).resolve().parent / "prompts"


def prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


class ScriptedModel:
    """Test double. Production uses llm.LLM."""

    def __init__(self, replies: list[str], metrics=None):
        self.replies = list(replies)
        self.metrics = metrics
        self.calls = 0

    async def complete(self, system: str, user: str, max_tokens: int, phase: str) -> str:
        self.calls += 1
        if self.metrics is not None:
            self.metrics.add_llm(len(user) // 4, 20, 0, 0.0, estimated=True)
        if not self.replies:
            raise RuntimeError("scripted model ran out of replies")
        return self.replies.pop(0)


async def extract_into(model, ledger: Ledger, text: str, locator: str, phase: str) -> int:
    user = f"SOURCE: {locator}\n\nDOCUMENT:\n{(text or '')[:14000]}"
    raw = await model.complete(prompt("extract.md"), user, 2500, phase)
    try:
        data = parse_json(raw)
    except ValueError:
        raw = await model.complete(prompt("extract.md"), user + "\n\nReply with one JSON object only.", 2500, phase)
        data = parse_json(raw)
    return ledger.merge(data, locator)


async def pursue(driver, model, ledger: Ledger, goal: str, *, role: str, metrics, max_steps: int,
                 deadline: float, mode: str = "investigate", answer: dict | None = None) -> str:
    """Run until the model says done, the step budget ends, or the deadline passes."""
    hint = ""
    previous = ""
    repeats = 0
    for _ in range(max_steps):
        if time.monotonic() > deadline:
            metrics.fail(f"{role}:deadline")
            return "deadline"
        snapshot = await driver.snapshot()
        if mode == "submit":
            goal = _submit_goal(goal, answer, ledger.ev.tan)
        user = render_turn(goal, ledger.ev.summary(), snapshot, hint)
        raw = await model.complete(prompt("policy.md"), user, 400, role)
        try:
            actions = batch_until_navigation(parse_actions(raw))
        except ValueError:
            hint = "Reply with one JSON object whose actions array uses the allowed ops."
            continue
        if not actions:
            hint = "Choose one action from the elements on this page."
            continue
        hint = ""
        for action in actions:
            signature = action.signature()
            if signature == previous:
                repeats += 1
                metrics.repeated_actions += 1
            else:
                repeats = 0
            previous = signature
            if repeats >= 2:
                hint = "That action was just repeated. Choose a different element, go back, or extract."
                break
            refusal = _guard(action, snapshot, mode, bool(getattr(driver, "signed", False)), answer, ledger.ev.tan)
            if refusal:
                hint = refusal
                break
            if action.op == "done":
                if mode != "submit" or _proved(action, snapshot):
                    return "done"
                hint = "Copy a short verbatim phrase from the page that shows the bid was accepted into text."
                break
            if action.op == "need_tan":
                return "need_tan"
            if action.op == "extract":
                await extract_into(model, ledger, await driver.reading(), snapshot.get("url") or role, role)
                break
            outcome = await driver.perform(action)
            metrics.browser_actions += 1
            print(f"[{role}] {action.op} {action.target if action.target is not None else ''}".rstrip(), flush=True)
            if outcome == "error":
                metrics.failed_actions += 1
                hint = "The last action did not change the page. Try another control."
                break
            if action.op == "click" and is_sign_control(_node_name(snapshot, action.target)):
                driver.signed = True
            if outcome in {"downloaded", "collected"}:
                await extract_into(model, ledger, await driver.reading(), snapshot.get("url") or role, role)
                break
            if outcome != "filled":
                break
    return "steps"


def _node_name(snapshot: dict, target: int | None) -> str:
    for node in snapshot.get("nodes") or []:
        if node.get("i") == target:
            return node.get("name") or ""
    return ""


def _proved(action: Action, snapshot: dict) -> bool:
    proof = (action.text or "").strip()
    return len(proof) >= 4 and proof in (snapshot.get("text") or "")


def _guard(action: Action, snapshot: dict, mode: str, signed: bool, answer: dict | None, tan: str | None) -> str | None:
    nodes = {node.get("i") for node in snapshot.get("nodes") or []}
    if action.op in {"click", "fill", "collect_pages"} and action.target not in nodes:
        return "target must be the i of an element listed above."
    if action.op == "click" and signed and is_sign_control(_node_name(snapshot, action.target)):
        return "The bid was already sent for signature. Do not submit it again."
    if action.op == "open_archive":
        files = snapshot.get("archive_files") or []
        if action.file not in files:
            return "file must be one of the archive names listed above."
    if mode == "submit" and action.op == "fill":
        if not answer or not action.value_key:
            return "During submission, fill fields with value_key, not with a typed price."
        expected = lookup_value(answer, tan, action.value_key, action.number_format)
        if expected is None:
            return f"{action.value_key} is not a field of this bid."
        action.text = expected
    if action.op == "collect_pages":
        action.times = min(8, action.times or 3)
    return None


def _submit_goal(base: str, answer: dict | None, tan: str | None) -> str:
    if not answer:
        return base
    lines = [base, "VALUE KEYS (you choose the field, the program types the value):"]
    for key in ("tender_reference", "decision", "currency", "discount_percent", "total_net"):
        lines.append(f"- {key} = {lookup_value(answer, tan, key)}")
    for position in answer.get("positions") or []:
        pid = position["position"]
        for field in ("material_number", "quantity", "unit_price", "lead_time_weeks", "base_unit_price",
                      "surcharge_per_unit", "packaging_per_unit", "rebate_total"):
            lines.append(f"- position.{pid}.{field} = {lookup_value(answer, tan, f'position.{pid}.{field}')}")
    if tan:
        lines.append(f"- tan = {tan}")
    else:
        lines.append("- tan is not known yet. Use need_tan before the signature control.")
    return "\n".join(lines)
