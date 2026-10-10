"""The orchestrator: the one loop that turns events into tasks and results into evidence.

It owns nothing clever. Workers report results on the bus; the orchestrator publishes those into the store (which
validates and raises change events), lets the scheduler re-evaluate the dependency graph after every event, and
hands new tasks to the worker in charge with only the facts that task depends on. It stops at the deadline or when
nothing is pending, running or queued.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .agent_worker import Worker
from .events import Event, EventLog, EventType
from .evidence_store import EvidenceStore
from .scheduler import Scheduler
from .schemas import Provenance, Task, TaskResult


class Orchestrator:
    def __init__(self, store: EvidenceStore, scheduler: Scheduler, workers: dict[str, Worker],
                 log: EventLog | None = None, deadline: float | None = None, stop_grace: float = 5.0):
        """`deadline` is time.monotonic(); None runs until idle. Workers are rewired to the store's bus."""
        self.store, self.scheduler, self.workers, self.log = store, scheduler, workers, log
        self.bus = store.bus
        self.deadline, self.stop_grace = deadline, stop_grace
        self.events_handled = 0
        for worker in workers.values():
            worker.bus = self.bus
            worker.on_start = lambda task: self.scheduler.mark_running(task.id)

    # --- the loop ------------------------------------------------------------------------------------------------

    async def run(self) -> dict[str, Any]:
        """Start the workers, plan the first tasks, react to events until idle or out of time. Returns the facts in
        the shape the analyst reads (`EvidenceStore.snapshot`)."""
        for worker in self.workers.values():
            worker.start()
        self._log("run_started", workers=sorted(self.workers), deadline_in=self._left())
        self.dispatch(self.scheduler.evaluate())
        reason = "idle"
        try:
            while True:
                left = self._left()
                if left is not None and left <= 0:
                    reason = "deadline"
                    break
                if not self.scheduler.open_tasks() and self.bus.empty():
                    break
                event = await self.bus.get(timeout=left)
                if event is None:
                    continue
                await self.handle_event(event)
        finally:
            await asyncio.gather(*(w.stop(self.stop_grace) for w in self.workers.values()), return_exceptions=True)
            await self._collect_remaining()
            self._log("run_finished", reason=reason, tasks=self.scheduler.summary(), facts=self.store.counts(),
                      events=self.events_handled)
        return self.store.snapshot()

    async def handle_event(self, event: Event, plan: bool = True):
        self.events_handled += 1
        if event.type == EventType.TASK_FINISHED:
            await self._finished(event.payload)
        elif event.type == EventType.TASK_FAILED:
            self._failed(event.payload)
        elif event.type == EventType.TASK_PROGRESS:
            await self._progress(event.task_id, event.payload)
        elif event.type == EventType.EVIDENCE_INVALIDATED and event.fact_key:
            self.scheduler.on_fact_changed(event.fact_key, event.detail.get("superseded_by"))
        if plan:
            self.dispatch(self.scheduler.evaluate())

    async def _collect_remaining(self):
        """After the stop: whatever the workers still reported (results at the deadline, interim saves) goes into
        the store, without planning anything new."""
        collected = 0
        while True:
            events = [e for e in self.bus.drain()
                      if e.type in (EventType.TASK_FINISHED, EventType.TASK_FAILED, EventType.TASK_PROGRESS)]
            if not events:
                break
            for event in events:
                await self.handle_event(event, plan=False)
                collected += 1
        if collected:
            self._log("results_collected_after_stop", results=collected)

    def dispatch(self, tasks: list[Task]):
        for task in tasks:
            worker = self.workers.get(task.agent)
            if worker is None:
                self.scheduler.mark_failed(task.id, f"no worker for agent {task.agent}")
                continue
            task.context = self.store.context_for(task.context_keys)
            worker.submit(task)

    # --- results -------------------------------------------------------------------------------------------------

    async def _finished(self, result: TaskResult):
        task = self.scheduler.tasks.get(result.task_id)
        if task is None:
            self._log("result_ignored", task=result.task_id, reason="unknown task")
            return
        if result.facts:
            provenance = Provenance(site=task.agent, agent=task.agent, task_id=task.id)
            outcomes = await self.store.publish_facts(result.facts, provenance)
            self._log("result_published", task=task.id, agent=task.agent, records=len(outcomes),
                      accepted=sum(o.accepted for o in outcomes), changed=sum(o.changed for o in outcomes))
        self.scheduler.mark_done(task.id, result.summary, result.seconds)

    async def _progress(self, task_id: str | None, facts: Any):
        task = self.scheduler.tasks.get(task_id or "")
        if task is None or not isinstance(facts, dict):
            return
        provenance = Provenance(site=task.agent, agent=task.agent, task_id=task.id)
        outcomes = await self.store.publish_facts(facts, provenance)
        changed = sum(o.changed for o in outcomes)
        if changed:
            self._log("progress_published", task=task.id, agent=task.agent, records=len(outcomes), changed=changed)

    def _failed(self, result: TaskResult):
        if result.task_id in self.scheduler.tasks:
            self.scheduler.mark_failed(result.task_id, result.error or "unknown error")

    # --- helpers -------------------------------------------------------------------------------------------------

    def _left(self) -> float | None:
        return None if self.deadline is None else self.deadline - time.monotonic()

    def _log(self, name: str, **fields):
        if self.log:
            self.log.write(name, **{k: v for k, v in fields.items() if v is not None})
