"""Parallel site workers on a shared task board, then the sonnet-100 submit path.

1. One Claude Code worker per website (own sandbox browser). Each session does exactly one
   board task: what a priced bid still needs on that site, given what colleagues already found.
2. Finishing a task merges evidence and refreshes the board (resolver gaps become new tasks).
   ERP is not seeded; it starts when a Kataloggruppe appears. Workers re-read board.json.
3. When the resolver can price a bid, or the research budget ends, submit uses the reserved
   no-JS / goal / idle-kill path from sonnet-100.

Docs: $API/docs/run-contract.md, $API/docs/fair-play.md
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
from board import Board, assign_slots, slot_for_gap
from evidence import Evidence
from resolver import resolve

HERE = Path(__file__).resolve().parent
NODE_BIN = HERE / "node_modules" / ".bin"
MODEL = os.environ.get("RFQ_MODEL", "sonnet")
RESEARCH_S = 720
TASK_S = 180
SUBMIT_S = 420
SUBMIT_IDLE_S = 25
UNSAFE_JS = (
    "browser_run_code_unsafe", "browser_evaluate",
    "mcp__browser__browser_run_code_unsafe", "mcp__browser__browser_evaluate",
)
OPENROUTER_MODELS = {"OPUS": "~anthropic/claude-opus-latest", "SONNET": "~anthropic/claude-sonnet-latest",
                     "HAIKU": "~anthropic/claude-haiku-latest"}

TASK = """You research one assigned website for a sales engineer. Work in the browser (the `browser` tools).
Your result is evidence, not conclusions. Code merges your records with your colleagues' and prices the bid.

<brief>
{brief}
</brief>

Your assigned website (stay on this one):
{site}

What is already known (read `{board}` if you need the full records; do not re-collect these unless the
task says to replace a record):
{known}

Your only task — do this and stop:
{question}

Write dates in ISO 8601 with timezone after applying the site's format hints; numbers after applying locale hints.
Keep references exactly as written (also look-alikes). Get past cookie walls, popups and gates.
Do not submit or sign anything. Write the records this task found to evidence.json in the working directory
(same shape as the structured output). Return that evidence as the structured output."""

SUBMIT = """Goal (check this after every few actions; stop the moment it is true): the tender portal itself
shows that the bid was entered, signed and accepted. Reply with that confirmation text and nothing else.

Each website has its own browser tool. Use the tender portal and the webmail. Do not run page JavaScript.
Use snapshot, click, type, fill_form and select_option. Never write a click loop. If a control does not
respond, snapshot again and try another control — do not repeat the same action.

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
everything else is entered, read the newest TAN mail, enter it within five minutes and confirm the binding-offer
dialog the brief describes. When the goal is met, stop."""


def one_site_text(site: dict) -> str:
    c = site.get("credentials") or {}
    return (f"- `{site.get('siteId')}`: {site['url']} · username `{c.get('username', '')}` · password "
            f"`{c.get('password', '')}`")


def sites_text(task: dict) -> str:
    return "\n".join(one_site_text(s) for s in task["access"])


def submission_section(brief: str) -> str:
    match = re.search(r"^#+ *(Submission|Abgabe|Angebotsabgabe)\b.*$", brief, re.M | re.I)
    return brief[match.start():] if match else brief


def german(value) -> str:
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


def known_text(evidence: Evidence | None) -> str:
    if evidence is None:
        return "(nothing yet)"
    groups = sorted({s.catalog_group for s in evidence.erp_searches if s.catalog_group})
    reqs = []
    for rev in evidence.revisions:
        reqs.extend(r.id for r in rev.requirements if r.id)
    return (
        f"{len(evidence.positions)} positions, {len(evidence.qa)} Q&A, {len(evidence.revisions)} revisions, "
        f"{len(evidence.materials)} materials, {len(evidence.messages)} messages"
        + (f"; Kataloggruppen already searched: {', '.join(groups)}" if groups else "")
        + (f"; requirement ids: {', '.join(reqs[:20])}" if reqs else "")
    )


def claude_bin() -> str:
    local = NODE_BIN / "claude"
    found = str(local) if local.exists() else shutil.which("claude")
    if not found:
        raise SystemExit(f"claude not found: run `npm ci` in {HERE}")
    return found


def claude_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
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
    env["ANTHROPIC_AUTH_TOKEN"] = env["OPENROUTER_API_KEY"]
    env["ANTHROPIC_CUSTOM_HEADERS"] = f"x-provider-key: {env['OPENROUTER_API_KEY']}"
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
    for role, model in OPENROUTER_MODELS.items():
        env.setdefault(f"ANTHROPIC_DEFAULT_{role}_MODEL", model)
    return env


def check_openrouter_key(env: dict) -> None:
    raw = os.environ.get("OPENROUTER_API_KEY") or ""
    key = env["OPENROUTER_API_KEY"]
    print(f"OpenRouter key: starts {key[:9]!r}, {len(key)} characters"
          + (", had surrounding whitespace (stripped)" if raw != key else ""), flush=True)
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


def last_result(text: str) -> dict | None:
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


async def preflight() -> None:
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


def submit_stalled(result: dict) -> bool:
    text = str(result.get("result") or "").lower()
    return any(marker in text for marker in (
        "initializeserver", "browser tools never connected", "never connected",
        "browser server never finished",
    ))


def submit_succeeded(result: dict) -> bool:
    if result.get("is_error") or submit_stalled(result):
        return False
    text = str(result.get("result") or "").lower()
    if any(marker in text for marker in ("couldn't submit", "could not submit", "did not submit")):
        return False
    signed = "verbindlich" in text and any(word in text for word in ("abgegeben", "bestätigt", "angenommen"))
    confirmed = "confirmation" in text and any(word in text for word in ("submitted", "signed", "accepted"))
    return signed or confirmed


def wait_budget(now: float, started: float, last_progress: float, limit_s: float,
                idle_s: float) -> tuple[float, str | None]:
    left_deadline = started + limit_s - now
    left_idle = last_progress + idle_s - now
    if left_deadline <= 0:
        return 0.0, "deadline"
    if left_idle <= 0:
        return 0.0, "idle"
    return min(left_deadline, left_idle), None


async def claude(name: str, prompt: str, browser_cdp: dict[str, str], workdir: Path, schema: dict | None,
                 *, extra_dirs: list[Path] | None = None, allow_js: bool = True, allow_write: bool = False,
                 limit_s: float | None = None, idle_s: float | None = None,
                 mcp_extra: list[str] | None = None) -> dict:
    d = workdir / name
    (d / "downloads").mkdir(parents=True, exist_ok=True)
    args_base = ["--output-dir", str(d / "downloads"), "--image-responses", "omit"]
    if mcp_extra:
        args_base += mcp_extra
    mcp = {"mcpServers": {name_: {"command": str(NODE_BIN / "playwright-mcp"), "args": [
        "--cdp-endpoint", url, *args_base]} for name_, url in browser_cdp.items()}}
    (d / "mcp.json").write_text(json.dumps(mcp))
    disallowed = ["Bash", "Edit", "WebFetch", "WebSearch", "Task"]
    allowed = [f"mcp__{n}" for n in browser_cdp] + ["Read"]
    if allow_write:
        allowed.append("Write")
    else:
        disallowed.append("Write")
    if not allow_js:
        disallowed += list(UNSAFE_JS)
    cmd = [claude_bin(), "-p", prompt, "--model", MODEL, "--mcp-config", str(d / "mcp.json"), "--strict-mcp-config",
           "--setting-sources", "user", "--allowedTools", *allowed, "--disallowedTools", *disallowed,
           "--add-dir", str(d), "--output-format", "stream-json", "--verbose", "--no-session-persistence"]
    for extra in extra_dirs or []:
        cmd += ["--add-dir", str(extra)]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    started = last_progress = time.monotonic()
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=d, env=claude_env(), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, limit=2**26)
    result, tools, why = {}, 0, None
    with (d / "session.jsonl").open("w") as log:
        while True:
            if limit_s is not None or idle_s is not None:
                left, why = wait_budget(time.monotonic(), started, last_progress,
                                        limit_s if limit_s is not None else 1e9,
                                        idle_s if idle_s is not None else 1e9)
                if why:
                    proc.kill()
                    break
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=left)
                except asyncio.TimeoutError:
                    _, why = wait_budget(time.monotonic(), started, last_progress,
                                         limit_s if limit_s is not None else 1e9,
                                         idle_s if idle_s is not None else 1e9)
                    why = why or "idle"
                    proc.kill()
                    break
            else:
                raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace")
            log.write(line)
            try:
                event = json.loads(line)
            except ValueError:
                print(f"[{name}] {line.rstrip()[:300]}", flush=True)
                continue
            last_progress = time.monotonic()
            if event.get("type") == "assistant":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        tools += 1
                        print(f"[{name}] {time.monotonic() - started:5.0f}s {block['name']}", flush=True)
            elif event.get("type") == "result":
                result = event
    await proc.wait()
    if why:
        result = {"is_error": True, "result": f"{why} after {time.monotonic() - started:.0f}s",
                  "structured_output": result.get("structured_output")}
        print(f"[{name}] {why} — killed after {time.monotonic() - started:.0f}s", flush=True)
    print(f"[{name}] done in {time.monotonic() - started:.0f}s, {tools} tool calls, {result.get('num_turns')} turns, "
          f"${result.get('total_cost_usd') or 0:.2f}" + (" (error)" if result.get("is_error") else ""), flush=True)
    return result


def try_evidence(result: dict) -> Evidence | None:
    try:
        return evidence_of(result)
    except (SystemExit, ValueError, Exception):  # noqa: BLE001
        return None


def load_checkpoint(path: Path) -> Evidence | None:
    if not path.is_file() or path.stat().st_size < 2:
        return None
    try:
        return Evidence.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def evidence_from(result: dict, session_dir: Path) -> Evidence | None:
    found = try_evidence(result)
    if found is not None:
        return found
    checkpoint = load_checkpoint(session_dir / "evidence.json")
    if checkpoint is not None:
        print(f"[{session_dir.name}] using evidence.json checkpoint "
              f"({len(checkpoint.positions)} positions, {len(checkpoint.messages)} messages)", flush=True)
    return checkpoint


def evidence_of(result: dict) -> Evidence:
    data = result.get("structured_output")
    if data is None:
        text = str(result.get("result") or "")
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise SystemExit(
                ("Claude session failed: " + text[:800])
                if result.get("is_error")
                else ("Claude returned no evidence JSON: " + text[:800]))
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            raise SystemExit("Claude returned no evidence JSON: " + text[:800])
    return Evidence.model_validate(data)


def submit_cdp_ready(url: str) -> str | None:
    try:
        return browsers.check_cdp(url)
    except SystemExit as error:
        print(f"Submit CDP not answering: {error}", flush=True)
        return None


def submit_browsers(slots: list[dict]) -> dict[str, str]:
    """Tender portal and webmail: that is where the bid and the TAN live."""
    chosen = []
    for site in ("tender-portal", "mail"):
        slot = slot_for_gap(slots, site)
        if slot is not None and slot not in chosen:
            chosen.append(slot)
    if not chosen:
        chosen = list(slots)
    return {s["name"]: s["cdp"] for s in chosen}


async def run_task(name: str, prompt: str, cdp: str, workdir: Path, schema: dict,
                   limit_s: float) -> Evidence | None:
    result = await claude(name, prompt, {"browser": cdp}, workdir, schema, extra_dirs=[workdir],
                          allow_js=True, allow_write=True, limit_s=limit_s, idle_s=90)
    return evidence_from(result, workdir / name)


async def worker(slot: dict, board: Board, task: dict, workdir: Path, schema: dict,
                 board_path: Path, until: float) -> None:
    n = 0
    while time.monotonic() < until and not board.is_complete():
        claimed = await board.claim(slot)
        if claimed is None:
            if not board.has_work():
                return
            await asyncio.sleep(2)
            continue
        n += 1
        print(f"[{slot['name']}] task {n}: {claimed.question}", flush=True)
        prompt = TASK.format(brief=task["brief"], site=one_site_text(slot["site"]),
                             board=str(board_path), known=known_text(board.evidence),
                             question=claimed.question)
        left = until - time.monotonic()
        if left < 20:
            await board.absorb(claimed, None, board_path)
            return
        try:
            part = await run_task(f"{slot['name']}-{n}", prompt, slot["cdp"], workdir, schema,
                                  limit_s=min(TASK_S, left))
        except Exception as error:  # noqa: BLE001
            print(f"[{slot['name']}] task failed: {error!r}", flush=True)
            part = None
        await board.absorb(claimed, part, board_path)
        print(f"board: {known_text(board.evidence)}; {len(board.pending)} open, "
              f"{len(board.inflight)} inflight", flush=True)


async def enter_bid(prompt: str, browser_cdp: dict[str, str], workdir: Path) -> dict:
    extra = ["--timeout-action", "15000"]

    async def once(name: str) -> dict:
        return await claude(name, prompt, browser_cdp, workdir, None, allow_js=False,
                            limit_s=SUBMIT_S, idle_s=SUBMIT_IDLE_S, mcp_extra=extra)

    result = await once("submit")
    if submit_succeeded(result):
        return result
    why = "MCP did not attach" if submit_stalled(result) else "goal not reached"
    print(f"Submit {why}; retrying once", flush=True)
    return await once("submit-retry")


async def solve(task: dict, cdp_urls: list[str]) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="rfq-par-"))
    started = time.monotonic()
    schema = Evidence.model_json_schema()
    slots = assign_slots(task["access"], cdp_urls)
    if not slots:
        raise SystemExit("No website/browser pair")
    board = Board(slots)
    board_path = workdir / "board.json"
    board.persist(board_path)
    print("Workers: " + ", ".join(s["name"] for s in slots)
          + f"; seed tasks {len(board.pending)}", flush=True)

    wall = contract.seconds_left(task)
    research_s = RESEARCH_S if wall == float("inf") else min(RESEARCH_S, max(90.0, wall - SUBMIT_S - 30))
    until = time.monotonic() + research_s
    await asyncio.gather(*(worker(s, board, task, workdir, schema, board_path, until) for s in slots))

    if board.evidence is None:
        raise SystemExit("No evidence from any worker")
    resolution = resolve(board.evidence)
    print("Trace:\n  " + "\n  ".join(resolution.trace), flush=True)
    print("Warnings: " + ("; ".join(resolution.warnings) or "none"), flush=True)
    if resolution.answer is None:
        print("No answer: nothing could be priced; gaps: " + "; ".join(g.question for g in resolution.gaps),
              flush=True)
        return
    bid = bid_text(resolution.answer, resolution.omitted)
    print(f"Bid at {time.monotonic() - started:.0f}s:\n{bid}", flush=True)

    submit_cdp = submit_browsers(slots)
    for name, url in submit_cdp.items():
        ready = submit_cdp_ready(url)
        print(f"Submit {name} {url}" + (f" ({ready})" if ready else " (CDP check failed)"), flush=True)
    result = await enter_bid(SUBMIT.format(sites=sites_text(task),
                                           submission=submission_section(task["brief"]), bid=bid),
                             submit_cdp, workdir)
    print(f"Submission at {time.monotonic() - started:.0f}s: {result.get('result')}", flush=True)


async def main() -> int:
    task, _ = contract.load_run()
    print(f"Run {task.get('run_id') or '(local: the scouting logins, nothing counts)'}; websites: "
          + ", ".join(f"{s.get('siteId')} {s.get('url')}" for s in task["access"]), flush=True)
    await preflight()
    async with async_playwright() as playwright:
        urls, local = await browsers.cdp_endpoints(
            playwright, min(4, max(1, len(task.get("access") or []))))
        try:
            await solve(task, urls)
        finally:
            for browser in local:
                await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
