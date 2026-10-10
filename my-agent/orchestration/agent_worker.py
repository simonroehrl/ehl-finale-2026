"""Workers: one per logical agent (tender, spec, erp, mail), each with its own queue and its own browser.

A worker takes tasks from its queue one at a time (its browser actions stay sequential), decides how to find what
the task asks for, and reports what it found as a TaskResult on the bus. A failing task is reported, not raised:
the worker goes on with the next one and the other workers are not affected.

`SiteWorker` wraps the existing `llm_agent.Agent` (the model in a tool loop over Playwright MCP) and keeps it open
between tasks, so the browser session and its logins survive. `MockWorker` runs a plain function: for tests and
for the demo.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .events import Event, EventBus, EventLog, EventType
from .schemas import Task, TaskResult
from .validation import as_facts

Handler = Callable[[Task, dict[str, Any]], Awaitable[dict | None] | dict | None]


class Worker:
    def __init__(self, name: str, bus: EventBus | None = None, log: EventLog | None = None):
        self.name = name
        self.bus = bus or EventBus()
        self.log = log
        self.queue: asyncio.Queue[Task | None] = asyncio.Queue()
        self.current: Task | None = None
        self.handled: list[str] = []
        self.on_start: Callable[[Task], Any] | None = None   # the orchestrator's hook: a task was picked up
        self._runner: asyncio.Task | None = None

    # --- the orchestrator's side ---------------------------------------------------------------------------------

    def submit(self, task: Task):
        self.queue.put_nowait(task)

    def start(self) -> asyncio.Task:
        if self._runner is None:
            self._runner = asyncio.create_task(self.run(), name=f"worker-{self.name}")
        return self._runner

    @property
    def busy(self) -> bool:
        return self.current is not None or not self.queue.empty()

    async def stop(self, grace: float = 5.0):
        """Let the current task finish (up to `grace` seconds), then close."""
        if self._runner is None:
            return
        self.queue.put_nowait(None)
        try:
            await asyncio.wait_for(asyncio.shield(self._runner), grace)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._runner.cancel()
            try:
                await self._runner
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        finally:
            await self.close()

    # --- the worker's side ---------------------------------------------------------------------------------------

    async def run(self):
        while True:
            task = await self.queue.get()
            if task is None:
                return
            self.current = task
            started = time.monotonic()
            try:
                if self.on_start:
                    self.on_start(task)
                facts = as_facts(await self.handle(task, task.context))
                result = TaskResult(task_id=task.id, agent=self.name, facts=facts,
                                    seconds=round(time.monotonic() - started, 1))
                event_type = EventType.TASK_FINISHED
            except asyncio.CancelledError:
                self.current = None
                raise
            except Exception as error:  # noqa: BLE001  one task failing must not stop the worker
                result = TaskResult(task_id=task.id, agent=self.name, error=repr(error),
                                    seconds=round(time.monotonic() - started, 1))
                event_type = EventType.TASK_FAILED
            finally:
                self.current = None
            self.handled.append(task.id)
            self.bus.publish(Event(type=event_type, task_id=task.id, payload=result,
                                   detail={"agent": self.name, "spec": task.spec}))

    async def handle(self, task: Task, context: dict[str, Any]) -> dict | None:
        """Find what `task` asks for, given `context` (the facts it depends on). Returns the facts found as the
        worker's JSON (top-level keys like `positions`, `materials`), or None for nothing."""
        raise NotImplementedError

    def report_progress(self, facts: dict | None):
        """Interim facts while the current task goes on: the store takes them at once, so dependent tasks can
        start before this one is finished (the ERP needs the requirements, not the whole specification)."""
        facts = as_facts(facts)
        if self.current is not None and facts:
            self.bus.publish(Event(type=EventType.TASK_PROGRESS, task_id=self.current.id, payload=facts,
                                   detail={"agent": self.name, "spec": self.current.spec}))

    async def close(self):
        """Release what the worker holds (browser sessions). Called once, after the last task."""


class MockWorker(Worker):
    """A worker whose `handle` is a function: `handler(task, context) -> facts`, sync or async."""

    def __init__(self, name: str, handler: Handler, bus: EventBus | None = None, log: EventLog | None = None,
                 delay: float = 0.0):
        super().__init__(name, bus, log)
        self.handler, self.delay = handler, delay
        self.seen: list[Task] = []

    async def handle(self, task: Task, context: dict[str, Any]) -> dict | None:
        self.seen.append(task)
        if self.delay:
            await asyncio.sleep(self.delay)
        out = self.handler(task, context)
        if inspect.isawaitable(out):
            out = await out
        return out


class SiteWorker(Worker):
    """The real thing: `llm_agent.Agent` on one of the run's browsers, kept open across tasks.

    `render_prompt(task)` turns a task into the model's instructions (runtime.py builds it from the existing prompt
    templates and the run's site access); `deadline` is time.monotonic() when all research must stop."""

    def __init__(self, name: str, cdp_url: str, workdir: Path, system_prompt: str,
                 render_prompt: Callable[[Task], str], deadline: float, bus: EventBus | None = None,
                 log: EventLog | None = None, max_turns: int = 80):
        super().__init__(name, bus, log)
        self.cdp_url, self.workdir, self.system = cdp_url, workdir, system_prompt
        self.render_prompt, self.deadline, self.max_turns = render_prompt, deadline, max_turns
        self.window_start = time.monotonic()      # the research window: from here to `deadline`
        self.agent = None
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    async def _open(self):
        if self.agent is None:
            from llm_agent import Agent  # the harness, imported here so tests without node/MCP can import this module
            agent = Agent(self.name, {self.name: self.cdp_url}, self.workdir)
            await agent.__aenter__()
            agent.on_facts = self.report_progress   # every save_facts reaches the store at once
            self.agent = agent
        return self.agent

    def task_deadline(self, task: Task) -> float:
        """A first visit gets its share of the research window (so a slow website cannot starve the ones that
        depend on it); anything else may run until the window closes."""
        if task.time_share is None:
            return self.deadline
        return min(self.deadline, self.window_start + task.time_share * (self.deadline - self.window_start))

    async def handle(self, task: Task, context: dict[str, Any]) -> dict | None:
        agent = await self._open()
        report = await agent.run(self.system, self.render_prompt(task), self.task_deadline(task),
                                 max_turns=self.max_turns, required=task.required_keys)
        for key in self.usage:
            self.usage[key] += (report.get("usage") or {}).get(key, 0)
        if self.log:
            self.log.write("worker_report", agent=self.name, task=task.id, seconds=report.get("seconds"),
                           usage=report.get("usage"), summary=str(report.get("summary") or "")[:200])
        return report.get("facts")

    async def close(self):
        if self.agent is not None:
            agent, self.agent = self.agent, None
            await agent.__aexit__(None, None, None)
