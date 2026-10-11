"""Cheap path: four Qwen browser agents in parallel, then the cc-agent rules and a Qwen submit.

Qwen only navigates and fills typed Evidence. resolver.py + pricing.py decide and calculate. That split is what
made the Claude path score 100; this package keeps the rules and swaps the expensive model for the hosted Qwen
so cost and wall-clock can match the leaderboard front.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from playwright.async_api import async_playwright
from pydantic import ValidationError

import browsers
import run as contract
from evidence import (Case, Evidence, ErpSearch, Material, Message, QAAnswer, SpecRevision, TenderPosition,
                      merge, schema_for)
from llm_agent import Agent
from marking_pass import answer_or_no_bid, is_marking_gap, marking_instruction, marking_turns
from resolver import Gap, senders_from_brief, resolve

HERE = Path(__file__).resolve().parent
PROMPTS = HERE / "prompts"
BROWSERS = 4
SUBMIT_S = 360
SAFETY_S = 30
GAP_S = 180
GAP_ROUNDS = 2
ROLE_S = 240
GAP_ROLE_S = 90
MAX_TURNS = {"tender": 28, "spec": 28, "erp": 32, "mail": 28, "submit": 70}

SITE_ALIASES = {
    "tender": ("tender-portal", "tender", "vergabeportal"),
    "spec": ("customer-portal", "spec", "supplier-portal", "lieferantenportal"),
    "erp": ("erp",),
    "mail": ("mail", "webmail", "werkpost"),
}
ROLE_FIELDS = {
    "tender": ("case", "positions", "qa"),
    "spec": ("case", "revisions"),
    "erp": ("case", "materials", "erp_searches"),
    "mail": ("case", "messages"),
}
ROLE_FOCUS = {
    "tender": "every bill-of-quantities position (number, quantity, unit_raw, unit_kind piece|set|other, "
              "requirement_id) and every Q&A answer on every page (number, ISO date, text, withdraws, "
              "quantity_changes, amendments, assignments)",
    "spec": "every revision (id, ISO date, marking current|superseded|unknown) and its requirements (id, "
            "bore_mm, stroke_mm, pressure_bar, mounting, certification, catalog_group, usable_stroke, "
            "pieces_per_set). marking=current only if the revision itself says so",
    "erp": "every catalogue group named in the brief, visible after login, or listed in the context: a complete "
           "erp_searches row, then every listed candidate as materials (number, status active|blocked|other, "
           "attributes, price_conditions with dates and scales, stock, lead times, carton, masses, metal_index)",
    "mail": "inbox and archive, every page: every message for this reference or a look-alike (id, sender, ISO "
            "date, subject, reference, kind, and the structured fields for that kind). kind must be one of the "
            "schema kinds; record every kind you see, including ones that will not apply",
}
ROLE_FOCUS_SURVEY = {
    "tender": "every position (number, quantity, unit, requirement id) and every Q&A (number, date, withdrawn, "
              "quantity changes, amendments). Skip long prose",
    "spec": "every revision (id, date, marking) and each requirement (id, catalogue group, numeric attributes). "
            "marking=current only if the revision says so",
    "erp": "every catalogue group from the brief or the login screen: the search row and every listed material "
            "number and status. Open a row only when attributes are not on the list. Price scales can wait",
    "mail": "inbox and archive: every message for this reference or a look-alike (id, sender, date, subject, "
            "reference, kind). Open a body only when the subject or kind points to structured values",
}
LIST_MODELS = {"positions": TenderPosition, "qa": QAAnswer, "revisions": SpecRevision,
               "materials": Material, "erp_searches": ErpSearch, "messages": Message}


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


def creds(task: dict, role: str) -> dict:
    site = site_map(task).get(role) or {}
    c = site.get("credentials") or {}
    return {"url": site.get("url", ""), "username": c.get("username", ""), "password": c.get("password", ""),
            "siteId": site.get("siteId", role)}


def phase_deadlines(task: dict, now: float | None = None, submit_s: float = SUBMIT_S,
                    gap_s: float = GAP_S, safety_s: float = SAFETY_S) -> tuple[float, float, float]:
    """Research, gap-fill and submit cutoffs (monotonic). Gap and submit time are reserved up front."""
    now = time.monotonic() if now is None else now
    wall = min(contract.seconds_left(task), 20 * 60)
    submit_by = now + max(90.0, wall - safety_s)
    gap_by = submit_by - submit_s
    research_by = gap_by - gap_s
    if research_by < now + 45:
        research_by = now + 45
    return research_by, gap_by, submit_by


def role_until(deadline: float, cap_s: float = ROLE_S, now: float | None = None) -> float:
    """A site agent stops at the earlier of the phase cutoff and a per-role cap."""
    now = time.monotonic() if now is None else now
    return min(deadline, now + cap_s)


def case_from_brief(brief: str) -> Case:
    """Best-effort case from the run brief (formats the platform always states), not from websites."""
    when = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", brief)
    ref = re.search(r"\b([A-Z]{1,6}-\d{4}-\d+)\b", brief)
    if not ref:
        ref = re.search(r"\(([A-Z0-9][-A-Z0-9/]{4,})\)", brief)
    domain = re.search(r"@([a-z0-9.-]+\.[a-z]{2,})", brief.lower())
    return Case(tender_reference=(ref.group(1) if ref else "UNKNOWN"),
                scenario_date=when.group(0) if when else "1970-01-01T00:00:00Z",
                bidder_mail_domain=domain.group(1) if domain else "example.invalid",
                authorized_senders=senders_from_brief(brief))


def coerce_evidence(facts: dict | None, fallback: Case) -> Evidence | None:
    """Keep valid records; drop broken ones into notes so one bad date does not discard a whole site."""
    if not isinstance(facts, dict) or not facts:
        return None
    notes = list(facts["notes"]) if isinstance(facts.get("notes"), list) else (
        [facts["notes"]] if facts.get("notes") else [])
    try:
        case = Case.model_validate(facts["case"]) if facts.get("case") else fallback
    except ValidationError:
        case = fallback
        notes.append("case from brief: worker case failed validation")
    data: dict = {"case": case, "notes": notes}
    for name, model in LIST_MODELS.items():
        raw = facts.get(name) or []
        if raw in ("", {}):
            raw = []
        if not isinstance(raw, list):
            raw = [raw]
        kept = []
        for item in raw:
            if not isinstance(item, dict):
                notes.append(f"dropped {name}: not an object")
                continue
            if name == "materials" and "attributes" not in item:
                item = {**item, "attributes": {}}
            try:
                kept.append(model.model_validate(item))
            except ValidationError as error:
                notes.append(f"dropped {name}: {error.error_count()} schema issues")
        data[name] = kept
    try:
        return Evidence.model_validate(data)
    except ValidationError:
        return None


def catalog_rows(spec: Evidence | None) -> list[dict]:
    if spec is None:
        return []
    rows = []
    for rev in spec.revisions:
        for req in rev.requirements:
            if req.catalog_group:
                rows.append({"requirement_id": req.id, "revision": rev.id, "catalog_group": req.catalog_group,
                             "usable_stroke": req.usable_stroke, "pieces_per_set": req.pieces_per_set,
                             "bore_mm": req.bore_mm, "stroke_mm": req.stroke_mm, "pressure_bar": req.pressure_bar,
                             "mounting": req.mounting, "certification": req.certification})
    return rows


def searched_groups(erp: Evidence | None) -> set[str]:
    if erp is None:
        return set()
    return {row.catalog_group for row in erp.erp_searches if row.catalog_group}


def pending_catalog(spec: Evidence | None, erp: Evidence | None) -> list[dict]:
    seen = searched_groups(erp)
    return [row for row in catalog_rows(spec) if row["catalog_group"] not in seen]


def role_prompt(role: str, task: dict, extra: str = "", context: str = "", mode: str = "survey") -> str:
    s = creds(task, role)
    focus = ROLE_FOCUS_SURVEY[role] if mode == "survey" else ROLE_FOCUS[role]
    how = ("Scout: open lists, result pages and archives. When a page has records, call record_page "
           "(browser_evaluate innerText first on a noisy page). Do not type the JSON yourself."
           if mode == "survey" else
           "Deepen: open only what the follow-up names. record_page on those pages. Fill nested fields.")
    return (
        f"Website `{s['siteId']}`: {s['url']} · username `{s['username']}` · password `{s['password']}`\n\n"
        f"<brief>\n{task['brief']}\n</brief>\n\n"
        f"{how}\nRecord {focus}. Include records that will not apply.\n"
        f"{context}\n{extra}\n"
        "Stay on this website. Do not open the other sites. Do not submit or sign anything."
    )


def submission_section(brief: str) -> str:
    match = re.search(r"^#+ *(Submission|Abgabe|Angebotsabgabe)\b.*$", brief, re.M | re.I)
    return brief[match.start():] if match else brief


def german(value) -> str:
    return f"{Decimal(str(value)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def bid_text(answer: dict, omitted: list[str]) -> str:
    lines = [f"Tender reference: {answer['tender_reference']}", f"Decision: {answer['decision']}",
             f"Discount: {german(answer['discount_percent'])} %"]
    for p in answer["positions"]:
        lines.append(
            f"- Position {p['position']}: material {p['material_number']}, quantity {p['quantity']} Stück, unit "
            f"price {german(p['unit_price'])} EUR, lead time {p['lead_time_weeks']} weeks, base unit price "
            f"{german(p['base_unit_price'])}, surcharge per unit {german(p['surcharge_per_unit'])}, packaging per "
            f"unit {german(p['packaging_per_unit'])}, rebate total {german(p['rebate_total'])}")
    for p in omitted:
        lines.append(f"- Position {p}: omit it (Position auslassen)")
    lines.append(f"Total net: {german(answer['total_net'])} EUR")
    return "\n".join(lines)


def role_for_gap(gap: Gap, mapping: dict[str, dict]) -> str | None:
    sid = (gap.site or "").lower()
    for role, site in mapping.items():
        if sid in {role, str(site.get("siteId") or "").lower()}:
            return role
    for role, aliases in SITE_ALIASES.items():
        if sid in aliases:
            return role
    return None


async def run_role(role: str, task: dict, cdp: str, workdir: Path, deadline: float,
                   extra: str = "", context: str = "", mode: str = "survey",
                   max_turns: int | None = None, progress_checks: bool = True) -> Evidence | None:
    system = (PROMPTS / "system.md").read_text()
    fields = list(ROLE_FIELDS[role][1:])
    async with Agent(role, {role: cdp}, workdir, facts_schema=schema_for(*fields),
                     extract=True, extract_mode=mode) as agent:
        report = await agent.run(system, role_prompt(role, task, extra, context, mode), deadline,
                                 max_turns=max_turns or MAX_TURNS.get(role, 28), required=fields,
                                 progress_checks=progress_checks)
    print(f"[{role}/{mode}] {report.get('seconds')}s usage {report.get('usage')}", flush=True)
    return coerce_evidence(report.get("facts"), case_from_brief(task["brief"]))


async def retrieve(task: dict, urls: dict[str, str], workdir: Path, deadline: float,
                   fallback: Case) -> list[Evidence]:
    async def one(role: str, extra: str = "", context: str = "", cap_s: float = ROLE_S) -> Evidence | None:
        if role not in urls or time.monotonic() >= deadline - 8:
            print(f"[{role}] skipped", flush=True)
            return None
        try:
            return await run_role(role, task, urls[role], workdir, role_until(deadline, cap_s),
                                  extra, context, mode="survey")
        except Exception as error:  # noqa: BLE001
            print(f"[{role}] failed: {error!r}", flush=True)
            return None

    tender = asyncio.create_task(one("tender"))
    spec = asyncio.create_task(one("spec"))
    mail = asyncio.create_task(one("mail"))
    erp = asyncio.create_task(one(
        "erp", context="Search every catalogue group named in the brief or visible after login. "
                       "A later pass may add groups found on another site."))
    spec_ev, erp_ev, tender_ev, mail_ev = await asyncio.gather(spec, erp, tender, mail)
    parts = [p for p in (tender_ev, spec_ev, erp_ev, mail_ev) if p]

    extra_groups = pending_catalog(spec_ev, erp_ev)
    if extra_groups and time.monotonic() < deadline - 20:
        ctx = "Catalogue groups not yet searched, from another site's records:\n" + json.dumps(
            extra_groups, default=str)
        erp2 = await one("erp", extra="Follow-up: only groups not already recorded. "
                                      "Replace earlier records with the same id.", context=ctx)
        if erp2:
            parts.append(erp2)

    print(f"parts={len(parts)}", flush=True)
    return parts


async def fill_gaps(task: dict, urls: dict[str, str], workdir: Path, evidence: Evidence,
                    gaps: list[Gap], deadline: float, mapping: dict[str, dict]) -> Evidence:
    grouped: dict[str, list[Gap]] = {}
    for gap in gaps:
        role = role_for_gap(gap, mapping)
        if role:
            grouped.setdefault(role, []).append(gap)
    if not grouped or time.monotonic() >= deadline - 12:
        return evidence

    async def one(role: str, role_gaps: list[Gap]) -> Evidence | None:
        if role not in urls or time.monotonic() >= deadline - 12:
            return None
        q = "\n".join(f"- {g.question}" for g in role_gaps)
        until = role_until(deadline, GAP_ROLE_S)
        return await run_role(role, task, urls[role], workdir, until, mode="deep",
                              extra=f"Follow-up only. Open the named records and record_page:\n{q}")

    extras = [p for p in await asyncio.gather(*(one(r, g) for r, g in grouped.items())) if p]
    return merge(evidence.case, evidence, *extras) if extras else evidence


async def recover_current_revision(task: dict, urls: dict[str, str], workdir: Path, evidence: Evidence,
                                   deadline: float) -> Evidence:
    """One spec session until the gap cutoff, only to mark each revision. Separate from the 90s gap rounds."""
    if "spec" not in urls or time.monotonic() >= deadline - 12:
        return evidence
    try:
        extra = await run_role("spec", task, urls["spec"], workdir, deadline, extra=marking_instruction(),
                               mode="marking", max_turns=marking_turns(deadline - time.monotonic()),
                               progress_checks=False)
    except Exception as error:  # noqa: BLE001
        print(f"[spec/marking] failed: {error!r}", flush=True)
        return evidence
    return merge(evidence.case, evidence, extra) if extra else evidence


async def submit(task: dict, urls: dict[str, str], workdir: Path, bid: str, deadline: float) -> dict:
    t, m = creds(task, "tender"), creds(task, "mail")
    prompt = (PROMPTS / "submit.md").read_text()
    for key, value in {
        "TENDER_URL": t["url"], "TENDER_USERNAME": t["username"], "TENDER_PASSWORD": t["password"],
        "MAIL_URL": m["url"], "MAIL_USERNAME": m["username"], "MAIL_PASSWORD": m["password"],
        "BRIEF": submission_section(task["brief"]), "BID": bid,
    }.items():
        prompt = prompt.replace("{" + key + "}", str(value))
    browsers_cdp = {k: urls[k] for k in ("tender", "mail") if k in urls}
    if not browsers_cdp:
        return {}
    system = ("You enter a prepared bid on the tender website. Use the mail browser only for the signing TAN. "
              "When the portal shows a receipt, save_facts with submitted true and finish.")
    submit_schema = {"type": "object", "properties": {
        "submitted": {"type": "boolean"}, "confirmation": {"type": "string"}}, "required": ["submitted"]}
    async with Agent("submit", browsers_cdp, workdir, facts_schema=submit_schema) as agent:
        return await agent.run(system, prompt, deadline, max_turns=MAX_TURNS["submit"],
                               required=["submitted"], progress_checks=False)


async def solve(task: dict, cdp_urls: list[str]) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="lite-run-"))
    started = time.monotonic()
    research_by, gap_by, submit_by = phase_deadlines(task)
    mapping = site_map(task)
    urls = {role: cdp_urls[i] for i, role in enumerate(("tender", "spec", "erp", "mail"))
            if i < len(cdp_urls) and role in mapping}
    fallback = case_from_brief(task["brief"])
    print(f"Workdir {workdir}; research {research_by - started:.0f}s gap {gap_by - started:.0f}s "
          f"submit {submit_by - started:.0f}s; case {fallback.tender_reference} {fallback.scenario_date}",
          flush=True)

    parts = await retrieve(task, urls, workdir, research_by, fallback)
    if not parts:
        print("No evidence", flush=True)
        return
    evidence = merge(parts[0].case, *parts)
    if not evidence.case.authorized_senders and fallback.authorized_senders:
        evidence.case.authorized_senders = fallback.authorized_senders
    resolution = resolve(evidence)
    for _ in range(GAP_ROUNDS):
        pending = [gap for gap in resolution.gaps if not is_marking_gap(gap)]
        if not pending or time.monotonic() >= gap_by - 12:
            break
        print(f"{len(pending)} gaps", flush=True)
        evidence = await fill_gaps(task, urls, workdir, evidence, pending, gap_by, mapping)
        resolution = resolve(evidence)
    if any(is_marking_gap(gap) for gap in resolution.gaps):
        print("marking gap: spec until gap cutoff", flush=True)
        evidence = await recover_current_revision(task, urls, workdir, evidence, gap_by)
        resolution = resolve(evidence)

    print("Trace:\n  " + "\n  ".join(resolution.trace), flush=True)
    print("Warnings: " + ("; ".join(resolution.warnings) or "none"), flush=True)
    answer = answer_or_no_bid(resolution.answer, resolution.gaps, evidence.case.tender_reference)
    if answer is None:
        print("No answer; gaps: " + "; ".join(g.question for g in resolution.gaps), flush=True)
        return
    if resolution.answer is None:
        print(f"No governing revision; submitting no_bid for {answer['tender_reference']}", flush=True)
    bid = bid_text(answer, resolution.omitted if resolution.answer is not None else [])
    print(f"Bid at {time.monotonic() - started:.0f}s:\n{bid}", flush=True)
    report = await submit(task, urls, workdir, bid, submit_by)
    print(f"Submit at {time.monotonic() - started:.0f}s facts={report.get('facts')} "
          f"usage={report.get('usage')}", flush=True)


async def main() -> int:
    task, _ = contract.load_run()
    print(f"Run {task.get('run_id') or '(local)'}; " +
          ", ".join(f"{s.get('siteId')} {s.get('url')}" for s in task["access"]), flush=True)
    async with async_playwright() as playwright:
        urls, local = await browsers.cdp_endpoints(playwright, BROWSERS)
        try:
            await solve(task, urls)
        finally:
            for browser in local:
                await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
