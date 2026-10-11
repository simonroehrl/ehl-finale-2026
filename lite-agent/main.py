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
from marking_pass import is_marking_gap, marking_instruction, marking_turns, portal_answer
from resolver import ATTRIBUTES, Gap, ref, senders_from_brief, resolve

HERE = Path(__file__).resolve().parent
PROMPTS = HERE / "prompts"
BROWSERS = 4
SUBMIT_S = 180
SUBMIT_MIN_S = 150
SAFETY_S = 45
GAP_S = 120
MAX_RESEARCH_ROUNDS = 8
ROLE_S = 240
GAP_ROLE_S = 120
MARKING_S = 90
MAX_TURNS = {"tender": 32, "spec": 36, "erp": 40, "mail": 32, "submit": 70}

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
ROLE_GOAL = {
    "tender": "This site holds the bill of quantities and bidder Q&A. Find every position (number, quantity, "
              "stated unit, linked requirement) and every Q&A answer on every page, including withdrawn ones. "
              "A list of tenders is not the bill.",
    "spec": "This site holds specification revisions. Find each revision's document body: id, the marking "
            "words printed on that document, and the requirements with attributes and catalogue group. "
            "A search hit or register row is not the document. Do not infer current from the date.",
    "erp": "This site holds the material catalogue. Run one search per catalogue group (brief, login screen "
           "or context), then open every listed candidate: status, attributes, prices, stock, lead times. "
           "A search list alone is not complete; narrow a search that returns nothing useful.",
    "mail": "This site holds the sales mailbox. Find every message for this tender reference or a look-alike, "
            "inbox and archive: sender, date, subject, kind and that kind's fields. Search the reference plus "
            "one topic; open the message, not only the folder list.",
}
ROLE_FOCUS = {
    "tender": "every bill-of-quantities position (number, quantity, unit_raw, unit_kind piece|set|other, "
              "requirement_id) and every Q&A answer on every page (number, ISO date, text, withdraws, "
              "quantity_changes, amendments, assignments)",
    "spec": "every revision (id, ISO date, marking as the document writes it) and its requirements (id, "
            "bore_mm, stroke_mm, pressure_bar, mounting, certification, catalog_group, usable_stroke, "
            "pieces_per_set)",
    "erp": "every catalogue group named in the brief, visible after login, or listed in the context: an "
           "erp_searches row, then every listed candidate as materials (number, status active|blocked|other, "
           "attributes, price_conditions with dates and scales, stock, lead times, carton, masses, metal_index). "
           "complete is true only after every listed material is in save_facts with bore, stroke, pressure, "
           "mounting and certification",
    "mail": "inbox and archive, every page: every message for this reference or a look-alike (id, sender, ISO "
            "date, subject, reference, kind, and the structured fields for that kind). kind must be one of the "
            "schema kinds; record every kind you see, including ones that will not apply",
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


def next_action(resolution, now: float, research_until: float) -> str:
    """What to do next: marking, gaps, submit a resolver answer, or stop without a portal entry."""
    answer = resolution.answer if resolution else None
    gaps = resolution.gaps if resolution else []
    if answer and answer.get("decision") == "no_bid":
        return "submit"
    if now >= research_until:
        return "submit" if answer else "stop"
    if any(is_marking_gap(gap) for gap in gaps):
        return "marking"
    if gaps:
        return "gaps"
    if answer:
        return "submit"
    return "stop"


def describe_resolution(resolution) -> str:
    answer = resolution.answer if resolution else None
    decision = answer.get("decision") if answer else "none"
    npos = len(answer.get("positions") or []) if answer else 0
    ngap = len(resolution.gaps) if resolution else 0
    omitted = ",".join(resolution.omitted) if resolution and resolution.omitted else "-"
    lines = [f"resolve: decision={decision} priced={npos} omitted={omitted} gaps={ngap}"]
    if resolution:
        for gap in resolution.gaps:
            where = f"{gap.site}:{gap.position}" if gap.position else gap.site
            lines.append(f"  gap [{where}] {gap.question}")
    return "\n".join(lines)


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


def coerce_evidence(facts: dict | None, fallback: Case) -> Evidence:
    """Keep valid records; drop broken ones into notes. Always returns Evidence so gap-fill can continue."""
    notes: list[str] = []
    if isinstance(facts, dict) and facts.get("notes"):
        raw_notes = facts["notes"] if isinstance(facts["notes"], list) else [facts["notes"]]
        notes = [n if isinstance(n, str) else json.dumps(n, default=str) for n in raw_notes]
    try:
        case = Case.model_validate(facts["case"]) if isinstance(facts, dict) and facts.get("case") else fallback
    except ValidationError:
        case = fallback
        notes.append("case from brief: worker case failed validation")
    data: dict = {"case": case, "notes": notes}
    raw_facts = facts if isinstance(facts, dict) else {}
    for name, model in LIST_MODELS.items():
        raw = raw_facts.get(name) or []
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
            if name == "revisions":
                item = {**item}
                mark = item.get("marking")
                if not mark:
                    item["marking"] = "unknown"
                elif isinstance(mark, str) and mark not in ("current", "superseded", "unknown"):
                    item.setdefault("marking_raw", mark)
                reqs = item.get("requirements") or []
                if isinstance(reqs, list):
                    kept_reqs = []
                    for req in reqs:
                        if not isinstance(req, dict) or not req.get("id"):
                            notes.append("dropped requirement: missing id")
                            continue
                        try:
                            kept_reqs.append(Requirement.model_validate(req).model_dump(mode="json"))
                        except ValidationError:
                            slim = {key: req[key] for key in
                                    ("id", "catalog_group", "bore_mm", "stroke_mm", "pressure_bar",
                                     "mounting", "certification", "usable_stroke", "pieces_per_set")
                                    if key in req}
                            try:
                                kept_reqs.append(Requirement.model_validate(slim).model_dump(mode="json"))
                            except ValidationError as error:
                                notes.append(f"dropped requirement {req.get('id')}: {error.error_count()} issues")
                    item["requirements"] = kept_reqs
            if name == "messages" and "kind" not in item:
                item = {**item, "kind": "other"}
            try:
                kept.append(model.model_validate(item))
            except ValidationError as error:
                notes.append(f"dropped {name}: {error.error_count()} schema issues")
        data[name] = kept
    data["notes"] = notes
    try:
        return sanitize_erp_complete(Evidence.model_validate(data))
    except ValidationError as error:
        notes.append(f"evidence validate: {error.error_count()} issues")
        return Evidence(case=case, notes=notes)


def sanitize_erp_complete(evidence: Evidence) -> Evidence:
    """A search is not complete if listed materials are missing or have no full attributes.

    Otherwise an empty extract with complete=true becomes a false no_bid.
    """
    searches = []
    notes = list(evidence.notes)
    changed = False
    for row in evidence.erp_searches:
        if not row.complete:
            searches.append(row)
            continue
        group = ref(row.catalog_group)
        recorded = [m for m in evidence.materials
                    if not m.catalog_group or ref(m.catalog_group) == group]
        recorded_ids = {ref(m.number) for m in recorded}
        listed = {ref(n) for n in row.material_numbers}
        full = [m for m in recorded
                if all(getattr(m.attributes, name) is not None for name in ATTRIBUTES)]
        if (listed and not listed.issubset(recorded_ids)) or not full:
            searches.append(row.model_copy(update={"complete": False}))
            notes.append(f"erp search {row.catalog_group}: complete ignored "
                         f"({len(full)} full materials, {len(listed - recorded_ids)} listed missing)")
            changed = True
        else:
            searches.append(row)
    if not changed:
        return evidence
    return evidence.model_copy(update={"erp_searches": searches, "notes": notes})


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
    """Only groups whose search is still trusted as complete."""
    if erp is None:
        return set()
    return {row.catalog_group for row in erp.erp_searches if row.catalog_group and row.complete}


def pending_catalog(spec: Evidence | None, erp: Evidence | None) -> list[dict]:
    seen = searched_groups(erp)
    return [row for row in catalog_rows(spec) if row["catalog_group"] not in seen]


def brief_priors(case: Case) -> str:
    """Facts the brief states for every run. No website menus or copied portal labels."""
    lines = [
        f"From the brief, not from this website: tender reference `{case.tender_reference}`, "
        f"scenario date {case.scenario_date}, bidder mail domain `{case.bidder_mail_domain}`.",
        "Use these to recognise records. Labels and menus on the site will differ; follow what you see.",
    ]
    senders = case.authorized_senders or {}
    if senders:
        parts = [f"{kind} ← {local}" for kind, local in senders.items()]
        lines.append("Authorized mailbox local-parts (plus that domain): " + ", ".join(parts) + ".")
    return "\n".join(lines)


def role_prompt(role: str, task: dict, extra: str = "", context: str = "", mode: str = "survey") -> str:
    s = creds(task, role)
    priors = brief_priors(case_from_brief(task["brief"]))
    return (
        f"Website `{s['siteId']}`: {s['url']} · username `{s['username']}` · password `{s['password']}`\n\n"
        f"{priors}\n\n"
        f"Goal: {ROLE_GOAL[role]}\n\n"
        f"<brief>\n{task['brief']}\n</brief>\n\n"
        f"Record {ROLE_FOCUS[role]}. Include records that will not apply.\n"
        f"Top-level JSON keys: {', '.join(ROLE_FIELDS[role])}, notes. save_facts after each real finding.\n"
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


def follow_up_prompt(questions: str) -> str:
    return (
        "Same browser session. You are already on this website. Do not start over. "
        "Log in again only if the session expired.\n"
        f"Follow-up only. Open the named records and save_facts:\n{questions}"
    )


class SiteWorker:
    """One Playwright session per website. Survey and later follow-ups reuse the same browser and facts."""

    def __init__(self, role: str, task: dict, cdp: str, workdir: Path, fallback: Case):
        self.role, self.task, self.cdp, self.workdir, self.fallback = role, task, cdp, workdir, fallback
        self.agent: Agent | None = None

    async def __aenter__(self):
        fields = list(ROLE_FIELDS[self.role][1:])
        self.agent = Agent(self.role, {self.role: self.cdp}, self.workdir,
                           facts_schema=schema_for(*fields), extract=True)
        await self.agent.__aenter__()
        return self

    async def __aexit__(self, *exc):
        if self.agent is not None:
            await self.agent.__aexit__(*exc)
            self.agent = None

    def live(self) -> Evidence:
        return coerce_evidence(self.agent.facts if self.agent else None, self.fallback)

    async def work(self, deadline: float, extra: str = "", context: str = "", mode: str = "survey",
                   resume: bool = False, cap_s: float | None = None, max_turns: int | None = None,
                   progress_checks: bool = True) -> Evidence:
        if self.agent is None or time.monotonic() >= deadline - 8:
            print(f"[{self.role}] skipped", flush=True)
            return self.live()
        try:
            until = role_until(deadline, ROLE_S if cap_s is None else cap_s)
            system = (PROMPTS / "system.md").read_text()
            if resume:
                prompt = extra if extra.startswith("Same browser") else follow_up_prompt(extra)
                if context:
                    prompt = f"{prompt}\n{context}"
            else:
                prompt = role_prompt(self.role, self.task, extra, context, mode)
            report = await self.agent.run(
                system, prompt, until, max_turns=max_turns or MAX_TURNS.get(self.role, 28),
                required=list(ROLE_FIELDS[self.role][1:]), progress_checks=progress_checks,
                resume=resume, extract_mode=mode)
            print(f"[{self.role}/{mode}] {report.get('seconds')}s usage {report.get('usage')}", flush=True)
            return coerce_evidence(self.agent.facts or report.get("facts"), self.fallback)
        except Exception as error:  # noqa: BLE001
            print(f"[{self.role}] failed: {error!r}", flush=True)
            return self.live()


async def feed_erp_catalog(workers: dict[str, SiteWorker], deadline: float) -> None:
    """As soon as the spec session has catalogue groups the ERP has not searched, continue that ERP session."""
    spec, erp = workers.get("spec"), workers.get("erp")
    if spec is None or erp is None or time.monotonic() >= deadline - 20:
        return
    extra = pending_catalog(spec.live(), erp.live())
    if not extra:
        return
    print(f"[erp] streaming {len(extra)} catalog groups", flush=True)
    ctx = "Catalogue groups not yet searched, from another site's records:\n" + json.dumps(extra, default=str)
    await erp.work(deadline, extra="Follow-up: only groups not already recorded. "
                                   "Replace earlier records with the same id.",
                   context=ctx, mode="deep", resume=True, cap_s=GAP_ROLE_S)


async def retrieve(workers: dict[str, SiteWorker], deadline: float) -> list[Evidence]:
    surveys: dict[str, asyncio.Task] = {}
    for role, context in (
        ("tender", ""),
        ("spec", ""),
        ("mail", ""),
        ("erp", "Search every catalogue group named in the brief or visible after login. "
                "A later pass may add groups found on another site."),
    ):
        if role in workers:
            surveys[role] = asyncio.create_task(workers[role].work(deadline, context=context, mode="survey"))

    spec_t, erp_t = surveys.get("spec"), surveys.get("erp")
    while time.monotonic() < deadline - 20:
        erp_done = erp_t is None or erp_t.done()
        spec_has = bool(catalog_rows(workers["spec"].live()) if "spec" in workers else [])
        spec_done = spec_t is None or spec_t.done()
        if erp_done and (spec_has or spec_done):
            break
        await asyncio.sleep(4)

    if erp_t is not None and not erp_t.done():
        await erp_t
    await feed_erp_catalog(workers, deadline)
    if surveys:
        await asyncio.gather(*surveys.values(), return_exceptions=True)
    await feed_erp_catalog(workers, deadline)

    parts = [worker.live() for worker in workers.values()]
    for worker, part in zip(workers.values(), parts):
        facts = worker.agent.facts if worker.agent else None
        print(f"[{worker.role}] rows pos={len(part.positions)} qa={len(part.qa)} rev={len(part.revisions)} "
              f"mat={len(part.materials)} erp={len(part.erp_searches)} mail={len(part.messages)} "
              f"raw={'none' if not facts else list(facts)}", flush=True)
    print(f"parts={sum(1 for p in parts if p.positions or p.qa or p.revisions or p.materials or p.messages)}",
          flush=True)
    return parts


def needs_mail_harvest(evidence: Evidence) -> bool:
    """Mail gaps do not exist until a governing revision is priced. Harvest anyway."""
    return not evidence.messages


async def harvest_background(workers: dict[str, SiteWorker], evidence: Evidence, deadline: float,
                             skip_mail: bool = False, skip_erp: bool = False) -> None:
    """While spec hunts a marking, keep filling mail and ERP from whatever is already on disk."""
    jobs = []
    if not skip_erp:
        jobs.append(feed_erp_catalog(workers, deadline))
    mail = workers.get("mail")
    if (mail is not None and not skip_mail and needs_mail_harvest(evidence)
            and time.monotonic() < deadline - 15):
        print("[mail] harvest while other gaps are open", flush=True)
        jobs.append(mail.work(
            deadline,
            extra=follow_up_prompt(
                "- Record every inbox and archive message for this tender reference or a look-alike. "
                "Open each message and save_facts. Do not start over if you are already in the inbox."
            ),
            mode="deep", resume=True, cap_s=min(GAP_ROLE_S, 90.0), progress_checks=False))
    if jobs:
        await asyncio.gather(*jobs, return_exceptions=True)


async def fill_gaps(workers: dict[str, SiteWorker], evidence: Evidence, gaps: list[Gap],
                    deadline: float, mapping: dict[str, dict]) -> Evidence:
    grouped: dict[str, list[Gap]] = {}
    for gap in gaps:
        role = role_for_gap(gap, mapping)
        if role:
            grouped.setdefault(role, []).append(gap)
    if not grouped or time.monotonic() >= deadline - 12:
        return evidence

    async def one(role: str, role_gaps: list[Gap]) -> Evidence | None:
        if role not in workers or time.monotonic() >= deadline - 12:
            return None
        questions = "\n".join(f"- {gap.question}" for gap in role_gaps)
        return await workers[role].work(deadline, extra=follow_up_prompt(questions), mode="deep",
                                        resume=True, cap_s=GAP_ROLE_S, progress_checks=False)

    extras = [part for part in await asyncio.gather(*(one(role, items) for role, items in grouped.items())) if part]
    return merge(evidence.case, evidence, *extras) if extras else evidence


async def close_evidence(workers: dict[str, SiteWorker], evidence: Evidence, mapping: dict[str, dict],
                         research_until: float) -> tuple[Evidence, object]:
    """Keep filling resolver gaps until a priced bid, a true no_bid, or the research cutoff."""
    evidence = sanitize_erp_complete(evidence)
    resolution = resolve(evidence)
    print(describe_resolution(resolution), flush=True)
    last_sig, idle = None, 0
    for round_ in range(MAX_RESEARCH_ROUNDS):
        action = next_action(resolution, time.monotonic(), research_until)
        if action in ("submit", "stop"):
            print(f"research {action} after {round_} gap rounds", flush=True)
            return evidence, resolution
        sig = tuple(sorted((gap.site, gap.position or "", gap.question) for gap in resolution.gaps))
        if sig == last_sig:
            idle += 1
            marking_only = bool(resolution.gaps) and all(is_marking_gap(g) for g in resolution.gaps)
            if idle >= 2 and not marking_only:
                print("research: no gap progress", flush=True)
                return evidence, resolution
        else:
            idle = 0
        last_sig = sig
        pending = [gap for gap in resolution.gaps if not is_marking_gap(gap)]
        skip_mail = any(role_for_gap(gap, mapping) == "mail" for gap in pending)
        skip_erp = any(role_for_gap(gap, mapping) == "erp" for gap in pending)
        jobs: list = [harvest_background(workers, evidence, research_until,
                                         skip_mail=skip_mail, skip_erp=skip_erp)]
        if action == "marking" and "spec" in workers:
            left = min(MARKING_S, max(40.0, research_until - time.monotonic()))
            print(f"marking gap: continue spec session {left:.0f}s", flush=True)

            async def mark():
                return await workers["spec"].work(
                    research_until, extra=marking_instruction(), mode="marking", resume=True,
                    cap_s=left, max_turns=marking_turns(left), progress_checks=False)

            jobs.append(mark())
        if pending:
            print(f"{len(pending)} priced gaps", flush=True)
            jobs.append(fill_gaps(workers, evidence, pending, research_until, mapping))
        results = await asyncio.gather(*jobs, return_exceptions=True)
        for result in results:
            if isinstance(result, Evidence):
                evidence = merge(evidence.case, evidence, result)
            elif isinstance(result, Exception):
                print(f"research task failed: {result!r}", flush=True)
        lives = [worker.live() for worker in workers.values()]
        if lives:
            evidence = sanitize_erp_complete(merge(evidence.case, evidence, *lives))
        resolution = resolve(evidence)
        print(describe_resolution(resolution), flush=True)
    return evidence, resolution


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
    last = {}
    for attempt in (1, 2):
        if time.monotonic() >= deadline - 20:
            break
        async with Agent(f"submit-{attempt}", browsers_cdp, workdir, facts_schema=submit_schema) as agent:
            last = await agent.run(system, prompt, deadline, max_turns=MAX_TURNS["submit"],
                                   required=["submitted"], progress_checks=False)
        facts = last.get("facts") if isinstance(last.get("facts"), dict) else {}
        if facts.get("submitted") is True:
            return last
        print(f"[submit] attempt {attempt} not confirmed; retry" if attempt == 1 else
              f"[submit] attempt {attempt} still not confirmed", flush=True)
    return last


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

    evidence, resolution = None, None
    async with AsyncExitStack() as stack:
        workers: dict[str, SiteWorker] = {}
        for role, cdp in urls.items():
            try:
                workers[role] = await stack.enter_async_context(
                    SiteWorker(role, task, cdp, workdir, fallback))
            except Exception as error:  # noqa: BLE001
                print(f"[{role}] start failed: {error!r}", flush=True)
        parts = await retrieve(workers, research_by)
        if not parts:
            print("No site lists; continuing from the brief case", flush=True)
            evidence = Evidence(case=fallback)
        else:
            evidence = merge(parts[0].case, *parts)
        if not evidence.case.authorized_senders and fallback.authorized_senders:
            evidence.case.authorized_senders = fallback.authorized_senders
        research_until = max(gap_by, submit_by - SUBMIT_MIN_S)
        evidence, resolution = await close_evidence(workers, evidence, mapping, research_until)

    print("Trace:\n  " + "\n  ".join(resolution.trace if resolution else []), flush=True)
    print("Warnings: " + ("; ".join(resolution.warnings) if resolution else "none"), flush=True)
    answer = portal_answer(resolution.answer if resolution else None)
    if answer is None:
        print("No priced answer — not submitting no_bid. Gaps: " +
              "; ".join(g.question for g in (resolution.gaps if resolution else [])), flush=True)
        return
    if answer.get("decision") == "no_bid":
        print("Resolver decided no_bid (no matching material)", flush=True)
    bid = bid_text(answer, resolution.omitted if resolution and resolution.answer is not None else [])
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
