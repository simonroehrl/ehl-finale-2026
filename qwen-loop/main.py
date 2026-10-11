"""Thin Qwen loop: four Playwright browsers, a shared task board, the 100er resolver, then a typed submit.

Qwen only chooses the next control or copies the records on the current page. Code merges, prices and types
every bid amount. No Claude CLI, no MCP, no OpenRouter key.

Docs: $API/docs/run-contract.md, $API/docs/fair-play.md
"""
from __future__ import annotations

import asyncio
import re
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

from playwright.async_api import async_playwright

import browsers
import run as contract
from board import Board, assign_slots, slot_for_gap
from extract import case_from_brief
from llm import LLM
from resolver import resolve
from actions import portal_confirmed
from submit import enter_bid
from worker import run_site

HERE = Path(__file__).resolve().parent
RESEARCH_S = 540
SUBMIT_S = 360
SAFETY_S = 40


def submission_section(brief: str) -> str:
    match = re.search(r"^#+ *(Submission|Abgabe|Angebotsabgabe)\b.*$", brief, re.M | re.I)
    return brief[match.start():] if match else brief


def german(value) -> str:
    return f"{Decimal(str(value)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def bid_text(answer: dict, omitted: list[str]) -> str:
    lines = [f"Tender reference: {answer['tender_reference']}", f"Decision: {answer['decision']}",
             f"Discount: {german(answer['discount_percent'])} %"]
    for p in answer["positions"]:
        lines.append(
            f"- Position {p['position']}: material {p['material_number']}, quantity {p['quantity']} Stück, unit "
            f"price {german(p['unit_price'])} EUR, lead time {p['lead_time_weeks']} weeks, base unit price "
            f"{german(p['base_unit_price'])}, surcharge per unit {german(p['surcharge_per_unit'])}, packaging per "
            f"unit {german(p['packaging_per_unit'])}, rebate total {german(p['rebate_total'])}")
    for p in omitted:
        lines.append(f"- Position {p}: omit it (Position auslassen)")
    lines.append(f"Total net: {german(answer['total_net'])} EUR")
    return "\n".join(lines)


async def attach(playwright, cdp_url: str):
    browser = await playwright.chromium.connect_over_cdp(cdp_url)
    context = browser.contexts[0] if browser.contexts else await browser.new_context(
        viewport={"width": 1280, "height": 800})
    page = context.pages[0] if context.pages else await context.new_page()
    return browser, page


async def solve(task: dict, playwright, cdp_urls: list[str]) -> None:
    workdir = Path(tempfile.mkdtemp(prefix="rfq-qwen-"))
    started = time.monotonic()
    wall = contract.seconds_left(task)
    end = started + (min(wall, 20 * 60) if wall != float("inf") else 20 * 60) - SAFETY_S
    llm = LLM()
    case = await case_from_brief(llm, task["brief"], (HERE / "prompts" / "case.md").read_text(encoding="utf-8"))
    print(f"Case {case.tender_reference} @ {case.scenario_date} ({case.bidder_mail_domain})", flush=True)

    slots = assign_slots(task["access"], cdp_urls)
    if not slots:
        raise SystemExit("No website/browser pair")
    for slot, url in zip(slots, cdp_urls):
        _, page = await attach(playwright, url)
        slot["page"] = page

    board = Board(slots)
    board_path = workdir / "board.json"
    board.persist(board_path)
    print("Workers: " + ", ".join(s["name"] for s in slots) + f"; seeds {len(board.pending)}", flush=True)

    research_until = min(started + RESEARCH_S, end - SUBMIT_S)
    await asyncio.gather(*(run_site(s, board, llm, case, board_path, research_until) for s in slots))

    if board.evidence is None:
        raise SystemExit("No evidence from any worker")
    resolution = resolve(board.evidence)
    print("Trace:\n  " + "\n  ".join(resolution.trace), flush=True)
    print("Warnings: " + ("; ".join(resolution.warnings) or "none"), flush=True)
    if resolution.answer is None:
        print("No answer: " + "; ".join(g.question for g in resolution.gaps), flush=True)
        return
    print(f"Bid at {time.monotonic() - started:.0f}s:\n{bid_text(resolution.answer, resolution.omitted)}", flush=True)

    tender = slot_for_gap(slots, "tender-portal") or slots[0]
    mail = slot_for_gap(slots, "mail") or slots[-1]
    result = await enter_bid(llm, tender, mail, resolution.answer, resolution.omitted,
                             submission_section(task["brief"]), until=end)
    if not portal_confirmed(result) and time.monotonic() < end - 40:
        print("Submit goal not reached; retrying once", flush=True)
        result = await enter_bid(llm, tender, mail, resolution.answer, resolution.omitted,
                                 submission_section(task["brief"]), until=end)
    print(f"Submission at {time.monotonic() - started:.0f}s: {result}", flush=True)


async def main() -> int:
    task, _ = contract.load_run()
    print(f"Run {task.get('run_id') or '(local: the scouting logins, nothing counts)'}; websites: "
          + ", ".join(f"{s.get('siteId')} {s.get('url')}" for s in task["access"]), flush=True)
    async with async_playwright() as playwright:
        urls, local = await browsers.cdp_endpoints(
            playwright, min(4, max(1, len(task.get("access") or []))))
        try:
            await solve(task, playwright, urls)
        finally:
            for browser in local:
                await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
