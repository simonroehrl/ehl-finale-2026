"""The entry point: three stages, the models at the ends and code in the middle.

1. Retrieval: a Claude Code session (`claude -p`, the unmodified CLI) works the run's websites through Playwright
   MCP and returns the evidence it found, validated against the Evidence schema (evidence.py). It records evidence,
   not conclusions: every revision, Q&A answer, ERP candidate and mail, also the ones that turn out not to apply.
2. Rules: resolver.py applies the brief's rules to the evidence and pricing.py does the arithmetic. If evidence is
   missing, the resolver phrases follow-up questions and one more retrieval session answers them.
3. Submission: a second Claude Code session enters the bid on a reserved browser and signs it with the TAN.
   The goal is the portal confirmation. Code watches the session: no page JavaScript (that is how the last
   scored run looped), a time box, and a kill if one tool stops producing output. If the goal is not met,
   one retry.

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
MODEL = os.environ.get("RFQ_MODEL", "sonnet")
GAP_ROUNDS = 1
SUBMIT_S = 420
SUBMIT_IDLE_S = 25
UNSAFE_JS = (
    "browser_run_code_unsafe", "browser_evaluate",
    "mcp__browser__browser_run_code_unsafe", "mcp__browser__browser_evaluate",
)
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
outdated documents shown first: get past them. Do not submit or sign anything. After each website, write the
evidence gathered so far to evidence.json in the working directory (same shape as the structured output). Update
that file; do not start over. If the session is cut off, that file is what we price. {extra}
Return the evidence as the structured output."""

GAPS = """Your colleagues' evidence is incomplete. Answer exactly these follow-up questions in the browser and return the
evidence you find for them (same structure; records you return replace earlier ones with the same id):

{gaps}"""

SUBMIT = """Goal (check this after every few actions; stop the moment it is true): the tender portal itself
shows that the bid was entered, signed and accepted. Reply with that confirmation text and nothing else.

Use the browser tools (tabs for the tender portal and the webmail). This browser has not been used yet: open
those sites yourself. Do not run page JavaScript. Use snapshot, click, type, fill_form and select_option.
Never write a click loop. If a control does not respond, snapshot again and try another control — do not
repeat the same action.

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


def pick_cdp(urls: list[str]) -> tuple[str, str]:
    """Research on browser 1; submit on browser 2 when the sandbox gave us one, else the same URL."""
    if not urls:
        raise SystemExit("No browser CDP URL")
    return urls[0], urls[1] if len(urls) > 1 else urls[0]


def submit_stalled(result: dict) -> bool:
    """True when submit never reached the portal (Playwright MCP failed to attach)."""
    text = str(result.get("result") or "").lower()
    return any(marker in text for marker in (
        "initializeserver",
        "browser tools never connected",
        "never connected",
        "browser server never finished",
    ))


def submit_succeeded(result: dict) -> bool:
    """True when the session reports the portal confirmed a signed bid (the submit goal)."""
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
    """Seconds until the next kill during submit, and why if we should kill now."""
    left_deadline = started + limit_s - now
    left_idle = last_progress + idle_s - now
    if left_deadline <= 0:
        return 0.0, "deadline"
    if left_idle <= 0:
        return 0.0, "idle"
    return min(left_deadline, left_idle), None


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


async def claude(name: str, prompt: str, cdp_url: str, workdir: Path, schema: dict | None,
                 *, mcp_extra: list[str] | None = None, allow_js: bool = True, allow_write: bool = False,
                 limit_s: float | None = None, idle_s: float | None = None) -> dict:
    """One headless Claude Code session on the browser at `cdp_url`; only browser and file-reading tools.

    Retrieval leaves `limit_s` unset and reads until Claude exits. Submit sets a deadline and an idle
    kill so one hung tool cannot burn the rest of the run.
    """
    d = workdir / name
    (d / "downloads").mkdir(parents=True, exist_ok=True)
    args = ["--cdp-endpoint", cdp_url, "--output-dir", str(d / "downloads"), "--image-responses", "omit"]
    if mcp_extra:
        args += mcp_extra
    mcp = {"mcpServers": {"browser": {"command": str(NODE_BIN / "playwright-mcp"), "args": args}}}
    (d / "mcp.json").write_text(json.dumps(mcp))
    disallowed = ["Bash", "Edit", "WebFetch", "WebSearch", "Task"]
    allowed = ["mcp__browser", "Read"]
    if allow_write:
        allowed.append("Write")
    else:
        disallowed.append("Write")
    if not allow_js:
        disallowed += list(UNSAFE_JS)
    cmd = [claude_bin(), "-p", prompt, "--model", MODEL, "--mcp-config", str(d / "mcp.json"), "--strict-mcp-config",
           "--setting-sources", "user", "--allowedTools", *allowed,
           "--disallowedTools", *disallowed,
           "--add-dir", str(d), "--output-format", "stream-json", "--verbose", "--no-session-persistence"]
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


async def collect(name: str, prompt: str, cdp_url: str, workdir: Path, schema: dict) -> Evidence:
    """Research session: structured output, or the evidence.json it wrote along the way."""
    first = await claude(name, prompt, cdp_url, workdir, schema, allow_write=True)
    evidence = evidence_from(first, workdir / name)
    if evidence is not None:
        return evidence
    print(f"[{name}] no evidence; retrying once", flush=True)
    second = await claude(f"{name}-retry", prompt, cdp_url, workdir, schema, allow_write=True)
    evidence = evidence_from(second, workdir / f"{name}-retry")
    if evidence is not None:
        return evidence
    raise SystemExit(f"[{name}] Claude returned no evidence")


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
    """Browser id string if CDP answers; None if this Chrome is already gone."""
    try:
        return browsers.check_cdp(url)
    except SystemExit as error:
        print(f"Submit CDP not answering: {error}", flush=True)
        return None


async def enter_bid(prompt: str, cdp_url: str, workdir: Path) -> dict:
    """Submit without page JS; retry once if MCP died or the portal confirmation never arrived."""
    extra = ["--timeout-action", "15000"]

    async def once(name: str) -> dict:
        return await claude(name, prompt, cdp_url, workdir, None, mcp_extra=extra, allow_js=False,
                            limit_s=SUBMIT_S, idle_s=SUBMIT_IDLE_S)

    result = await once("submit")
    if submit_succeeded(result):
        return result
    why = "MCP did not attach" if submit_stalled(result) else "goal not reached"
    print(f"Submit {why}; retrying once on the same unused browser", flush=True)
    return await once("submit-retry")


async def solve(task: dict, research_cdp: str, submit_cdp: str) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="rfq-run-"))
    started = time.monotonic()
    schema = Evidence.model_json_schema()
    sites = sites_text(task)

    evidence = await collect(
        "retrieval", RETRIEVAL.format(brief=task["brief"], sites=sites, extra=""), research_cdp, workdir, schema)
    resolution = resolve(evidence)
    for round_ in range(GAP_ROUNDS):
        if not resolution.gaps:
            break
        print(f"{len(resolution.gaps)} gaps: " + "; ".join(g.question for g in resolution.gaps), flush=True)
        questions = "\n".join(f"- [{g.site}] {g.question}" for g in resolution.gaps)
        extra = await collect(
            f"gaps{round_ + 1}",
            RETRIEVAL.format(brief=task["brief"], sites=sites, extra=GAPS.format(gaps=questions)),
            research_cdp, workdir, schema)
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

    target = submit_cdp
    ready = submit_cdp_ready(target)
    if ready is None and submit_cdp != research_cdp:
        print("Unused submit browser is down; last resort is the research browser", flush=True)
        target = research_cdp
        ready = submit_cdp_ready(target)
    print(f"Submit on {target}" + (f" ({ready})" if ready else " (CDP check failed)"), flush=True)
    result = await enter_bid(SUBMIT.format(sites=sites, submission=submission_section(task["brief"]), bid=bid),
                             target, workdir)
    print(f"Submission at {time.monotonic() - started:.0f}s: {result.get('result')}", flush=True)


async def main() -> int:
    task, _ = contract.load_run()
    print(f"Run {task.get('run_id') or '(local: the scouting logins, nothing counts)'}; websites: "
          + ", ".join(f"{s.get('siteId')} {s.get('url')}" for s in task["access"]), flush=True)
    await preflight()
    async with async_playwright() as playwright:
        urls, local = await browsers.cdp_endpoints(playwright, 2)
        research_cdp, submit_cdp = pick_cdp(urls)
        print(f"Research browser {research_cdp}; submit reserved {submit_cdp}", flush=True)
        try:
            await solve(task, research_cdp, submit_cdp)
        finally:
            for browser in local:
                await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
