"""Code is the director. Qwen only answers at a fork: which control, or is this a document."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from actions import Action
from board import Board, Task
from extract import evidence_from, kind_for, parse_extract, SHAPES
from llm import Budget
from pages import (case_link_ids, close_ids, cookie_ids, login_submit_ids,
                   looks_document, looks_login, parse_fork, pick_login_fields,
                   search_box_ids, skip_link, task_link_ids)
from view import Driver, format_view

PROMPTS = Path(__file__).resolve().parent / "prompts"
STEPS = 8


def _prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


def known_text(board: Board) -> str:
    ev = board.evidence
    if ev is None:
        return "(nothing yet)"
    groups = sorted({s.catalog_group for s in ev.erp_searches if s.catalog_group})
    reqs = [r.id for rev in ev.revisions for r in rev.requirements if r.id]
    return (
        f"{len(ev.positions)} positions, {len(ev.qa)} Q&A, {len(ev.revisions)} revisions, "
        f"{len(ev.materials)} materials, {len(ev.messages)} messages"
        + (f"; Kataloggruppen: {', '.join(groups)}" if groups else "")
        + (f"; requirement ids: {', '.join(reqs[:16])}" if reqs else "")
    )


async def dismiss_blocker(driver: Driver) -> bool:
    ids = cookie_ids(driver._nodes)
    if not ids:
        ids = close_ids(driver._nodes)
        label = "closed modal"
    else:
        label = "dismissed blocker"
    if not ids:
        return False
    await driver.perform(Action("click", target=ids[0]))
    await asyncio.sleep(0.4)
    print(f"[director] {label}", flush=True)
    return True


async def fill_login(driver: Driver, creds: dict) -> bool:
    user_id, password_id = pick_login_fields(driver._nodes)
    if user_id is None or password_id is None:
        return False
    user = await driver.perform(Action("fill", target=user_id, text=creds.get("username") or ""))
    password = await driver.perform(Action("fill", target=password_id, text=creds.get("password") or ""))
    print("[director] filled login", flush=True)
    return user == "filled" and password == "filled"


async def submit_login(driver: Driver) -> None:
    for nid in login_submit_ids(driver._nodes):
        print(f"[director] login submit {nid}", flush=True)
        await driver.perform(Action("click", target=nid))
        await asyncio.sleep(1.0)
        return
    await driver.perform(Action("press", text="Enter"))
    await asyncio.sleep(1.0)


async def ensure_session(driver: Driver, creds: dict, name: str) -> bool:
    """Get past cookies and login without the model. Qwen must not see a login form."""
    for _ in range(3):
        await driver.snapshot()
        await dismiss_blocker(driver)
        snapshot = await driver.snapshot()
        if not looks_login(snapshot):
            return True
        if not await fill_login(driver, creds):
            print(f"[{name}] login fields not found", flush=True)
            return False
        await submit_login(driver)
        snapshot = await driver.snapshot()
        if not looks_login(snapshot):
            try:
                await driver.page.keyboard.press("Escape")
            except Exception:
                pass
            print(f"[{name}] logged in", flush=True)
            return True
    print(f"[{name}] still on login after retries", flush=True)
    return False


async def _extract(llm, task: Task, snapshot: dict, page_text: str):
    kind = kind_for(task)
    labels = "\n".join(
        f"[{n.get('i')}] {n.get('name') or ''}" for n in (snapshot.get("nodes") or [])[:80])
    user = (
        f"Task: {task.question}\nShape:\n{json.dumps(SHAPES[kind], ensure_ascii=False)}\n\n"
        f"Controls:\n{labels}\n\nPage:\n{(page_text or snapshot.get('text') or '')[:12000]}"
    )
    raw = await llm.complete(_prompt("extract.md"), user, 800, f"extract:{kind}",
                             json_object=True, think=False)
    try:
        data = parse_extract(raw)
    except ValueError:
        data = {}
    if not any(value for value in data.values() if value):
        raw = await llm.complete(_prompt("extract.md"), user + "\nJSON only.", 1400,
                                 f"extract:{kind}", json_object=True, think=True)
        data = parse_extract(raw)
    return kind, data


def stop_research(board: Board, llm, until: float) -> bool:
    if time.monotonic() >= until or llm.research_left() <= 0:
        return True
    if board.is_complete():
        return True
    found = board.resolution()
    return bool(found and found.answer is not None and llm.research_calls >= 50)


async def run_site(slot: dict, board: Board, llm, case, board_path: Path, until: float) -> None:
    driver = Driver(slot["page"])
    try:
        await driver.open(slot["site"]["url"])
        print(f"[{slot['name']}] opened", flush=True)
    except Exception as error:  # noqa: BLE001
        print(f"[{slot['name']}] open failed: {error!r}", flush=True)
        return
    if not await ensure_session(driver, slot["site"].get("credentials") or {}, slot["name"]):
        return
    while not stop_research(board, llm, until):
        claimed = await board.claim(slot)
        if claimed is None:
            if not board.has_work():
                return
            await asyncio.sleep(1.5)
            continue
        print(f"[{slot['name']}] {claimed.question}", flush=True)
        if not await ensure_session(driver, slot["site"].get("credentials") or {}, slot["name"]):
            await board.absorb(claimed, None, board_path)
            continue
        try:
            found = await pursue(slot, claimed, driver, board, llm, case, board_path, until)
        except Budget:
            await board.absorb(claimed, None, board_path)
            return
        await board.absorb(claimed, found, board_path)
        print(f"board: {known_text(board)}; {len(board.pending)} open; r{llm.research_calls}", flush=True)


async def pursue(slot, task: Task, driver: Driver, board: Board, llm, case, board_path: Path, until: float):
    hint, last_part = "", None
    clicked: set = set()
    extracted_urls: set[str] = set()
    extract_misses: dict[str, int] = {}
    last_url = ""
    creds = slot["site"].get("credentials") or {}
    kind = kind_for(task)
    for _ in range(STEPS):
        if stop_research(board, llm, until):
            break
        snapshot = await driver.snapshot()
        if await dismiss_blocker(driver):
            snapshot = await driver.snapshot()
        if looks_login(snapshot):
            if not await ensure_session(driver, creds, slot["name"]):
                return last_part
            continue
        names = ", ".join(
            f"{n.get('i')}:{(n.get('name') or '')[:24]}" for n in (snapshot.get("nodes") or [])[:16])
        print(f"[{slot['name']}] {snapshot.get('url') or ''} | {names}", flush=True)
        extracted_here = False
        page_url = snapshot.get("url") or ""
        if page_url != last_url:
            clicked.clear()
            last_url = page_url
        if looks_document(snapshot, kind) and page_url not in extracted_urls:
            extracted_urls.add(page_url)
            part = await _try_extract(llm, task, driver, snapshot, case)
            if part is not None:
                last_part = part
                extracted_here = True
                await board.publish(part, board_path)
                print(f"[{slot['name']}] auto-extract", flush=True)
                hint = "Recorded. If this task has another page, click it; else done."
            else:
                extract_misses[page_url] = extract_misses.get(page_url, 0) + 1
                if extract_misses[page_url] >= 2:
                    extracted_urls.add(page_url)
                else:
                    extracted_urls.discard(page_url)
                hint = "This looks like a document but extract was empty. Open the actual record."
        nodes = {n.get("i") for n in snapshot.get("nodes") or []}
        if kind in {"mail", "erp"} and case.tender_reference and "searched" not in clicked:
            boxes = [i for i in search_box_ids(snapshot) if i in nodes]
            if boxes:
                await driver.perform(Action("fill", target=boxes[0], text=case.tender_reference))
                await driver.perform(Action("press", text="Enter"))
                clicked.add("searched")
                print(f"[{slot['name']}] search {case.tender_reference}", flush=True)
                continue
        targets = [i for i in task_link_ids(snapshot, kind) + case_link_ids(snapshot, case.tender_reference)
                   if i in nodes and i not in clicked]
        if targets and not looks_document(snapshot, kind):
            action = Action("click", target=targets[0])
            clicked.add(targets[0])
            outcome = await driver.perform(action)
            print(f"[{slot['name']}] hint click {targets[0]}", flush=True)
            if outcome == "downloaded":
                part = await _try_extract(llm, task, driver, snapshot, case)
                if part is not None:
                    last_part = part
                    await board.publish(part, board_path)
            elif outcome == "error":
                hint = "That control failed. Pick another."
            continue
        extra = (
            f"\nSite: `{slot['site'].get('siteId')}` {slot['site']['url']}\n"
            f"Task: {task.question}\nKnown: {known_text(board)}\n"
        )
        if hint:
            extra += f"Hint: {hint}\n"
        raw = ""
        try:
            raw = await llm.complete(
                _prompt("fork.md"), format_view(snapshot, extra=extra, compact=True), 64,
                f"fork:{slot['name']}", think=False)
            page, action = parse_fork(raw)
        except Budget:
            raise
        except (ValueError, RuntimeError) as error:
            print(f"[{slot['name']}] bad fork {error}: {raw[:120]}", flush=True)
            hint = "Last line must be: click N   or  extract  or  done"
            continue
        node = next((item for item in snapshot.get("nodes") or [] if item.get("i") == action.target), None)
        if node and skip_link(node):
            hint = "Skip that control. Pick a document or record."
            continue
        if action.op == "fill" and kind not in {"erp", "mail"}:
            hint = "Do not fill. Click a listed control."
            continue
        if action.op in {"click", "fill"} and action.target in clicked:
            hint = "already tried that i. Pick a different listed i."
            continue
        if action.op in {"click", "fill"} and action.target is not None:
            clicked.add(action.target)
        if (page == "document" or action.op == "extract") and not extracted_here:
            if not looks_document(snapshot, kind):
                hint = "Not a document. Click into the record."
                continue
            if page_url in extracted_urls:
                hint = "Already extracted this URL. Click a different page."
                continue
            extracted_urls.add(page_url)
            part = await _try_extract(llm, task, driver, snapshot, case)
            if part is not None:
                last_part = part
                await board.publish(part, board_path)
                print(f"[{slot['name']}] extract", flush=True)
                hint = "Recorded. Next page or done."
            else:
                extract_misses[page_url] = extract_misses.get(page_url, 0) + 1
                if extract_misses[page_url] >= 2:
                    extracted_urls.add(page_url)
                else:
                    extracted_urls.discard(page_url)
                hint = "Extract was empty. Click into the document."
            continue
        if action.op == "done":
            break
        if action.op in {"click", "fill"} and action.target not in nodes:
            hint = "target must be a listed i."
            continue
        if action.op == "fill" and not (action.text or "").strip():
            hint = "fill.text must be a search from the task."
            continue
        if action.op == "fill" and looks_login(snapshot):
            hint = "Login is already filled. Click submit."
            continue
        outcome = await driver.perform(action)
        print(f"[{slot['name']}] {page} {action.op} {action.target or ''}".rstrip(), flush=True)
        if outcome == "error":
            hint = "That control failed. Pick another."
            continue
        if outcome == "downloaded":
            part = await _try_extract(llm, task, driver, snapshot, case)
            if part is not None:
                last_part = part
                await board.publish(part, board_path)
            continue
        hint = ""
    return last_part


async def _try_extract(llm, task, driver, snapshot, case):
    try:
        kind, data = await _extract(llm, task, snapshot, await driver.reading())
    except Budget:
        raise
    except (ValueError, RuntimeError) as error:
        print(f"extract parse fail: {error}", flush=True)
        return None
    found = evidence_from(case, data, kind)
    if found is None:
        keys = list(data)[:8] if isinstance(data, dict) else type(data).__name__
        rows = data.get(kind) or data.get("qa") or data.get("messages") or data.get("positions") or []
        sample = rows[0] if isinstance(rows, list) and rows else None
        print(f"extract {kind} empty keys={keys} n={len(rows) if isinstance(rows, list) else 0} sample={sample}", flush=True)
    return found
