"""Shared evidence. Site workers write records; the resolver reads one ledger."""
from __future__ import annotations

import asyncio
import json
import re

from pydantic import ValidationError

from evidence import (Case, Evidence, ErpSearch, Material, Message, Requirement, SpecRevision,
                      TenderPosition, QAAnswer, facts_index, merge)
from resolver import ATTRIBUTES, senders_from_brief, ref

LIST_MODELS = {"positions": TenderPosition, "qa": QAAnswer, "revisions": SpecRevision,
               "materials": Material, "erp_searches": ErpSearch, "messages": Message}


def case_from_brief(brief: str) -> Case:
    """Formats the platform always states in the run brief, not website values."""
    when = re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", brief)
    found = re.search(r"\b([A-Z]{1,6}-\d{4}-\d+)\b", brief) or re.search(r"\(([A-Z0-9][-A-Z0-9/]{4,})\)", brief)
    domain = re.search(r"@([a-z0-9.-]+\.[a-z]{2,})", brief.lower())
    return Case(tender_reference=(found.group(1) if found else "UNKNOWN"),
                scenario_date=when.group(0) if when else "1970-01-01T00:00:00Z",
                bidder_mail_domain=domain.group(1) if domain else "example.invalid",
                authorized_senders=senders_from_brief(brief))


def coerce(facts: dict | None, fallback: Case) -> Evidence:
    notes: list[str] = []
    if isinstance(facts, dict) and facts.get("notes"):
        raw = facts["notes"] if isinstance(facts["notes"], list) else [facts["notes"]]
        notes = [n if isinstance(n, str) else json.dumps(n, default=str) for n in raw]
    try:
        case = Case.model_validate(facts["case"]) if isinstance(facts, dict) and facts.get("case") else fallback
    except ValidationError:
        case = fallback
        notes.append("case failed validation")
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
                            except ValidationError:
                                notes.append(f"dropped requirement {req.get('id')}")
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
    except ValidationError:
        return Evidence(case=case, notes=notes)


def sanitize_erp_complete(evidence: Evidence) -> Evidence:
    searches, notes, changed = [], list(evidence.notes), False
    for row in evidence.erp_searches:
        if not row.complete:
            searches.append(row)
            continue
        group = ref(row.catalog_group)
        recorded = [m for m in evidence.materials if not m.catalog_group or ref(m.catalog_group) == group]
        recorded_ids = {ref(m.number) for m in recorded}
        listed = {ref(n) for n in row.material_numbers}
        full = [m for m in recorded if all(getattr(m.attributes, name) is not None for name in ATTRIBUTES)]
        if (listed and not listed.issubset(recorded_ids)) or not full:
            searches.append(row.model_copy(update={"complete": False}))
            notes.append(f"erp search {row.catalog_group}: complete ignored")
            changed = True
        else:
            searches.append(row)
    return evidence if not changed else evidence.model_copy(update={"erp_searches": searches, "notes": notes})


def catalog_groups(evidence: Evidence) -> list[str]:
    found = []
    for revision in evidence.revisions:
        for requirement in revision.requirements:
            if requirement.catalog_group and requirement.catalog_group not in found:
                found.append(requirement.catalog_group)
    return found


def tan_from(evidence: Evidence) -> str | None:
    for message in evidence.messages:
        if message.kind != "signing_tan":
            continue
        for note in evidence.notes:
            if note.lower().startswith("tan "):
                return note.split(" ", 1)[1].strip()
        extra = getattr(message, "notes", None)
        if extra:
            text = extra if isinstance(extra, str) else str(extra)
            if text.lower().startswith("tan "):
                return text.split(" ", 1)[1].strip()
    for note in evidence.notes:
        if note.lower().startswith("tan "):
            return note.split(" ", 1)[1].strip()
    return None


class Store:
    def __init__(self, case: Case):
        self.case = case
        self.evidence = Evidence(case=case)
        self.listed: dict[str, list[str]] = {}
        self.opened: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    def note_listed(self, role: str, ids: list[str]) -> None:
        have = self.listed.setdefault(role, [])
        for ident in ids:
            if ident and ident != self.case.tender_reference and ident not in have:
                have.append(ident)

    def note_opened(self, role: str, ids: list[str]) -> None:
        bucket = self.opened.setdefault(role, set())
        for ident in ids:
            if ident:
                bucket.add(ident)

    def unread(self, role: str) -> list[str]:
        opened = self.opened.get(role, set())
        return [ident for ident in self.listed.get(role, []) if ident not in opened]

    async def add(self, facts: dict | None) -> Evidence:
        async with self._lock:
            incoming = coerce(facts, self.case)
            self.evidence = merge(self.case, self.evidence, incoming)
            return self.evidence

    def summary(self) -> str:
        dumped = self.evidence.model_dump(mode="json")
        lines = [facts_index(dumped)]
        groups = catalog_groups(self.evidence)
        if groups:
            lines.append("catalog groups: " + ", ".join(groups))
        if self.evidence.revisions:
            marks = [f"{r.id}:{r.marking_raw or r.marking}" for r in self.evidence.revisions]
            lines.append("revisions: " + ", ".join(marks))
        return "\n".join(line for line in lines if line)
