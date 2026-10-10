"""Event-driven orchestration of the site agents: a shared evidence store, an event bus, a static dependency graph,
a deterministic scheduler and one worker per website.

    store      = EvidenceStore(bus, log)                 # facts: validated, versioned, attributed
    scheduler  = Scheduler(default_graph(), store, log)  # which tasks should exist now
    workers    = {"tender": ..., "spec": ..., "erp": ..., "mail": ...}
    facts      = await Orchestrator(store, scheduler, workers, log, deadline).run()

`runtime.research_orchestrated` wires this to the run's browsers; `demo.py` runs it with mock workers.
"""
from .agent_worker import MockWorker, SiteWorker, Worker
from .dependency_graph import DependencyGraph, Needs, TaskSpec, default_graph
from .events import Event, EventBus, EventLog, EventType
from .evidence_store import EvidenceStore, PublishResult
from .orchestrator import Orchestrator
from .scheduler import Scheduler
from .schemas import Fact, FactStatus, Provenance, Task, TaskResult, TaskStatus

__all__ = [
    "DependencyGraph", "Event", "EventBus", "EventLog", "EventType", "EvidenceStore", "Fact", "FactStatus",
    "MockWorker", "Needs", "Orchestrator", "Provenance", "PublishResult", "Scheduler", "SiteWorker", "Task",
    "TaskResult", "TaskSpec", "TaskStatus", "Worker", "default_graph",
]
