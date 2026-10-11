"""Dependency-aware scheduling across the four sites.

Login starts on every site at once. The specification and the mailbox wait briefly for the tender
reference, and the ERP waits until a catalogue group is known. The kernel then prices the ledger.
Missing facts become a short second pass. The tender browser enters the bid; the mailbox is asked
for the TAN only when the form needs it.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from contract import seconds_left, submit_answer
from driver import PlaywrightDriver, open_browsers
from kernel import resolve, validate_answer
from loop import prompt, pursue
from planning import group_gaps, parse_json
from records import Ledger

ROLE_OF = {"tender-portal": "tender", "customer-portal": "spec", "erp": "erp", "mail": "mail"}
SITE_OF = {role: site for site, role in ROLE_OF.items()}


def deadlines(run: dict) -> tuple[float, float]:
    left = seconds_left(run)
    if left == float("inf"):
        left = float(os.environ.get("OPT_LOCAL_BUDGET_S", "1200"))
    reserve = min(180.0, max(90.0, left * 0.2))
    now = time.monotonic()
    return now + max(45.0, left - reserve), now + max(30.0, left - 20.0)


async def run_solution(task: dict, token: str | None, metrics, model) -> None:
    access = list(task.get("access") or [])
    if not access:
        raise SystemExit("the run has no website access")
    ledger = Ledger()
    metrics.begin("brief")
    await _read_brief(model, ledger, task.get("brief") or "")
    metrics.end("brief")
    _note_mailbox_domain(ledger, access)
    research_end, hard_end = deadlines(task)
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        pages, closers = await open_browsers(playwright, min(4, len(access)))
        drivers = [PlaywrightDriver(page) for page in pages]
        try:
            roles = await _roles(access, drivers, model)
            events = {role: asyncio.Event() for role in roles}
            await _investigate(roles, drivers, ledger, model, metrics, research_end, events)
            resolution = await _recover(roles, drivers, ledger, model, metrics, research_end)
            if resolution.answer is None and time.monotonic() < hard_end:
                resolution = resolve(ledger.ev, strict=False)
            if resolution.answer is None:
                metrics.fail("no-answer")
                print("[opt] no bid could be priced", flush=True)
                return
            problems = validate_answer(resolution.answer)
            if problems:
                print(f"[opt] answer rejected locally: {problems}", flush=True)
                metrics.fail("invalid-answer")
                return
            print(json.dumps(resolution.answer, ensure_ascii=False), flush=True)
            await _submit(task, token, resolution, roles, drivers, ledger, model, metrics, hard_end)
        finally:
            for driver in drivers:
                driver.close()
            for browser in closers:
                await browser.close()


async def _read_brief(model, ledger: Ledger, brief: str) -> None:
    if not brief.strip():
        return
    try:
        raw = await model.complete(prompt("brief.md"), brief[:30000], 600, "brief")
        ledger.merge(parse_json(raw), "run-brief")
    except Exception as error:  # noqa: BLE001 - the sites can still supply the case
        print(f"[brief] skipped: {type(error).__name__}", flush=True)


def _note_mailbox_domain(ledger: Ledger, access: list[dict]) -> None:
    for site in access:
        if ROLE_OF.get(site.get("siteId")) != "mail":
            continue
        username = ((site.get("credentials") or {}).get("username") or "")
        if "@" in username:
            ledger.note_domain(username.split("@", 1)[1])


async def _roles(access: list[dict], drivers: list, model) -> dict[str, dict]:
    """Map role -> site. Unknown site ids are classified from the first page, one call each."""
    assigned: dict[str, dict] = {}
    pending = []
    for site in access:
        role = ROLE_OF.get(site.get("siteId"))
        if role and role not in assigned:
            assigned[role] = site
        elif role is None:
            pending.append(site)
    for index, site in enumerate(pending):
        driver = drivers[index % len(drivers)]
        try:
            await driver.open_start(site["url"])
            raw = await model.complete(prompt("classify.md"), json.dumps(await driver.snapshot())[:4000], 40, "classify")
            role = parse_json(raw).get("role")
        except Exception as error:  # noqa: BLE001
            print(f"[classify] {site.get('siteId')}: {type(error).__name__}", flush=True)
            continue
        if role in SITE_OF and role not in assigned:
            assigned[role] = site
    return assigned


async def _investigate(roles, drivers, ledger, model, metrics, deadline, events) -> None:
    pairs = list(roles.items())
    parallel = []
    for index, (role, site) in enumerate(pairs):
        if index < len(drivers):
            parallel.append(_work(role, site, drivers[index], ledger, model, metrics, deadline, events))
    await asyncio.gather(*parallel)
    for index, (role, site) in enumerate(pairs[len(drivers):]):
        await _work(role, site, drivers[0], ledger, model, metrics, deadline, events)


async def _work(role, site, driver, ledger, model, metrics, deadline, events) -> None:
    try:
        credentials = site.get("credentials") or {}
        driver.username = credentials.get("username") or ""
        driver.password = credentials.get("password") or ""
        await driver.open_start(site["url"])
        login = (
            "Log in if a login form is shown. Choose the username and password fields yourself and type the "
            f"credentials exactly.\nUsername: {credentials.get('username', '')}\nPassword: {credentials.get('password', '')}\n"
            "If a dialog blocks the page, close it. Use done when the main page is usable. Do not collect the bid yet."
        )
        metrics.begin(f"{role}:login")
        await pursue(driver, model, ledger, login, role=role, metrics=metrics, max_steps=7, deadline=deadline)
        metrics.end(f"{role}:login")
        await _wait(role, ledger, events, deadline)
        metrics.begin(role)
        status = await pursue(driver, model, ledger, _goal(role, ledger), role=role, metrics=metrics, max_steps=16, deadline=deadline)
        metrics.end(role)
        if role == "mail" and status == "done":
            ledger.ev.mail_complete = True
    except Exception as error:  # noqa: BLE001 - one site must not cancel the others
        metrics.fail(role)
        print(f"[{role}] failed: {type(error).__name__}: {error}", flush=True)
    finally:
        events[role].set()


def _goal(role: str, ledger: Ledger) -> str:
    reference = ledger.ev.reference() or "the tender reference shown in KNOWN FACTS"
    groups = ", ".join(ledger.ev.catalog_groups()) or "each Kataloggruppe shown in KNOWN FACTS"
    if role == "tender":
        return (
            "Find the tender that this login belongs to. Extract its reference, the scenario date, every "
            "bill-of-quantities position (quantity, unit, requirement) and every Q&A answer. Keep withdrawals, "
            "quantity changes, technical amendments and assigned commercial keys. Do not decide which revision "
            "or answer wins, and do not submit a bid."
        )
    if role == "spec":
        return (
            f"Find the technical specification for {reference}. Open the revision that is explicitly marked "
            "current or gültig, not merely the newest file. Extract every requirement of that revision: bore, "
            "stroke, pressure, mounting, certification, Kataloggruppe, pieces per set, and whether the stroke "
            "is a usable stroke with its Endreserve key. Also extract superseded revisions only far enough to "
            "see that they are not current."
        )
    if role == "erp":
        return (
            f"Search the ERP for {groups}. Keep each search narrow enough to list at most 20 materials. Open "
            "every listed candidate and extract status, the five technical attributes, stock, both lead times, "
            "price conditions with scales, currency, validity and price unit, pieces per carton, allocation "
            "mass and surcharge mass. When every listed candidate of a group was opened, extract that search "
            "with complete=true."
        )
    return (
        f"Search the mailbox for {reference}. The search combines words with AND, so search the reference plus "
        "one topic at a time. Read the inbox and the archive. Extract every message about this exact reference "
        "from the authorized senders: director discount, Konditionsregeln, Preisindizes, Technikfreigabe, "
        "Verpackungstarife, Terminfreigabe, Devisenfixing, Rüstpools and Projektbonus. Include superseded "
        "messages; do not decide which one wins. Leave the signing TAN until it is asked for. Use done only "
        "after the inbox and the archive have both been searched."
    )


async def _wait(role: str, ledger: Ledger, events, deadline: float) -> None:
    start = time.monotonic()
    if role in {"spec", "mail"}:
        while not ledger.ev.reference() and time.monotonic() < min(deadline, start + 45):
            await asyncio.sleep(1)
    if role == "erp":
        spec_done = events.get("spec")
        while not ledger.ev.catalog_groups():
            if spec_done is None or spec_done.is_set() or time.monotonic() > deadline - 120:
                break
            await asyncio.sleep(1)


async def _recover(roles, drivers, ledger, model, metrics, deadline) -> object:
    metrics.begin("kernel")
    resolution = resolve(ledger.ev, strict=True)
    metrics.end("kernel")
    role_driver = {role: drivers[min(index, len(drivers) - 1)] for index, role in enumerate(roles)}
    for round_number in (1, 2):
        if not resolution.gaps or time.monotonic() > deadline:
            break
        print(f"[opt] recovery {round_number}: {len(resolution.gaps)} gaps", flush=True)
        tasks = []
        task_roles = []
        for site, questions in group_gaps(resolution.gaps).items():
            role = next((name for name, site_id in SITE_OF.items() if site_id == site), None)
            if role not in role_driver:
                continue
            goal = "The pricing rules still need these facts. Open the document that contains each one and extract it.\n" + "\n".join(
                f"- {question}" for question in questions[:8]
            )
            task_roles.append(role)
            tasks.append(pursue(
                role_driver[role], model, ledger, goal, role=role, metrics=metrics, max_steps=6, deadline=deadline,
            ))
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for role, result in zip(task_roles, results):
                if role == "mail" and result == "done":
                    ledger.ev.mail_complete = True
        metrics.begin("kernel")
        resolution = resolve(ledger.ev, strict=True)
        metrics.end("kernel")
    return resolution


async def _submit(task, token, resolution, roles, drivers, ledger, model, metrics, hard_end) -> None:
    answer = resolution.answer
    submission = task.get("submission") or {}
    metrics.begin("submit")
    try:
        if submission.get("kind") == "api":
            status, body = submit_answer(task, token, answer)
            metrics.submitted = status == 202
            print(f"[submit] HTTP {status} {body[:300]}", flush=True)
            return
        role_driver = {role: drivers[min(index, len(drivers) - 1)] for index, role in enumerate(roles)}
        tender = role_driver.get("tender")
        if tender is None:
            metrics.fail("submit")
            return
        omitted = ", ".join(resolution.omitted) or "none"
        goal = (
            "Enter this bid in the portal's bid form and sign it once. Match each visible input to a value_key; "
            "the program types the value. If the form can omit an unfinished position, omit only the listed ones. "
            "When the signature needs a TAN, use need_tan and wait. After the page confirms that the bid was "
            f"accepted, use done with a short verbatim proof phrase. Omitted positions: {omitted}."
        )
        if getattr(tender, "username", ""):
            goal += (
                "\nIf the session has expired, log in again with "
                f"username {tender.username} and password {tender.password}."
            )
        for _ in range(4):
            if time.monotonic() > hard_end:
                break
            status = await pursue(
                tender, model, ledger, goal, role="submit", metrics=metrics, max_steps=8,
                deadline=hard_end, mode="submit", answer=answer,
            )
            if status == "done":
                metrics.submitted = True
                print("[submit] portal confirmed the bid", flush=True)
                return
            if status == "need_tan" and "mail" in role_driver:
                tan_goal = (
                    f"Find the signing TAN for {answer['tender_reference']} in the mailbox. Extract the code. "
                    "Do not send a message."
                )
                await pursue(role_driver["mail"], model, ledger, tan_goal, role="mail", metrics=metrics, max_steps=8, deadline=hard_end)
        metrics.fail("submit")
        print("[submit] the portal did not confirm the bid", flush=True)
    finally:
        metrics.end("submit")
