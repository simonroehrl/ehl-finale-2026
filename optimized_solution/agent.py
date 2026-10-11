"""Entry point. agent.json runs `uv run --no-sync python agent.py`.

The model chooses navigation, searches and documents. A typed ledger holds what the pages said.
kernel.py applies the bid rules and the arithmetic. The same tender browser then enters the bid.
"""

from __future__ import annotations

import asyncio
import json

from contract import load_run
from llm import LLM
from metrics import Metrics
from orchestrator import run_solution


def main() -> None:
    metrics = Metrics()
    try:
        task, token = load_run()
        asyncio.run(run_solution(task, token, metrics, LLM(metrics)))
    finally:
        print(json.dumps({"metrics": metrics.report()}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
