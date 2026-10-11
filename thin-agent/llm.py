"""Hosted Qwen through the run proxy. Short completions only."""
from __future__ import annotations

import os


class LLM:
    def __init__(self):
        self._client = None
        self.model = os.environ.get("QWEN_MODEL", "qwen")
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def _client_or_create(self):
        if self._client is None:
            if not os.environ.get("QWEN_BASE_URL") or not os.environ.get("QWEN_API_KEY"):
                raise SystemExit("QWEN_BASE_URL and QWEN_API_KEY are missing.")
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(
                base_url=os.environ["QWEN_BASE_URL"],
                api_key=os.environ["QWEN_API_KEY"],
                timeout=120,
            )
        return self._client

    async def complete(self, system: str, user: str, max_tokens: int, phase: str) -> str:
        last = None
        for attempt in range(3):
            try:
                response = await self._client_or_create().chat.completions.create(
                    model=self.model, temperature=0, max_tokens=max_tokens,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                )
            except Exception as error:  # noqa: BLE001
                last = error
                if attempt == 0 and self.model == "qwen" and "model" in str(error).lower():
                    self.model = "qwen3.8-flash-next"
                await _sleep(2 * (attempt + 1))
                continue
            text = response.choices[0].message.content or ""
            usage = getattr(response, "usage", None)
            self.calls += 1
            if usage:
                self.prompt_tokens += usage.prompt_tokens or 0
                self.completion_tokens += usage.completion_tokens or 0
            print(f"[llm] {phase} call {self.calls} in={getattr(usage, 'prompt_tokens', '?')} "
                  f"out={getattr(usage, 'completion_tokens', '?')}", flush=True)
            return text
        raise RuntimeError(f"model call failed during {phase}: {last}")


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
