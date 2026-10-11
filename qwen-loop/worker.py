"""One site worker: Qwen picks a control or asks to extract; code clicks and merges."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from actions import RESEARCH_OPS, parse_action
from board import Board, Task
from extract import evidence_from, kind_for, parse_extract, SHAPES
from view import Driver, format_view

PROMPTS = Path(__file__).resolve().parent / "prompts"
STEPS = 36


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
        + (f"; requirement ids: {', '.join(reqs[:20])}" if reqs else "")
    )


def site_blob(site: dict) -> str:
    c = site.get("credentials") or {}
    return (f"site `{site.get('siteId')}` {site['url']}\n"
            f"username `{c.get('username', '')}` password `{c.get('password', '')}`")


async def _act(llm, task: Task, site: dict, snapshot: dict, board: Board, hint: str) -> object:
    extra = (
        f"\nAssigned site:\n{site_blob(site)}\n"
        f"Task (only this): {task.question}\n"
        f"Already known: {known_text(board)}\n"
    )
    if hint:
        extra += f"\nHint: {hint}\n"
    user = format_view(snapshot, extra=extra)
    raw = await llm.complete(_prompt("act.md"), user, 250, f"act:{task.site}")
    return parse_action(raw, RESEARCH_OPS)


async def _extract(llm, task: Task, snapshot: dict, page_text: str):
    kind = kind_for(task)
    shape = json.dumps(SHAPES[kind], ensure_ascii=False)
    user = (
        f"Task: {task.question}\n"
        f"Source: {snapshot.get('url')}\n"
        f"Shape:\n{shape}\n\n"
        f"Page:\n{(page_text or snapshot.get('text') or '')[:14000]}"
    )
    raw = await llm.complete(_prompt("extract.md"), user, 1800, f"extract:{kind}")
    try:
        data = parse_extract(raw)
    except ValueError:
        raw = await llm.complete(_prompt("extract.md"), user + "\n\nJSON object only.", 1800, f"extract:{kind}")
        data = parse_extract(raw)
    return kind, data


async def run_site(slot: dict, board: Board, llm, case, board_path: Path, until: float) -> None:
    driver = Driver(slot["page"])
    try:
        await driver.open(slot["site"]["url"])
    except Exception as error:  # noqa: BLE001
        print(f"[{slot['name']}] open failed: {error!r}", flush=True)
        return
    while time.monotonic() < until and not board.is_complete():
        claimed = await board.claim(slot)
        if claimed is None:
            if not board.has_work():
                return
            await asyncio.sleep(2)
            continue
        print(f"[{slot['name']}] {claimed.question}", flush=True)
        found = await _pursue(slot, claimed, driver, board, llm, case, board_path, until)
        await board.absorb(claimed, found, board_path)
        print(f"board: {known_text(board)}; {len(board.pending)} open", flush=True)


async def _pursue(slot, task: Task, driver: Driver, board: Board, llm, case, board_path: Path, until: float):
    hint, previous, repeats, last_part = "", "", 0, None
    for _ in range(STEPS):
        if time.monotonic() > until or board.is_complete():
            break
        snapshot = await driver.snapshot()
        try:
            action = await _act(llm, task, slot["site"], snapshot, board, hint)
        except (ValueError, RuntimeError) as error:
            hint = f"Reply with one allowed JSON action. ({error})"
            continue
        if action.signature() == previous:
            repeats += 1
        else:
            repeats = 0
        previous = action.signature()
        if repeats >= 2:
            hint = "That action just repeated. Pick a different control, extract, or done."
            continue
        nodes = {n.get("i") for n in snapshot.get("nodes") or []}
        if action.op in {"click", "fill", "collect_pages"} and action.target not in nodes:
            hint = "target must be the i of a listed control."
            continue
        if action.op == "open_archive" and action.file not in (snapshot.get("archive_files") or []):
            hint = "file must be an archive name listed above."
            continue
        if action.op == "done":
            break
        if action.op == "extract":
            try:
                kind, data = await _extract(llm, task, snapshot, await driver.reading())
            except (ValueError, RuntimeError) as error:
                hint = f"extract failed ({error}); try another page or done."
                continue
            part = evidence_from(case, data, kind)
            if part is None:
                hint = "Nothing valid was copied. Open the document that holds the records, then extract again."
                continue
            last_part = part
            await board.publish(part, board_path)
            hint = "Recorded. Continue if this task has more pages; otherwise done."
            print(f"[{slot['name']}] extracted {kind}", flush=True)
            continue
        outcome = await driver.perform(action)
        print(f"[{slot['name']}] {action.op} {action.target or ''}".rstrip(), flush=True)
        if outcome == "error":
            hint = "That control did not work. Choose another."
            continue
        if outcome == "downloaded":
            try:
                kind, data = await _extract(llm, task, snapshot, await driver.reading())
                part = evidence_from(case, data, kind)
            except (ValueError, RuntimeError):
                part = None
            if part is not None:
                last_part = part
                await board.publish(part, board_path)
                print(f"[{slot['name']}] extracted {kind} from download", flush=True)
            hint = ""
            continue
        hint = ""
    return last_part
