"""DeepSeek steers four tools (view, submit, record, gaps) in a loop of our own: one worker per website, all in one
process, recording into one evidence store; code resolves and prices the evidence. view and submit are plain HTTP
(web_tools.py). Model calls go to OpenRouter: in a run through the run's proxy with the OPENROUTER_API_KEY team
secret; locally with that variable or ~/.config/rfq/openrouter_key.

Context: page dumps stay in a worker's history in full until they exceed KEEP_CHARS; then the oldest shrink to their
first line, in batches (so the cached prefix holds), never the latest turn's. A document read over several turns stays
in view as a whole. What the worker read is in the store by then (it records as it reads; gaps(kind) shows it), so old pages
are dead weight: the prompt stays small and its cached prefix stable.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import threading
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import Request, urlopen

import run as contract

HERE = Path(__file__).resolve().parent
MODEL = os.environ.get("RFQ_MODEL", "deepseek/deepseek-v4.1-flash")
# Relace first (the user's pick); a call with no token after HEDGE_S gets a twin on InferenceNet (lowest latency in
# the endpoint comparison: p50 1.3 s). Not left to OpenRouter's default routing: that once hung for minutes.
PROVIDER = {"order": os.environ.get("RFQ_PROVIDERS", "inference-net,relace,baidu").split(","),
            "allow_fallbacks": True}
REASONING = os.environ.get("RFQ_REASONING", "off")      # off | low | medium | high
RESEARCH_S = float(os.environ.get("RFQ_RESEARCH_S", "600"))
GAP_ROUNDS = int(os.environ.get("RFQ_GAP_ROUNDS", "6"))  # gap rounds while time is left (RESEARCH_S bounds them)
NUDGES = 1   # a worker that answers in prose instead of "done" is asked once to act or say done
MAX_TURNS = int(os.environ.get("RFQ_MAX_TURNS", "80"))  # model calls per worker per round
KEEP_CHARS = int(os.environ.get("RFQ_KEEP_CHARS", "160000"))  # page dumps a worker keeps in full (~45k tokens)
MAX_OUTPUT = 120_000                                       # characters of one tool result
HEDGE_S = float(os.environ.get("RFQ_HEDGE_S", "5"))  # no token after this: a twin request on the next provider
STALL_S = 20          # no token for this long: the stream is dead (calls take 1–3 s)
MAX_TOKENS = 8000     # one answer; more is a repetition loop (DeepSeek's "Freigabe Freigabe ..." searches)
REPEATS = 2                                                # the same call with the same result: blocked after this

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
point, no thousands separator), and the marker stays next to it: "12345 (en-US)" means 12345. Copy such numbers as
they are. A price per price unit stays per price unit (e.g. 12345 per 100 pieces): never divide it yourself.
Only older page dumps are cut from your history: record what you read before moving on.

How to work:
1. Record the case from the brief first (kind 'case'; the bidder's mailbox domain only as a page shows it, e.g. the
   address the webmail says you are logged in as), then log in to your website.
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

NUDGE = ("You replied without a tool call. If anything you read is not recorded yet, or you meant to open or record "
         "something, make those tool calls now. If your website holds nothing more for the open gaps, reply with "
         "exactly: done")

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

The bid, already calculated; enter exactly these values, written the way the form expects numbers (its labels,
examples, placeholders or format hint tell: dot or comma decimals; each amount is given both ways):
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


class Stream:
    """One streamed request, read in a thread. `first` is set at the model's first token; `stop` makes the reader
    give up at its next line (the twin of a call that already has its answer)."""

    def __init__(self, body: dict):
        self.body, self.first, self.stop = body, threading.Event(), threading.Event()

    def run(self) -> dict:
        request = Request(URL, data=json.dumps(self.body).encode(), headers=HEADERS)
        with urlopen(request, timeout=STALL_S) as response:
            if not self.body.get("stream"):
                return json.load(response)
            content, calls, usage, provider, finish = [], {}, {}, "?", None
            last = time.monotonic()
            for raw in response:
                if self.stop.is_set():
                    raise TimeoutError("abandoned: the twin answered first")
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):   # OpenRouter's keep-alive comments: bytes, but no progress
                    if time.monotonic() - last > STALL_S:
                        raise TimeoutError(f"no tokens for {STALL_S:.0f}s")
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                if chunk.get("error"):
                    raise ValueError(str(chunk["error"])[:300])
                last = time.monotonic()
                provider = chunk.get("provider") or provider
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    content.append(delta.get("content") or "")
                    for tc in delta.get("tool_calls") or []:
                        index = tc.get("index")
                        if index is None:
                            index = tc.get("id") or (list(calls)[-1] if calls else 0)
                        slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        fn = tc.get("function") or {}
                        slot["id"] = tc.get("id") or slot["id"]
                        if fn.get("name") and fn["name"] != slot["name"]:
                            slot["name"] += fn["name"]
                        slot["arguments"] += fn.get("arguments") or ""
                    if delta.get("content") or delta.get("tool_calls"):
                        self.first.set()
                    finish = choice.get("finish_reason") or finish
        if finish is None:
            raise ValueError("the stream ended without a finish reason")
        message = {"content": "".join(content)}
        if calls:
            message["tool_calls"] = [{"id": c["id"], "function": {"name": c["name"], "arguments": c["arguments"]}}
                                     for c in calls.values()]
        return {"choices": [{"message": message}], "usage": usage, "provider": provider}


STREAM = os.environ.get("RFQ_STREAM", "1") == "1"   # turned off for the run if the endpoint refuses streaming


async def chat(messages: list[dict], tools: list[dict] | None, meter: dict) -> dict:
    """One model call; the assistant message. Hedged: a request with no token after HEDGE_S gets a twin on the next
    provider, and the first answer wins (hung calls cost 30 s each before). Retried on errors."""
    global STREAM
    body = {"model": MODEL, "messages": messages, "temperature": 0, "usage": {"include": True},
            "reasoning": {"enabled": False} if REASONING == "off" else {"effort": REASONING},
            "max_tokens": MAX_TOKENS}
    if tools:
        body["tools"] = tools
    order = PROVIDER["order"]
    for attempt in range(3):
        started = time.monotonic()
        streams: list[Stream] = []
        tasks: list[asyncio.Future] = []

        def launch(providers: list[str]) -> None:
            stream = Stream({**body, "stream": STREAM, "provider": {"order": providers, "allow_fallbacks": True}})
            streams.append(stream)
            tasks.append(asyncio.ensure_future(asyncio.to_thread(stream.run)))

        launch(order)
        while time.monotonic() - started < HEDGE_S and not tasks[0].done() and not streams[0].first.is_set():
            await asyncio.sleep(0.2)
        if HEDGE_S and len(order) > 1 and not tasks[0].done() and not streams[0].first.is_set():
            meter["hedged"] = meter.get("hedged", 0) + 1
            launch(order[1:] + order[:1])
        data, error, pending = None, None, set(tasks)
        while pending and data is None:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception() is None and "choices" in task.result():
                    data, winner = task.result(), tasks.index(task)
                    break
                error = task.exception() or ValueError(str(task.result().get("error") or task.result())[:300])
        for stream in streams:
            stream.stop.set()
        for task in pending:   # the losing twin ends on its own; its error is of no interest
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
        if data is not None:
            usage = data.get("usage") or {}
            meter["calls"] += 1
            meter["seconds"] += time.monotonic() - started
            meter["twin_won"] = meter.get("twin_won", 0) + (winner == 1)
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
        if isinstance(error, HTTPError):
            detail = error.read().decode("utf-8", errors="replace")[:300]
            print(f"  model HTTP {error.code} ({attempt + 1}/3): {detail}", flush=True)
            if error.code == 400 and STREAM and "stream" in detail.lower():
                STREAM = False
                print("  streaming refused: calls go unstreamed from now on", flush=True)
                continue
            if error.code in (400, 401, 402, 403) or attempt == 2:
                raise error
        else:
            print(f"  model error ({attempt + 1}/3) after {time.monotonic() - started:.0f}s: "
                  f"{type(error).__name__} {str(error)[:200]}", flush=True)
            if attempt == 2:
                raise error
        await asyncio.sleep(1 + attempt)
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
        "1,234.5). Units: a field named *_kg, *_g, *_mm, ... is in that unit; if the page shows another unit, send "
        '{"value": <the number as shown>, "unit": "<the unit as shown>"} and code converts it. Every number you '
        "record must be one a page showed you. kind 'case' takes one record with the case fields. Invalid records come back with the reason: "
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
        self.turns = 0
        self.pages: dict[str, int] = {}
        self.meter = {"calls": 0, "tool_calls": 0, "blocked": 0, "seconds": 0.0, "prompt_tokens": 0,
                      "completion_tokens": 0, "cached_tokens": 0, "cost_usd": 0.0}
        self.started = time.monotonic()
        self.last = ""
        self.system = system
        self.heard: list[str] = []   # what gaps and record told this worker: search terms may come from there too
        self.brief_numbers = {k for t in set(wt.NUMBER.findall(system)) for k in wt.number_keys(t)} if wt else set()

    def log(self, text: str) -> None:
        print(f"[{self.name}] {time.monotonic() - self.started:5.0f}s {text}", flush=True)

    async def call(self, name: str, args: dict) -> str:
        wt = self.wt
        if name in ("view", "submit"):
            if args.get("site") not in self.sites:
                return f"ERROR: you work on {', '.join(self.sites)} only; other workers cover the other websites."
            if name == "view":
                unread = self.unread_terms(args["site"], str(args.get("url") or "")) if not args.get("link") else []
                if unread:
                    self.meter["guessed"] = self.meter.get("guessed", 0) + 1
                    return (f"BLOCKED: {', '.join(repr(t) for t in unread)} appears on no page you read, in no record "
                            "and not in the brief. Search only for terms you have read (references, keys, groups, "
                            "names from pages or gaps); never guess or try a series.")
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
            kind = str(args.get("kind") or "")
            grounded, refused = self.grounded(kind, records)
            converted = []
            for r in grounded:
                try:
                    converted.append(convert_units(r, wt))
                except (ValueError, ArithmeticError) as error:
                    refused.append(f"{kind} {r.get(wt.KEYS.get(kind, ''), '')}: {error}")
            out = await asyncio.to_thread(wt.record, kind, converted) if converted else ""
            if refused:
                self.meter["ungrounded"] = self.meter.get("ungrounded", 0) + len(refused)
                self.log(f"UNGROUNDED {kind}: {'; '.join(refused)[:200]}")
                out += ("\nNOT saved, because these values appear on no page you opened (never invent or carry over "
                        "values: open the page that shows them, then record what it says): " + "; ".join(refused))
            return out.strip()
        if name == "gaps":
            waited = ""
            if args.get("wait"):
                waited = await wait_for_store(wt, min(float(args["wait"]), 90))
            return waited + await asyncio.to_thread(wt.gaps, str(args.get("kind") or ""))
        return f"ERROR: unknown tool {name!r}; use view, submit, record or gaps"

    def unread_terms(self, site: str, url: str) -> list[str]:
        """Query values of a URL the worker typed itself that it can't have read anywhere: guessed search terms.
        Page numbers and other small numbers pass; so does anything a page of this site, the store or the brief shows."""
        query = parse_qsl(urlsplit(url).query, keep_blank_values=False)
        if not query:
            return []
        known = "\n".join(self.wt.seen_text.get(site, []) + self.heard) + "\n" + self.system.lower()
        try:
            known += "\n" + self.wt.STORE.read_text().lower()
        except OSError:
            pass
        unread = []
        for _, value in query:
            words = value.lower().split()
            if len(words) > 3 and len(set(words)) < len(words) / 2:
                return [f"{value[:60]}... (the same words repeated)"]
            for term in value.lower().split():
                if re.fullmatch(r"\d{1,3}", term) or term in known:
                    continue
                unread.append(term)
        return unread

    def grounded(self, kind: str, records: list[dict]) -> tuple[list[dict], list[str]]:
        """Records whose numbers all appeared on a page this worker's sites showed (or in the brief); the others are
        refused with the values that appeared nowhere. Small whole numbers (counts, ids, 1-9) are not checked."""
        seen, units = set(self.brief_numbers), {}
        for site in self.sites:
            seen |= self.wt.seen_numbers.get(site, set())
            for key, us in self.wt.seen_units.get(site, {}).items():
                units.setdefault(key, set()).update(us)
        if kind == "case":   # shared by all workers: its e-mail domain must have appeared on any site's page
            domains = {m.lower() for m in self.wt.DOMAIN.findall(self.system)}
            for site_domains in self.wt.seen_domains.values():
                domains |= site_domains
            refused = []
            for record in records:
                domain = str(record.get("bidder_mail_domain") or "").strip().lower().lstrip("@")
                if domain and domain not in domains:
                    record.pop("bidder_mail_domain")
                    refused.append(f"bidder_mail_domain={domain} appears on no page yet (the webmail shows the "
                                   "address you are logged in as; whoever reads it records it)")
            return [r for r in records if r], refused
        ok, refused = [], []
        for record in records:
            bad = []
            for path, value in numbers(self.wt.numbers_from_raw(record)):
                try:
                    number = Decimal(str(value))
                except Exception:  # noqa: BLE001
                    continue
                if number == number.to_integral_value() and abs(number) < 10:
                    continue
                readings = self.wt.number_keys(value.strip()) if isinstance(value, str) else {self.wt.canonical(number)}
                if not readings & seen:
                    bad.append(f"{path}={value}")
                    continue
                field_unit = path.rsplit(".", 1)[-1].rsplit("_", 1)[-1]
                shown = units.get(self.wt.canonical(number), set())
                if field_unit in self.wt.UNIT_SCALE and not path.endswith(".value") and shown and field_unit not in shown:
                    unit = sorted(shown)[0]
                    bad.append(f"{path}={value}: the page shows {value} {unit}, the field is in {field_unit}; send "
                               f'{{"value": {value}, "unit": "{unit}"}} and code converts it')
            own = {m.lower() for m in self.wt.DOMAIN.findall(self.system)}
            for site in self.sites:
                own |= self.wt.seen_domains.get(site, set())
            for domain in {m.lower() for m in self.wt.DOMAIN.findall(json.dumps(record, ensure_ascii=False))}:
                if domain not in own:
                    bad.append(f"e-mail domain @{domain} (copy addresses exactly as the page shows them)")
            key = record.get(self.wt.KEYS.get(kind, "")) if kind in self.wt.KEYS else None
            if bad:
                refused.append(f"{kind} {key}: " + ", ".join(bad[:8]))
            else:
                ok.append(record)
        return ok, refused

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
        started = time.monotonic()
        try:
            result = await self.call(name, args)
        except Exception as error:  # noqa: BLE001  (a tool must never end the worker)
            result = f"ERROR: {type(error).__name__}: {error}"
        result = str(result)
        if name in ("view", "submit") or time.monotonic() - started > 5:
            status = re.search(r"\(HTTP \d+", result.split("\n", 1)[0])
            self.log(f"  -> {status.group(0)[1:] if status else result[:80]!r} {time.monotonic() - started:.1f}s "
                     f"{len(result)} chars")
        if len(result) > MAX_OUTPUT:
            result = (result[:MAX_OUTPUT] + f"\n... (cut: {len(result)} characters, {MAX_OUTPUT} shown; open a narrower "
                      "page, a search or the next page for the rest)")
        self.seen.setdefault(key, []).append(hashlib.sha1(result.encode()).hexdigest())
        if name in ("gaps", "record"):
            self.heard.append(result.lower())
        if name in ("view", "submit") and not result.startswith("ERROR"):
            page = page_hash(result)
            self.pages[page] = self.pages.get(page, 0) + 1
            if self.pages[page] >= 4:
                self.meter["blocked"] += 1
                self.log(f"CIRCLE {self.pages[page]}x {result.split(chr(10), 1)[0][:120]}")
                return (f"BLOCKED: this is the same page you already got {self.pages[page] - 1} times "
                        f"({result.split(chr(10), 1)[0]}). You are going in circles. Link numbers differ from page to "
                        "page: read the link text before following one (e.g. next vs. previous page), or open a "
                        "page by its URL.")
            if self.pages[page] >= 2:
                result = (f"NOTE: you already got this exact page {self.pages[page] - 1} time(s) before. Check the "
                          "link text: data-ref numbers are per page.\n" + result)
        return result

    async def run_calls(self, calls: list[dict]) -> list[str]:
        """The turn's calls in order, except that consecutive views by URL (they don't depend on the last dump) are
        fetched PARALLEL at a time. Afterwards the last of them in call order is the site's "last dump" for link=N."""
        results: list[str] = []
        i = 0
        while i < len(calls):
            batch = [calls[i]]
            while i + len(batch) < len(calls) and len(batch) < PARALLEL and by_url(calls[i]) and \
                    by_url(calls[i + len(batch)]):
                batch.append(calls[i + len(batch)])
            results += await asyncio.gather(*(self.tool(c) for c in batch))
            if len(batch) > 1:
                head = results[-1].split("\n", 1)[0]
                site, _, rest = head.partition(": ")
                url = rest.rsplit(" (HTTP", 1)[0]
                page = self.wt.pages.get(site, {}).get(url)
                if page is not None:
                    self.wt.current[site] = (url, page)
            i += len(batch)
        return results

    def trim(self) -> None:
        """When the page dumps in full exceed KEEP_CHARS, the oldest shrink to their first line until half the budget
        is left. In batches, not one per turn: the history stays unchanged between cuts, so its prefix stays cached.
        Dumps of the latest turn always stay (cutting them before the model read them made it reload pages in loops),
        and keeping more than the last two stops the rereading of pages whose dumps were cut too early."""
        last = max((i for i, m in enumerate(self.messages) if m.get("role") == "assistant"), default=-1)
        dumps = [m for i, m in enumerate(self.messages[:last]) if m.get("role") == "tool" and m.get("_dump")
                 and not m.get("_cut")]
        total = sum(len(m["content"]) for m in dumps) + \
            sum(len(m["content"]) for m in self.messages[last + 1:] if m.get("role") == "tool" and m.get("_dump"))
        if total <= KEEP_CHARS:
            return
        for m in dumps:
            if total <= KEEP_CHARS // 2:
                break
            total -= len(m["content"])
            head = m["content"].split("\n", 1)[0][:300]
            m["content"] = f"{head}\n(older page dump removed from your history: what you recorded from it is in " \
                           "gaps(kind=...); view it again only if you did not record it)"
            m["_cut"] = True

    async def turn(self, prompt: str, deadline: float) -> str:
        """The worker's loop for one prompt: until it answers without a tool call, or runs out of turns or time."""
        self.messages.append({"role": "user", "content": prompt})
        ACTIVE.add(self.name)
        try:
            return await self._turn(deadline)
        finally:
            ACTIVE.discard(self.name)

    async def _turn(self, deadline: float) -> str:
        nudged = 0
        for _ in range(MAX_TURNS):
            if time.monotonic() > deadline:
                self.log("time budget used up")
                return "timeout"
            self.trim()
            wire = [{k: v for k, v in m.items() if not k.startswith("_")} for m in self.messages]
            msg = await chat(wire, self.tools, self.meter)
            self.turns += 1
            self.messages.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                self.last = msg["content"].strip()
                self.log(f"says: {self.last[:200]}")
                # a prose reply ended the round even when it only announced the next step ("Let me record them"):
                # one run lost every stock figure that way and entered no answer
                if nudged < NUDGES and not re.fullmatch(r"\W*done\W*", self.last.lower()):
                    nudged += 1
                    self.messages.append({"role": "user", "content": NUDGE})
                    continue
                return self.last
            results = await self.run_calls(calls)
            for call, result in zip(calls, results):
                self.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result, "_turn": self.turns,
                                      "_dump": call["function"]["name"] in ("view", "submit") or
                                               (call["function"]["name"] == "gaps" and len(result) > 2000)})
        self.log(f"{MAX_TURNS} turns used up")
        return "turns"

    def metrics(self) -> dict:
        m = dict(self.meter)
        m.update(name=self.name, seconds=round(time.monotonic() - self.started, 1), cost_usd=round(m["cost_usd"], 5),
                 model_seconds=round(m["seconds"], 1))
        m.pop("seconds_", None)
        return m


def page_hash(result: str) -> str:
    """A page's identity: its URL line and content, without the per-request tokens (csrf, nonces) in attributes."""
    return hashlib.sha1(re.sub(r'value="[^"]*"', "", result).encode()).hexdigest()


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


PARALLEL = 4   # views by URL a worker fetches at once


def by_url(call: dict) -> bool:
    if call["function"]["name"] != "view":
        return False
    try:
        args = json.loads(call["function"]["arguments"] or "{}")
    except ValueError:
        return False
    return isinstance(args, dict) and bool(args.get("url")) and not args.get("link")


def convert_units(value, wt, key: str = ""):
    """{"value": 2125, "unit": "g/Stück"} (or "raw" for "value", with an optional "format") in a field named *_kg ->
    "2.125": the model copies, code converts. Raises ValueError when it can't."""
    if isinstance(value, dict):
        if "unit" in value and ("value" in value or "raw" in value) and set(value) <= {"value", "raw", "unit", "format"}:
            raw = str(value.get("value", value.get("raw"))).strip()
            fmt = str(value.get("format") or "").lower()[:2]
            number = Decimal(wt.plain_number(raw, fmt) if fmt in ("de", "en") else raw.replace(",", "."))
            unit = re.match(r"\s*(mm|cm|kg|g|t|m)(?![a-z])", str(value["unit"]).lower())
            target = key.rsplit("_", 1)[-1]
            if target not in wt.UNIT_SCALE:
                raise ValueError(f"{key} has no unit of its own: send the plain number")
            if not unit or wt.UNIT_SCALE[unit.group(1)][0] != wt.UNIT_SCALE[target][0]:
                raise ValueError(f"{key}: unit {value['unit']!r} cannot be converted to {target}")
            scaled = number * wt.UNIT_SCALE[unit.group(1)][1] / wt.UNIT_SCALE[target][1]
            return {"raw": f"{scaled.normalize():f}", "format": "en"}   # explicit, so the 3-decimals guard accepts it
        return {k: convert_units(v, wt, k) for k, v in value.items()}
    if isinstance(value, list):
        return [convert_units(v, wt, key) for v in value]
    return value


def numbers(value, path: str = "") -> list[tuple[str, object]]:
    """Every numeric leaf of a record (numbers and number-like strings) with its path."""
    if isinstance(value, dict):
        return [x for k, v in value.items() for x in numbers(v, f"{path}.{k}" if path else str(k))]
    if isinstance(value, list):
        return [x for i, v in enumerate(value) for x in numbers(v, f"{path}[{i}]")]
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\s*-?\d+(\.\d+)?\s*", value)):
        return [(path, value)]
    return []


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
    """1234.5 -> "1234.50 (de: 1.234,50)": both ways of writing it; the form decides which one it takes."""
    plain = f"{Decimal(str(value)):.2f}"
    de = f"{Decimal(str(value)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return plain if plain == de else f"{plain} (de: {de})"


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
    from resolver import resolve

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
    confirmed_no_bid = False
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
            no_bid = (resolution.answer or {}).get("decision") == "no_bid"
            if no_bid and not confirmed_no_bid and time.monotonic() < deadline - 60:
                confirmed_no_bid = True   # one re-check: a false no_bid scores almost nothing
                why = [line for line in resolution.trace if "no_bid" in line or "match" in line or "inactive" in line]
                questions = ("- The rules would decide NO BID: " + "; ".join(why[-12:]) + ". Before that stands, check "
                             "everything your website holds about it: for each requirement, search its catalogue group "
                             "(at most 20 results), open every listed material and record its status and every "
                             "attribute exactly as shown; record every Q&A or message that changes a requirement.")
            elif not resolution.gaps and not extra:
                break
            else:
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
    print("Trace:\n  " + "\n  ".join(resolution.trace), flush=True)
    print("Warnings: " + ("; ".join(resolution.warnings) or "none"), flush=True)
    if resolution.answer is None:
        print("No answer: nothing could be priced; gaps: " + "; ".join(g.question for g in resolution.gaps),
              flush=True)
        if not (evidence and evidence.case.tender_reference and evidence.positions):
            return
        # no answer scores 0; a bid that omits every unpriced position still earns the order-level points
        from resolver import position_key
        resolution.answer = {"tender_reference": evidence.case.tender_reference, "decision": "bid",
                             "currency": "EUR", "discount_percent": 0, "positions": [], "total_net": 0}
        resolution.omitted = sorted({p.position for p in evidence.positions}, key=position_key)
        print("Fallback: entering a bid that omits every position", flush=True)
    write_out("answer.json", resolution.answer)
    bid = bid_text(resolution.answer, resolution.omitted, evidence)
    print(f"Bid at {time.monotonic() - started:.0f}s ({research_s}s research, ${METRICS['cost_usd']:.3f}):\n{bid}",
          flush=True)
    submit = os.environ.get("RFQ_SUBMIT", "1" if os.environ.get("CHALLENGE_RUN_TOKEN") else "0") == "1"
    if not submit:
        print("Submission skipped (RFQ_SUBMIT is not 1; it is by default only in a run)", flush=True)
        return
    submitter = Worker("submit", sites, SYSTEM, wt, tools)
    try:
        text = await submitter.turn(SUBMIT.format(sites=sites_text(task), submission=submission_section(task["brief"]),
                                                  bid=bid), started + 1140)
    except HTTPError as error:
        if error.code != 401:
            raise
        text = submitter.last or "the run token expired (the run ended after the submission)"
        print(f"Model call refused with 401 after the submission: {text}", flush=True)
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
