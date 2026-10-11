"""Local run metrics. The platform's own meter is authoritative; this estimate uses the published Qwen prices."""

from __future__ import annotations

import time
from decimal import Decimal

QWEN_INPUT_PER_TOKEN = Decimal("0.15") / Decimal(1_000_000)
QWEN_CACHED_PER_TOKEN = Decimal("0.016") / Decimal(1_000_000)
QWEN_OUTPUT_PER_TOKEN = Decimal("0.47") / Decimal(1_000_000)


class Metrics:
    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.llm_calls = 0
        self.input_tokens = 0
        self.cached_input_tokens = 0
        self.output_tokens = 0
        self.llm_latency_s = 0.0
        self.browser_actions = 0
        self.repeated_actions = 0
        self.failed_actions = 0
        self.failed_tasks: list[str] = []
        self.phases_s: dict[str, float] = {}
        self.submitted = False
        self.usage_estimated = False
        self._open: dict[str, float] = {}

    def begin(self, phase: str) -> None:
        self._open[phase] = time.perf_counter()

    def end(self, phase: str) -> None:
        started = self._open.pop(phase, None)
        if started is not None:
            self.phases_s[phase] = self.phases_s.get(phase, 0.0) + (time.perf_counter() - started)

    def add_llm(self, input_tokens: int, output_tokens: int, cached_tokens: int, latency_s: float, estimated: bool) -> None:
        self.llm_calls += 1
        self.input_tokens += max(0, input_tokens)
        self.output_tokens += max(0, output_tokens)
        self.cached_input_tokens += max(0, cached_tokens)
        self.llm_latency_s += max(0.0, latency_s)
        self.usage_estimated = self.usage_estimated or estimated

    def fail(self, task: str) -> None:
        if task not in self.failed_tasks:
            self.failed_tasks.append(task)

    @property
    def estimated_cost_usd(self) -> Decimal:
        fresh = max(0, self.input_tokens - self.cached_input_tokens)
        return (Decimal(fresh) * QWEN_INPUT_PER_TOKEN
                + Decimal(self.cached_input_tokens) * QWEN_CACHED_PER_TOKEN
                + Decimal(self.output_tokens) * QWEN_OUTPUT_PER_TOKEN)

    def report(self) -> dict:
        elapsed = time.perf_counter() - self.started
        cost = self.estimated_cost_usd
        return {
            "elapsed_s": round(elapsed, 3),
            "estimated_cost_usd": float(cost),
            "estimated_cost_times_time": float(cost) * max(elapsed, 1.0),
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "llm_latency_s": round(self.llm_latency_s, 3),
            "browser_actions": self.browser_actions,
            "repeated_actions": self.repeated_actions,
            "failed_actions": self.failed_actions,
            "failed_tasks": list(self.failed_tasks),
            "phases_s": {key: round(value, 3) for key, value in self.phases_s.items()},
            "submitted": self.submitted,
            "usage_estimated": self.usage_estimated,
        }
