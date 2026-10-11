"""One model per browser picks the next control. Code reads the opened page and prices the bid."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import run as contract
from driver import Driver, open_browsers
from llm import LLM
from loop import pursue
from resolver import Gap, resolve
from store import Store, case_from_brief, catalog_groups

SITE_ALIASES = {
    "tender": ("tender-portal", "tender", "vergabeportal"),
    "spec": ("customer-portal", "spec", "supplier-portal", "lieferantenportal"),
    "erp": ("erp",),
    "mail": ("mail", "webmail", "werkpost"),
}
SUBMIT_S = 180


def site_map(task: dict) -> dict[str, dict]:
    access = list(task.get("access") or [])
    by_id = {s.get("siteId"): s for s in access}
    out, used = {}, set()
    for role, aliases in SITE_ALIASES.items():
        found = next((by_id[a] for a in aliases if a in by_id), None)
        if found:
            out[role] = found
            used.add(found.get("siteId"))
    leftover = [s for s in access if s.get("siteId") not in used]
    for role in SITE_ALIASES:
        if role not in out and leftover:
            out[role] = leftover.pop(0)
    return out


def deadlines(task: dict) -> tuple[float, float]:
    left = contract.seconds_left(task)
    if left == float("inf"):
        left = float(os.environ.get("THIN_LOCAL_BUDGET_S", "1200"))
    now = time.monotonic()
    return now + max(90.0, left - SUBMIT_S), now + max(60.0, left - 20.0)


def priors(store: Store) -> str:
    case = store.case
    return (
        f"From the brief, not from this website: tender reference `{case.tender_reference}`, "
        f"scenario date {case.scenario_date}, bidder mail domain `{case.bidder_mail_domain}`."
    )


def goal(role: str, store: Store) -> str:
    ref = store.case.tender_reference
    groups = ", ".join(catalog_groups(store.evidence)) or "each catalogue group you find on this site or in KNOWN FACTS"
    login = ""
    # filled by worker
    if role == "tender":
        hunt = (
            "This site holds the bill of quantities and bidder Q&A. Open the bill itself, not a list of tenders. "
            "Find every position (number, quantity, stated unit, linked requirement) and every Q&A answer on every page."
        )
    elif role == "spec":
        hunt = (
            f"This site holds specification revisions for {ref}. A search hit or register row is not the document. "
            "Open each revision document and let the program read the marking words and the requirements "
            "(attributes and catalogue group). Do not infer current from the date."
        )
    elif role == "erp":
        hunt = (
            f"This site holds the material catalogue. Search for: {groups}. "
            "Keep each search narrow enough to list at most 20 materials, then open every listed candidate."
        )
    else:
        hunt = (
            f"This site holds the sales mailbox. Search {ref} plus one topic at a time (AND). "
            "Read inbox and archive. Open each message, not only the folder list."
        )
    return f"{priors(store)}\n{hunt}\nDo not submit or sign anything."


def login_goal(site: dict) -> str:
    c = site.get("credentials") or {}
    return (
        "Log in if a login form is shown. Choose the username and password fields yourself.\n"
        f"Username: {c.get('username', '')}\nPassword: {c.get('password', '')}\n"
        "Close cookie walls and dialogs. Use done when the main page is usable."
    )


def describe(resolution) -> str:
    answer = resolution.answer if resolution else None
    decision = answer.get("decision") if answer else "none"
    npos = len(answer.get("positions") or []) if answer else 0
    lines = [f"resolve: decision={decision} priced={npos} gaps={len(resolution.gaps)}"]
    for gap in resolution.gaps:
        lines.append(f"  gap [{gap.site}] {gap.question}")
    return "\n".join(lines)


async def work(role: str, site: dict, driver: Driver, store: Store, model, research_end: float,
               events: dict[str, asyncio.Event]) -> None:
    try:
        c = site.get("credentials") or {}
        driver.username = c.get("username") or ""
        driver.password = c.get("password") or ""
        await driver.open(site["url"])
        await pursue(driver, model, store, login_goal(site), role=role, max_steps=6, deadline=research_end)
        if role == "erp":
            while not catalog_groups(store.evidence) and time.monotonic() < research_end - 90:
                if events["spec"].is_set():
                    break
                await asyncio.sleep(2)
        await pursue(driver, model, store, goal(role, store), role=role, max_steps=18, deadline=research_end)
    except Exception as error:  # noqa: BLE001
        print(f"[{role}] failed: {type(error).__name__}: {error}", flush=True)
    finally:
        events[role].set()


def role_for(gap: Gap) -> str | None:
    site = (gap.site or "").lower()
    for role, aliases in SITE_ALIASES.items():
        if site in aliases or site == role:
            return role
    return None


async def recover(roles: dict[str, Driver], store: Store, model, research_end: float) -> object:
    resolution = resolve(store.evidence)
    print(describe(resolution), flush=True)
    if not resolution.gaps or time.monotonic() > research_end:
        return resolution
    by_role: dict[str, list[str]] = {}
    for gap in resolution.gaps:
        role = role_for(gap)
        if role in roles:
            by_role.setdefault(role, []).append(gap.question)
    tasks = []
    for role, questions in by_role.items():
        text = "These facts are still missing. Open the document that contains each one.\n" + "\n".join(
            f"- {q}" for q in questions[:8])
        tasks.append(pursue(roles[role], model, store, text, role=role, max_steps=8, deadline=research_end))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    resolution = resolve(store.evidence)
    print(describe(resolution), flush=True)
    return resolution


def bid_text(answer: dict) -> str:
    from decimal import Decimal
    def german(value):
        return f"{Decimal(str(value)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    lines = [f"Tender reference: {answer['tender_reference']}", f"Decision: {answer['decision']}",
             f"Discount: {german(answer['discount_percent'])} %"]
    for p in answer.get("positions") or []:
        lines.append(
            f"- Position {p['position']}: material {p['material_number']}, quantity {p['quantity']}, "
            f"unit price {german(p['unit_price'])}, lead {p['lead_time_weeks']}"
        )
    lines.append(f"Total net: {german(answer['total_net'])} EUR")
    return "\n".join(lines)


async def submit_bid(task, token, resolution, roles: dict[str, Driver], store: Store, model, hard_end: float) -> None:
    answer = resolution.answer
    submission = task.get("submission") or {}
    if submission.get("kind") == "api":
        if token:
            status, body = contract.submit_answer(task, token, answer)
            print(f"[submit] HTTP {status} {body[:300]}", flush=True)
        else:
            print(json.dumps(answer, ensure_ascii=False), flush=True)
        return
    tender = roles.get("tender")
    if tender is None:
        print("[submit] no tender browser", flush=True)
        return
    omitted = ", ".join(resolution.omitted) or "none"
    text = (
        "Enter this bid in the portal's bid form and sign it once. Match each visible input to a value_key; "
        "the program types the value. If the form can omit an unfinished position, omit only the listed ones. "
        f"When the signature needs a TAN, use need_tan. Omitted: {omitted}.\n{bid_text(answer)}"
    )
    if tender.username:
        text += f"\nIf the session expired, log in as {tender.username} / {tender.password}."
    for _ in range(3):
        if time.monotonic() > hard_end:
            break
        status = await pursue(tender, model, store, text, role="submit", max_steps=10,
                              deadline=hard_end, mode="submit", answer=answer)
        if status == "done":
            print("[submit] portal confirmed the bid", flush=True)
            return
        if status == "need_tan" and "mail" in roles:
            await pursue(
                roles["mail"], model, store,
                f"Find the signing TAN for {answer['tender_reference']}. Open the message that contains it.",
                role="mail", max_steps=8, deadline=hard_end,
            )
    print("[submit] the portal did not confirm the bid", flush=True)


async def solve(task: dict, token: str | None) -> None:
    store = Store(case_from_brief(task.get("brief") or ""))
    print(f"case {store.case.tender_reference} {store.case.scenario_date} @{store.case.bidder_mail_domain}", flush=True)
    research_end, hard_end = deadlines(task)
    mapping = site_map(task)
    from playwright.async_api import async_playwright
    model = LLM()
    async with async_playwright() as playwright:
        pages, closers = await open_browsers(playwright, 4)
        drivers = [Driver(page) for page in pages]
        roles = {}
        try:
            events = {role: asyncio.Event() for role in SITE_ALIASES}
            workers = []
            for index, role in enumerate(SITE_ALIASES):
                site = mapping.get(role)
                if not site:
                    events[role].set()
                    continue
                roles[role] = drivers[index]
                workers.append(work(role, site, drivers[index], store, model, research_end, events))
            await asyncio.gather(*workers)
            resolution = await recover(roles, store, model, research_end)
            if resolution.answer is None:
                print("[thin] no priced answer", flush=True)
                return
            print(json.dumps(resolution.answer, ensure_ascii=False), flush=True)
            await submit_bid(task, token, resolution, roles, store, model, hard_end)
        finally:
            print(f"[thin] llm calls={model.calls} prompt={model.prompt_tokens} "
                  f"completion={model.completion_tokens}", flush=True)
            for driver in drivers:
                driver.close()
            for browser in closers:
                await browser.close()


async def main() -> int:
    task, token = contract.load_run()
    sites = ", ".join(f"{s.get('siteId')} {s.get('url')}" for s in task["access"])
    print(f"Run {task.get('run_id') or 'local'}; websites: {sites}", flush=True)
    await solve(task, token)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
