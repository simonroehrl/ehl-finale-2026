"""A small end-to-end run of the orchestration with mock workers: no browser, no model, invented placeholder data.

    uv run --no-sync python -m orchestration.demo

Four workers start. The tender worker reports a tender and its positions; the store validates them and raises
TenderDiscovered / PositionsFound; the scheduler sees check_discount_approval's needs met and the orchestrator
hands that task to the mail worker, while the spec and ERP workers go on with theirs (requirements -> one
find_materials batch for the ERP). Every placeholder value below is made up and carries no meaning.
"""
from __future__ import annotations

import asyncio
import json

from .agent_worker import MockWorker
from .dependency_graph import default_graph
from .events import EventBus, EventLog
from .evidence_store import EvidenceStore
from .orchestrator import Orchestrator
from .scheduler import Scheduler
from .schemas import Task

DELAY = 0.05   # a little "browsing" time per task, so the parallelism is visible in the log


async def tender_worker(task: Task, context: dict) -> dict:
    return {"tender": {"reference": "DEMO-REF-1", "title": "demo tender", "buyer": "demo buyer"},
            "positions": [{"number": "1", "quantity": "10", "unit": "piece", "requirement": "R-A"},
                          {"number": "2", "quantity": "4", "unit": "set", "requirement": "R-B"}],
            "clarifications": [{"number": "1", "date": "01/01 (MM/DD)", "text": "demo answer",
                                "changes": "quantity of position 2"}]}


async def spec_worker(task: Task, context: dict) -> dict:
    return {"revisions": [{"id": "rev-1", "status": "superseded"}, {"id": "rev-2", "status": "current"}],
            "current_revision": "rev-2",
            "requirements": [{"id": "R-A", "kataloggruppe": "group-x", "bohrung": "a", "hub": "b"},
                             {"id": "R-B", "kataloggruppe": "group-y", "bohrung": "c", "hub": "d"}]}


async def erp_worker(task: Task, context: dict) -> dict:
    return {"materials": [{"material_number": f"MAT-{requirement}", "status": "demo", "found_for": requirement,
                           "matches_requirement": True,
                           "price_conditions": [{"valid_from": "x", "valid_to": "y", "scales": []}]}
                          for requirement in task.params.get("requirement_ids", [])]}


async def mail_worker(task: Task, context: dict) -> dict:
    if task.spec == "check_discount_approval":
        reference = task.params["reference"]
        return {"mails": [{"sender": "demo@example.invalid", "subject": f"approval {reference}",
                           "folder": "inbox", "reference": reference, "body": "demo"}],
                "approvals": [{"reference": reference, "percent": "0", "sender": "demo@example.invalid"}]}
    return {"mailbox_domain": "example.invalid", "mails": [{"sender": "other@example.invalid", "subject": "hello",
                                                             "folder": "inbox", "body": "demo"}]}


async def main() -> dict:
    log = EventLog(None, echo=True)
    bus = EventBus()
    store = EvidenceStore(bus, log)
    scheduler = Scheduler(default_graph(), store, log)
    workers = {name: MockWorker(name, handler, bus, log, delay=DELAY) for name, handler in
               [("tender", tender_worker), ("spec", spec_worker), ("erp", erp_worker), ("mail", mail_worker)]}
    facts = await Orchestrator(store, scheduler, workers, log, deadline=None).run()

    print("\nTasks per worker:")
    for name, worker in workers.items():
        print(f"  {name}: {[t.spec for t in worker.seen]}")
    print("\nEvents:", [e.type.value for e in bus.history])
    print("\nFacts:", json.dumps(facts, ensure_ascii=False, indent=1)[:1500])
    return facts


if __name__ == "__main__":
    asyncio.run(main())
