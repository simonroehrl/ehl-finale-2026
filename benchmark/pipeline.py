"""Benchmark: the three-stage pipeline on public-1, locally, with Claude Code as retrieval and submission.

    retrieval (claude -p + Playwright MCP) -> evidence.json -> bid_engine.resolve -> bid -> submission (claude -p)

Run from the repo root with the scouting logins loaded:
    eval "$(atira scout --challenge rfq --env)"
    my-agent/.venv/bin/python benchmark/pipeline.py [--no-submit] [--model opus]

Everything it writes (benchmark/out/) holds real public-1 values: never copy any of it into a package.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "bid_engine"), str(ROOT / "my-agent")]

from playwright.async_api import async_playwright  # noqa: E402

import browsers  # noqa: E402
import run as contract  # noqa: E402
from agent import german, submission_section  # noqa: E402
from evidence import Evidence  # noqa: E402
from resolver import resolve  # noqa: E402

PLAYWRIGHT_MCP = ROOT / "my-agent" / "node_modules" / ".bin" / "playwright-mcp"
OUT = ROOT / "benchmark" / "out"


def sites_text(task: dict) -> str:
    rows = []
    for s in task["access"]:
        c = s.get("credentials") or {}
        rows.append(f"- `{s['siteId']}`: {s['url']} · username `{c.get('username', '')}` · password "
                    f"`{c.get('password', '')}`")
    return "\n".join(rows)


RETRIEVAL = """You research a public tender for a sales engineer. Work in the browser (the `browser` tools; use tabs
for the different websites). Your result is evidence, not conclusions: the structured output you return is checked
and priced by code that applies the brief's rules itself.

<brief>
{brief}
</brief>

Websites and logins:
{sites}

Record, on every website, everything the rules need, including the records that will turn out not to apply:
- tender portal: every position of the bill of quantities, every bidder Q&A answer on every page (withdrawals,
  quantity changes, technical amendments with their revision, key assignments, setup fees);
- specification: every revision with its marking as shown in the revision itself (current only if marked so), and
  for each revision you can open, its requirements with all attributes, Kataloggruppe, pieces per set;
- ERP: for each requirement, search its Kataloggruppe (narrowed to at most 20 results), record the search with every
  listed material number, and every candidate with status, attributes, price conditions, stock, lead times, carton
  size, masses, metal index;
- mailbox (inbox and archive, every page): every message that concerns this tender or a look-alike reference, with
  sender, date, subject, reference and the values it states.

Write dates in ISO 8601 with timezone after applying the site's format hints; numbers after applying locale hints.
Keep references exactly as written (also look-alikes). Websites have cookie walls, popups, gates, timeouts and
outdated documents shown first: get past them. Do not submit or sign anything. {extra}
Return the evidence as the structured output."""

GAPS = """Your colleagues' evidence is incomplete. Answer exactly these follow-up questions in the browser and return the
evidence you find for them (same structure; records you return replace earlier ones with the same id):

{gaps}"""

SUBMIT = """Enter and sign this bid on the tender portal. Use the browser tools (tabs for the tender portal and the
webmail).

Websites and logins:
{sites}

How to submit, from the task brief:
<submission>
{submission}
</submission>

The bid, already calculated; enter exactly these values:
<bid>
{bid}
</bid>

Go straight to the bid submission. Never use a back button: it clears later steps. Request the TAN only when
everything else is entered, read the newest TAN mail, enter it within five minutes and confirm "Angebot verbindlich
abgeben" in the browser dialog. Reply with the confirmation the portal shows."""


def bid_text(answer: dict, omitted: list[str]) -> str:
    lines = [f"Tender reference: {answer['tender_reference']}", f"Decision: {answer['decision']}",
             f"Discount: {german(answer['discount_percent'])} %"]
    for p in answer["positions"]:
        lines.append(
            f"- Position {p['position']}: material {p['material_number']}, quantity {p['quantity']} Stück, unit "
            f"price {german(p['unit_price'])} EUR, lead time {p['lead_time_weeks']} weeks, base unit price "
            f"{german(p['base_unit_price'])}, surcharge per unit {german(p['surcharge_per_unit'])}, packaging per "
            f"unit {german(p['packaging_per_unit'])}, rebate total {german(p['rebate_total'])}")
    for p in omitted:
        lines.append(f"- Position {p}: omit it (Position auslassen)")
    lines.append(f"Total net: {german(answer['total_net'])} EUR")
    return "\n".join(lines)


async def claude(name: str, prompt: str, cdp_url: str, workdir: Path, model: str, schema: dict | None) -> dict:
    """One headless Claude Code session on the browser at `cdp_url`. Returns the result event."""
    d = workdir / name
    (d / "downloads").mkdir(parents=True, exist_ok=True)
    mcp = {"mcpServers": {"browser": {"command": str(PLAYWRIGHT_MCP), "args": [
        "--cdp-endpoint", cdp_url, "--output-dir", str(d / "downloads"), "--image-responses", "omit"]}}}
    (d / "mcp.json").write_text(json.dumps(mcp))
    cmd = ["claude", "-p", prompt, "--model", model, "--mcp-config", str(d / "mcp.json"), "--strict-mcp-config",
           "--setting-sources", "user", "--allowedTools", "mcp__browser", "Read",
           "--disallowedTools", "Bash", "Write", "Edit", "WebFetch", "WebSearch",
           "--add-dir", str(d), "--output-format", "stream-json", "--verbose", "--no-session-persistence"]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=d, env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, limit=2**26)
    result, tools = {}, 0
    with (d / "session.jsonl").open("w") as log:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace")
            log.write(line)
            try:
                event = json.loads(line)
            except ValueError:
                print(f"[{name}] {line.rstrip()}", flush=True)
                continue
            if event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        tools += 1
                        print(f"[{name}] {time.monotonic() - started:5.0f}s {block['name']}", flush=True)
            elif event.get("type") == "result":
                result = event
    await proc.wait()
    print(f"[{name}] done in {time.monotonic() - started:.0f}s, {tools} tool calls, {result.get('num_turns')} turns, "
          f"${result.get('total_cost_usd') or 0:.2f}", flush=True)
    return result


def evidence_of(result: dict) -> dict:
    data = result.get("structured_output")
    if data is None:  # some versions put it in `result` as text
        text = result.get("result") or ""
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    return data


async def main(args):
    task, _ = contract.load_run()
    run_dir = OUT / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)
    workdir = Path(tempfile.mkdtemp(prefix="rfq-bench-"))  # outside the repo: no CLAUDE.md, no hooks
    print(f"Writing to {run_dir} (sessions in {workdir})", flush=True)
    schema = Evidence.model_json_schema()
    timings = {}

    async with async_playwright() as playwright:
        urls, local = await browsers.cdp_endpoints(playwright, 1)
        try:
            t = time.monotonic()
            prompt = RETRIEVAL.format(brief=task["brief"], sites=sites_text(task), extra="")
            raw = evidence_of(await claude("retrieval", prompt, urls[0], workdir, args.model, schema))
            evidence = Evidence.model_validate(raw)
            timings["retrieval_s"] = round(time.monotonic() - t)
            (run_dir / "evidence.json").write_text(evidence.model_dump_json(indent=2))

            resolution = resolve(evidence)
            for round_ in range(args.gap_rounds):
                if not resolution.gaps:
                    break
                print(f"{len(resolution.gaps)} gaps: " + "; ".join(g.question for g in resolution.gaps), flush=True)
                t = time.monotonic()
                questions = "\n".join(f"- [{g.site}] {g.question}" for g in resolution.gaps)
                prompt = RETRIEVAL.format(brief=task["brief"], sites=sites_text(task),
                                          extra=GAPS.format(gaps=questions))
                extra = Evidence.model_validate(evidence_of(
                    await claude(f"gaps{round_ + 1}", prompt, urls[0], workdir, args.model, schema)))
                from evidence import merge
                evidence = merge(evidence.case, evidence, extra)
                timings[f"gaps{round_ + 1}_s"] = round(time.monotonic() - t)
                (run_dir / "evidence.json").write_text(evidence.model_dump_json(indent=2))
                resolution = resolve(evidence)

            report = {"answer": resolution.answer, "omitted": resolution.omitted,
                      "gaps": [vars(g) for g in resolution.gaps], "trace": resolution.trace,
                      "warnings": resolution.warnings, "timings": timings}
            (run_dir / "resolution.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            print("Trace:\n  " + "\n  ".join(resolution.trace), flush=True)
            print("Warnings: " + ("; ".join(resolution.warnings) or "none"), flush=True)
            if resolution.answer is None:
                print("No answer: nothing could be priced", flush=True)
                return
            print("Bid:\n" + bid_text(resolution.answer, resolution.omitted), flush=True)

            if not args.no_submit:
                t = time.monotonic()
                prompt = SUBMIT.format(sites=sites_text(task), submission=submission_section(task["brief"]),
                                       bid=bid_text(resolution.answer, resolution.omitted))
                result = await claude("submit", prompt, urls[0], workdir, args.model, None)
                timings["submit_s"] = round(time.monotonic() - t)
                report["submission"] = result.get("result")
                (run_dir / "resolution.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2, default=str))
                print(f"Submission: {result.get('result')}", flush=True)
            print(f"Timings: {timings}", flush=True)
        finally:
            for b in local:
                await b.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="opus")
    parser.add_argument("--no-submit", action="store_true")
    parser.add_argument("--gap-rounds", type=int, default=1)
    asyncio.run(main(parser.parse_args()))
