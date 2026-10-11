"""Playwright adapter. The sandbox browsers are attached over CDP; locally Chromium is started."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import zipfile
from pathlib import Path

SNAPSHOT_JS = """
(start) => {
  const nodes = [];
  let next = start;
  const selector = 'a, button, input, textarea, select, summary, [role="button"], [role="link"], [role="tab"], [role="menuitem"], [role="option"]';
  function walk(root) {
    if (!root || !root.querySelectorAll || nodes.length >= 70) return;
    for (const el of root.querySelectorAll(selector)) {
      if (nodes.length >= 70) break;
      const rect = el.getBoundingClientRect();
      if (rect.width < 2 || rect.height < 2) continue;
      const style = window.getComputedStyle(el);
      if (style.visibility === 'hidden' || style.display === 'none') continue;
      const name = (el.getAttribute('aria-label') || el.innerText || el.getAttribute('placeholder') || el.getAttribute('name') || '')
        .replace(/\\s+/g, ' ').trim().slice(0, 140);
      el.setAttribute('data-opt-id', String(next));
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
      if (nodes.length >= 70) break;
      if (el.shadowRoot) walk(el.shadowRoot);
    }
  }
  walk(document);
  const full = document.body ? document.body.innerText : '';
  return {
    title: document.title || '',
    url: location.href,
    nodes,
    text: (full || '').replace(/\\n{3,}/g, '\\n\\n').trim().slice(0, 7000),
    truncated: (full || '').length > 7000
  };
}
"""


def _cdp_urls() -> list[str]:
    try:
        urls = [url for url in json.loads(os.environ.get("CDP_URLS") or "[]") if isinstance(url, str)]
    except ValueError:
        urls = []
    if urls:
        return urls
    if os.environ.get("CDP_URL"):
        return [os.environ["CDP_URL"]]
    return []


class PlaywrightDriver:
    def __init__(self, page):
        self.page = page
        self.signed = False
        self.opened_text = ""
        self.archive_files: list[str] = []
        self._archive: dict[str, str] = {}
        self._frames: dict[int, object] = {}
        self._nodes: list[dict] = []
        self._tmp = tempfile.TemporaryDirectory()

    async def open_start(self, url: str) -> None:
        last = None
        for attempt in range(3):
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
                return
            except Exception as error:  # noqa: BLE001 - a flaky first load is retried
                last = error
                await asyncio.sleep(1 + attempt)
        raise RuntimeError(f"could not open the site: {last}")

    async def snapshot(self) -> dict:
        self._frames = {}
        nodes: list[dict] = []
        texts: list[str] = []
        title, url, truncated = "", self.page.url, False
        for frame in self.page.frames:
            try:
                part = await frame.evaluate(SNAPSHOT_JS, len(nodes))
            except Exception:
                continue
            if frame == self.page.main_frame:
                title = part.get("title") or title
                url = part.get("url") or url
            truncated = truncated or bool(part.get("truncated"))
            if part.get("text"):
                texts.append(part["text"])
            for node in part.get("nodes") or []:
                self._frames[node["i"]] = frame
                nodes.append(node)
        self._nodes = nodes
        text = "\n\n".join(texts)[:9000]
        if self.opened_text:
            text = self.opened_text[:15000]
        if self.archive_files:
            text = "Archive files:\n" + "\n".join(self.archive_files) + "\n\n" + text
        return {
            "url": url,
            "title": title,
            "nodes": nodes,
            "text": text,
            "truncated": truncated,
            "archive_files": list(self.archive_files),
            "opened_document": bool(self.opened_text),
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

    async def perform(self, action) -> str:
        try:
            if action.op == "wait":
                await asyncio.sleep(1)
                return "navigated"
            if action.op == "back":
                await self.page.go_back(timeout=15000, wait_until="domcontentloaded")
                return "navigated"
            if action.op == "scroll":
                await self.page.mouse.wheel(0, 700)
                return "navigated"
            if action.op == "press":
                await self.page.keyboard.press(action.text or "Enter")
                return "navigated"
            if action.op == "open_archive":
                self.opened_text = self._archive.get(action.file or "", "")
                return "downloaded" if self.opened_text else "error"
            frame = self._frames.get(action.target)
            if frame is None:
                return "error"
            locator = frame.locator(f'[data-opt-id="{action.target}"]').first
            if action.op == "fill":
                await locator.fill(action.text or "", timeout=8000)
                return "filled"
            if action.op == "collect_pages":
                parts = [await self._body()]
                for _ in range(action.times or 1):
                    before = parts[-1]
                    await locator.click(timeout=8000)
                    await asyncio.sleep(0.5)
                    current = await self._body()
                    if current == before:
                        break
                    parts.append(current)
                self.opened_text = "\n\n--- page ---\n\n".join(parts)[:20000]
                return "collected"
            if action.op == "click":
                if self._looks_like_file(action.target):
                    return await self._click_maybe_download(locator)
                await locator.click(timeout=8000)
                try:
                    await self.page.wait_for_load_state("domcontentloaded", timeout=8000)
                except Exception:
                    pass
                return "navigated"
            return "error"
        except Exception as error:  # noqa: BLE001 - the investigator treats a failed action as a hint
            print(f"[browser] {action.op} failed: {type(error).__name__}", flush=True)
            return "error"

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
            return "navigated"
        return "navigated"

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
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)[:16000]
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


async def open_browsers(playwright, count: int):
    """Return `(pages, browsers_to_close)`. Sandbox browsers are not closed."""
    urls = _cdp_urls()
    pages, closers = [], []
    if urls:
        if len(urls) < count:
            print(f"[browser] sandbox provided {len(urls)} of {count} browsers; extra roles will queue", flush=True)
        for url in urls[:count]:
            browser = await playwright.chromium.connect_over_cdp(url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context(viewport={"width": 1280, "height": 800})
            pages.append(context.pages[0] if context.pages else await context.new_page())
        return pages, closers
    for index in range(count):
        browser = await playwright.chromium.launch(
            headless=os.environ.get("HEADLESS") == "1",
            args=[f"--window-size=1280,800", f"--window-position={40 + index * 30},{40 + index * 30}"],
        )
        closers.append(browser)
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        pages.append(await context.new_page())
    return pages, closers
