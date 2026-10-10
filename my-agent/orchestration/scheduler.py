"""The scheduler: from the graph and the store to the tasks that should exist now.

Deterministic and idempotent: `evaluate()` may run after every event and only yields a task when its spec's needs
are met, its parameters are not already pending, running or done, and (for a done task) an input changed since.
No model is involved in planning.
"""
from __future__ import annotations

import json
import time

from .dependency_graph import DependencyGraph, Params, TaskSpec
from .events import EventLog
from .evidence_store import EvidenceStore
from .schemas import Task, TaskStatus


def dedup_key(spec_name: str, params: Params) -> str:
    return spec_name if not params else f"{spec_name}:{json.dumps(params, sort_keys=True, ensure_ascii=False)}"


class Scheduler:
    def __init__(self, graph: DependencyGraph, store: EvidenceStore, log: EventLog | None = None,
                 max_attempts: int = 2):
        self.graph, self.store, self.log, self.max_attempts = graph, store, log, max_attempts
        self.tasks: dict[str, Task] = {}
        self._satisfied: set[str] = set()      # specs whose needs were seen met at least once

    # --- planning ------------------------------------------------------------------------------------------------

    def evaluate(self) -> list[Task]:
        """New tasks whose prerequisites hold now and that are not already planned, running or done and current."""
        new: list[Task] = []
        for spec in self.graph.specs.values():
            if not spec.satisfied(self.store):
                continue
            if spec.name not in self._satisfied:
                self._satisfied.add(spec.name)
                if spec.requires:
                    self._log("dependency_satisfied", spec=spec.name, needs=[str(n) for n in spec.requires])
            if spec.agent is None:
                continue
            existing = [t for t in self.tasks.values() if t.spec == spec.name]
            for params in spec.param_sets(self.store, existing):
                task = self._plan(spec, params)
                if task is not None:
                    new.append(task)
        return new

    def _plan(self, spec: TaskSpec, params: Params) -> Task | None:
        key = dedup_key(spec.name, params)
        existing = sorted((t for t in self.tasks.values() if t.dedup_key == key), key=lambda t: t.created_at)
        latest = existing[-1] if existing else None
        if latest is not None:
            if latest.status in (TaskStatus.PENDING, TaskStatus.RUNNING):
                return None
            if latest.status == TaskStatus.DONE and not latest.stale:
                return None
            if latest.status == TaskStatus.FAILED and not latest.stale and latest.attempts >= self.max_attempts:
                return None
            if latest.stale and self._source_still_running(latest.stale_keys):
                return None   # the fact is still being worked on: re-run once its source has settled
        context_keys = spec.context_keys(self.store, params)
        task = Task(spec=spec.name, agent=spec.agent, params=params, dedup_key=key, instruction=spec.instruction,
                    prompt_template=spec.prompt_template, required_keys=list(spec.required_keys),
                    context_keys=context_keys, time_share=spec.time_share,
                    version_seen={k: f.version for k in context_keys if (f := self.store.get(k)) is not None},
                    attempts=(latest.attempts + 1) if latest is not None else 1)
        self.tasks[task.id] = task
        if latest is None:
            self._log("task_created", task=task.id, spec=spec.name, agent=spec.agent, params=params or None)
        else:
            self._log("task_rescheduled", task=task.id, spec=spec.name, agent=spec.agent, params=params or None,
                      previous=latest.id, reason="stale input" if latest.stale else f"attempt {task.attempts}")
        return task

    # --- state changes -------------------------------------------------------------------------------------------

    def mark_running(self, task_id: str) -> Task:
        task = self.tasks[task_id]
        task.status, task.started_at = TaskStatus.RUNNING, time.time()
        self._log("task_started", task=task.id, spec=task.spec, agent=task.agent)
        return task

    def mark_done(self, task_id: str, summary: str | None = None, seconds: float | None = None) -> Task:
        task = self.tasks[task_id]
        task.status, task.finished_at = TaskStatus.DONE, time.time()
        self._log("task_finished", task=task.id, spec=task.spec, agent=task.agent, seconds=seconds,
                  stale=task.stale or None, summary=(summary or "")[:200] or None)
        return task

    def mark_failed(self, task_id: str, error: str) -> Task:
        task = self.tasks[task_id]
        task.status, task.finished_at, task.error = TaskStatus.FAILED, time.time(), error
        self._log("task_failed", task=task.id, spec=task.spec, agent=task.agent, error=error[:300],
                  attempt=task.attempts, retry=task.attempts < self.max_attempts)
        return task

    def on_fact_changed(self, key: str, version: int | None) -> list[Task]:
        """A fact got a new version (or was withdrawn): tasks planned on an older version are stale. Done ones get
        re-planned by the next `evaluate()`; running ones finish, then get re-planned."""
        stale = []
        for task in self.tasks.values():
            seen = task.version_seen.get(key)
            if seen is None or not (version is None or seen < version):
                continue
            if task.status in (TaskStatus.DONE, TaskStatus.RUNNING, TaskStatus.FAILED):
                if key not in task.stale_keys:
                    task.stale_keys.append(key)
                if not task.stale:
                    task.stale = True
                    stale.append(task)
                    self._log("task_stale", task=task.id, spec=task.spec, fact=key, seen=seen, now=version)
        return stale

    def _source_still_running(self, keys: list[str]) -> bool:
        """Is a task that produced one of these facts still running (reporting interim results)?"""
        for key in keys:
            fact = self.store.get(key)
            source = self.tasks.get(fact.provenance.task_id or "") if fact else None
            if source is not None and source.status == TaskStatus.RUNNING:
                return True
        return False

    # --- queries -------------------------------------------------------------------------------------------------

    def open_tasks(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.status in (TaskStatus.PENDING, TaskStatus.RUNNING)]

    def by_status(self, status: TaskStatus) -> list[Task]:
        return [t for t in self.tasks.values() if t.status == status]

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for task in self.tasks.values():
            out[task.status.value] = out.get(task.status.value, 0) + 1
        return out

    def _log(self, name: str, **fields):
        if self.log:
            self.log.write(name, **{k: v for k, v in fields.items() if v is not None})
