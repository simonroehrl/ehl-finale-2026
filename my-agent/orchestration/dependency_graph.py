"""The dependency graph: which facts a kind of task needs before it can run, and what it produces.

Plain Python: a list of `TaskSpec`s, each with `Needs` on the store. It names kinds of information (a requirement,
a material, a tender reference), never websites, pages or values; the agents work out where those live. How many
tasks a spec yields is decided at run time by `fan_out` from the facts found so far (one per requirement, one per
shortlisted material, ...).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .evidence_store import EvidenceStore
from .schemas import Task, TaskStatus

Params = dict[str, Any]
FanOut = Callable[[EvidenceStore, list[Task]], list[Params]]


@dataclass(frozen=True)
class Needs:
    """At least `min_count` validated facts of `kind`, optionally filtered by `where` on the fact's value."""
    kind: str
    min_count: int = 1
    where: Callable[[dict], bool] | None = None
    label: str = ""

    def matching(self, store: EvidenceStore) -> list[dict]:
        return [v for v in store.values(self.kind) if self.where is None or self.where(v)]

    def satisfied(self, store: EvidenceStore) -> bool:
        return len(self.matching(store)) >= self.min_count

    def __str__(self) -> str:
        return self.label or (f"{self.kind} x{self.min_count}" if self.min_count > 1 else self.kind)


@dataclass
class TaskSpec:
    name: str
    agent: str | None                      # worker name; None: a marker (an extension point, not dispatched)
    instruction: str                       # what to find out, in words: the orchestrator's half of the job
    requires: list[Needs] = field(default_factory=list)
    produces: list[str] = field(default_factory=list)        # kinds of facts expected
    required_keys: list[str] = field(default_factory=list)   # top-level keys the worker must report
    prompt_template: str | None = None                        # prompts/<file>.md to use, else the generic one
    fan_out: FanOut | None = None      # (store, this spec's tasks so far) -> param sets; None: one task, no params
    context: Callable[[EvidenceStore, Params], list[str]] | None = None  # fact keys the worker gets
    time_share: float | None = None    # share of the research window a task of this spec may use (None: all of it)

    def satisfied(self, store: EvidenceStore) -> bool:
        return all(need.satisfied(store) for need in self.requires)

    def param_sets(self, store: EvidenceStore, existing: list[Task] | None = None) -> list[Params]:
        return self.fan_out(store, existing or []) if self.fan_out else [{}]

    def context_keys(self, store: EvidenceStore, params: Params) -> list[str]:
        return list(dict.fromkeys(self.context(store, params))) if self.context else []


class DependencyGraph:
    def __init__(self, specs: list[TaskSpec]):
        self.specs: dict[str, TaskSpec] = {}
        for spec in specs:
            if spec.name in self.specs:
                raise ValueError(f"duplicate task spec {spec.name}")
            self.specs[spec.name] = spec

    def consumers(self, kind: str) -> list[TaskSpec]:
        return [s for s in self.specs.values() if any(n.kind == kind for n in s.requires)]

    def producers(self, kind: str) -> list[TaskSpec]:
        return [s for s in self.specs.values() if kind in s.produces]

    def describe(self) -> str:
        lines = []
        for spec in self.specs.values():
            needs = ", ".join(str(n) for n in spec.requires) or "nothing"
            lines.append(f"{spec.name} [{spec.agent or 'marker'}]: needs {needs}; produces {', '.join(spec.produces)}")
        return "\n".join(lines)


# --- the default graph -------------------------------------------------------------------------------------------

def _present(*names: str) -> Callable[[dict], bool]:
    return lambda v: all(v.get(n) not in (None, "", [], {}) for n in names)


def claimed(tasks: list[Task], param: str, kind: str) -> set[str]:
    """Ids already covered by a task's batch (`params[param]` is a list). A failed-out, or a stale task's id whose
    fact changed, is not covered any more, so the next batch picks it up again."""
    out: set[str] = set()
    for task in tasks:
        exhausted = task.status == TaskStatus.FAILED
        for item in task.params.get(param, []):
            if exhausted or (task.stale and f"{kind}:{item}" in task.stale_keys):
                continue
            out.add(str(item))
    return out


def batch(ids: list[str], param: str) -> list[Params]:
    """One task for every id not yet covered: a worker handles them in one browser session. Empty: no task."""
    return [{param: sorted(set(ids))}] if ids else []


def _requirement_keys(store: EvidenceStore, requirement_ids: list[str]) -> list[str]:
    """The requirements, the positions that refer to them, and which revision is current."""
    wanted = {str(r) for r in requirement_ids}
    keys = [f.key for f in store.read(kind="requirement") if str(f.value.get("id")) in wanted]
    keys += [f.key for f in store.read(kind="position")
             if str(f.value.get("requirement") or "").strip() in wanted]
    keys += [f.key for f in store.read(kind="current_revision")]
    return keys


def _material_keys(store: EvidenceStore, material_numbers: list[str]) -> list[str]:
    wanted = {str(m) for m in material_numbers}
    keys, found_for = [], set()
    for f in store.read(kind="material"):
        if str(f.value.get("material_number")) in wanted:
            keys.append(f.key)
            if f.value.get("found_for"):
                found_for.add(str(f.value["found_for"]).strip())
    keys += [f.key for f in store.read(kind="position")
             if str(f.value.get("requirement") or "").strip() in found_for]
    return keys


def _tender_keys(store: EvidenceStore, reference: str) -> list[str]:
    keys = [f.key for f in store.read(kind="tender") if f.value.get("reference") == reference]
    keys += [f.key for f in store.read(kind="position")]
    return keys


def default_graph() -> DependencyGraph:
    """The time shares of the first visits: the tender gets 60% of the research window, the mailbox 60% (its
    follow-up, the discount approval, needs the rest), the specification 90%: the ERP starts on the requirements as
    they are published (TASK_PROGRESS), so the specification need not be cut short; follow-ups run until the window
    closes."""
    has_pricing = _present("price_conditions")
    return DependencyGraph([
        TaskSpec(
            name="discover_tender", agent="tender", prompt_template="tender.md", time_share=0.6,
            instruction="Find the tender named in the brief: its reference and header data, every position of its "
                        "bill of quantities with quantity, unit and the requirement it refers to, and every bidder "
                        "Q&A answer with its date, full text and what it changes or withdraws.",
            produces=["tender", "position", "clarification", "document"],
            required_keys=["tender", "positions", "clarifications"]),
        TaskSpec(
            name="read_specification", agent="spec", prompt_template="spec.md", time_share=0.9,
            instruction="Find the buyer's technical specification for the tender in the brief: every revision with "
                        "date and status, which one is current, and every requirement of the current revision with "
                        "all its attributes and its catalogue group.",
            produces=["revision", "current_revision", "requirement", "document"],
            required_keys=["revisions", "requirements"]),
        TaskSpec(
            name="search_mails", agent="mail", prompt_template="mail.md", time_share=0.6,
            instruction="Search the sales mailbox, inbox and archive, for everything about the tender reference in "
                        "the brief: approvals, condition rules, technical releases, tariffs, FX fixings, pools, "
                        "bonuses and indices. Record every mail in full.",
            produces=["mail", "mailbox"], required_keys=["mails"]),
        TaskSpec(
            name="find_materials", agent="erp", prompt_template="erp.md",
            instruction="For the requirements in the context, search the ERP catalogue for each catalogue group and "
                        "record every candidate material with its attributes and status. Open the details of the "
                        "candidates that match or nearly match and record their price conditions, stock, lead "
                        "times, pieces per carton and masses. Mark each candidate with matches_requirement.",
            requires=[Needs("requirement", where=_present("id", "catalogue_group"),
                            label="a requirement with a catalogue group")],
            produces=["material"], required_keys=["materials"],
            # one batch per wave of new requirements: the ERP worker handles them in one session, and requirements
            # that arrive later (or change) form the next batch
            fan_out=lambda store, tasks: batch(
                [v["id"] for v in store.values("requirement") if _present("id", "catalogue_group")(v)
                 and v["id"] not in claimed(tasks, "requirement_ids", "requirement")], "requirement_ids"),
            context=lambda store, p: _requirement_keys(store, p["requirement_ids"])),
        TaskSpec(
            name="read_price_conditions", agent="erp",
            instruction="For the materials in the context, open each ERP record and record every price condition "
                        "(validity from and to, currency, price unit, every quantity scale with its price, FX key), "
                        "stock, lead time from stock and production lead time, pieces per carton and masses.",
            requires=[Needs("material", where=lambda v: v.get("matches_requirement") is True and not has_pricing(v),
                            label="a matching material without price data")],
            produces=["material", "price_condition"], required_keys=["materials"],
            fan_out=lambda store, tasks: batch(
                [v["material_number"] for v in store.values("material")
                 if v.get("matches_requirement") is True and not has_pricing(v)
                 and v["material_number"] not in claimed(tasks, "material_numbers", "material")],
                "material_numbers"),
            context=lambda store, p: _material_keys(store, p["material_numbers"])),
        TaskSpec(
            name="check_discount_approval", agent="mail",
            instruction="The tender reference and its positions are confirmed (see context). Search the mailbox "
                        "for discount approvals and condition rules for exactly this reference from the senders the "
                        "brief authorizes, inbox and archive, and record each as a mail; summarize the approvals "
                        "found under approvals with sender, date, percent and any minimum order value.",
            requires=[Needs("tender", where=_present("reference")), Needs("position")],
            produces=["mail", "approval"], required_keys=["mails"],
            fan_out=lambda store, tasks: [{"reference": v["reference"]} for v in store.values("tender")
                                          if _present("reference")(v)],
            context=lambda store, p: _tender_keys(store, p["reference"])),
        TaskSpec(
            name="compute_bid", agent=None,
            instruction="Extension point: the analyst and the pricing engine take over once every position has a "
                        "requirement, a priced material candidate exists and the mailbox has been read.",
            requires=[Needs("position"), Needs("requirement"), Needs("material", where=has_pricing,
                                                                     label="a priced material"), Needs("mail")],
            produces=[]),
    ])
