"""Events and the two places they go: the bus the orchestrator listens to, and the JSONL log for reading a run later.

An event is only raised when something actually changed (the store compares before it publishes), so the
orchestrator never re-plans on noise.
"""
from __future__ import annotations

import asyncio
import json
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventType(StrEnum):
    TENDER_DISCOVERED = "TenderDiscovered"
    POSITIONS_FOUND = "PositionsFound"
    CLARIFICATION_FOUND = "ClarificationFound"
    REVISION_FOUND = "RevisionFound"
    REQUIREMENTS_UPDATED = "RequirementsUpdated"
    MATERIAL_FOUND = "MaterialFound"
    PRICING_DATA_UPDATED = "PricingDataUpdated"
    MAIL_FOUND = "MailFound"
    DISCOUNT_APPROVAL_FOUND = "DiscountApprovalFound"
    EVIDENCE_UPDATED = "EvidenceUpdated"          # any other kind (documents, notes, unknown information)
    EVIDENCE_INVALIDATED = "EvidenceInvalidated"
    TASK_PROGRESS = "TaskProgress"               # a worker saved interim facts; its task goes on
    TASK_FINISHED = "TaskFinished"
    TASK_FAILED = "TaskFailed"


EVENT_FOR_KIND: dict[str, EventType] = {
    "tender": EventType.TENDER_DISCOVERED,
    "position": EventType.POSITIONS_FOUND,
    "clarification": EventType.CLARIFICATION_FOUND,
    "revision": EventType.REVISION_FOUND,
    "current_revision": EventType.REVISION_FOUND,
    "requirement": EventType.REQUIREMENTS_UPDATED,
    "material": EventType.MATERIAL_FOUND,
    "price_condition": EventType.PRICING_DATA_UPDATED,
    "mail": EventType.MAIL_FOUND,
    "approval": EventType.DISCOUNT_APPROVAL_FOUND,
}


class Event(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    type: EventType
    fact_key: str | None = None
    kind: str | None = None
    version: int | None = None
    task_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    payload: Any = Field(default=None, exclude=True)   # in-process only (e.g. a TaskResult), never logged
    at: float = Field(default_factory=time.time)


class EventBus:
    """One asyncio.Queue. Producers `publish`, the orchestrator `get`s; `history` keeps every event for tests."""

    def __init__(self):
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self.history: list[Event] = []

    def publish(self, event: Event) -> Event:
        self.history.append(event)
        self._queue.put_nowait(event)
        return event

    async def get(self, timeout: float | None = None) -> Event | None:
        """The next event; None when `timeout` seconds pass without one."""
        if timeout is None:
            return await self._queue.get()
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def drain(self) -> list[Event]:
        out = []
        while not self._queue.empty():
            out.append(self._queue.get_nowait())
        return out

    def empty(self) -> bool:
        return self._queue.empty()


class EventLog:
    """Structured log: one JSON object per line in `path` (if given), echoed as `[orch] name k=v` like the rest of
    the agent prints. Records are small: keys and counts, never whole page contents."""

    def __init__(self, path: Path | None = None, echo: bool = True):
        self.path = path
        self.echo = echo
        self.started = time.monotonic()
        self.records: list[dict] = []
        self._file = path.open("a", encoding="utf-8") if path else None

    def write(self, name: str, **fields: Any) -> dict:
        record = {"t": round(time.monotonic() - self.started, 3), "event": name, **fields}
        self.records.append(record)
        if self._file:
            self._file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._file.flush()
        if self.echo:
            shown = " ".join(f"{k}={_short(v)}" for k, v in fields.items())
            print(f"[orch] {record['t']:7.1f}s {name} {shown}", flush=True)
        return record

    def close(self):
        if self._file:
            self._file.close()
            self._file = None


def _short(value: Any, limit: int = 80) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."
