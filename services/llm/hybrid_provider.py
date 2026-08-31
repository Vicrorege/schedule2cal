from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date

from google.genai import types

from bot.config import Settings
from models.schedule import ClassList, DayOfWeek, Schedule
from services.llm.base import LLMProvider
from services.llm.key_pool import (
    GeminiKeyPool,
    OpenAIEndpointPool,
    cooldown_seconds_from_error,
    is_quota_error,
)
from services.llm.openai_provider import _extract_json
from services.llm.prompts import DETECT_CLASSES_PROMPT, PARSE_SCHEDULE_PROMPT

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Slot:
    kind: str  # "gemini" | "openai"
    index: int
    label: str


class HybridLLMProvider(LLMProvider):
    """Нативный Gemini + кастомные OpenAI-compatible слоты в одном пуле."""

    def __init__(self, settings: Settings):
        self._gemini_model = settings.gemini_model
        self._slots: list[_Slot] = []
        self._gemini_pool: GeminiKeyPool | None = None
        self._openai_pool: OpenAIEndpointPool | None = None

        if settings.gemini_native_keys:
            self._gemini_pool = GeminiKeyPool(
                settings.gemini_native_keys, proxy=settings.proxy
            )
            for i in range(self._gemini_pool.size):
                self._slots.append(_Slot("gemini", i, f"gemini#{i + 1}"))

        if settings.custom_endpoints:
            self._openai_pool = OpenAIEndpointPool(
                settings.custom_endpoints,
                proxy=settings.proxy,
                default_model=settings.openai_model,
            )
            for i, ep in enumerate(settings.custom_endpoints):
                self._slots.append(_Slot("openai", i, ep.label()))

        if not self._slots:
            raise ValueError("Hybrid pool пуст")

        self._cooldown_until = [0.0] * len(self._slots)
        self._rr = 0
        self._lock = asyncio.Lock()
        logger.info(
            "Hybrid LLM pool: %d слотов (gemini=%s, custom=%s)",
            len(self._slots),
            self._gemini_pool.size if self._gemini_pool else 0,
            self._openai_pool.size if self._openai_pool else 0,
        )

    async def _pick(self, tried: set[int]) -> int | None:
        now = time.time()
        async with self._lock:
            n = len(self._slots)
            for offset in range(n):
                idx = (self._rr + offset) % n
                if idx in tried or self._cooldown_until[idx] > now:
                    continue
                self._rr = (idx + 1) % n
                return idx
            candidates = [i for i in range(n) if i not in tried]
            if not candidates:
                return None
            return min(candidates, key=lambda i: self._cooldown_until[i])

    async def _mark_cooldown(self, idx: int, seconds: float) -> None:
        until = time.time() + seconds
        async with self._lock:
            self._cooldown_until[idx] = max(self._cooldown_until[idx], until)
        logger.warning(
            "Hybrid slot #%d (%s) cooldown %.0fs",
            idx + 1,
            self._slots[idx].label,
            seconds,
        )

    async def _run_hybrid(self, call):
        tried: set[int] = set()
        last_exc: BaseException | None = None
        while len(tried) < len(self._slots):
            idx = await self._pick(tried)
            if idx is None:
                break
            tried.add(idx)
            try:
                return await call(self._slots[idx])
            except Exception as exc:
                last_exc = exc
                if is_quota_error(exc):
                    await self._mark_cooldown(idx, cooldown_seconds_from_error(exc))
                    continue
                raise
        raise RuntimeError(
            f"Все LLM слоты исчерпали квоту ({len(self._slots)} шт.). Подожди или добавь ключи."
        ) from last_exc

    async def _gemini_generate(self, slot: _Slot, *, schema, prompt: str, image_bytes: bytes):
        assert self._gemini_pool is not None

        async def _call(client):
            return await client.aio.models.generate_content(
                model=self._gemini_model,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0.1,
                ),
            )

        return await self._gemini_pool.call_at(slot.index, _call)

    async def _openai_chat(
        self, slot: _Slot, *, prompt: str, image_bytes: bytes
    ) -> str:
        assert self._openai_pool is not None
        import base64

        async def _call(client, model: str) -> str:
            b64 = base64.standard_b64encode(image_bytes).decode()
            full_prompt = (
                prompt + "\n\nОтветь ТОЛЬКО валидным JSON-объектом, без markdown и пояснений."
            )
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                            },
                            {"type": "text", "text": full_prompt},
                        ],
                    }
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            return _extract_json(response.choices[0].message.content or "")

        return await self._openai_pool.call_at(slot.index, _call)

    async def detect_classes(self, image_bytes: bytes) -> ClassList:
        prompt = DETECT_CLASSES_PROMPT.format(today=date.today().isoformat())

        async def _call(slot: _Slot) -> ClassList:
            if slot.kind == "gemini":
                response = await self._gemini_generate(
                    slot, schema=ClassList, prompt=prompt, image_bytes=image_bytes
                )
                return ClassList.model_validate_json(response.text)
            raw = await self._openai_chat(slot, prompt=prompt, image_bytes=image_bytes)
            return ClassList.model_validate_json(raw)

        return await self._run_hybrid(_call)

    async def parse_schedule(
        self,
        image_bytes: bytes,
        class_name: str,
        schedule_date: date,
        day_of_week: str,
    ) -> Schedule:
        prompt = PARSE_SCHEDULE_PROMPT.format(
            class_name=class_name,
            schedule_date=schedule_date.isoformat(),
            day_of_week=day_of_week,
        )

        async def _call(slot: _Slot) -> Schedule:
            if slot.kind == "gemini":
                response = await self._gemini_generate(
                    slot, schema=Schedule, prompt=prompt, image_bytes=image_bytes
                )
                result = Schedule.model_validate_json(response.text)
            else:
                raw = await self._openai_chat(slot, prompt=prompt, image_bytes=image_bytes)
                result = Schedule.model_validate_json(raw)
            result.class_name = class_name
            result.date = schedule_date.isoformat()
            for lesson in result.schedule:
                lesson.day_of_week = DayOfWeek(day_of_week)
            return result

        return await self._run_hybrid(_call)
