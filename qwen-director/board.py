"""Shared task board: what the bid still needs, minus what the workers already found.

Initial tasks are the evidence kinds a priced bid always needs (positions, Q&A, revisions, mail).
ERP work is not seeded: it appears only after a Kataloggruppe shows up in the merged evidence
(via resolver gaps). Completing a task merges its records and refreshes the open list.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

from evidence import Evidence, merge
from resolver import Resolution, resolve

SITE_HINTS = {
    "tender-portal": ("tender", "vergabe"),
    "customer-portal": ("customer", "supplier", "spec", "lieferant"),
    "erp": ("erp",),
    "mail": ("mail", "webmail", "post", "inbox"),
}

# Generic jobs for a first wave. Not public-scenario URLs or answers.
SEED_TASKS = (
    ("tender-portal", "Record every position of the bill of quantities."),
    ("tender-portal", "Record every bidder Q&A answer on every page."),
    ("customer-portal", "Record every specification revision with its marking and every requirement you can open."),
    ("mail", "Record every mailbox message (inbox and archive) for this tender or a look-alike, "
             "including the structured fields for each message kind."),
)


@dataclass(frozen=True)
class Task:
    site: str
    question: str

    @property
    def key(self) -> str:
        return f"{self.site}::{self.question.strip().casefold()}"


def slot_name(site_id: str, index: int) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", site_id or "")[:40].strip("-")
    return cleaned or f"site{index}"


def assign_slots(access: list, cdp_urls: list[str]) -> list[dict]:
    slots = []
    for i, site in enumerate(list(access)[: min(len(access), len(cdp_urls), 4)]):
        slots.append({"name": slot_name(str(site.get("siteId") or ""), i + 1),
                      "site": site, "cdp": cdp_urls[i]})
    return slots


def slot_for_gap(slots: list[dict], gap_site: str) -> dict | None:
    want = (gap_site or "").strip().lower()
    if not want:
        return None
    for slot in slots:
        if (slot["site"].get("siteId") or "").lower() == want:
            return slot
    hints = SITE_HINTS.get(want, (want,))
    for slot in slots:
        sid = (slot["site"].get("siteId") or "").lower()
        if any(h in sid for h in hints if h):
            return slot
    for slot in slots:
        sid = (slot["site"].get("siteId") or "").lower()
        if want in sid or sid in want:
            return slot
    return None


def task_for_slot(slots: list[dict], task: Task) -> dict | None:
    return slot_for_gap(slots, task.site)


def tasks_from_resolution(resolution: Resolution) -> list[Task]:
    return [Task(g.site, g.question) for g in resolution.gaps]


class Board:
    def __init__(self, slots: list[dict]):
        self.slots = slots
        self.pending: dict[str, Task] = {}
        self.inflight: set[str] = set()
        self.done: set[str] = set()
        self.failures: dict[str, int] = {}
        self.evidence: Evidence | None = None
        self.lock = asyncio.Lock()
        for site, question in SEED_TASKS:
            if slot_for_gap(slots, site) is not None:
                task = Task(site, question)
                self.pending[task.key] = task

    def resolution(self) -> Resolution | None:
        return None if self.evidence is None else resolve(self.evidence)

    def is_complete(self) -> bool:
        found = self.resolution()
        return bool(found and found.answer is not None and not found.gaps)

    def open_for(self, slot: dict) -> list[Task]:
        return [t for t in self.pending.values() if task_for_slot(self.slots, t) is slot]

    def has_work(self) -> bool:
        return bool(self.pending or self.inflight)

    async def claim(self, slot: dict) -> Task | None:
        async with self.lock:
            for task in self.open_for(slot):
                self.pending.pop(task.key, None)
                self.inflight.add(task.key)
                return task
            return None

    async def publish(self, part: Evidence | None, path: Path | None = None) -> Evidence | None:
        """Merge a partial find without closing the task, so other workers see new gaps immediately."""
        async with self.lock:
            if part is not None:
                self.evidence = part if self.evidence is None else merge(self.evidence.case, self.evidence, part)
                self._refine_case_locked()
            self._refresh_locked()
            if path is not None:
                self.persist(path)
            return self.evidence

    async def absorb(self, task: Task, part: Evidence | None, path: Path | None = None) -> Evidence | None:
        async with self.lock:
            self.inflight.discard(task.key)
            if part is not None:
                self.done.add(task.key)
                self.evidence = part if self.evidence is None else merge(self.evidence.case, self.evidence, part)
                self._refine_case_locked()
            self._refresh_locked()
            if part is None and task.key not in self.pending and task.key not in self.inflight:
                n = self.failures.get(task.key, 0) + 1
                self.failures[task.key] = n
                if n < 2 and not self.is_complete():
                    self.pending[task.key] = task
                else:
                    self.done.add(task.key)
            if path is not None:
                self.persist(path)
            return self.evidence

    def _refine_case_locked(self) -> None:
        ev = self.evidence
        if ev is None or (ev.case.bidder_mail_domain and ev.case.bidder_mail_domain != "pending.local"):
            return
        host = None
        for message in ev.messages:
            local, _, found = (message.sender or "").partition("@")
            if found and local.casefold() == "leitung":
                host = found
                break
        if host is None:
            for message in ev.messages:
                _, _, found = (message.sender or "").partition("@")
                if found:
                    host = found
                    break
        if host:
            self.evidence = ev.model_copy(
                update={"case": ev.case.model_copy(update={"bidder_mail_domain": host})})

    def persist(self, path: Path) -> None:
        path.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")

    def _refresh_locked(self) -> None:
        found = self.resolution()
        if found is None:
            return
        if found.answer is not None and not found.gaps:
            self.pending.clear()
            return
        for task in tasks_from_resolution(found):
            if task.key in self.inflight or task.key in self.pending:
                continue
            if task_for_slot(self.slots, task) is None:
                continue
            self.pending[task.key] = task

    def snapshot(self) -> dict:
        ev = None if self.evidence is None else json.loads(self.evidence.model_dump_json())
        return {
            "known": ev,
            "open": [{"site": t.site, "question": t.question} for t in self.pending.values()],
            "inflight": sorted(self.inflight),
            "done": sorted(self.done),
        }
