"""The entry point: three stages, the models at the ends and code in the middle.

1. Retrieval: one Claude Code session per website, in parallel (`claude -p`, the unmodified CLI). Each session
   has its own sandbox browser (Playwright MCP) and records evidence, not conclusions: every revision, Q&A
   answer, ERP candidate and mail on that site, also the ones that turn out not to apply. Code merges the parts.
2. Rules: resolver.py applies the brief's rules to the merged evidence and pricing.py does the arithmetic. If
   evidence is missing, the resolver phrases follow-up questions and the matching site sessions answer them,
   again in parallel.
3. Submission: a Claude Code session enters the bid on the tender portal and signs it with the TAN.

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
# Claude Code's model classes on OpenRouter (its Claude Code guide): Anthropic's own models.
OPENROUTER_MODELS = {"OPUS": "~anthropic/claude-opus-latest", "SONNET": "~anthropic/claude-sonnet-latest",
                     "HAIKU": "~anthropic/claude-haiku-latest"}

RETRIEVAL = """You research a public tender for a sales engineer. Work in the browser (the `browser` tools).
Your result is evidence, not conclusions: the structured output you return is checked and priced by code that
applies the brief's rules itself.

<brief>
{brief}
</brief>

Your assigned website (stay on this one; colleagues research the others in parallel):
{site}

Record everything on this website the rules need, including the records that will turn out not to apply.
Depending on what this site is, that can include:
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
outdated documents shown first: get past them. Do not submit or sign anything. After each useful page, write the
evidence gathered so far to evidence.json in the working directory (same shape as the structured output). Update
that file; do not start over. {extra}
Return the evidence as the structured output."""

GAPS = """Your colleagues' evidence is incomplete. Answer exactly these follow-up questions in the browser and return the
evidence you find for them (same structure; records you return replace earlier ones with the same id):

{gaps}"""

SUBMIT = """Enter and sign this bid on the tender portal. Each website has its own browser tool; use the tender
portal and the webmail.

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
    return "\n".join(one_site_text(s) for s in task["access"])


def one_site_text(site: dict) -> str:
    c = site.get("credentials") or {}
    return (f"- `{site.get('siteId')}`: {site['url']} · username `{c.get('username', '')}` · password "
            f"`{c.get('password', '')}`")


def slot_name(site_id: str, index: int) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", site_id or "")[:40].strip("-")
    return cleaned or f"site{index}"


def assign_slots(access: list, cdp_urls: list[str]) -> list[dict]:
    """One browser per website the run gives us, in access order (at most 4)."""
    slots = []
    for i, site in enumerate(list(access)[: min(len(access), len(cdp_urls), 4)]):
        slots.append({"name": slot_name(str(site.get("siteId") or ""), i + 1),
                      "site": site, "cdp": cdp_urls[i]})
    return slots


# Resolver gap.site values and leftover siteId fragments. Not public-scenario URLs or answers.
SITE_HINTS = {
    "tender-portal": ("tender", "vergabe"),
    "customer-portal": ("customer", "supplier", "spec", "lieferant"),
    "erp": ("erp",),
    "mail": ("mail", "webmail", "post", "inbox"),
}


def slot_for_gap(slots: list[dict], gap_site: str) -> dict | None:
    want = (gap_site or "").strip().lower()
    if not want:
        return None
    for slot in slots:
        if (slot["site"].get("siteId") or "").lower() == want:
            return slot
    hints = SITE_HINTS.get(want, (want,))
    for slot in slots:
        sid = (slot["site"].get("siteId") or "").lower()
        if any(h in sid for h in hints if h):
            return slot
    for slot in slots:
        sid = (slot["site"].get("siteId") or "").lower()
        if want in sid or sid in want:
            return slot
    return None


def group_gaps(slots: list[dict], gaps: list) -> list[tuple[dict, list]]:
    """Which browser should answer which follow-ups. Unmatched questions go to every running slot."""
    grouped: dict[str, list] = {slot["name"]: [] for slot in slots}
    by_name = {slot["name"]: slot for slot in slots}
    unmatched = []
    for gap in gaps:
        slot = slot_for_gap(slots, getattr(gap, "site", "") or "")
        if slot is None:
            unmatched.append(gap)
        else:
            grouped[slot["name"]].append(gap)
    return [(by_name[name], gs + unmatched) for name, gs in grouped.items() if gs or unmatched]


def merge_all(parts: list[Evidence | None]) -> Evidence:
    kept = [p for p in parts if p is not None]
    if not kept:
        raise SystemExit("No evidence from any site")
    return merge(kept[0].case, *kept)


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


UNSAFE_JS = (
    "browser_run_code_unsafe", "browser_evaluate",
    "mcp__browser__browser_run_code_unsafe", "mcp__browser__browser_evaluate",
    "mcp__tender__browser_run_code_unsafe", "mcp__tender__browser_evaluate",
    "mcp__mail__browser_run_code_unsafe", "mcp__mail__browser_evaluate",
)
BASE_DISALLOWED = ("Bash", "Edit", "WebFetch", "WebSearch", "Task")
IDLE_S = 90
SITE_S = 480
GAP_SITE_S = 300
NOJS_S = 240
RETRIEVAL_S = SITE_S
SUBMIT_S = 600
PROGRESS_TYPES = frozenset({"assistant", "result", "user", "stream_event"})


def mcp_config(browser_cdp: dict[str, str], downloads: Path) -> dict:
    return {"mcpServers": {name: {"command": str(NODE_BIN / "playwright-mcp"), "args": [
        "--cdp-endpoint", url, "--output-dir", str(downloads), "--image-responses", "omit",
        "--timeout-action", "15000"]}
        for name, url in browser_cdp.items()}}


def claude_tool_flags(browser_cdp: dict[str, str], allow_js: bool) -> list[str]:
    allowed = [f"mcp__{name}" for name in browser_cdp] + ["Read", "Write"]
    disallowed = list(BASE_DISALLOWED)
    if not allow_js:
        disallowed += list(UNSAFE_JS)
    return ["--allowedTools", *allowed, "--disallowedTools", *disallowed]


def progress_event(event: dict) -> bool:
    """Stdout that means Claude is still working, not just MCP noise or a keep-alive line."""
    return event.get("type") in PROGRESS_TYPES


def wait_budget(now: float, started: float, last_progress: float, limit_s: float,
                idle_s: float = IDLE_S) -> tuple[float, str | None]:
    """Seconds until the next kill, and why if the session should be killed now."""
    left_deadline = started + limit_s - now
    left_idle = last_progress + idle_s - now
    if left_deadline <= 0:
        return 0.0, "deadline"
    if left_idle <= 0:
        return 0.0, "idle"
    return min(left_deadline, left_idle), None


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
              f"({len(checkpoint.positions)} positions)", flush=True)
    return checkpoint


async def claude(name: str, prompt: str, browser_cdp: dict[str, str], workdir: Path, schema: dict | None,
                 *, allow_js: bool = True, limit_s: float = RETRIEVAL_S) -> dict:
    """One headless Claude Code session. Kills the process if it goes idle or over `limit_s`."""
    d = workdir / name
    (d / "downloads").mkdir(parents=True, exist_ok=True)
    (d / "mcp.json").write_text(json.dumps(mcp_config(browser_cdp, d / "downloads")))
    cmd = [claude_bin(), "-p", prompt, "--model", MODEL, "--mcp-config", str(d / "mcp.json"), "--strict-mcp-config",
           "--setting-sources", "user", *claude_tool_flags(browser_cdp, allow_js),
           "--add-dir", str(d), "--output-format", "stream-json", "--verbose", "--no-session-persistence"]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    started = last_progress = time.monotonic()
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=d, env=claude_env(), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, limit=2**26)
    result, tools, why = {}, 0, None
    with (d / "session.jsonl").open("w") as log:
        while True:
            left, why = wait_budget(time.monotonic(), started, last_progress, limit_s)
            if why:
                proc.kill()
                break
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=left)
            except asyncio.TimeoutError:
                _, why = wait_budget(time.monotonic(), started, last_progress, limit_s)
                why = why or "idle"
                proc.kill()
                break
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace")
            log.write(line)
            try:
                event = json.loads(line)
            except ValueError:
                print(f"[{name}] {line.rstrip()[:300]}", flush=True)
                continue
            if progress_event(event):
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


def site_prompt(task: dict, site: dict, extra: str = "") -> str:
    return RETRIEVAL.format(brief=task["brief"], site=one_site_text(site), extra=extra)


async def retrieve_site(name: str, prompt: str, browser_cdp: dict[str, str], workdir: Path,
                        schema: dict, limit_s: float) -> Evidence | None:
    first = await claude(name, prompt, browser_cdp, workdir, schema, allow_js=True, limit_s=limit_s)
    evidence = evidence_from(first, workdir / name)
    if evidence is not None:
        return evidence
    print(f"[{name}] first pass failed; retry without page JavaScript", flush=True)
    extra = "\nDo not run page JavaScript. Use snapshot, click, type, fill_form and Read."
    second = await claude(f"{name}-nojs", prompt + extra, browser_cdp, workdir, schema,
                          allow_js=False, limit_s=min(NOJS_S, limit_s))
    return evidence_from(second, workdir / f"{name}-nojs")


async def research_parallel(task: dict, slots: list[dict], workdir: Path, schema: dict,
                            extra: str = "", limit_s: float = SITE_S) -> list[Evidence]:
    print("Researching " + ", ".join(s["name"] for s in slots) + " in parallel", flush=True)

    async def one(slot: dict) -> Evidence | None:
        try:
            found = await retrieve_site(slot["name"], site_prompt(task, slot["site"], extra),
                                        {"browser": slot["cdp"]}, workdir, schema, limit_s)
        except Exception as error:  # noqa: BLE001
            print(f"[{slot['name']}] failed: {error!r}", flush=True)
            return None
        if found is not None:
            print(f"[{slot['name']}] records: {len(found.positions)} positions, {len(found.qa)} qa, "
                  f"{len(found.revisions)} revisions, {len(found.materials)} materials, "
                  f"{len(found.messages)} messages", flush=True)
        return found

    return [p for p in await asyncio.gather(*(one(s) for s in slots)) if p is not None]


def evidence_of(result: dict) -> Evidence:
    data = result.get("structured_output")
    if data is None:
        text = str(result.get("result") or "")
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise SystemExit(
                ("Claude session failed: " + str(result.get("result") or result)[:800])
                if result.get("is_error")
                else ("Claude returned no evidence JSON: " + text[:800]))
        try:
            data = json.loads(text[start:end + 1])
        except ValueError:
            raise SystemExit("Claude returned no evidence JSON: " + text[:800])
    return Evidence.model_validate(data)


async def solve(task: dict, cdp_urls: list[str]) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="rfq-run-"))
    started = time.monotonic()
    schema = Evidence.model_json_schema()
    slots = assign_slots(task["access"], cdp_urls)
    if not slots:
        raise SystemExit("No website/browser pair")
    sites = sites_text(task)

    evidence = merge_all(await research_parallel(task, slots, workdir, schema, limit_s=SITE_S))
    resolution = resolve(evidence)
    for round_ in range(GAP_ROUNDS):
        if not resolution.gaps:
            break
        print(f"{len(resolution.gaps)} gaps: " + "; ".join(g.question for g in resolution.gaps), flush=True)
        jobs = group_gaps(slots, resolution.gaps)

        async def gap_job(slot: dict, gaps: list) -> Evidence | None:
            questions = "\n".join(f"- [{g.site}] {g.question}" for g in gaps)
            name = f"gaps{round_ + 1}-{slot['name']}"
            try:
                return await retrieve_site(name, site_prompt(task, slot["site"], GAPS.format(gaps=questions)),
                                           {"browser": slot["cdp"]}, workdir, schema, GAP_SITE_S)
            except Exception as error:  # noqa: BLE001
                print(f"[{name}] failed: {error!r}", flush=True)
                return None

        extras = [e for e in await asyncio.gather(*(gap_job(s, gs) for s, gs in jobs)) if e is not None]
        if extras:
            evidence = merge(evidence.case, evidence, *extras)
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
                                                  bid=bid), {s["name"]: s["cdp"] for s in slots},
                          workdir, None, limit_s=SUBMIT_S)
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
