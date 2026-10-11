"""The model picks the next control; code reads the page it just opened."""
from __future__ import annotations

import time
from pathlib import Path

from actions import (Action, batch_until_navigation, is_sign_control, lookup_value, parse_actions,
                     render_turn)
from extract import read_page, worth_reading
from results import find_controls, ids_on_snapshot, listed_from, opened_from

PROMPTS = Path(__file__).resolve().parent / "prompts"


def policy() -> str:
    return (PROMPTS / "policy.md").read_text(encoding="utf-8")


class Scripted:
    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, system: str, user: str, max_tokens: int, phase: str) -> str:
        self.calls += 1
        if not self.replies:
            raise RuntimeError("scripted model ran out of replies")
        return self.replies.pop(0)


async def harvest(driver, store, role: str, snapshot: dict | None = None) -> int:
    text, tables = await driver.reading()
    facts = read_page(text, tables, store.case.authorized_senders) if worth_reading(text) else {}
    if snapshot:
        store.note_listed(role, ids_on_snapshot(snapshot, {store.case.tender_reference}))
    if facts:
        store.note_listed(role, listed_from(facts, role))
        store.note_opened(role, opened_from(facts, role))
        before = store.summary()
        await store.add(facts)
        after = store.summary()
        if after != before:
            print(f"[{role}] read: {after.replace(chr(10), ' | ')}", flush=True)
            return 1
    return 0


async def drain(driver, store, role: str, snapshot: dict, limit: int = 12) -> int:
    """Open each unread row of the result set the model is already on."""
    opened = 0
    for ident in list(store.unread(role))[:limit]:
        current = await driver.snapshot()
        targets = find_controls(current, ident)
        if not targets:
            continue
        for target in targets[:3]:
            outcome = await driver.perform(Action(op="click", target=target))
            print(f"[{role}] open {ident} via {target}", flush=True)
            if outcome == "error":
                continue
            await harvest(driver, store, role, await driver.snapshot())
            if ident not in store.unread(role):
                opened += 1
                await driver.perform(Action(op="back"))
                break
            await driver.perform(Action(op="back"))
    if opened:
        print(f"[{role}] opened {opened} listed records", flush=True)
    return opened


async def pursue(driver, model, store, goal: str, *, role: str, max_steps: int, deadline: float,
                 mode: str = "investigate", answer: dict | None = None) -> str:
    hint = ""
    previous = ""
    repeats = 0
    last_digest = ""
    for _ in range(max_steps):
        if time.monotonic() > deadline:
            return "deadline"
        snapshot = await driver.snapshot()
        digest = (snapshot.get("url") or "") + (snapshot.get("text") or "")[:400]
        if digest != last_digest:
            last_digest = digest
            await harvest(driver, store, role, snapshot)
            if mode != "submit" and store.unread(role):
                await drain(driver, store, role, snapshot)
            snapshot = await driver.snapshot()
        if mode == "submit":
            goal = _submit_goal(goal, answer, store)
        unread = store.unread(role)
        shown_goal = goal
        if unread and mode != "submit":
            shown_goal = goal + "\nUnread rows on this result set (the helper opens them): " + ", ".join(unread[:12])
        user = render_turn(shown_goal, store.summary(), snapshot, hint)
        raw = await model.complete(policy(), user, 280, role)
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
            if action.signature() == previous:
                repeats += 1
            else:
                repeats = 0
            previous = action.signature()
            if repeats >= 2:
                hint = "That action was just repeated. Choose a different element, go back, or open a document."
                break
            refusal = _guard(action, snapshot, mode, bool(getattr(driver, "signed", False)), answer, store)
            if refusal:
                hint = refusal
                break
            if action.op == "done":
                if mode != "submit" and store.unread(role):
                    await drain(driver, store, role, snapshot)
                    if store.unread(role):
                        hint = ("Unread records remain: " + ", ".join(store.unread(role)[:8])
                                + ". Open a matching row; done is blocked until they are read.")
                        break
                if mode != "submit" or _proved(action, snapshot):
                    return "done"
                hint = "Copy a short verbatim phrase from the page that shows the bid was accepted into text."
                break
            if action.op == "need_tan":
                return "need_tan"
            if action.op == "fill" and action.value_key and answer is not None:
                typed = lookup_value(answer, _tan(store), action.value_key, action.number_format)
                if typed is None:
                    hint = f"Unknown value_key {action.value_key}."
                    break
                action = Action(op="fill", target=action.target, text=typed)
            outcome = await driver.perform(action)
            print(f"[{role}] {action.op} {action.target if action.target is not None else ''}".rstrip(), flush=True)
            if outcome == "error":
                hint = "The last action did not change the page. Try another control."
                break
            if action.op == "click" and is_sign_control(_node_name(snapshot, action.target)):
                driver.signed = True
            if outcome in {"downloaded", "collected", "navigated"}:
                last_digest = ""
                break
            if outcome != "filled":
                break
    return "steps"


def _tan(store) -> str | None:
    from store import tan_from
    return tan_from(store.evidence)


def _node_name(snapshot: dict, target: int | None) -> str:
    for node in snapshot.get("nodes") or []:
        if node.get("i") == target:
            return node.get("name") or ""
    return ""


def _proved(action: Action, snapshot: dict) -> bool:
    proof = (action.text or "").strip()
    return bool(proof) and proof.lower() in (snapshot.get("text") or "").lower()


def _submit_goal(goal: str, answer: dict | None, store) -> str:
    tan = _tan(store) or "(not found yet)"
    extra = f"\nTAN if the form asks: {tan}"
    return goal + extra


def _guard(action: Action, snapshot: dict, mode: str, signed: bool, answer: dict | None, store) -> str:
    if action.op == "need_tan" and mode != "submit":
        return "need_tan is only for bid entry."
    if mode == "submit" and action.op == "done" and not signed:
        return "Sign the bid before done."
    if action.target is not None and action.op in {"click", "fill", "collect_pages"}:
        if not any(node.get("i") == action.target for node in snapshot.get("nodes") or []):
            return "That target is not on this page."
    return ""
