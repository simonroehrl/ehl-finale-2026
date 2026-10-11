"""Enter the priced bid. Qwen picks controls; code types every amount. Native dialogs are accepted."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from actions import (SUBMIT_OPS, is_sign_control, lookup_value, parse_action, parse_json,
                     portal_confirmed, value_keys)
from extract import SHAPES
from view import Driver, format_view

PROMPTS = Path(__file__).resolve().parent / "prompts"
STEPS = 80
IDLE_S = 25


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def _site_blob(site: dict) -> str:
    c = site.get("credentials") or {}
    return f"`{site.get('siteId')}` {site['url']} user `{c.get('username', '')}` password `{c.get('password', '')}`"


async def _accept_dialogs(page) -> None:
    async def handle(dialog):
        try:
            await dialog.accept()
        except Exception:
            pass
    page.on("dialog", lambda dialog: asyncio.create_task(handle(dialog)))


def _guard(action, snapshot, answer, tan: str | None, signed: bool, name: str) -> str | None:
    nodes = {n.get("i") for n in snapshot.get("nodes") or []}
    if action.op in {"click", "fill"} and action.target not in nodes:
        return "target must be the i of a listed control."
    if action.op == "click" and signed and is_sign_control(name):
        return "The bid was already sent. Do not submit it again."
    if action.op == "fill" and action.value_key:
        if lookup_value(answer, tan, action.value_key, action.number_format) is None:
            return f"{action.value_key} is not a field of this bid."
    if action.op == "fill" and not action.value_key:
        # login / search only — allow text; amounts must use value_key
        if action.text is None:
            return "fill needs value_key (bid field) or text (login only)."
    return None


async def enter_bid(llm, tender: dict, mail: dict, answer: dict, omitted: list[str],
                    submission: str, until: float) -> str:
    t_driver, m_driver = Driver(tender["page"]), Driver(mail["page"])
    await _accept_dialogs(tender["page"])
    try:
        await t_driver.open(tender["site"]["url"])
    except Exception as error:  # noqa: BLE001
        print(f"[submit] tender open failed: {error!r}", flush=True)
    tan, signed, hint, previous, repeats = None, False, "", "", 0
    last_progress = time.monotonic()
    active = "tender"
    drivers = {"tender": t_driver, "mail": m_driver}

    for _ in range(STEPS):
        now = time.monotonic()
        if now > until:
            return "submit deadline"
        if now - last_progress > IDLE_S:
            return "submit idle"
        driver = drivers[active]
        snapshot = await driver.snapshot()
        if portal_confirmed(snapshot.get("text") or ""):
            return snapshot.get("text") or "portal confirmed"
        extra = (
            f"\nTender: {_site_blob(tender['site'])}\nMail: {_site_blob(mail['site'])}\n"
            f"Active browser: {active}\n"
            f"How to submit, from the brief:\n{submission[:4000]}\n"
            f"Omitted positions: {', '.join(omitted) or 'none'}\n"
            f"VALUE KEYS:\n" + "\n".join(value_keys(answer, tan)) + "\n"
        )
        if hint:
            extra += f"\nHint: {hint}\n"
        try:
            raw = await llm.complete(_prompt("submit.md"), format_view(snapshot, extra=extra), 220, "submit",
                                    research=False)
            action = parse_action(raw, SUBMIT_OPS)
        except (ValueError, RuntimeError) as error:
            hint = f"Reply with one allowed JSON action. ({error})"
            continue
        if action.browser in drivers and action.browser != active:
            active = action.browser
            hint = f"Now looking at {active}."
            last_progress = time.monotonic()
            continue
        if action.signature() == previous:
            repeats += 1
        else:
            repeats = 0
        previous = action.signature()
        if repeats >= 2:
            hint = "That action just repeated. Pick a different control."
            continue
        name = driver.node_name(action.target)
        blocked = _guard(action, snapshot, answer, tan, signed, name)
        if blocked:
            hint = blocked
            continue
        if action.op == "done":
            if portal_confirmed(snapshot.get("text") or "") or portal_confirmed(action.text or ""):
                proof = action.text or ""
                if proof and proof in (snapshot.get("text") or ""):
                    return proof
                if portal_confirmed(snapshot.get("text") or ""):
                    return snapshot.get("text") or proof
            hint = "done only after the portal shows the signed-bid confirmation. Copy a phrase from that text."
            continue
        if action.op == "extract_tan":
            tan = await _read_tan(llm, m_driver) or tan
            active = "tender"
            hint = "TAN recorded." if tan else "No TAN on that mail view. Open the newest signing mail."
            last_progress = time.monotonic()
            continue
        if action.op == "fill" and action.value_key:
            action.text = lookup_value(answer, tan, action.value_key, action.number_format)
        outcome = await driver.perform(action)
        print(f"[submit/{active}] {action.op} {action.target or action.value_key or ''}".rstrip(), flush=True)
        if outcome == "error":
            hint = "That control did not work. Choose another."
            continue
        if action.op == "click" and is_sign_control(name):
            signed = True
        last_progress = time.monotonic()
        hint = ""
    return "submit steps exhausted"


async def _read_tan(llm, driver: Driver) -> str | None:
    snapshot = await driver.snapshot()
    user = (
        f"Shape:\n{SHAPES['tan']}\n\n"
        f"Copy the signing TAN printed on this mailbox view. JSON only.\n\n"
        f"{(snapshot.get('text') or '')[:8000]}"
    )
    try:
        raw = await llm.complete(_prompt("extract.md"), user, 200, "tan", research=False)
        data = parse_json(raw)
    except (ValueError, RuntimeError):
        return None
    tan = data.get("tan")
    return str(tan).strip() if tan else None
