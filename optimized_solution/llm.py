"""Qwen through the run's metering proxy. A cold model can take minutes on its first call."""

from __future__ import annotations

import os
import time

from metrics import Metrics


class LLM:
    def __init__(self, metrics: Metrics):
        self.metrics = metrics
        self._client = None
        self.model = os.environ.get("QWEN_MODEL", "qwen")

    def _client_or_create(self):
        if self._client is None:
            if not os.environ.get("QWEN_BASE_URL") or not os.environ.get("QWEN_API_KEY"):
                raise SystemExit("QWEN_BASE_URL and QWEN_API_KEY are missing. Start `atira models proxy`, or run inside a sandbox.")
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                base_url=os.environ["QWEN_BASE_URL"],
                api_key=os.environ["QWEN_API_KEY"],
                timeout=360,
            )
        return self._client

    async def complete(self, system: str, user: str, max_tokens: int, phase: str) -> str:
        last = None
        for attempt in range(3):
            started = time.perf_counter()
            try:
                response = await self._once(system, user, max_tokens)
            except Exception as error:  # noqa: BLE001 - retry timeouts and a cold model
                last = error
                message = str(error).lower()
                if attempt == 0 and self.model == "qwen" and "model" in message:
                    self.model = "qwen3.8-flash-next"
                await _sleep(2 * (attempt + 1))
                continue
            text = response.choices[0].message.content or ""
            usage = getattr(response, "usage", None)
            if usage is None:
                self.metrics.add_llm(len(system + user) // 4, len(text) // 4, 0, time.perf_counter() - started, True)
            else:
                details = getattr(usage, "prompt_tokens_details", None)
                cached = getattr(details, "cached_tokens", 0) or 0 if details else 0
                self.metrics.add_llm(usage.prompt_tokens or 0, usage.completion_tokens or 0, cached, time.perf_counter() - started, False)
            print(f"[llm] {phase} call {self.metrics.llm_calls} ({self.model})", flush=True)
            return text
        raise RuntimeError(f"model call failed during {phase}: {last}")

    async def _once(self, system: str, user: str, max_tokens: int):
        return await self._client_or_create().chat.completions.create(
            model=self.model,
            temperature=0,
            max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
