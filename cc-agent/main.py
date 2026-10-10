"""The entry point: three stages, the models at the ends and code in the middle.

1. Retrieval: a Claude Code session (`claude -p`, the unmodified CLI) works the run's websites through Playwright
   MCP and returns the evidence it found, validated against the Evidence schema (evidence.py). It records evidence,
   not conclusions: every revision, Q&A answer, ERP candidate and mail, also the ones that turn out not to apply.
2. Rules: resolver.py applies the brief's rules to the evidence and pricing.py does the arithmetic. If evidence is
   missing, the resolver phrases follow-up questions and one more retrieval session answers them.
3. Submission: a second Claude Code session enters the bid on the tender portal and signs it with the TAN.

In an Atira run, Claude Code reaches OpenRouter through the run's proxy with OPENROUTER_API_KEY (a team secret);
on your machine it uses your own Claude login. Docs: $API/docs/run-contract.md, $API/docs/fair-play.md
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from playwright.async_api import async_playwright

import browsers
import run as contract
from evidence import Evidence, merge
from resolver import resolve

HERE = Path(__file__).resolve().parent
NODE_BIN = HERE / "node_modules" / ".bin"
MODEL = os.environ.get("RFQ_MODEL", "opus")
GAP_ROUNDS = 1
# Claude Code's model classes on OpenRouter (its Claude Code guide): Anthropic's own models.
OPENROUTER_MODELS = {"OPUS": "~anthropic/claude-opus-latest", "SONNET": "~anthropic/claude-sonnet-latest",
                     "HAIKU": "~anthropic/claude-haiku-latest"}

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


def sites_text(task: dict) -> str:
    rows = []
    for s in task["access"]:
        c = s.get("credentials") or {}
        rows.append(f"- `{s.get('siteId')}`: {s['url']} · username `{c.get('username', '')}` · password "
                    f"`{c.get('password', '')}`")
    return "\n".join(rows)


def submission_section(brief: str) -> str:
    """The brief's part about entering the bid (from its "Submission" heading); the whole brief if it has none."""
    match = re.search(r"^#+ *(Submission|Abgabe|Angebotsabgabe)\b.*$", brief, re.M | re.I)
    return brief[match.start():] if match else brief


def german(value) -> str:
    """1234.5 -> "1.234,50", the format the tender portal expects."""
    return f"{Decimal(str(value)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


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


def claude_bin() -> str:
    local = NODE_BIN / "claude"
    found = str(local) if local.exists() else shutil.which("claude")
    if not found:
        raise SystemExit(f"claude not found: run `npm ci` in {HERE}")
    return found


def claude_env() -> dict:
    """In an Atira run, Claude Code goes through the run's proxy to OpenRouter with OPENROUTER_API_KEY (a team
    secret). On your machine it always uses your own Claude login (OAuth), never OpenRouter."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}  # also starts inside a Claude Code session
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"  # the sandbox reaches only the platform and the websites
    env["DISABLE_AUTOUPDATER"] = "1"
    in_run = bool(env.get("CHALLENGE_RUN_TOKEN"))
    if not in_run:
        for name in ("OPENROUTER_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"):
            env.pop(name, None)
        return env
    env["OPENROUTER_API_KEY"] = (env.get("OPENROUTER_API_KEY") or "").strip()
    if not env["OPENROUTER_API_KEY"]:
        raise SystemExit("OPENROUTER_API_KEY is missing: add it as a team secret on the team page")
    env["ANTHROPIC_BASE_URL"] = env["LLM_PROXY_URL"].rstrip("/") + "/openrouter"
    env["ANTHROPIC_AUTH_TOKEN"] = env["OPENROUTER_API_KEY"]  # sent as Authorization: Bearer
    # Also as x-provider-key, which the run's proxy moves into the provider's own header. No ANTHROPIC_API_KEY at
    # all: an empty one goes out as an empty x-api-key, and the proxy reads that as "no key".
    env["ANTHROPIC_CUSTOM_HEADERS"] = f"x-provider-key: {env['OPENROUTER_API_KEY']}"
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    for role, model in OPENROUTER_MODELS.items():
        env.setdefault(f"ANTHROPIC_DEFAULT_{role}_MODEL", model)
    return env


def check_openrouter_key(env: dict) -> None:
    """In a run: does OpenRouter accept the key at all? Logs only the key's shape, never the key."""
    raw = os.environ.get("OPENROUTER_API_KEY") or ""
    key = env["OPENROUTER_API_KEY"]
    print(f"OpenRouter key: starts {key[:9]!r}, {len(key)} characters"
          + (", had surrounding whitespace (stripped)" if raw != key else "")
          + (", contains whitespace inside" if any(c.isspace() for c in key) else ""), flush=True)
    url = env["ANTHROPIC_BASE_URL"] + "/v1/key"
    try:
        with urlopen(Request(url, headers={"Authorization": f"Bearer {key}", "User-Agent": "atira-rfq-cc"}),
                     timeout=30) as response:
            info = json.load(response).get("data", {})
        print(f"OpenRouter accepts the key: label {info.get('label')!r}, limit {info.get('limit')}, "
              f"usage {info.get('usage')}", flush=True)
    except HTTPError as error:
        raise SystemExit(f"OpenRouter rejects the key: HTTP {error.code} "
                         f"{error.read().decode('utf-8', errors='replace')[:300]}")
    except (URLError, OSError) as error:
        print(f"OpenRouter key check not possible: {error!r}", flush=True)


async def preflight() -> None:
    """One tiny request, so a login or proxy problem shows in seconds instead of after the research."""
    env = claude_env()
    if os.environ.get("CHALLENGE_RUN_TOKEN"):
        check_openrouter_key(env)
    proc = await asyncio.create_subprocess_exec(
        claude_bin(), "-p", "Reply with exactly: ok", "--model", "haiku", "--output-format", "json",
        "--no-session-persistence", env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=240)
    except asyncio.TimeoutError:
        proc.kill()
        raise SystemExit("Preflight: claude did not answer within 240 s")
    text = out.decode("utf-8", errors="replace")
    result = last_result(text)
    if result is None:
        raise SystemExit(f"Preflight failed, no result from claude: {text[-800:]}")
    if result.get("is_error") or "ok" not in str(result.get("result", "")).lower():
        raise SystemExit(f"Preflight failed: {str(result.get('result'))[:800]}")
    print(f"Preflight ok: claude answered in {result.get('duration_ms')} ms", flush=True)


def last_result(text: str) -> dict | None:
    """The result object in claude's output, which may hold other lines (warnings, events) around it."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and ("result" in event or event.get("type") == "result"):
            return event
    try:
        return json.loads(text[text.find("{"): text.rfind("}") + 1])
    except ValueError:
        return None


async def claude(name: str, prompt: str, cdp_url: str, workdir: Path, schema: dict | None) -> dict:
    """One headless Claude Code session on the browser at `cdp_url`; only browser and file-reading tools."""
    d = workdir / name
    (d / "downloads").mkdir(parents=True, exist_ok=True)
    mcp = {"mcpServers": {"browser": {"command": str(NODE_BIN / "playwright-mcp"), "args": [
        "--cdp-endpoint", cdp_url, "--output-dir", str(d / "downloads"), "--image-responses", "omit"]}}}
    (d / "mcp.json").write_text(json.dumps(mcp))
    cmd = [claude_bin(), "-p", prompt, "--model", MODEL, "--mcp-config", str(d / "mcp.json"), "--strict-mcp-config",
           "--setting-sources", "user", "--allowedTools", "mcp__browser", "Read",
           "--disallowedTools", "Bash", "Write", "Edit", "WebFetch", "WebSearch", "Task",
           "--add-dir", str(d), "--output-format", "stream-json", "--verbose", "--no-session-persistence"]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=d, env=claude_env(), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, limit=2**26)
    result, tools = {}, 0
    with (d / "session.jsonl").open("w") as log:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace")
            log.write(line)
            try:
                event = json.loads(line)
            except ValueError:
                print(f"[{name}] {line.rstrip()[:300]}", flush=True)
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
          f"${result.get('total_cost_usd') or 0:.2f}" + (" (error)" if result.get("is_error") else ""), flush=True)
    return result


def evidence_of(result: dict) -> Evidence:
    data = result.get("structured_output")
    if data is None:
        text = result.get("result") or ""
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    return Evidence.model_validate(data)


async def solve(task: dict, cdp_url: str) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="rfq-run-"))
    started = time.monotonic()
    schema = Evidence.model_json_schema()
    sites = sites_text(task)

    evidence = evidence_of(await claude(
        "retrieval", RETRIEVAL.format(brief=task["brief"], sites=sites, extra=""), cdp_url, workdir, schema))
    resolution = resolve(evidence)
    for round_ in range(GAP_ROUNDS):
        if not resolution.gaps:
            break
        print(f"{len(resolution.gaps)} gaps: " + "; ".join(g.question for g in resolution.gaps), flush=True)
        questions = "\n".join(f"- [{g.site}] {g.question}" for g in resolution.gaps)
        extra = evidence_of(await claude(
            f"gaps{round_ + 1}", RETRIEVAL.format(brief=task["brief"], sites=sites, extra=GAPS.format(gaps=questions)),
            cdp_url, workdir, schema))
        evidence = merge(evidence.case, evidence, extra)
        resolution = resolve(evidence)

    print("Trace:\n  " + "\n  ".join(resolution.trace), flush=True)
    print("Warnings: " + ("; ".join(resolution.warnings) or "none"), flush=True)
    if resolution.answer is None:
        print("No answer: nothing could be priced; gaps: " + "; ".join(g.question for g in resolution.gaps),
              flush=True)
        return
    bid = bid_text(resolution.answer, resolution.omitted)
    print(f"Bid at {time.monotonic() - started:.0f}s:\n{bid}", flush=True)

    result = await claude("submit", SUBMIT.format(sites=sites, submission=submission_section(task["brief"]),
                                                  bid=bid), cdp_url, workdir, None)
    print(f"Submission at {time.monotonic() - started:.0f}s: {result.get('result')}", flush=True)


async def main() -> int:
    task, _ = contract.load_run()
    print(f"Run {task.get('run_id') or '(local: the scouting logins, nothing counts)'}; websites: "
          + ", ".join(f"{s.get('siteId')} {s.get('url')}" for s in task["access"]), flush=True)
    await preflight()
    async with async_playwright() as playwright:
        urls, local = await browsers.cdp_endpoints(playwright, 1)
        try:
            await solve(task, urls[0])
        finally:
            for browser in local:
                await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
