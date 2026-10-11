"""Compact page view: numbered controls the model can point at, plus a short text extract."""
from __future__ import annotations

import asyncio
import tempfile
import zipfile
from pathlib import Path

SNAPSHOT_JS = """
(start) => {
  const nodes = [];
  let next = start;
  const selector = 'a, button, input, textarea, select, summary, article, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="option"], [role="row"], [role="listitem"]';
  function walk(root) {
    if (!root || !root.querySelectorAll || nodes.length >= 90) return;
    for (const el of root.querySelectorAll(selector)) {
      if (nodes.length >= 90) break;
      const rect = el.getBoundingClientRect();
      if (rect.width < 2 || rect.height < 2) continue;
      const style = window.getComputedStyle(el);
      if (style.visibility === 'hidden' || style.display === 'none') continue;
      const name = (el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') || el.getAttribute('name') || '')
        .replace(/\\s+/g, ' ').trim().slice(0, 140);
      el.setAttribute('data-qwen-id', String(next));
      nodes.push({
        i: next,
        role: (el.getAttribute('role') || el.tagName || '').toLowerCase(),
        name,
        href: (el.getAttribute('href') || '').slice(0, 200),
        type: (el.getAttribute('type') || '').toLowerCase()
      });
      next += 1;
    }
    for (const el of root.querySelectorAll('*')) {
      if (nodes.length >= 90) break;
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  }
  walk(document);
  const full = document.body ? document.body.innerText : '';
  return {
    title: document.title || '',
    url: location.href,
    nodes,
    text: (full || '').replace(/\\n{3,}/g, '\\n\\n').trim().slice(0, 5000),
    truncated: (full || '').length > 5000
  };
}
"""


CHROME_UI = ("adresse", "neu laden", "downloadleiste", "zum entsperren", "download chrome",
             "sitzungsprotokoll")
CHROME_EXACT = {"zurück", "öffnen"}


def _chrome_ui(node: dict) -> bool:
    name = (node.get("name") or "").strip().casefold()
    return name in CHROME_EXACT or any(word in name for word in CHROME_UI)


def format_view(snapshot: dict, *, extra: str = "", compact: bool = False) -> str:
    lines = [
        f"URL: {snapshot.get('url') or ''}",
        f"Title: {snapshot.get('title') or ''}",
    ]
    if snapshot.get("archive_files"):
        lines.append("Archive files: " + ", ".join(snapshot["archive_files"]))
    lines.append("Controls:")
    nodes = snapshot.get("nodes") or []
    if compact:
        nodes = nodes[:40]
    for node in nodes:
        href = f" {node['href']}" if node.get("href") else ""
        typ = f" {node['type']}" if node.get("type") else ""
        lines.append(f"  [{node['i']}] {node.get('role','')}{typ} {node.get('name','')}{href}".rstrip())
    lines.append("Text:")
    lines.append((snapshot.get("text") or "")[:600 if compact else 4500])
    if extra:
        lines.append(extra)
    return "\n".join(lines)


class Driver:
    def __init__(self, page):
        self.page = page
        self.opened_text = ""
        self.archive_files: list[str] = []
        self._archive: dict[str, str] = {}
        self._frames: dict[int, object] = {}
        self._nodes: list[dict] = []
        self._tmp = tempfile.TemporaryDirectory()
        self.page.set_default_timeout(12000)
        self.page.set_default_navigation_timeout(45000)

    async def open(self, url: str) -> None:
        last = None
        for attempt in range(3):
            try:
                await self.page.goto(url, wait_until="commit", timeout=15000)
                await asyncio.sleep(0.6)
                return
            except Exception as error:  # noqa: BLE001
                last = error
                await asyncio.sleep(1 + attempt)
        raise RuntimeError(f"could not open {url}: {last}")

    async def snapshot(self) -> dict:
        self._frames = {}
        nodes: list[dict] = []
        texts: list[str] = []
        title, url, truncated = "", self.page.url, False
        nxt = 0
        for frame in self.page.frames:
            try:
                part = await asyncio.wait_for(frame.evaluate(SNAPSHOT_JS, nxt), 6)
            except Exception:
                continue
            if frame == self.page.main_frame:
                title = part.get("title") or title
                url = part.get("url") or url
            truncated = truncated or bool(part.get("truncated"))
            if part.get("text"):
                texts.append(part["text"])
            for node in part.get("nodes") or []:
                if _chrome_ui(node):
                    continue
                self._frames[node["i"]] = frame
                nodes.append(node)
                nxt = max(nxt, int(node["i"]) + 1)
        self._nodes = nodes
        text = "\n\n".join(texts)[:7000]
        if self.opened_text:
            text = self.opened_text[:14000]
        if self.archive_files:
            text = "Archive files:\n" + "\n".join(self.archive_files) + "\n\n" + text
        return {
            "url": url,
            "title": title,
            "nodes": nodes,
            "text": text,
            "truncated": truncated,
            "archive_files": list(self.archive_files),
        }

    async def reading(self) -> str:
        if self.opened_text:
            text = self.opened_text
            self.opened_text = ""
            return text[:16000]
        try:
            return (await self.page.inner_text("body", timeout=8000))[:16000]
        except Exception:
            return ""

    def node_name(self, target: int | None) -> str:
        node = next((item for item in self._nodes if item.get("i") == target), {})
        return node.get("name") or ""

    async def perform(self, action) -> str:
        try:
            if action.op == "wait":
                await asyncio.sleep(1)
                return "ok"
            if action.op == "back":
                await self.page.go_back(timeout=15000, wait_until="domcontentloaded")
                return "ok"
            if action.op == "scroll":
                await self.page.mouse.wheel(0, 700)
                return "ok"
            if action.op == "press":
                await self.page.keyboard.press(action.text or "Enter")
                return "ok"
            if action.op == "open_archive":
                self.opened_text = self._archive.get(action.file or "", "")
                return "downloaded" if self.opened_text else "error"
            frame = self._frames.get(action.target)
            if frame is None:
                return "error"
            locator = frame.locator(f'[data-qwen-id="{action.target}"]').first
            if action.op == "fill":
                await locator.fill(action.text or "", timeout=8000)
                return "filled"
            if action.op == "collect_pages":
                parts = [await self._body()]
                for _ in range(min(8, action.times or 1)):
                    before = parts[-1]
                    await locator.click(timeout=8000)
                    await asyncio.sleep(0.4)
                    current = await self._body()
                    if current == before:
                        break
                    parts.append(current)
                self.opened_text = "\n\n--- page ---\n\n".join(parts)[:20000]
                return "downloaded"
            if action.op == "click":
                if self._looks_like_file(action.target):
                    return await self._click_maybe_download(locator)
                await self._click(locator)
                try:
                    await self.page.wait_for_load_state("domcontentloaded", timeout=4000)
                except Exception:
                    pass
                return "ok"
            return "error"
        except Exception as error:  # noqa: BLE001
            print(f"[browser] {action.op} failed: {type(error).__name__}", flush=True)
            return "error"

    async def _click(self, locator) -> None:
        try:
            await locator.click(timeout=2500)
            return
        except Exception:
            pass
        try:
            await locator.click(timeout=2000, force=True)
            return
        except Exception:
            pass
        await locator.evaluate("el => el.click()")

    async def _body(self) -> str:
        try:
            return await self.page.inner_text("body", timeout=5000)
        except Exception:
            return ""

    def _looks_like_file(self, target: int | None) -> bool:
        node = next((item for item in self._nodes if item.get("i") == target), {})
        label = f"{node.get('name', '')} {node.get('href', '')}".lower()
        return any(piece in label for piece in (".pdf", ".zip", ".txt", ".html", "download", "herunterladen"))

    async def _click_maybe_download(self, locator) -> str:
        try:
            async with self.page.expect_download(timeout=4000) as info:
                await locator.click(timeout=8000)
            download = await info.value
            target = Path(self._tmp.name) / Path(download.suggested_filename).name
            await download.save_as(str(target))
            self._ingest(target, target.name)
            return "downloaded"
        except Exception:
            return "ok"

    def _ingest(self, path: Path, name: str) -> None:
        lower = name.lower()
        if lower.endswith(".pdf"):
            self.opened_text = _pdf_text(path)
            return
        if lower.endswith(".zip"):
            self._archive = _zip_texts(path, Path(self._tmp.name))
            self.archive_files = sorted(self._archive)
            return
        try:
            self.opened_text = path.read_text(encoding="utf-8", errors="replace")[:16000]
        except OSError:
            self.opened_text = ""

    def close(self) -> None:
        self._tmp.cleanup()


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)[:16000]
    except Exception:
        return ""


def _zip_texts(path: Path, directory: Path) -> dict[str, str]:
    found = {}
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = Path(info.filename).name
                target = directory / name
                target.write_bytes(archive.read(info))
                if name.lower().endswith(".pdf"):
                    found[name] = _pdf_text(target)
                elif name.lower().endswith((".txt", ".html", ".htm", ".xml", ".csv")):
                    found[name] = target.read_text(encoding="utf-8", errors="replace")[:16000]
                else:
                    found[name] = ""
    except zipfile.BadZipFile:
        return {}
    return found
