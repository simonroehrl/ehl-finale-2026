"""The entry point: agent.json runs `uv run --no-sync python agent.py`, in the sandbox and on your machine.

The agent system, in four steps, each a model at work (the hosted Qwen through the run's proxy):
1. Site agents research the tender portal, the specification and the mailbox in parallel, then the ERP once the
   specification names the catalogue groups (llm_agent.py, prompts/).
2. The analyst applies the brief's rules to their facts and decides every value (analyst.py).
3. pricing.py does the arithmetic.
4. The submit agent enters the bid on the tender portal and signs it with the TAN from the mailbox.
Docs: $API/docs/building-an-agent.md, $API/docs/run-contract.md, and the rules: $API/docs/fair-play.md
"""
import asyncio
import json
import re
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from playwright.async_api import async_playwright

import analyst
import browsers
import run as contract
from llm_agent import Agent

PROMPTS = Path(__file__).resolve().parent / "prompts"
BROWSERS = ["tender", "spec", "erp", "mail"]           # browser n works on one website
SITE_IDS = {"tender": "tender-portal", "spec": "customer-portal", "erp": "erp", "mail": "mail"}
REQUIRED = {"tender": ["tender", "positions", "clarifications"], "spec": ["revisions", "requirements"],
            "mail": ["mails"], "erp": ["materials"], "submit": ["confirmation"]}
ANALYST_S = 120             # the analyst's longest wait
SUBMIT_S = 570              # the submit agent: the form, the TAN mail, signing
SAFETY_S = 60                # an answer after expires_at scores 0


def site(task: dict, site_id: str) -> dict:
    found = next(s for s in task["access"] if s.get("siteId") == site_id)
    return {"url": found["url"], **(found.get("credentials") or {})}


def fill(template: str, **values) -> str:
    text = (PROMPTS / template).read_text()
    for key, value in values.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def site_task(name: str, task: dict, **extra) -> str:
    s = site(task, SITE_IDS[name])
    return fill(f"{name}.md", SITE_URL=s["url"], USERNAME=s.get("username", ""), PASSWORD=s.get("password", ""),
                BRIEF=task["brief"], **extra)


def submission_section(brief: str) -> str:
    """The brief's part about entering the bid (from its "Submission" heading); the whole brief if it has none."""
    match = re.search(r"^#+ *(Submission|Abgabe|Angebotsabgabe)\b.*$", brief, re.M | re.I)
    return brief[match.start():] if match else brief


def german(value) -> str:
    """1234.5 -> "1.234,50", the format the tender portal expects."""
    return f"{Decimal(str(value)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def bid_text(answer: dict) -> str:
    lines = [f"Tender reference: {answer['tender_reference']}", f"Decision: {answer['decision']}",
             f"Discount: {german(answer['discount_percent'])} %"]
    for p in answer["positions"]:
        lines.append(f"- Position {p['position']}: material {p['material_number']}, quantity {p['quantity']} Stück, "
                     f"unit price {german(p['unit_price'])} EUR, lead time {p['lead_time_weeks']} weeks, "
                     f"base unit price {german(p['base_unit_price'])}, surcharge per unit "
                     f"{german(p['surcharge_per_unit'])}, packaging per unit {german(p['packaging_per_unit'])}, "
                     f"rebate total {german(p['rebate_total'])}")
    lines.append(f"Total net: {german(answer['total_net'])} EUR")
    return "\n".join(lines)


async def research(task: dict, urls: dict, workdir: Path, deadline: float) -> dict:
    """Tender, specification and mailbox in parallel; the ERP as soon as the specification agent has saved the
    requirements with their catalogue groups. Each site gets its own share of the clock."""
    system = (PROMPTS / "system.md").read_text()
    agents: dict[str, Agent] = {}
    start = time.monotonic()

    async def run_site(name: str, prompt: str, until: float) -> dict:
        try:
            async with Agent(name, {name: urls[name]}, workdir) as agent:
                agents[name] = agent
                return await agent.run(system, prompt, min(until, deadline), required=REQUIRED[name])
        except Exception as error:  # noqa: BLE001  one site failing must not stop the others
            print(f"[{name}] failed: {error!r}", flush=True)
            return {"name": name, "facts": agents[name].facts if name in agents else None, "error": repr(error)}

    share = deadline - start
    tender = asyncio.create_task(run_site("tender", site_task("tender", task), start + 0.6 * share))
    spec = asyncio.create_task(run_site("spec", site_task("spec", task), start + 0.5 * share))
    mail = asyncio.create_task(run_site("mail", site_task("mail", task), start + 0.75 * share))

    def facts_of(name: str):
        return agents[name].facts if name in agents else None

    async def erp_when_requirements_known():
        while not spec.done() and time.monotonic() < deadline - 0.4 * share:
            f = facts_of("spec")
            if isinstance(f, dict) and f.get("requirements") and "atalog" in json.dumps(f, ensure_ascii=False):
                break
            await asyncio.sleep(5)
        print(f"[erp] starting at {time.monotonic() - start:.0f}s", flush=True)
        requirements = {"specification": facts_of("spec"), "tender": facts_of("tender")}
        return await run_site("erp", site_task("erp", task, REQUIREMENTS=json.dumps(requirements, ensure_ascii=False)),
                              deadline)

    erp = asyncio.create_task(erp_when_requirements_known())
    reports = await asyncio.gather(tender, spec, mail, erp)
    for r in reports:
        print(f"[{r['name']}] {r.get('seconds')}s, usage {r.get('usage')}, summary: {str(r.get('summary'))[:300]}",
              flush=True)
    return {r["name"]: r.get("facts") for r in reports}


async def submit(task: dict, urls: dict, workdir: Path, answer: dict, deadline: float) -> dict:
    t, m = site(task, "tender-portal"), site(task, "mail")
    prompt = fill("submit.md", TENDER_URL=t["url"], TENDER_USERNAME=t.get("username", ""),
                  TENDER_PASSWORD=t.get("password", ""), MAIL_URL=m["url"], MAIL_USERNAME=m.get("username", ""),
                  MAIL_PASSWORD=m.get("password", ""), BRIEF=submission_section(task["brief"]), BID=bid_text(answer))
    system = (PROMPTS / "system.md").read_text()
    async with Agent("submit", {"tender": urls["tender"], "mail": urls["mail"]}, workdir) as agent:
        return await agent.run(system, prompt, deadline, max_turns=120, required=REQUIRED["submit"],
                               progress_checks=False)


async def solve(task: dict, cdp_urls: list[str]) -> dict | None:
    """Research, analyse, price, submit. `task` is the run; `cdp_urls` its browsers, one per website.

    The answer goes on the tender portal ("site" submission), so this returns None; it prints the bid."""
    start = time.monotonic()
    end = start + min(contract.seconds_left(task), 20 * 60) - SAFETY_S
    urls = dict(zip(BROWSERS, cdp_urls))
    workdir = Path(tempfile.mkdtemp(prefix="rfq-run-"))
    print(f"Workdir {workdir}; {end - start:.0f}s on the clock", flush=True)

    facts = await research(task, urls, workdir, deadline=end - SUBMIT_S - ANALYST_S)
    (workdir / "facts.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2))
    print(f"Research done at {time.monotonic() - start:.0f}s", flush=True)

    try:
        decided = await analyst.analyse(task["brief"], facts,
                                        timeout=max(60, min(ANALYST_S, end - time.monotonic() - SUBMIT_S)))
        (workdir / "order.json").write_text(json.dumps(decided, ensure_ascii=False, indent=2))
        positions = (facts.get("tender") or {}).get("positions") if isinstance(facts.get("tender"), dict) else None
        answer = analyst.to_answer(decided, expected_positions=len(positions) if positions else None)
    except Exception as error:  # noqa: BLE001
        print(f"Analysis failed: {error!r}", flush=True)
        return None
    print(f"Bid at {time.monotonic() - start:.0f}s: {decided.get('decision')} because {decided.get('decision_notes')}",
          flush=True)
    for line in decided.get("lines") or []:
        print(f"  {line.get('position')}: {line.get('quantity_pieces')} pcs, material {line.get('material')} "
              f"({str(line.get('material_reasoning'))[:200]})", flush=True)
    print(bid_text(answer), flush=True)
    (workdir / "bid.json").write_text(json.dumps(answer, ensure_ascii=False, indent=2))

    report = await submit(task, urls, workdir, answer, deadline=end)
    print(f"Submit: {time.monotonic() - start:.0f}s, usage {report.get('usage')}, facts {report.get('facts')}, "
          f"summary: {report.get('summary')}", flush=True)
    return None


async def main() -> int:
    task, token = contract.load_run()
    sites = ", ".join(f"{s.get('siteId')} {s.get('url')}" for s in task["access"])
    print(f"Run {task.get('run_id') or '(local: the scouting logins, nothing counts)'}; websites: {sites}; "
          f"answer via {task['submission']['kind']}", flush=True)
    async with async_playwright() as playwright:
        cdp_urls, local_browsers = await browsers.cdp_endpoints(playwright, len(BROWSERS))
        try:
            answer = await solve(task, cdp_urls)
        finally:
            for browser in local_browsers:
                await browser.close()
    if answer is None:
        print("No answer to submit through the API (a site submission is entered on the website)", flush=True)
        return 0
    if not token:
        print("Your answer (locally nothing is submitted):", json.dumps(answer, ensure_ascii=False), flush=True)
        return 0
    status, body = contract.submit_answer(task, token, answer)
    print(f"Answer: HTTP {status} {body[:2000]}", flush=True)
    return 0 if status == 202 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
