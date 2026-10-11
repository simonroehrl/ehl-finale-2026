"""DeepSeek steers four tools (view, submit, record, gaps) in a loop of our own: one worker per website, all in one
process, recording into one evidence store; code resolves and prices the evidence. view and submit are plain HTTP
(web_tools.py). Model calls go to OpenRouter: in a run through the run's proxy with the OPENROUTER_API_KEY team
secret; locally with that variable or ~/.config/rfq/openrouter_key.

Context: every page dump stays in a worker's history only until KEEP_DUMPS newer ones arrive, then it shrinks to its
first line. What the worker read is in the store by then (it records as it reads; gaps(kind) shows it), so old pages
are dead weight: the prompt stays small and its cached prefix stable.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import run as contract

HERE = Path(__file__).resolve().parent
MODEL = os.environ.get("RFQ_MODEL", "deepseek/deepseek-v4.1-flash")
# Baidu first (about 1 s per call, $0.035/M input); its shared pool sometimes answers 429, and then OpenRouter falls
# back to the next ones. Not left to OpenRouter's default routing: that once hung for minutes on DeepSeek.
PROVIDER = {"order": os.environ.get("RFQ_PROVIDERS", "baidu,deepseek,fireworks").split(","),
            "allow_fallbacks": True}
REASONING = os.environ.get("RFQ_REASONING", "off")      # off | low | medium | high
RESEARCH_S = float(os.environ.get("RFQ_RESEARCH_S", "600"))
GAP_ROUNDS = int(os.environ.get("RFQ_GAP_ROUNDS", "2"))
MAX_TURNS = int(os.environ.get("RFQ_MAX_TURNS", "80"))  # model calls per worker per round
KEEP_DUMPS = int(os.environ.get("RFQ_KEEP_DUMPS", "2"))  # page dumps a worker keeps in full
MAX_OUTPUT = 120_000                                       # characters of one tool result
CALL_TIMEOUT_S = 30   # calls take 1–3 s; a hung connection is retried instead of waited out
REPEATS = 2                                                # the same call with the same result: blocked after this
MAX_WAIT_S = 120      # gaps(wait) per worker and round: waiting longer only spins
STUCK = 8             # refused tool calls in a row (blocked repeats, made-up URLs) that end a worker's round

RETRIEVAL = """You research a public tender for a sales engineer, using page dumps and HTTP form submissions. Your job is evidence, not conclusions:
you record what the websites say, and code applies the brief's rules to your records and computes the bid.

<brief>
{brief}
</brief>

Websites and logins:
{sites}

Several workers do this at once, one per website; each uses only its own website. All record into one shared
store, and gaps shows what is still missing across all of them.

Exactly four tools:
- `view(site, url)` GETs a page and dumps its whole server HTML (no scripts/styles). Links, frames, forms and fields
  carry data-ref numbers; view(site, link=N) follows link N of the last dump of that site.
- `submit(site, form, button, values)` sends form N of the last dump, as a browser would, and dumps the response.
  values maps field names to values; hidden fields and defaults are sent as the page has them.
- `record` saves facts; `gaps` says what the rules still miss. gaps(kind=...) shows recorded facts.
There is no browser: nothing runs JavaScript, and you read the server's HTML. Collapsed menus, closed dialogs,
overlays, sliders and "not a robot" boxes block nothing here: their links and forms work like any other.
Every dump ends with a list of its links: ref, text, target, where on the page, and (visited) for pages you already
opened. A list or register page only names things: open each entry's own page (its link, often in an actions menu)
to read it. Prefer links you have not visited; never add made-up parameters to a URL to reload it.
Your site keeps its cookies; if a page comes back as the login form, log in again.
Numbers: where a page marks a number's format, code has already converted it to a plain number (dot = decimal
point, no thousands separator), and the marker stays next to it: "64853 (en-US)" means 64853. Copy such numbers as
they are. A price per price unit stays per price unit (e.g. 64853 per 100 pieces): never divide it yourself.
Only older page dumps are cut from your history: record what you read before moving on.

How to work:
1. Record the case from the brief first (kind 'case'), then log in to your website.
2. Find what your website holds: the brief says what each website is for, but the information may be split
   differently, so look at everything on it.
3. Record everything the rules need, as soon as you have read it, including the records that will turn out not to
   apply: every position of the bill of quantities; every clarification or Q&A answer on every page (withdrawals,
   quantity changes, technical amendments with the revision they name, key assignments, setup fees); every
   specification revision with its marking as shown in the revision itself and its requirements with all attributes,
   catalogue group and pieces per set; for each requirement, the catalogue search narrowed to at most 20 results with
   every listed material, and every candidate material with status, attributes, price conditions, stock, lead times,
   carton size, masses and metal index; every message about this tender or a look-alike reference, in every folder
   and on every page, with sender, date, subject, reference and the values it states.
4. Search only for terms you have read: on your website, in the brief or in what gaps lists. Never guess codes or
   numbers by trying a series (X-1, X-2, ...). If you need a key another website holds (e.g. a catalogue group from
   a specification), call gaps(wait=60): it waits for the other workers and then lists what is open.
5. Page through every list completely: follow the link whose text says next page, and stop at the last page. Keep references exactly as written (look-alikes too). Dates in ISO 8601 with
   timezone after applying the site's format hints.
6. When your website holds nothing more, call `gaps` and answer every question your website can answer; repeat
   until the rest is for other websites.
Do not submit or sign a bid. Several independent tool calls may go in one turn. When you are done, reply with one
line: done."""

START = """Your website: `{site}` ({url}). Start now."""

GAPS = """The rules still miss this (from all websites together):
{questions}
Answer what your website `{site}` holds: find it, record it, then call gaps again. If none of it can be on your
website, reply with one line: done."""

SUBMIT = """Enter and sign this bid on the tender portal with the same tools (plain HTTP, no browser).
view(site, url) dumps a whole page; view(site, link=N) follows a numbered link.
submit(site, form=N, button="N", values={{"field-name": "value"}}) sends one HTML form; hidden inputs and cookies
are kept. Use the data-ref numbers of the latest dump of that site. Where the page's script would copy a value into
a field (for example from a data-* attribute of the button), send that field yourself in values.
Enter only data from the bid or the brief; never other personal data.

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

Go straight to the bid submission. The bid lists each material's data for any configurator: never look anything
up on the other websites. Send all fields for each form step in one request. Never use a back button: it clears later
steps. Request the TAN only when everything else is entered, read the newest TAN mail, enter it within five minutes
and send the final confirmation form. Never reuse a TAN: if signing has no visible effect or the
TAN is rejected, request a new one. Reply with the confirmation the portal shows."""

SYSTEM = ("You are an autonomous agent that works websites through HTML page dumps and HTTP form tools for a business "
          "task. Follow the task instructions exactly. Be brief in text; act through tools. Never ask anything: "
          "nobody is there.")

OUT = Path(os.environ["RFQ_OUT"]) if os.environ.get("RFQ_OUT") else None
METRICS: dict = {"model": MODEL, "reasoning": REASONING, "sessions": [], "cost_usd": 0.0}


def write_out(name: str, data) -> None:
    """Locally (the harness sets RFQ_OUT): the run's results, for scoring. Nothing in a sandbox run."""
    if OUT:
        (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str))


# --- the model ---------------------------------------------------------------------------------------------------------

def endpoint() -> tuple[str, dict]:
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if os.environ.get("CHALLENGE_RUN_TOKEN"):
        if not key:
            raise SystemExit("OPENROUTER_API_KEY is missing: add it as a team secret on the team page")
        base = os.environ["LLM_PROXY_URL"].rstrip("/") + "/openrouter/v1"
    else:
        if not key:
            key_file = Path.home() / ".config" / "rfq" / "openrouter_key"
            key = key_file.read_text().strip() if key_file.exists() else ""
        if not key:
            raise SystemExit("No OpenRouter key: set OPENROUTER_API_KEY or ~/.config/rfq/openrouter_key")
        base = "https://openrouter.ai/api/v1"
    return base + "/chat/completions", {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                        "User-Agent": "atira-rfq-d1"}


URL, HEADERS = None, None


def post(body: dict) -> dict:
    with urlopen(Request(URL, data=json.dumps(body).encode(), headers=HEADERS), timeout=CALL_TIMEOUT_S) as response:
        return json.load(response)


async def chat(messages: list[dict], tools: list[dict] | None, meter: dict) -> dict:
    """One model call (retried on errors and timeouts); the assistant message."""
    body = {"model": MODEL, "messages": messages, "temperature": 0, "provider": PROVIDER,
            "usage": {"include": True},
            "reasoning": {"enabled": False} if REASONING == "off" else {"effort": REASONING}}
    if tools:
        body["tools"] = tools
    for attempt in range(4):
        started = time.monotonic()
        try:
            data = await asyncio.to_thread(post, body)
            if "choices" not in data:
                raise ValueError(str(data.get("error") or data)[:300])
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:300]
            print(f"  model HTTP {error.code} ({attempt + 1}/4): {detail}", flush=True)
            if error.code in (400, 401, 402, 403) or attempt == 3:
                raise
        except (URLError, OSError, ValueError) as error:
            print(f"  model error ({attempt + 1}/4) after {time.monotonic() - started:.0f}s: "
                  f"{type(error).__name__} {str(error)[:200]}", flush=True)
            if attempt == 3:
                raise
        else:
            usage = data.get("usage") or {}
            meter["calls"] += 1
            meter["seconds"] += time.monotonic() - started
            meter["prompt_tokens"] += usage.get("prompt_tokens") or 0
            meter["completion_tokens"] += usage.get("completion_tokens") or 0
            meter["cached_tokens"] += (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            meter["cost_usd"] += float(usage.get("cost") or 0)
            provider = data.get("provider") or "?"
            meter.setdefault("providers", {})[provider] = meter.setdefault("providers", {}).get(provider, 0) + 1
            msg = data["choices"][0]["message"]
            out = {"role": "assistant", "content": msg.get("content") or ""}
            if msg.get("tool_calls"):
                out["tool_calls"] = [{"id": c.get("id") or f"call_{i}", "type": "function",
                                      "function": {"name": c["function"]["name"],
                                                   "arguments": c["function"].get("arguments") or "{}"}}
                                     for i, c in enumerate(msg["tool_calls"])]
            return out
        await asyncio.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


# --- the tools ---------------------------------------------------------------------------------------------------------

def tool_specs(wt, sites: list[str]) -> list[dict]:
    site = {"type": "string", "enum": sites}
    record_doc = (
        "Save evidence records of one kind as soon as you have read them (several records per call is best). A "
        "record with the same key as an earlier one is merged field by field: send only what is new or corrected; "
        "empty values never erase known ones. Use exactly the schema's field names (unknown names are rejected with "
        "the nearest correct one). Numbers: if a page writes a number with thousands separators or a decimal comma "
        'and no format marker, send {"raw": "<exactly as shown>", "format": "de"} (de: 1.234,5) or "en" (en: '
        "1,234.5). kind 'case' takes one record with the case fields. Invalid records come back with the reason: "
        "fix and resend.\nFields per kind (* = required; nested objects indented):\n"
        f"- case: {wt.CASE_SCHEMA}\n" + wt.SCHEMAS)
    fn = lambda name, doc, props, req: {"type": "function", "function": {  # noqa: E731
        "name": name, "description": doc, "parameters": {"type": "object", "properties": props, "required": req}}}
    return [
        fn("view", wt.view.__doc__, {"site": site, "url": {"type": "string"}, "link": {"type": "integer"}}, ["site"]),
        fn("submit", wt.submit.__doc__, {"site": site, "form": {"type": "integer"}, "button": {"type": "string"},
                                         "values": {"type": "object"}}, ["site", "form"]),
        fn("record", record_doc, {"kind": {"type": "string", "enum": ["case", *wt.KINDS]},
                                  "records": {"type": "array", "items": {"type": "object"}}}, ["kind", "records"]),
        fn("gaps", wt.gaps.__doc__ + " wait=N (seconds, at most 90): first wait until another worker records "
           "something; use it when what you need comes from another website.",
           {"kind": {"type": "string"}, "wait": {"type": "integer"}}, []),
    ]


def as_json(value):
    """Arguments a model sent as a JSON string instead of an object or list."""
    if isinstance(value, str) and value.strip()[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


class Worker:
    def __init__(self, name: str, sites: list[str], system: str, wt, tools: list[dict]):
        self.name, self.sites, self.wt, self.tools = name, sites, wt, tools
        self.messages = [{"role": "system", "content": system}]
        self.seen: dict[str, list[str]] = {}
        self.pages: dict[str, tuple[int, int]] = {}   # page content -> (times got, records count at the last)
        self.records = 0
        self.refused = 0
        self.waited = 0.0
        self.meter = {"calls": 0, "tool_calls": 0, "blocked": 0, "seconds": 0.0, "prompt_tokens": 0,
                      "completion_tokens": 0, "cached_tokens": 0, "cost_usd": 0.0}
        self.started = time.monotonic()
        self.last = ""

    def log(self, text: str) -> None:
        print(f"[{self.name}] {time.monotonic() - self.started:5.0f}s {text}", flush=True)

    async def call(self, name: str, args: dict) -> str:
        wt = self.wt
        if name in ("view", "submit"):
            if args.get("site") not in self.sites:
                return f"ERROR: you work on {', '.join(self.sites)} only; other workers cover the other websites."
            if name == "view":
                return await wt.view(args["site"], str(args.get("url") or ""), int(args.get("link") or 0))
            values = as_json(args.get("values")) or {}
            if not isinstance(values, dict):
                return "ERROR: values must be an object mapping field names to values"
            return await wt.submit(args["site"], int(args.get("form") or 0), str(args.get("button") or ""), values)
        if name == "record":
            records = as_json(args.get("records"))
            if isinstance(records, dict):
                records = [records]
            if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
                return "ERROR: records must be a list of objects"
            return await asyncio.to_thread(wt.record, str(args.get("kind") or ""), records)
        if name == "gaps":
            waited = ""
            if args.get("wait"):
                if self.waited >= MAX_WAIT_S:
                    return (f"STOP WAITING: you waited {self.waited:.0f}s this round already. Answer what your "
                            "website holds, or reply with one line: done. You will get the open questions again "
                            "once the other workers have finished.")
                seconds = min(float(args["wait"]), 90, MAX_WAIT_S - self.waited)
                self.waited += seconds
                waited = await wait_for_store(wt, seconds)
            return waited + await asyncio.to_thread(wt.gaps, str(args.get("kind") or ""))
        return f"ERROR: unknown tool {name!r}; use view, submit, record or gaps"

    async def tool(self, call: dict) -> str:
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
            if not isinstance(args, dict):
                raise ValueError("not an object")
        except ValueError as error:
            return f"ERROR: the arguments are not a JSON object ({error}); send them again"
        key = name + json.dumps(args, sort_keys=True, ensure_ascii=False)
        earlier = self.seen.get(key, [])
        waiting = name == "gaps" and bool(args.get("wait"))   # waiting again is never a loop: let it wait
        if not waiting and len(earlier) >= REPEATS and len(set(earlier[-REPEATS:])) == 1:
            self.meter["blocked"] += 1
            self.log(f"BLOCKED repeat of {name} {short(args)}")
            return (f"BLOCKED: you made exactly this call {len(earlier)} times and got the same result each time. "
                    "Doing it again changes nothing: take another link, form, field value or search, or move on "
                    "(reply done if your website holds nothing more).")
        self.meter["tool_calls"] += 1
        self.log(f"{name} {short(args)}")
        try:
            result = await self.call(name, args)
        except Exception as error:  # noqa: BLE001  (a tool must never end the worker)
            result = f"ERROR: {type(error).__name__}: {error}"
        result = str(result)
        if len(result) > MAX_OUTPUT:
            result = (result[:MAX_OUTPUT] + f"\n... (cut: {len(result)} characters, {MAX_OUTPUT} shown; open a narrower "
                      "page, a search or the next page for the rest)")
        self.seen.setdefault(key, []).append(hashlib.sha1(result.encode()).hexdigest())
        if name == "record" and not result.startswith("ERROR"):
            self.records += 1
        if name in ("view", "submit") and not result.startswith("ERROR"):
            page = page_hash(result)
            count, recorded = self.pages.get(page, (0, -1))
            self.pages[page] = (count + 1, self.records)
            first = result.split("\n", 1)[0]
            # a form sent again may legitimately answer the same page; a page read again after recording is rarer
            if name == "view" and (count + 1 >= 5 or (count + 1 >= 3 and recorded == self.records)):
                self.meter["blocked"] += 1
                self.log(f"CIRCLE {count + 1}x {first[:120]}")
                return (f"BLOCKED: you already got this same page content {count} times ({first}), under this or "
                        "another URL. You are going in circles. Record what you need from it if you have not, then "
                        "open pages you have not visited, or reply done if your website holds nothing more.")
            if count:
                result = ((f"NOTE: you already read this same page content before and recorded nothing since. "
                           "Record what you need from it now, before you open anything else.\n")
                          if recorded == self.records else
                          "NOTE: you already got this same page content before (data-ref numbers are per page: "
                          "check the link text).\n") + result
        return result

    def trim(self) -> None:
        """Page dumps (view/submit results) older than the newest KEEP_DUMPS shrink to their first line."""
        # Dumps the model has not read yet (results of its latest turn) always stay: cutting them made it reload the
        # same pages with made-up parameters in endless loops when one turn viewed more than KEEP_DUMPS pages.
        last = max((i for i, m in enumerate(self.messages) if m.get("role") == "assistant"), default=-1)
        dumps = [m for i, m in enumerate(self.messages[:last]) if m.get("role") == "tool" and m.get("_dump")
                 and not m.get("_cut")]
        fresh = sum(1 for m in self.messages[last + 1:] if m.get("role") == "tool" and m.get("_dump"))
        keep = max(KEEP_DUMPS - fresh, 0)
        for m in dumps[:-keep] if keep else dumps:
            head = m["content"].split("\n", 1)[0][:300]
            m["content"] = f"{head}\n(older page dump removed from your history: record before you move on; view it " \
                           "again if you still need it)"
            m["_cut"] = True

    async def turn(self, prompt: str, deadline: float) -> str:
        """The worker's loop for one prompt: until it answers without a tool call, or runs out of turns or time."""
        self.messages.append({"role": "user", "content": prompt})
        self.waited = 0.0
        ACTIVE.add(self.name)
        try:
            return await self._turn(deadline)
        finally:
            ACTIVE.discard(self.name)

    async def _turn(self, deadline: float) -> str:
        for _ in range(MAX_TURNS):
            if time.monotonic() > deadline:
                self.log("time budget used up")
                return "timeout"
            self.trim()
            wire = [{k: v for k, v in m.items() if not k.startswith("_")} for m in self.messages]
            msg = await chat(wire, self.tools, self.meter)
            self.messages.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                self.last = msg["content"].strip()
                self.log(f"says: {self.last[:200]}")
                return self.last
            for call in calls:   # in order: calls on one site depend on its last dump
                result = await self.tool(call)
                refused = result.startswith(("BLOCKED", "STOP WAITING")) or "made-up parameters" in result[:300]
                self.refused = self.refused + 1 if refused else 0
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result,
                                      "_dump": call["function"]["name"] in ("view", "submit") or
                                               (call["function"]["name"] == "gaps" and len(result) > 2000)})
            if self.refused >= STUCK:
                self.log(f"stuck: {self.refused} refused calls in a row, round ended")
                self.refused = 0
                return "stuck"
        self.log(f"{MAX_TURNS} turns used up")
        return "turns"

    def metrics(self) -> dict:
        m = dict(self.meter)
        m.update(name=self.name, seconds=round(time.monotonic() - self.started, 1), cost_usd=round(m["cost_usd"], 5),
                 model_seconds=round(m["seconds"], 1))
        m.pop("seconds_", None)
        return m


def page_hash(result: str) -> str:
    """A page's identity: its content without the URL line (made-up parameters don't make it another page) and
    without per-request tokens (csrf, nonces) in attributes."""
    body = result.split("\n", 1)[1] if "\n" in result else result
    body = body.split("\nLinks (ref: ", 1)[0]   # the link index marks visited links: it changes on every visit
    return hashlib.sha1(re.sub(r'value="[^"]*"', "", body).encode()).hexdigest()


ACTIVE: set[str] = set()   # workers still researching


async def wait_for_store(wt, seconds: float) -> str:
    """Until another worker records something (the store file changes), at most `seconds`, while any is working."""
    path = wt.STORE
    before = path.stat().st_mtime if path.exists() else 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        await asyncio.sleep(2)
        if (path.stat().st_mtime if path.exists() else 0) != before:
            return "(new records arrived)\n"
        if len(ACTIVE) <= 1:
            return "(no other worker is still researching)\n"
    return f"(nothing new within {seconds:.0f}s)\n"


def short(args: dict) -> str:
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= 160 else text[:157] + "..."


# --- the run -----------------------------------------------------------------------------------------------------------

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


def material_facts(evidence, number: str) -> str:
    """What a material configurator may ask about the chosen material, so the submission never has to look it up."""
    for m in evidence.materials:
        if m.number == number:
            a = m.attributes
            facts = [f"{k} {v}" for k, v in (("bore mm", a.bore_mm), ("stroke mm", a.stroke_mm),
                                             ("pressure bar", a.pressure_bar), ("mounting", a.mounting),
                                             ("certification", a.certification), ("catalogue group", m.catalog_group))
                     if v not in (None, "")]
            return f" (material data: {', '.join(facts)})" if facts else ""
    return ""


def bid_text(answer: dict, omitted: list[str], evidence) -> str:
    lines = [f"Tender reference: {answer['tender_reference']}", f"Decision: {answer['decision']}",
             f"Discount: {german(answer['discount_percent'])} %"]
    for p in answer["positions"]:
        lines.append(
            f"- Position {p['position']}: material {p['material_number']}"
            f"{material_facts(evidence, p['material_number'])}, quantity {p['quantity']} pieces, unit "
            f"price {german(p['unit_price'])} EUR, lead time {p['lead_time_weeks']} weeks, base unit price "
            f"{german(p['base_unit_price'])}, surcharge per unit {german(p['surcharge_per_unit'])}, packaging per "
            f"unit {german(p['packaging_per_unit'])}, rebate total {german(p['rebate_total'])}")
    for p in omitted:
        lines.append(f"- Position {p}: omit it (use the form's option to leave a position out)")
    lines.append(f"Total net: {german(answer['total_net'])} EUR")
    return "\n".join(lines)


def save_metrics(workers: list[Worker], **extra) -> None:
    METRICS["sessions"] = [w.metrics() for w in workers]
    METRICS["cost_usd"] = round(sum(w.meter["cost_usd"] for w in workers), 5)
    METRICS.update(extra)
    write_out("metrics.json", METRICS)


async def solve(task: dict) -> None:
    global URL, HEADERS
    URL, HEADERS = endpoint()
    workdir = Path(tempfile.mkdtemp(prefix="rfq-run-"))
    print(f"Workdir {workdir}", flush=True)
    started = time.monotonic()
    store = workdir / "evidence.json"
    # web_tools reads its configuration when imported
    os.environ.update({"SITES_JSON": json.dumps([{"siteId": a["siteId"], "url": a["url"]} for a in task["access"]]),
                       "EVIDENCE_DIR": str(HERE), "STORE_PATH": str(store), "COOKIE_DIR": str(workdir)})
    import web_tools as wt
    from resolver import position_key, resolve

    sites = [a["siteId"] for a in task["access"]]
    tools = tool_specs(wt, sites)
    system = SYSTEM + "\n\n" + RETRIEVAL.format(brief=task["brief"], sites=sites_text(task))

    preflight = {"calls": 0, "seconds": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
                 "cost_usd": 0.0}
    reply = await chat([{"role": "user", "content": "Reply with exactly: ok"}], None, preflight)
    print(f"Preflight ok: {MODEL} answered {reply['content'][:20]!r} in {preflight['seconds']:.1f}s", flush=True)
    METRICS["preflight_cost_usd"] = preflight["cost_usd"]

    workers = [Worker(a["siteId"], [a["siteId"]], system, wt, tools) for a in task["access"]]
    deadline = started + RESEARCH_S
    prompts = {w.name: START.format(site=w.name, url=next(a["url"] for a in task["access"] if a["siteId"] == w.name))
               for w in workers}
    resolution, evidence = None, None
    for round_ in range(1 + GAP_ROUNDS):
        results = await asyncio.gather(*(w.turn(prompts[w.name], deadline) for w in workers), return_exceptions=True)
        for w, r in zip(workers, results):
            if isinstance(r, BaseException):
                w.log(f"failed: {type(r).__name__}: {r}")
        save_metrics(workers)
        try:
            evidence = wt.evidence()
            resolution = resolve(evidence)
        except Exception as error:  # noqa: BLE001
            print(f"Evidence unusable after round {round_}: {error!r}", flush=True)
            resolution = None
        if resolution is None:
            questions = "- Nothing usable is recorded yet: record the case and what your website holds."
        else:
            extra = wt.implausible_prices(evidence)
            print(f"After round {round_} at {time.monotonic() - started:.0f}s: "
                  f"{len((resolution.answer or {}).get('positions') or [])} positions priced, "
                  f"{len(resolution.gaps)} gaps, {len(extra)} implausible prices", flush=True)
            if not resolution.gaps and not extra:
                break
            questions = "\n".join([f"- {g.question}" for g in resolution.gaps] + [f"- {q}" for q in extra])
        if time.monotonic() > deadline - 30:
            break
        prompts = {w.name: GAPS.format(questions=questions, site=w.name) for w in workers}
    research_s = round(time.monotonic() - started, 1)
    save_metrics(workers, research_s=research_s)
    if OUT and store.exists():
        (OUT / "evidence.json").write_text(store.read_text())
    if resolution is None:
        print("Research recorded no usable evidence: nothing to bid", flush=True)
        return
    if resolution.gaps:
        # No answer scores 0, a partial one keeps the credit of every position it gets right: guess what stayed
        # ambiguous rather than enter nothing.
        best = resolve(evidence, best_effort=True)
        if len((best.answer or {}).get("positions") or []) > len((resolution.answer or {}).get("positions") or []):
            print(f"Best effort: {len(best.answer['positions'])} positions priced", flush=True)
            resolution = best
    print("Trace:\n  " + "\n  ".join(resolution.trace), flush=True)
    print("Warnings: " + ("; ".join(resolution.warnings) or "none"), flush=True)
    if resolution.answer is None:
        print("Nothing could be priced; gaps: " + "; ".join(g.question for g in resolution.gaps), flush=True)
        if not (evidence.case.tender_reference and evidence.positions):
            return
        # the bid form lets every unfinished position be omitted: a signed bid still earns its order-level points
        resolution.answer = {"tender_reference": evidence.case.tender_reference, "decision": "bid",
                             "currency": "EUR", "discount_percent": 0, "positions": [], "total_net": 0}
        resolution.omitted = sorted({p.position for p in evidence.positions}, key=position_key)
    write_out("answer.json", resolution.answer)
    bid = bid_text(resolution.answer, resolution.omitted, evidence)
    print(f"Bid at {time.monotonic() - started:.0f}s ({research_s}s research, ${METRICS['cost_usd']:.3f}):\n{bid}",
          flush=True)
    submit = os.environ.get("RFQ_SUBMIT", "1" if os.environ.get("CHALLENGE_RUN_TOKEN") else "0") == "1"
    if not submit:
        print("Submission skipped (RFQ_SUBMIT is not 1; it is by default only in a run)", flush=True)
        return
    submitter = Worker("submit", sites, SYSTEM, wt, tools)
    text = await submitter.turn(SUBMIT.format(sites=sites_text(task), submission=submission_section(task["brief"]),
                                              bid=bid), started + 1140)
    workers.append(submitter)
    save_metrics(workers, research_s=research_s, total_s=round(time.monotonic() - started, 1))
    print(f"Submission at {time.monotonic() - started:.0f}s: {text}", flush=True)
    receipt = re.search(r"\b[A-Z]{2,5}-[0-9A-F]{6,}\b", text)
    write_out("submit.json", {"receipt": receipt.group(0) if receipt else None, "result": text})


async def main() -> int:
    task, _ = contract.load_run()
    print(f"Run {task.get('run_id') or '(local: the scouting logins, nothing counts)'}; model {MODEL}, reasoning "
          f"{REASONING}; websites: " + ", ".join(f"{s.get('siteId')} {s.get('url')}" for s in task["access"]),
          flush=True)
    await solve(task)
    providers: dict = {}
    for session in METRICS["sessions"]:
        for name, n in (session.get("providers") or {}).items():
            providers[name] = providers.get(name, 0) + n
    print(f"Model cost ${METRICS['cost_usd']:.4f}; calls by provider: {providers}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
