"""Hosted Qwen. Research has a hard call budget so the model cannot wander."""
from __future__ import annotations

import asyncio
import os

from openai import AsyncOpenAI


def qwen_ready() -> None:
    if not (os.environ.get("QWEN_BASE_URL") and os.environ.get("QWEN_API_KEY")):
        raise SystemExit(
            "QWEN_BASE_URL and QWEN_API_KEY are missing. In a run they are set. "
            "Locally: `atira models proxy` and `atira scout --env`.")


class Budget(Exception):
    """Research call limit reached."""


class LLM:
    def __init__(self, research_limit: int = 80):
        qwen_ready()
        self.model = os.environ.get("QWEN_MODEL", "qwen")
        self.client = AsyncOpenAI(
            base_url=os.environ["QWEN_BASE_URL"],
            api_key=os.environ["QWEN_API_KEY"],
            timeout=60,
            max_retries=0,
        )
        self.calls = 0
        self.research_calls = 0
        self.research_limit = research_limit
        self._lock = asyncio.Lock()

    def research_left(self) -> int:
        return max(0, self.research_limit - self.research_calls)

    async def complete(self, system: str, user: str, max_tokens: int, phase: str,
                       *, json_object: bool = False, research: bool = True, think: bool = True) -> str:
        async with self._lock:
            if research and self.research_calls >= self.research_limit:
                raise Budget(f"research budget {self.research_limit}")
            if research:
                self.research_calls += 1
        last = None
        use_json = json_object
        extra = None
        if not think:
            extra = {"enable_thinking": False, "chat_template_kwargs": {"enable_thinking": False}}
        for attempt in range(3):
            try:
                kwargs = {}
                if use_json:
                    kwargs["response_format"] = {"type": "json_object"}
                if extra:
                    kwargs["extra_body"] = extra
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
                if extra and any(word in message for word in ("enable_thinking", "extra_body", "unexpected")):
                    extra = None
                    continue
                if attempt == 0 and self.model == "qwen" and "model" in message:
                    self.model = "qwen3.8-flash-next"
                await asyncio.sleep(1.2 * (attempt + 1))
                continue
            self.calls += 1
            text = _message_text(response.choices[0].message)
            usage = getattr(response, "usage", None)
            extra = ""
            if usage is not None:
                extra = f" {usage.prompt_tokens}/{usage.completion_tokens} tok"
            print(f"[llm] {phase} #{self.calls} r{self.research_calls}/{self.research_limit}{extra}", flush=True)
            return text
        raise RuntimeError(f"{phase}: Qwen failed after retries: {last}")


def _message_text(message) -> str:
    parts = []
    content = getattr(message, "content", None)
    if isinstance(content, str) and content.strip():
        parts.append(content)
    elif isinstance(content, list):
        for item in content:
            text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
            if text:
                parts.append(str(text))
    for key in ("reasoning_content", "reasoning"):
        extra = getattr(message, key, None)
        if extra:
            parts.append(str(extra))
    return "\n".join(part for part in parts if str(part).strip()).strip()
