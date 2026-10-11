"""The agent harness: a model in a tool loop, driving browsers through Playwright MCP.

The model (the hosted Qwen, OpenAI-compatible) chooses every navigation, search and document; this module only
relays its tool calls. Each browser gets its own Playwright MCP server attached over CDP to one of the run's
browsers, so tool names are prefixed by the browser's name (`tender__browser_click`). Local tools let the model
save what it found (`save_facts`), read a file it downloaded (`read_file`) and stop (`finish`).
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time
import zipfile
from contextlib import AsyncExitStack
from pathlib import Path

from urllib.parse import urlparse

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI

HERE = Path(__file__).resolve().parent
PLAYWRIGHT_MCP = HERE / "node_modules" / ".bin" / "playwright-mcp"

# The browser tools a site agent needs; the rest (install, resize, ...) only add noise for the model.
BROWSER_TOOLS = {
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click", "browser_type",
    "browser_fill_form", "browser_select_option", "browser_press_key", "browser_hover", "browser_wait_for",
    "browser_handle_dialog", "browser_tabs", "browser_evaluate", "browser_file_upload",
}
MAX_RESULT_CHARS = 14000     # one tool result as the model sees it
OLD_RESULT_CHARS = 600       # older browser results, once newer ones exist
KEEP_FULL_RESULTS = 2        # the latest browser results stay whole
CALL_TIMEOUT_S = 240
SNAPSHOT_LINK = re.compile(r"\[Snapshot\]\(([^)]+\.yml)\)")


def qwen_client() -> AsyncOpenAI:
    return AsyncOpenAI(base_url=os.environ["QWEN_BASE_URL"], api_key=os.environ.get("QWEN_API_KEY", "local"),
                       timeout=CALL_TIMEOUT_S, max_retries=3)


def read_document(path: Path, limit: int = 30000) -> str:
    """Text of a downloaded file: ZIPs listed and their text members read, PDFs extracted, the rest decoded."""
    data = path.read_bytes()
    if zipfile.is_zipfile(io.BytesIO(data)):
        out = []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                inner = archive.read(name)
                out.append(f"--- {name} ---\n{_bytes_to_text(name, inner)}")
        return "\n".join(out)[:limit]
    return _bytes_to_text(path.name, data)[:limit]


def _bytes_to_text(name: str, data: bytes) -> str:
    if name.lower().endswith(".pdf") or data[:4] == b"%PDF":
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)
    for encoding in ("utf-8", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class Agent:
    """One model session with tools over named browsers. Use `async with Agent(...) as agent: await agent.run(...)`."""

    def __init__(self, name: str, browsers: dict[str, str], workdir: Path, model: str = "qwen",
                 must_search: list[str] | None = None, more_groups=None, home_hosts: dict[str, str] | None = None,
                 auto_drain: bool = False):
        self.name, self.browsers, self.model = name, browsers, model
        self.must_search = [str(g).strip().split()[0] for g in (must_search or []) if g]
        self.more_groups = more_groups
        self.home_hosts = {key: str(host).lower() for key, host in (home_hosts or {}).items() if host}
        self.auto_drain = auto_drain
        self._drained = 0
        self.dir = workdir / name
        self.downloads = self.dir / "downloads"
        self.downloads.mkdir(parents=True, exist_ok=True)
        self.facts_path = self.dir / "facts.json"
        self.log = (self.dir / "transcript.jsonl").open("w")
        self.sessions: dict[str, ClientSession] = {}
        self.tools: list[dict] = []
        self.client = qwen_client()
        self.facts = None
        self.finished = None
        self.required: list[str] = []
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self._stack = AsyncExitStack()
        self._model_timeouts = 0

    async def __aenter__(self):
        try:
            for browser, cdp_url in self.browsers.items():
                params = StdioServerParameters(command=str(PLAYWRIGHT_MCP), cwd=str(self.dir), args=[
                    "--cdp-endpoint", cdp_url, "--output-dir", str(self.downloads), "--image-responses", "omit",
                    "--timeout-action", "10000"])
                read, write = await self._stack.enter_async_context(stdio_client(params))
                session = await self._stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), 60)
                self.sessions[browser] = session
                for tool in (await asyncio.wait_for(session.list_tools(), 60)).tools:
                    if tool.name in BROWSER_TOOLS:
                        self.tools.append(_openai_tool(f"{browser}__{tool.name}", tool.description,
                                                       tool.input_schema))
        except BaseException:
            await self.__aexit__()
            raise
        self.tools += LOCAL_TOOLS
        return self

    async def __aexit__(self, *exc):
        self.log.close()
        await self._stack.aclose()

    def _print(self, text: str):
        print(f"[{self.name}] {text}", flush=True)

    def _groups(self) -> list[str]:
        groups = list(self.must_search)
        extra = self.more_groups() if callable(self.more_groups) else []
        for group in extra or []:
            token = str(group).strip().split()[0]
            if token and token not in groups:
                groups.append(token)
        return groups

    async def run(self, system: str, task: str, deadline: float, max_turns: int = 80,
                  required: list[str] | None = None, progress_checks: bool = True) -> dict:
        """Loop until the model calls finish, the turns run out or `deadline` (time.monotonic()) passes.
        `required`: top-level keys the saved facts must have; the model is reminded of missing ones and cannot
        finish without them (unless time runs out)."""
        self.required = required or []
        messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]
        started, nudged = time.monotonic(), False
        for turn in range(max_turns):
            left = deadline - time.monotonic()
            if left < 5:
                self._print("deadline reached")
                break
            if left < 60 and not nudged:
                missing = missing_keys(self.facts, self.required)
                if missing:
                    messages.append({"role": "user", "content": "Less than a minute left. Still missing "
                                     f"{', '.join(missing)}. Open those pages, save_facts, then finish."})
                else:
                    messages.append({"role": "user", "content": "Less than a minute left: save_facts with everything "
                                     "you have now, then finish."})
                nudged = True
            if progress_checks and turn and turn % 10 == 0:
                missing = missing_keys(self.facts, self.required)
                messages.append({"role": "user", "content": "Progress check: call save_facts now with everything "
                                 "you found so far." + (f" Still missing: {', '.join(missing)}." if missing else "")})
            _compact(messages)
            try:
                response = await asyncio.wait_for(self.client.chat.completions.create(
                    model=self.model, messages=messages, tools=self.tools, temperature=0), timeout=min(left, 180))
            except (asyncio.TimeoutError, TimeoutError) as error:
                self._model_timeouts += 1
                self._print(f"model call timed out ({self._model_timeouts}): {error!r}")
                if self._model_timeouts >= 3:
                    break
                continue
            except Exception as error:  # noqa: BLE001  keep the facts saved so far
                self._print(f"model call failed: {error!r}")
                break
            self._count(response)
            message = response.choices[0].message
            messages.append(_assistant_message(message))
            self.log.write(json.dumps({"turn": turn, "assistant": messages[-1]}, ensure_ascii=False) + "\n")
            if not message.tool_calls:
                if self.finished is not None:
                    break
                messages.append({"role": "user", "content": "Continue with the tools. When you are done, call "
                                 "save_facts with everything you found, then finish."})
                continue
            for call in message.tool_calls:
                result = await self._call(call.function.name, call.function.arguments)
                self._print(f"{time.monotonic() - started:5.0f}s {call.function.name} {_loggable(call)}")
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.function.name,
                                 "content": result[:MAX_RESULT_CHARS]})
                self.log.write(json.dumps({"turn": turn, "tool": call.function.name, "result": result[:3000]},
                                          ensure_ascii=False) + "\n")
            if self.finished is not None:
                break
        self.log.flush()
        return {"name": self.name, "seconds": round(time.monotonic() - started), "facts": self.facts,
                "summary": self.finished, "usage": self.usage}

    def _count(self, response):
        self.usage["calls"] += 1
        if response.usage:
            self.usage["prompt_tokens"] += response.usage.prompt_tokens or 0
            self.usage["completion_tokens"] += response.usage.completion_tokens or 0

    async def _call(self, name: str, arguments: str) -> str:
        try:
            args = json.loads(arguments or "{}")
        except ValueError:
            return f"Error: the arguments are not valid JSON: {arguments[:200]}"
        if name == "save_facts":
            incoming = args.get("facts", args)
            if isinstance(incoming, dict) and isinstance(self.facts, dict):
                from harvest import merge_facts
                self.facts = merge_facts(self.facts, incoming)
            else:
                self.facts = incoming
            self.facts_path.write_text(json.dumps(self.facts, ensure_ascii=False, indent=2))
            missing = missing_keys(self.facts, self.required)
            hint = unread_records(self.facts, must_search=self._groups())
            extra = (f" Still missing: {', '.join(missing)}." if missing else
                     (" Everything required is there." if not hint else ""))
            extra += f" {hint}" if hint else " Finish now, unless a value you know exists is still unread."
            return "Saved." + extra
        if name == "finish":
            missing = missing_keys(self.facts, self.required)
            if missing:
                return (f"Not finished: the saved facts have no {', '.join(missing)}. Collect them, call "
                        "save_facts with the complete JSON, then finish.")
            hint = unread_records(self.facts, must_search=self._groups())
            if hint:
                return f"Not finished: {hint}"
            self.finished = args.get("summary", "")
            return "Finished."
        if name == "read_file":
            return self._read_file(args.get("name", ""))
        browser, _, tool = name.partition("__")
        session = self.sessions.get(browser)
        if session is None or tool not in BROWSER_TOOLS:
            return f"Error: unknown tool {name}"
        if tool == "browser_navigate":
            blocked = host_block(self.home_hosts.get(browser), args.get("url") or "")
            if blocked:
                return blocked
        try:
            result = await asyncio.wait_for(session.call_tool(tool, args), timeout=90)
        except Exception as error:  # noqa: BLE001  the model decides how to recover
            return f"Error: {error!r}"
        text = "\n".join(getattr(part, "text", "") for part in result.content if getattr(part, "type", "") == "text")
        text = self._inline_files(text) or "(no text result)"
        self._ingest(text)
        if self.auto_drain and tool in {"browser_snapshot", "browser_navigate", "browser_click"}:
            extra = await self._drain(browser, session, text)
            if extra:
                text = extra
        return text

    def _ingest(self, text: str):
        """Code reads the page the model opened and unions the records into facts."""
        from extract import read_page
        from harvest import merge_facts
        got = read_page(text)
        if not got:
            return
        base = self.facts if isinstance(self.facts, dict) else {}
        self.facts = merge_facts(base, got)
        self.facts_path.write_text(json.dumps(self.facts, ensure_ascii=False, indent=2))

    async def _drain(self, browser: str, session, text: str) -> str:
        """Open unread rows of the result set the model is already on. Does not search the site."""
        from harvest import ref_for, unread_on_page
        if self._drained >= 20:
            return ""
        unread = unread_on_page(text, self.facts)
        opened = 0
        last = text
        for ident in unread[:6]:
            ref = ref_for(text, ident)
            if not ref:
                continue
            args = {"ref": ref, "element": ident}
            try:
                result = await asyncio.wait_for(session.call_tool("browser_click", args), 25)
            except Exception:
                try:
                    result = await asyncio.wait_for(
                        session.call_tool("browser_click", {"target": ref, "element": ident}), 25)
                except Exception:
                    continue
            page = "\n".join(getattr(part, "text", "") for part in result.content
                             if getattr(part, "type", "") == "text")
            page = self._inline_files(page)
            self._ingest(page)
            last = page or last
            opened += 1
            self._drained += 1
            try:
                await asyncio.wait_for(session.call_tool("browser_navigate_back", {}), 15)
            except Exception:
                pass
        if opened:
            self._print(f"opened {opened} listed records")
            try:
                snap = await asyncio.wait_for(session.call_tool("browser_snapshot", {}), 20)
                last = "\n".join(getattr(part, "text", "") for part in snap.content
                                 if getattr(part, "type", "") == "text")
                last = self._inline_files(last)
                self._ingest(last)
            except Exception:
                pass
        return last

    def _inline_files(self, text: str) -> str:
        """Playwright MCP sometimes links a snapshot file instead of including it: put the page into the result."""
        def inline(match):
            path = Path(match.group(1))
            path = path if path.is_absolute() else self.dir / path
            try:
                return "```yaml\n" + path.read_text()[:MAX_RESULT_CHARS] + "\n```"
            except OSError:
                return match.group(0)
        return SNAPSHOT_LINK.sub(inline, text)

    def _read_file(self, name: str) -> str:
        files = sorted(p for p in self.downloads.rglob("*") if p.is_file())
        if not name:
            return "Downloaded files:\n" + "\n".join(p.name for p in files) if files else "No files downloaded yet."
        match = next((p for p in files if p.name == name or str(p).endswith(name)), None)
        if match is None:
            return f"No downloaded file named {name}. Downloaded: {[p.name for p in files]}"
        try:
            return read_document(match)
        except Exception as error:  # noqa: BLE001
            return f"Error reading {name}: {error!r}"


LOCAL_TOOLS = [
    {"type": "function", "function": {
        "name": "save_facts",
        "description": "Save everything you found so far as one JSON object (replaces the previous save). Save "
                       "often, so your work survives if you are stopped.",
        "parameters": {"type": "object", "properties": {"facts": {"type": "object"}}, "required": ["facts"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a file the browser downloaded (ZIP, PDF, CSV, text). Without a name, list them.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "Stop working. Call save_facts first.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}},
]


def _loggable(call) -> str:
    """A tool call for the log, without what is typed into forms (logins, TANs)."""
    if call.function.name.endswith(("browser_fill_form", "browser_type")):
        try:
            args = json.loads(call.function.arguments or "{}")
        except ValueError:
            return "(form input)"
        fields = args.get("fields") or [args]
        return "(form input: " + ", ".join(str(f.get("name") or f.get("element") or "?") for f in fields)[:100] + ")"
    return call.function.arguments[:100]


def _openai_tool(name: str, description: str | None, schema: dict | None) -> dict:
    schema = dict(schema or {"type": "object", "properties": {}})
    schema.pop("$schema", None)
    return {"type": "function", "function": {"name": name, "description": (description or "")[:1000],
                                             "parameters": schema}}


def _assistant_message(message) -> dict:
    out = {"role": "assistant", "content": message.content or ""}
    if message.tool_calls:
        out["tool_calls"] = [{"id": c.id, "type": "function",
                              "function": {"name": c.function.name, "arguments": c.function.arguments}}
                             for c in message.tool_calls]
    return out


def _compact(messages: list[dict]):
    """Old page snapshots dominate the context: keep the latest few whole, shorten the older ones. Results that
    carry data the model asked for (evaluate, read_file) stay, so it never has to read a page twice."""
    snapshots = [m for m in messages if m["role"] == "tool" and "__browser_" in m.get("name", "")
                 and not m["name"].endswith("browser_evaluate") and "```yaml" in m["content"]]
    for message in snapshots[:-KEEP_FULL_RESULTS]:
        if len(message["content"]) > OLD_RESULT_CHARS:
            message["content"] = message["content"][:OLD_RESULT_CHARS] + "\n[... older page snapshot shortened]"


def host_block(allowed: str | None, url: str) -> str:
    """Reject navigation off the website this browser was given. Empty or relative URLs are allowed."""
    if not allowed or not url or not str(url).startswith("http"):
        return ""
    host = (urlparse(str(url)).hostname or "").lower()
    allowed = allowed.lower().split(":")[0]
    if not host or host == allowed or host.endswith("." + allowed) or allowed.endswith("." + host):
        return ""
    return f"Error: this browser stays on {allowed}. Do not open {host}."


def missing_keys(facts, required: list[str]) -> list[str]:
    """Required top-level keys that are absent or empty in the saved facts."""
    facts = facts if isinstance(facts, dict) else {}
    return [key for key in required if facts.get(key) in (None, "", [], {})]


def unread_records(facts, must_search: list[str] | None = None) -> str:
    """Block finish when a register was saved but the document bodies were not, or a catalogue group is unsearched."""
    from match import catalog_groups, covered_groups

    facts = facts if isinstance(facts, dict) else {}
    revisions = facts.get("revisions") if isinstance(facts.get("revisions"), list) else []
    requirements = facts.get("requirements") if isinstance(facts.get("requirements"), list) else []
    if revisions and not requirements:
        return ("Revision ids are listed, but no requirements from a document body. "
                "Open each revision document itself — a register row is not enough — then save_facts.")
    unmarked = []
    for row in revisions:
        if not isinstance(row, dict):
            continue
        mark = " ".join(str(row.get(k) or "") for k in ("marking", "status", "marking_raw")).lower()
        if not any(w in mark for w in ("gültig", "gueltig", "current", "aktuell", "ersetzt", "superseded", "ungültig")):
            if row.get("id"):
                unmarked.append(str(row["id"]))
    if unmarked:
        return ("These revisions have no marking words from a document: "
                + ", ".join(unmarked[:8]) + ". Open each document and copy the marking as written.")
    named = facts.get("catalog_groups")
    groups = [str(g).strip().split()[0] for g in named if g] if isinstance(named, list) else catalog_groups(facts)
    for group in must_search or []:
        token = str(group).strip().split()[0]
        if token and token not in groups:
            groups.append(token)
    searched = {str(g).strip().split()[0] for g in (facts.get("searched_groups") or []) if g}
    searched |= covered_groups(facts)
    missing = [group for group in groups if group not in searched]
    if missing:
        return ("Catalogue groups not yet searched: " + ", ".join(missing[:8]) +
                ". Search each group, save materials or an empty result, add it to searched_groups, then save_facts.")
    return ""
