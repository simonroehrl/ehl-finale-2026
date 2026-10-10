"""The canonical shapes of the orchestration: facts and where they come from, the domain records, tasks and results.

Every record a worker reports is validated into one of the `Evidence` models below (unknown attributes are kept,
`extra="allow"`), wrapped in a `Fact` with its provenance, version and status, and only then usable downstream.
"""
from __future__ import annotations

import time
import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# --- provenance and facts -----------------------------------------------------------------------------------------

class Provenance(BaseModel):
    """Where a fact was observed: which logical source, which agent, which document or page, when."""
    site: str                              # logical source: tender | spec | erp | mail | system
    agent: str
    document_ref: str | None = None
    task_id: str | None = None
    observed_at: float = Field(default_factory=time.time)


class FactStatus(StrEnum):
    PROPOSED = "proposed"          # reported, but failed validation: not usable downstream
    VALIDATED = "validated"        # schema and business checks passed
    CONFLICTING = "conflicting"    # contradicts a validated fact from another source; kept, not used
    INVALIDATED = "invalidated"    # superseded by a newer version or withdrawn


class Fact(BaseModel):
    key: str                               # canonical identity, e.g. "material:<number>"
    kind: str                              # tender | position | requirement | material | mail | ...
    value: dict[str, Any]                  # normalized record
    version: int = 1
    status: FactStatus
    provenance: Provenance
    raw: Any = None                        # exactly what the worker reported
    source_key: str                        # top-level key the worker used ("positions", "materials", ...)
    singleton: bool = False                # one record per agent (tender, document) rather than a list entry
    issues: list[str] = Field(default_factory=list)   # missing or doubtful attributes, conflict notes
    conflicts_with: str | None = None      # key of the validated fact this one contradicts


# --- domain records ------------------------------------------------------------------------------------------------

class Evidence(BaseModel):
    """Base of every domain record: known attributes are typed loosely (sites show values in many formats and the
    workers must keep them as shown), unknown attributes are kept."""
    model_config = ConfigDict(extra="allow")


class Tender(Evidence):
    reference: str
    title: str | None = None
    buyer: str | None = None
    deadline: str | None = None


class TenderPosition(Evidence):
    number: str
    description: str | None = None
    quantity: Any = None
    unit: str | None = None
    requirement: str | None = None
    tender_reference: str | None = None


class Clarification(Evidence):
    number: str
    date: str | None = None
    text: str | None = None
    changes: Any = None
    withdraws: Any = None


class Document(Evidence):
    name: str
    content: Any = None


class SpecRevision(Evidence):
    id: str
    date: str | None = None
    status: str | None = None
    change_note: Any = None
    marking: str | None = None


class CurrentRevision(Evidence):
    id: str


class Requirement(Evidence):
    id: str
    revision: str | None = None
    catalogue_group: str | None = None
    bore: Any = None
    stroke: Any = None
    pressure: Any = None
    mounting: Any = None
    certification: Any = None
    pieces_per_set: Any = None
    wording: str | None = None


class Material(Evidence):
    material_number: str
    description: str | None = None
    status: str | None = None
    bore: Any = None
    stroke: Any = None
    pressure: Any = None
    mounting: Any = None
    certification: Any = None
    found_for: Any = None
    matches_requirement: bool | None = None
    price_conditions: list[Any] | None = None
    stock: Any = None
    stock_lead_time_weeks: Any = None
    production_lead_time_weeks: Any = None
    pieces_per_carton: Any = None


class PriceCondition(Evidence):
    material_number: str
    valid_from: str | None = None
    valid_to: str | None = None
    currency: str | None = None
    price_unit: Any = None
    scales: list[Any] | None = None
    fx_key: str | None = None


class Mail(Evidence):
    sender: str
    subject: str | None = None
    date: str | None = None
    folder: str | None = None
    reference: str | None = None
    body: str | None = None
    id: str | None = None


class Approval(Evidence):
    reference: str
    percent: Any = None
    sender: str | None = None
    date: str | None = None
    min_order_value: Any = None


class Mailbox(Evidence):
    domain: str


class Note(Evidence):
    text: Any


class UnknownInfo(Evidence):
    """Something a worker reported under a key the schema does not know. Kept, never silently dropped."""
    source_key: str
    payload: Any


# --- tasks ----------------------------------------------------------------------------------------------------------

class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def _task_id() -> str:
    return uuid.uuid4().hex[:8]


class Task(BaseModel):
    """What the orchestrator asks an agent to find out. The agent decides how."""
    id: str = Field(default_factory=_task_id)
    spec: str                                      # TaskSpec name
    agent: str                                     # worker name: tender | spec | erp | mail
    params: dict[str, Any] = Field(default_factory=dict)
    dedup_key: str
    instruction: str = ""                          # the WHAT, in words
    prompt_template: str | None = None             # a prompt file to use, if the spec names one
    required_keys: list[str] = Field(default_factory=list)   # top-level keys the worker must report
    context_keys: list[str] = Field(default_factory=list)    # facts the worker gets as context
    context: dict[str, Any] = Field(default_factory=dict)    # filled at dispatch: only those facts
    version_seen: dict[str, int] = Field(default_factory=dict)  # fact versions the task was planned on
    time_share: float | None = None                # share of the research window this task may use (None: all)
    status: TaskStatus = TaskStatus.PENDING
    stale: bool = False                            # an input changed after planning
    stale_keys: list[str] = Field(default_factory=list)   # which inputs changed
    attempts: int = 1
    error: str | None = None
    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None


class TaskResult(BaseModel):
    task_id: str
    agent: str
    facts: dict[str, Any] | None = None
    error: str | None = None
    summary: str | None = None
    seconds: float | None = None
