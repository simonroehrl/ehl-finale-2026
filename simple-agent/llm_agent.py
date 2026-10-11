"""Qwen opens pages and writes Evidence through save_facts. Code merges and later resolves."""
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

from evidence import facts_index, merge_dicts

HERE = Path(__file__).resolve().parent
PLAYWRIGHT_MCP = HERE / "node_modules" / ".bin" / "playwright-mcp"
BROWSER_TOOLS = {
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click", "browser_type",
    "browser_fill_form", "browser_select_option", "browser_press_key", "browser_hover", "browser_wait_for",
    "browser_handle_dialog", "browser_tabs", "browser_evaluate", "browser_file_upload",
}
PAGE_CHANGE = {"browser_navigate", "browser_navigate_back", "browser_click", "browser_snapshot",
               "browser_select_option"}
MAX_RESULT_CHARS = 8000
OLD_RESULT_CHARS = 300
KEEP_FULL_RESULTS = 1
CALL_TIMEOUT_S = 90
SNAPSHOT_LINK = re.compile(r"\[Snapshot\]\(([^)]+\.yml)\)")
SAVE_REMINDER = (
    "If this page has records, call save_facts now with every row (schema keys), "
    "then open the next unread record."
)


def qwen_client() -> AsyncOpenAI:
    return AsyncOpenAI(base_url=os.environ["QWEN_BASE_URL"], api_key=os.environ.get("QWEN_API_KEY", "local"),
                       timeout=CALL_TIMEOUT_S, max_retries=2)


def read_document(path: Path, limit: int = 30000) -> str:
    data = path.read_bytes()
    if zipfile.is_zipfile(io.BytesIO(data)):
        out = []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                out.append(f"--- {name} ---\n{_bytes_to_text(name, archive.read(name))}")
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
    def __init__(self, name: str, browsers: dict[str, str], workdir: Path, model: str = "qwen",
                 facts_schema: dict | None = None):
        self.name, self.browsers, self.model = name, browsers, model
        self.facts_schema = facts_schema
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

    async def run(self, system: str, task: str, deadline: float, max_turns: int = 40,
                  required: list[str] | None = None, resume: bool = False) -> dict:
        if required is not None:
            self.required = required
        self._deadline = deadline
        self.finished = None
        if resume and self.messages:
            self.messages.append({"role": "user", "content": task})
        else:
            if not resume:
                self.facts = None
            self.messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]
        started = time.monotonic()
        for turn in range(max_turns):
            left = deadline - time.monotonic()
            if left < 6:
                self._print("deadline reached")
                break
            _compact(self.messages)
            try:
                response = await asyncio.wait_for(self.client.chat.completions.create(
                    model=self.model, messages=self.messages, tools=self.tools, temperature=0.2),
                    timeout=min(left, CALL_TIMEOUT_S))
            except (asyncio.TimeoutError, Exception) as error:  # noqa: BLE001
                self._print(f"model call failed: {error!r}")
                break
            self._count(response)
            message = response.choices[0].message
            self.messages.append(_assistant_message(message))
            self.log.write(json.dumps({"turn": turn, "assistant": self.messages[-1]}, ensure_ascii=False) + "\n")
            if not message.tool_calls:
                if self.finished is not None:
                    break
                self.messages.append({"role": "user", "content": "Continue with the tools, then finish."})
                continue
            for call in message.tool_calls:
                result = await self._call(call.function.name, call.function.arguments)
                ack = self._nudge(call.function.name)
                shown = result[:MAX_RESULT_CHARS]
                if ack:
                    shown = f"{shown}\n{ack}"
                self._print(f"{time.monotonic() - started:5.0f}s {call.function.name} {_loggable(call)}")
                self.messages.append({"role": "tool", "tool_call_id": call.id, "name": call.function.name,
                                      "content": shown})
            if self.finished is not None:
                break
        if self.facts is None:
            self.facts = self._load_facts()
        self.log.flush()
        return {"name": self.name, "seconds": round(time.monotonic() - started), "facts": self.facts,
                "summary": self.finished, "usage": self.usage}

    def _nudge(self, name: str) -> str:
        if not self.facts_schema or time.monotonic() >= self._deadline - 8:
            return ""
        tool = name.partition("__")[2] or name
        if tool in PAGE_CHANGE or tool == "read_file":
            return SAVE_REMINDER
        return ""

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
            index = facts_index(self.facts)
            if index == "(none yet)":
                return ("Saved nothing usable. Each row needs its id: positions.position, qa.number, "
                        "revisions.id, materials.number, erp_searches.catalog_group, messages.id.")
            return f"Saved. {index}."
        if name == "finish":
            missing = missing_keys(self.facts, self.required)
            if missing and not any(key not in missing for key in self.required):
                return f"Not finished: no {', '.join(missing)} yet."
            self.finished = args.get("summary", "")
            return "Finished."
        if name == "read_file":
            return self._read_file(args.get("name", ""))
        browser, _, tool = name.partition("__")
        session = self.sessions.get(browser)
        if session is None or tool not in BROWSER_TOOLS:
            return f"Error: unknown tool {name}"
        try:
            result = await asyncio.wait_for(session.call_tool(tool, args), timeout=60)
        except Exception as error:  # noqa: BLE001
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

    def _inline_files(self, text: str) -> str:
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
            "description": "Write every structured record visible on the current page. Same id updates a row.",
            "parameters": {"type": "object", "properties": {"facts": facts}, "required": ["facts"]}}},
        {"type": "function", "function": {
            "name": "read_file",
            "description": "Read a downloaded file (ZIP, PDF, CSV, text). Without a name, list them.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}}}},
        {"type": "function", "function": {
            "name": "finish",
            "description": "Stop. Call when unread result pages are gone and required lists have rows.",
            "parameters": {"type": "object", "properties": {"summary": {"type": "string"}},
                           "required": ["summary"]}}},
    ]


def _loggable(call) -> str:
    if call.function.name.endswith(("browser_fill_form", "browser_type")):
        return "(form input)"
    return (call.function.arguments or "")[:80]


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
    pages = [m for m in messages if m["role"] == "tool"]
    for message in pages[:-KEEP_FULL_RESULTS]:
        if len(message["content"]) > OLD_RESULT_CHARS:
            message["content"] = message["content"][:OLD_RESULT_CHARS] + "\n[... shortened]"


def missing_keys(facts, required: list[str]) -> list[str]:
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
