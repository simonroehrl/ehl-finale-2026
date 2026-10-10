"""Wiring for a real run: the orchestration on the run's four browsers, with the same signature as
`agent.research()`, so `agent.solve()` can use either.

Reuses the existing pieces: `agent.site_task` / `agent.site` for the site prompts and logins (which read the
run's `access`), `prompts/*.md` for the first task on each website, `llm_agent.Agent` through `SiteWorker`.
"""
from __future__ import annotations

import json
from pathlib import Path

from .agent_worker import SiteWorker
from .dependency_graph import default_graph
from .events import EventBus, EventLog
from .evidence_store import EvidenceStore
from .orchestrator import Orchestrator
from .scheduler import Scheduler
from .schemas import Task

HERE = Path(__file__).resolve().parent
FOLLOWUP = HERE / "prompts" / "followup.md"
AGENTS = ["tender", "spec", "erp", "mail"]


def render_prompt(task: Task, run: dict, entry) -> str:
    """The model's instructions for `task`: the website's own template for a first visit, the generic follow-up
    template (instruction + context) otherwise. `entry` is the `agent` module (its site_task reads the run)."""
    if task.prompt_template:
        return entry.site_task(task.agent, run, REQUIREMENTS=json.dumps(task.context, ensure_ascii=False, indent=1))
    site = entry.site(run, entry.SITE_IDS[task.agent])
    text = FOLLOWUP.read_text()
    for key, value in {
        "SITE_ID": entry.SITE_IDS[task.agent], "BROWSER": task.agent, "SITE_URL": site["url"],
        "USERNAME": site.get("username", ""), "PASSWORD": site.get("password", ""), "BRIEF": run["brief"],
        "INSTRUCTION": task.instruction, "CONTEXT": json.dumps(task.context, ensure_ascii=False, indent=1),
        "REQUIRED_KEYS": ", ".join(task.required_keys) or "whatever fits",
    }.items():
        text = text.replace("{" + key + "}", str(value))
    return text


async def research_orchestrated(task: dict, urls: dict[str, str], workdir: Path, deadline: float) -> dict:
    """Research the four websites through the event-driven orchestration. `task` is the run, `urls` the CDP URL per
    agent name, `deadline` time.monotonic() when research must be over. Returns the facts per agent, the shape
    `analyst.analyse` reads."""
    import agent as entry  # agent.py imports this module; resolve the cycle at call time

    log = EventLog(workdir / "orchestration.jsonl")
    bus = EventBus()
    store = EvidenceStore(bus, log)
    scheduler = Scheduler(default_graph(), store, log)
    system = (entry.PROMPTS / "system.md").read_text()
    workers = {name: SiteWorker(name, urls[name], workdir, system, lambda t: render_prompt(t, task, entry),
                                deadline, bus, log) for name in AGENTS}
    orchestrator = Orchestrator(store, scheduler, workers, log, deadline)
    try:
        facts = await orchestrator.run()
    finally:
        for name, worker in workers.items():
            print(f"[{name}] tasks {len(worker.handled)}, usage {worker.usage}", flush=True)
        log.close()
    return {name: facts.get(name) for name in AGENTS}
