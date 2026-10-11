"""The agent harness: a model in a tool loop, driving browsers through Playwright MCP.

The model (the hosted Qwen, OpenAI-compatible) chooses every navigation, search and document; this module only
relays its tool calls. Each browser gets its own Playwright MCP server attached over CDP to one of the run's
browsers, so tool names are prefixed by the browser's name (`tender__browser_click`). Local tools let the model
save what it found (`save_facts`), read a file it downloaded (`read_file`) and stop (`finish`).
When `extract` is on, each opened page is sent through extract.py; the browse loop only navigates.
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

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI

import extract
from evidence import facts_index, merge_dicts

HERE = Path(__file__).resolve().parent
PLAYWRIGHT_MCP = HERE / "node_modules" / ".bin" / "playwright-mcp"

# The browser tools a site agent needs; the rest (install, resize, ...) only add noise for the model.
BROWSER_TOOLS = {
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click", "browser_type",
    "browser_fill_form", "browser_select_option", "browser_press_key", "browser_hover", "browser_wait_for",
    "browser_handle_dialog", "browser_tabs", "browser_evaluate", "browser_file_upload",
}
MAX_RESULT_CHARS = 10000     # one tool result as the model sees it
OLD_RESULT_CHARS = 400       # older browser results, once newer ones exist
KEEP_FULL_RESULTS = 1        # the latest browser result stays whole
CALL_TIMEOUT_S = 120
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
                 facts_schema: dict | None = None, extract: bool = False, extract_mode: str = "survey"):
        self.name, self.browsers, self.model = name, browsers, model
        self.facts_schema = facts_schema
        self.do_extract = extract
        self.extract_mode = extract_mode
        self._last_extract = ""
        self._deadline = 0.0
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
        self.messages: list[dict] = []
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self._stack = AsyncExitStack()

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
        self.tools += _local_tools(self.facts_schema)
        return self

    async def __aexit__(self, *exc):
        self.log.close()
        await self._stack.aclose()

    def _print(self, text: str):
        print(f"[{self.name}] {text}", flush=True)

    async def run(self, system: str, task: str, deadline: float, max_turns: int = 80,
                  required: list[str] | None = None, progress_checks: bool = True,
                  resume: bool = False, extract_mode: str | None = None) -> dict:
        """Loop until the model calls finish, the turns run out or `deadline` (time.monotonic()) passes.
        `required`: top-level keys the saved facts must have; the model is reminded of missing ones and cannot
        finish without them (unless time runs out). `resume` keeps the browser session, facts and transcript."""
        if required is not None:
            self.required = required
        if extract_mode is not None:
            self.extract_mode = extract_mode
        self._deadline = deadline
        self.finished = None
        if resume and self.messages:
            self.messages.append({"role": "user", "content": task})
        else:
            if not resume:
                self.facts = None
            self.messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]
        messages = self.messages
        started, nudged, enough = time.monotonic(), False, False
        for turn in range(max_turns):
            left = deadline - time.monotonic()
            if left < 5:
                self._print("deadline reached")
                break
            if left < 45 and not nudged:
                messages.append({"role": "user", "content": "Less than a minute left: save_facts with everything you "
                                 "have now, then finish."})
                nudged = True
            if progress_checks and turn and turn % 8 == 0:
                missing = missing_keys(self.facts, self.required)
                messages.append({"role": "user", "content": "Progress check: save_facts on unread pages. "
                                 f"Recorded:\n{facts_index(self.facts)}" + (
                                     f"\nStill missing: {', '.join(missing)}." if missing else "")})
            if (progress_checks and not enough and turn >= 8
                    and not missing_keys(self.facts, self.required)):
                hint = fact_guidance(self.facts)
                messages.append({"role": "user", "content": hint or (
                    "Required lists are populated. save_facts, then finish.")})
                if not hint:
                    enough = True
            _compact(messages)
            try:
                response = await asyncio.wait_for(self.client.chat.completions.create(
                    model=self.model, messages=messages, tools=self.tools, temperature=0.2), timeout=min(left, 300))
            except (asyncio.TimeoutError, Exception) as error:  # noqa: BLE001  keep the facts saved so far
                self._print(f"model call failed: {error!r}")
                break
            self._count(response)
            message = response.choices[0].message
            messages.append(_assistant_message(message))
            self.log.write(json.dumps({"turn": turn, "assistant": messages[-1]}, ensure_ascii=False) + "\n")
            if not message.tool_calls:
                if self.finished is not None:
                    break
                messages.append({"role": "user", "content": "Continue with the tools. When you are done, "
                                 "save_facts, then finish."})
                continue
            for call in message.tool_calls:
                result = await self._call(call.function.name, call.function.arguments)
                ack = await self._extract_page(call.function.name, result)
                shown = result[:MAX_RESULT_CHARS]
                if ack:
                    shown = shown[:7000] + "\n" + ack
                self._print(f"{time.monotonic() - started:5.0f}s {call.function.name} {_loggable(call)}")
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.function.name,
                                 "content": shown})
                self.log.write(json.dumps({"turn": turn, "tool": call.function.name, "result": result[:3000]},
                                          ensure_ascii=False) + "\n")
            if self.finished is not None:
                break
        if self.facts is None:
            self.facts = self._load_facts()
        self.log.flush()
        return {"name": self.name, "seconds": round(time.monotonic() - started), "facts": self.facts,
                "summary": self.finished, "usage": self.usage}

    async def _extract_page(self, tool: str, result: str) -> str:
        """After a page-bearing tool, fill facts from that page. The browse model only navigates."""
        if not self.do_extract or not self.facts_schema:
            return ""
        page = extract.page_for_extract(tool, result)
        if page is None or time.monotonic() >= self._deadline - 8:
            return ""
        digest = page[:400]
        if digest == self._last_extract:
            return ""
        self._last_extract = digest
        try:
            facts, response = await extract.extract_page(
                self.client, self.model, self.facts_schema, self.facts, page, self.extract_mode,
                timeout=min(extract.EXTRACT_TIMEOUT_S, max(4.0, self._deadline - time.monotonic() - 4)))
        except Exception as error:  # noqa: BLE001  keep browsing; the next page can still extract
            self._print(f"extract failed: {error!r}")
            return "Extract failed on this page; open the next record."
        self._count(response)
        self.facts = facts
        self._write_facts()
        ack = self._facts_ack("Extracted.")
        self._print(ack)
        return ack

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
            self.facts = merge_dicts(self.facts, incoming) if isinstance(self.facts, dict) else incoming
            self._write_facts()
            return self._facts_ack("Saved.")
        if name == "finish":
            missing = missing_keys(self.facts, self.required)
            if missing and not any(key not in missing for key in self.required):
                return f"Not finished: the saved facts have no {', '.join(missing)}. Collect them, then finish."
            hint = fact_guidance(self.facts)
            if hint:
                return f"Not finished: {hint}"
            self.finished = args.get("summary", "")
            return "Finished." + (f" Still missing {', '.join(missing)}." if missing else "")
        if name == "read_file":
            return self._read_file(args.get("name", ""))
        browser, _, tool = name.partition("__")
        session = self.sessions.get(browser)
        if session is None or tool not in BROWSER_TOOLS:
            return f"Error: unknown tool {name}"
        try:
            result = await asyncio.wait_for(session.call_tool(tool, args), timeout=90)
        except Exception as error:  # noqa: BLE001  the model decides how to recover
            return f"Error: {error!r}"
        text = "\n".join(getattr(part, "text", "") for part in result.content if getattr(part, "type", "") == "text")
        return self._inline_files(text) or "(no text result)"

    def _write_facts(self):
        if self.facts is not None:
            self.facts_path.write_text(json.dumps(self.facts, ensure_ascii=False, indent=2))

    def _load_facts(self) -> dict | None:
        try:
            data = json.loads(self.facts_path.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _facts_ack(self, prefix: str) -> str:
        missing = missing_keys(self.facts, self.required)
        extra = f" Still missing: {', '.join(missing)}." if missing else (
            " Required lists have rows: finish unless an unread page still has records.")
        hint = fact_guidance(self.facts)
        if hint:
            extra = " " + hint
        return f"{prefix} {facts_index(self.facts)}.{extra}"

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


def _local_tools(facts_schema: dict | None = None) -> list[dict]:
    facts = facts_schema or {"type": "object"}
    return [
        {"type": "function", "function": {
            "name": "save_facts",
            "description": "Save everything you found so far as one JSON object. Later saves merge by record id.",
            "parameters": {"type": "object", "properties": {"facts": facts}, "required": ["facts"]}}},
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
    """Old page bodies dominate the context: keep only the latest page whole."""
    pages = [m for m in messages if m["role"] == "tool" and (
        "__browser_" in m.get("name", "") or m.get("name") == "read_file")]
    for message in pages[:-KEEP_FULL_RESULTS]:
        if len(message["content"]) > OLD_RESULT_CHARS:
            message["content"] = message["content"][:OLD_RESULT_CHARS] + "\n[... older page shortened]"


def unmarked_revision_ids(facts) -> list[str]:
    """Ids that are listed but have no marking words from a document."""
    facts = facts if isinstance(facts, dict) else {}
    out = []
    for row in facts.get("revisions") or []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        mark = str(row.get("marking") or "").strip().lower()
        raw = str(row.get("marking_raw") or "").strip().lower()
        if mark in {"current", "superseded"}:
            continue
        if mark not in {"", "unknown"} or raw:
            continue
        out.append(str(row["id"]))
    return out


def fact_guidance(facts) -> str:
    """Steer the browse model toward unread records. No site paths, no forced clicks."""
    unread = unmarked_revision_ids(facts)
    if unread:
        return ("Revision ids are listed, but none has marking words from a document body "
                f"({', '.join(unread)}). Open each revision document itself — a register or "
                "search hit is not a marking — copy the words the document uses, then save_facts.")
    return ""


def missing_keys(facts, required: list[str]) -> list[str]:
    """Required top-level keys that are absent or empty. `submitted` must be true, not just present."""
    facts = facts if isinstance(facts, dict) else {}
    missing = []
    for key in required:
        value = facts.get(key)
        if key == "submitted":
            if value is not True:
                missing.append(key)
        elif value in (None, "", [], {}):
            missing.append(key)
    return missing
