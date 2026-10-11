"""Hosted Qwen through the run's proxy. One short HTTP call at a time; timeouts and retries stay here."""
from __future__ import annotations

import asyncio
import os

from openai import AsyncOpenAI


def qwen_ready() -> None:
    if not (os.environ.get("QWEN_BASE_URL") and os.environ.get("QWEN_API_KEY")):
        raise SystemExit(
            "QWEN_BASE_URL and QWEN_API_KEY are missing. In a run they are set. "
            "Locally: `atira models proxy` and `atira scout --env`.")


class LLM:
    def __init__(self):
        qwen_ready()
        self.model = os.environ.get("QWEN_MODEL", "qwen")
        self.client = AsyncOpenAI(
            base_url=os.environ["QWEN_BASE_URL"],
            api_key=os.environ["QWEN_API_KEY"],
            timeout=90,
            max_retries=0,
        )
        self.calls = 0

    async def complete(self, system: str, user: str, max_tokens: int, phase: str,
                       *, json_object: bool = False) -> str:
        last = None
        use_json = json_object
        for attempt in range(3):
            try:
                kwargs = {}
                if use_json:
                    kwargs["response_format"] = {"type": "json_object"}
                response = await self.client.chat.completions.create(
                    model=self.model,
                    temperature=0,
                    max_tokens=max_tokens,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": user}],
                    **kwargs,
                )
            except Exception as error:  # noqa: BLE001
                last = error
                message = str(error).lower()
                if use_json and "response_format" in message:
                    use_json = False
                    continue
                if attempt == 0 and self.model == "qwen" and "model" in message:
                    self.model = "qwen3.8-flash-next"
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            self.calls += 1
            text = (response.choices[0].message.content or "").strip()
            usage = getattr(response, "usage", None)
            tokens = ""
            if usage is not None:
                tokens = f" {usage.prompt_tokens}/{usage.completion_tokens} tok"
            print(f"[llm] {phase} #{self.calls}{tokens}", flush=True)
            return text
        raise RuntimeError(f"{phase}: Qwen failed after retries: {last}")
