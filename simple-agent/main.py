"""Qwen opens pages and writes Evidence. resolver.py + pricing.py decide."""
from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
from contextlib import AsyncExitStack
from decimal import Decimal
from pathlib import Path

from playwright.async_api import async_playwright
from pydantic import ValidationError

import browsers
import run as contract
from evidence import (Case, Evidence, ErpSearch, Material, Message, QAAnswer, Requirement, SpecRevision,
                      TenderPosition, merge, schema_for)
from llm_agent import Agent
from resolver import ATTRIBUTES, Gap, ref, senders_from_brief, resolve

HERE = Path(__file__).resolve().parent
PROMPTS = HERE / "prompts"
BROWSERS = 4
SUBMIT_S = 240
SAFETY_S = 40
ROLE_S = 420
GAP_ROUNDS = 2
MAX_TURNS = {"tender": 28, "spec": 32, "erp": 36, "mail": 28, "submit": 50}

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
    "tender": "bill-of-quantities positions and every Q&A answer on every page",
    "spec": "every specification revision document and its marking words plus requirements",
    "erp": "every catalogue group: search, then open every listed material",
    "mail": "inbox and archive: every message for this reference or a look-alike",
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


def case_from_brief(brief: str) -> Case:
    when = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", brief)
    found = re.search(r"\b([A-Z]{1,6}-\d{4}-\d+)\b", brief) or re.search(r"\(([A-Z0-9][-A-Z0-9/]{4,})\)", brief)
    domain = re.search(r"@([a-z0-9.-]+\.[a-z]{2,})", brief.lower())
    return Case(tender_reference=(found.group(1) if found else "UNKNOWN"),
                scenario_date=when.group(0) if when else "1970-01-01T00:00:00Z",
                bidder_mail_domain=domain.group(1) if domain else "example.invalid",
                authorized_senders=senders_from_brief(brief))


def coerce_evidence(facts: dict | None, fallback: Case) -> Evidence:
    notes: list[str] = []
    raw = facts if isinstance(facts, dict) else {}
    if raw.get("notes"):
        raw_notes = raw["notes"] if isinstance(raw["notes"], list) else [raw["notes"]]
        notes = [n if isinstance(n, str) else json.dumps(n, default=str) for n in raw_notes]
    try:
        case = Case.model_validate(raw["case"]) if raw.get("case") else fallback
    except ValidationError:
        case = fallback
    data: dict = {"case": case, "notes": notes}
    for name, model in LIST_MODELS.items():
        rows = raw.get(name) or []
        if not isinstance(rows, list):
            rows = [rows] if rows not in ("", {}) else []
        kept = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            if name == "materials" and "attributes" not in item:
                item = {**item, "attributes": {}}
            if name == "revisions":
                item = {**item}
                mark = item.get("marking")
                if not mark:
                    item["marking"] = "unknown"
                elif isinstance(mark, str) and mark not in ("current", "superseded", "unknown"):
                    item.setdefault("marking_raw", mark)
                reqs = []
                for req in item.get("requirements") or []:
                    if not isinstance(req, dict) or not req.get("id"):
                        continue
                    try:
                        reqs.append(Requirement.model_validate(req).model_dump(mode="json"))
                    except ValidationError:
                        slim = {k: req[k] for k in
                                ("id", "catalog_group", "bore_mm", "stroke_mm", "pressure_bar",
                                 "mounting", "certification", "usable_stroke", "pieces_per_set") if k in req}
                        try:
                            reqs.append(Requirement.model_validate(slim).model_dump(mode="json"))
                        except ValidationError:
                            pass
                item["requirements"] = reqs
            if name == "messages" and "kind" not in item:
                item = {**item, "kind": "other"}
            try:
                kept.append(model.model_validate(item))
            except ValidationError:
                pass
        data[name] = kept
    try:
        return sanitize_erp(Evidence.model_validate(data))
    except ValidationError:
        return Evidence(case=case, notes=notes)


def sanitize_erp(evidence: Evidence) -> Evidence:
    searches, notes, changed = [], list(evidence.notes), False
    for row in evidence.erp_searches:
        if not row.complete:
            searches.append(row)
            continue
        group = ref(row.catalog_group)
        recorded = [m for m in evidence.materials if not m.catalog_group or ref(m.catalog_group) == group]
        listed = {ref(n) for n in row.material_numbers}
        full = [m for m in recorded if all(getattr(m.attributes, n) is not None for n in ATTRIBUTES)]
        if (listed and not listed.issubset({ref(m.number) for m in recorded})) or not full:
            searches.append(row.model_copy(update={"complete": False}))
            notes.append(f"erp {row.catalog_group}: complete ignored")
            changed = True
        else:
            searches.append(row)
    return evidence.model_copy(update={"erp_searches": searches, "notes": notes}) if changed else evidence


def role_prompt(role: str, task: dict, extra: str = "") -> str:
    s = creds(task, role)
    return (
        f"Website `{s['siteId']}`: {s['url']} · username `{s['username']}` · password `{s['password']}`\n\n"
        f"<brief>\n{task['brief']}\n</brief>\n\n"
        f"Open pages that hold {ROLE_FOCUS[role]}. Log in, then open every list and every document. "
        f"Call save_facts after each record page.\n"
        f"{extra}\nStay on this website. Do not submit or sign."
    )


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


def submission_section(brief: str) -> str:
    match = re.search(r"^#+ *(Submission|Abgabe|Angebotsabgabe)\b.*$", brief, re.M | re.I)
    return brief[match.start():] if match else brief


class SiteWorker:
    def __init__(self, role: str, task: dict, cdp: str, workdir: Path, fallback: Case):
        self.role, self.task, self.cdp, self.workdir, self.fallback = role, task, cdp, workdir, fallback
        self.agent: Agent | None = None

    async def __aenter__(self):
        fields = list(ROLE_FIELDS[self.role][1:])
        self.agent = Agent(self.role, {self.role: self.cdp}, self.workdir,
                           facts_schema=schema_for(*fields))
        await self.agent.__aenter__()
        return self

    async def __aexit__(self, *exc):
        if self.agent is not None:
            await self.agent.__aexit__(*exc)

    def live(self) -> Evidence:
        return coerce_evidence(self.agent.facts if self.agent else None, self.fallback)

    async def work(self, deadline: float, extra: str = "", resume: bool = False) -> Evidence:
        if self.agent is None or time.monotonic() >= deadline - 8:
            return self.live()
        prompt = extra if resume else role_prompt(self.role, self.task, extra)
        try:
            until = min(deadline, time.monotonic() + ROLE_S)
            report = await self.agent.run(
                (PROMPTS / "system.md").read_text(), prompt, until,
                max_turns=MAX_TURNS.get(self.role, 28),
                required=list(ROLE_FIELDS[self.role][1:]), resume=resume)
            print(f"[{self.role}] {report.get('seconds')}s {report.get('usage')}", flush=True)
        except Exception as error:  # noqa: BLE001
            print(f"[{self.role}] failed: {error!r}", flush=True)
        return self.live()


def describe(resolution) -> str:
    answer = resolution.answer if resolution else None
    decision = answer.get("decision") if answer else "none"
    npos = len(answer.get("positions") or []) if answer else 0
    lines = [f"resolve: decision={decision} priced={npos} gaps={len(resolution.gaps) if resolution else 0}"]
    if resolution:
        for gap in resolution.gaps:
            lines.append(f"  gap [{gap.site}] {gap.question}")
    return "\n".join(lines)


async def retrieve(workers: dict[str, SiteWorker], deadline: float) -> list[Evidence]:
    await asyncio.gather(*(w.work(deadline) for w in workers.values()), return_exceptions=True)
    parts = [w.live() for w in workers.values()]
    for worker, part in zip(workers.values(), parts):
        print(f"[{worker.role}] pos={len(part.positions)} qa={len(part.qa)} rev={len(part.revisions)} "
              f"mat={len(part.materials)} mail={len(part.messages)}", flush=True)
    return parts


async def fill_gaps(workers: dict[str, SiteWorker], evidence: Evidence, gaps: list[Gap],
                    deadline: float, mapping: dict[str, dict]) -> Evidence:
    grouped: dict[str, list[Gap]] = {}
    for gap in gaps:
        role = role_for_gap(gap, mapping)
        if role:
            grouped.setdefault(role, []).append(gap)
    if not grouped:
        return evidence

    async def one(role: str, role_gaps: list[Gap]) -> Evidence | None:
        if role not in workers or time.monotonic() >= deadline - 12:
            return None
        questions = "\n".join(f"- {g.question}" for g in role_gaps)
        extra = ("Same session. Do not start over. Open only these records and save_facts them:\n" + questions)
        return await workers[role].work(deadline, extra=extra, resume=True)

    extras = [p for p in await asyncio.gather(*(one(r, g) for r, g in grouped.items())) if p]
    return merge(evidence.case, evidence, *extras) if extras else evidence


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
    schema = {"type": "object", "properties": {
        "submitted": {"type": "boolean"}, "confirmation": {"type": "string"}}, "required": ["submitted"]}
    async with Agent("submit", browsers_cdp, workdir, facts_schema=schema) as agent:
        return await agent.run(
            "Enter the prepared bid on the tender site. Mail browser only for the TAN. "
            "When a receipt is visible, save_facts submitted true and finish.",
            prompt, deadline, max_turns=MAX_TURNS["submit"], required=["submitted"])


async def solve(task: dict, cdp_urls: list[str]) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="simple-run-"))
    started = time.monotonic()
    wall = min(contract.seconds_left(task), 20 * 60)
    submit_by = time.monotonic() + max(90.0, wall - SAFETY_S)
    research_by = submit_by - SUBMIT_S
    mapping = site_map(task)
    urls = {role: cdp_urls[i] for i, role in enumerate(("tender", "spec", "erp", "mail"))
            if i < len(cdp_urls) and role in mapping}
    fallback = case_from_brief(task["brief"])
    print(f"Workdir {workdir}; research {research_by - started:.0f}s submit {submit_by - started:.0f}s; "
          f"case {fallback.tender_reference}", flush=True)

    resolution = None
    async with AsyncExitStack() as stack:
        workers: dict[str, SiteWorker] = {}
        for role, cdp in urls.items():
            try:
                workers[role] = await stack.enter_async_context(
                    SiteWorker(role, task, cdp, workdir, fallback))
            except Exception as error:  # noqa: BLE001
                print(f"[{role}] start failed: {error!r}", flush=True)
        parts = await retrieve(workers, research_by)
        evidence = merge(parts[0].case, *parts) if parts else Evidence(case=fallback)
        if not evidence.case.authorized_senders and fallback.authorized_senders:
            evidence.case.authorized_senders = fallback.authorized_senders
        evidence = sanitize_erp(evidence)
        resolution = resolve(evidence)
        print(describe(resolution), flush=True)
        for _ in range(GAP_ROUNDS):
            if resolution.answer or not resolution.gaps or time.monotonic() >= research_by - 15:
                break
            print(f"{len(resolution.gaps)} gaps", flush=True)
            evidence = sanitize_erp(await fill_gaps(workers, evidence, resolution.gaps, research_by, mapping))
            resolution = resolve(evidence)
            print(describe(resolution), flush=True)

    print("Trace:\n  " + "\n  ".join(resolution.trace if resolution else []), flush=True)
    answer = resolution.answer if resolution else None
    if answer is None:
        print("No priced answer — not submitting. Gaps: " +
              "; ".join(g.question for g in (resolution.gaps if resolution else [])), flush=True)
        return
    bid = bid_text(answer, resolution.omitted)
    print(f"Bid at {time.monotonic() - started:.0f}s:\n{bid}", flush=True)
    report = await submit(task, urls, workdir, bid, submit_by)
    print(f"Submit facts={report.get('facts')} usage={report.get('usage')}", flush=True)


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
