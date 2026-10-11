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

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI

from evidence import facts_index, merge_dicts
from extract import apply_extract, extract_messages

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
        self.extract = extract
        self.extract_mode = extract_mode
        self._last_page_text = ""
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
        self.tools += _local_tools(self.facts_schema, extract=self.extract)
        return self

    async def __aexit__(self, *exc):
        self.log.close()
        await self._stack.aclose()

    def _print(self, text: str):
        print(f"[{self.name}] {text}", flush=True)

    async def run(self, system: str, task: str, deadline: float, max_turns: int = 80,
                  required: list[str] | None = None, progress_checks: bool = True) -> dict:
        """Loop until the model calls finish, the turns run out or `deadline` (time.monotonic()) passes.
        `required`: top-level keys the saved facts must have; the model is reminded of missing ones and cannot
        finish without them (unless time runs out)."""
        self.required = required or []
        self.finished, self.facts = None, None
        messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]
        started, nudged, enough = time.monotonic(), False, False
        for turn in range(max_turns):
            left = deadline - time.monotonic()
            if left < 5:
                self._print("deadline reached")
                break
            if left < 45 and not nudged:
                messages.append({"role": "user", "content": "Less than a minute left: record_page on the current "
                                 "page if it has unread records, then finish." if self.extract else
                                 "Less than a minute left: save_facts with everything you have now, then finish."})
                nudged = True
            if progress_checks and turn and turn % 8 == 0:
                missing = missing_keys(self.facts, self.required)
                recorded = facts_index(self.facts)
                action = "record_page" if self.extract else "save_facts"
                messages.append({"role": "user", "content": f"Progress check: {action} on unread pages. "
                                 f"Recorded:\n{recorded}" + (f"\nStill missing: {', '.join(missing)}." if missing else "")})
            if (progress_checks and not enough and turn >= 8
                    and not missing_keys(self.facts, self.required)):
                messages.append({"role": "user", "content": "Required lists have rows. record_page any unread page, "
                                 "then finish. Do not re-read pages you already recorded." if self.extract else
                                 "Required lists are populated. save_facts, then finish."})
                enough = True
            _compact(messages)
            try:
                response = await asyncio.wait_for(self.client.chat.completions.create(
                    model=self.model, messages=messages, tools=self.tools, temperature=0.2), timeout=min(left, 300))
            except TimeoutError:
                self._print("model call timed out")
                if deadline - time.monotonic() > 20:
                    messages.append({"role": "user", "content": "That call timed out. record_page the page on "
                                     "screen if it has records, then continue." if self.extract else
                                     "That call timed out. save_facts with what you have, then continue."})
                    continue
                break
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
                messages.append({"role": "user", "content": "Continue with the tools. When a page has records, "
                                 "record_page, then finish when the lists are complete." if self.extract else
                                 "Continue with the tools. When you are done, save_facts, then finish."})
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
            self.facts = merge_dicts(self.facts, incoming) if self.extract else incoming
            self._write_facts()
            return self._facts_ack("Saved.")
        if name == "record_page":
            return await self._record_page(args.get("text") or self._last_page_text, args.get("note", ""))
        if name == "finish":
            missing = missing_keys(self.facts, self.required)
            if missing:
                return (f"Not finished: no {', '.join(missing)} yet. Open those pages, record_page, then finish."
                        if self.extract else
                        f"Not finished: the saved facts have no {', '.join(missing)}. Collect them, then finish.")
            self.finished = args.get("summary", "")
            return "Finished."
        if name == "read_file":
            text = self._read_file(args.get("name", ""))
            self._last_page_text = text
            return text
        browser, _, tool = name.partition("__")
        session = self.sessions.get(browser)
        if session is None or tool not in BROWSER_TOOLS:
            return f"Error: unknown tool {name}"
        try:
            result = await asyncio.wait_for(session.call_tool(tool, args), timeout=90)
        except Exception as error:  # noqa: BLE001  the model decides how to recover
            return f"Error: {error!r}"
        text = "\n".join(getattr(part, "text", "") for part in result.content if getattr(part, "type", "") == "text")
        text = self._inline_files(text) or "(no text result)"
        if tool in {"browser_snapshot", "browser_evaluate", "browser_navigate"}:
            self._last_page_text = text
        return text

    def _write_facts(self):
        if self.facts is not None:
            self.facts_path.write_text(json.dumps(self.facts, ensure_ascii=False, indent=2))

    def _facts_ack(self, prefix: str) -> str:
        missing = missing_keys(self.facts, self.required)
        extra = f" Still missing: {', '.join(missing)}." if missing else (
            " Required lists have rows: finish unless an unread page still has records.")
        return f"{prefix} {facts_index(self.facts)}.{extra}"

    async def _record_page(self, page: str, note: str = "") -> str:
        if not (page or "").strip() or page == "(no text result)":
            return "No page text. Call browser_evaluate with () => document.body.innerText, then record_page."
        left = 60.0
        messages = extract_messages(self.facts_schema, self.facts, page, self.extract_mode, note)
        try:
            response = await asyncio.wait_for(self.client.chat.completions.create(
                model=self.model, messages=messages, temperature=0, response_format={"type": "json_object"}),
                timeout=left)
        except Exception:
            try:
                response = await asyncio.wait_for(self.client.chat.completions.create(
                    model=self.model, messages=messages, temperature=0), timeout=left)
            except Exception as error:  # noqa: BLE001
                return f"Extract failed: {error!r}. Try browser_evaluate innerText and record_page again."
        self._count(response)
        text = (response.choices[0].message.content or "") if response.choices else ""
        try:
            self.facts = apply_extract(self.facts, text)
        except (ValueError, json.JSONDecodeError) as error:
            return f"Extract was not JSON: {error}. Retry record_page."
        self._write_facts()
        return self._facts_ack("Recorded.")

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


def _local_tools(facts_schema: dict | None = None, extract: bool = False) -> list[dict]:
    facts = facts_schema or {"type": "object"}
    tools = []
    if extract:
        tools.append({"type": "function", "function": {
            "name": "record_page",
            "description": "Extract records from the current page (or from text) into the evidence store. "
                           "Call after you open a list, document or message. Do not type the JSON yourself.",
            "parameters": {"type": "object", "properties": {
                "note": {"type": "string", "description": "What to look for on this page"},
                "text": {"type": "string", "description": "Page text if you already have it; otherwise the last page"},
            }}}})
    tools += [
        {"type": "function", "function": {
            "name": "save_facts",
            "description": "Save a JSON object of facts. Prefer record_page during research." if extract else
                           "Save everything you found so far as one JSON object (replaces the previous save).",
            "parameters": {"type": "object", "properties": {"facts": facts}, "required": ["facts"]}}},
        {"type": "function", "function": {
            "name": "read_file",
            "description": "Read a file the browser downloaded (ZIP, PDF, CSV, text). Without a name, list them.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}}}},
        {"type": "function", "function": {
            "name": "finish",
            "description": "Stop working. Record unread pages first." if extract else "Stop working. Call save_facts first.",
            "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}},
    ]
    return tools


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
    """Old page bodies dominate the context. After record_page the extract has the data; keep only the latest page."""
    pages = [m for m in messages if m["role"] == "tool" and (
        "__browser_" in m.get("name", "") or m.get("name") == "read_file")]
    for message in pages[:-KEEP_FULL_RESULTS]:
        if len(message["content"]) > OLD_RESULT_CHARS:
            message["content"] = message["content"][:OLD_RESULT_CHARS] + "\n[... older page shortened]"


def missing_keys(facts, required: list[str]) -> list[str]:
    """Required top-level keys that are absent or empty in the saved facts."""
    facts = facts if isinstance(facts, dict) else {}
    return [key for key in required if facts.get(key) in (None, "", [], {})]
