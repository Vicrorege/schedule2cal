from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from google.genai import types

from bot.config import Settings
from models.schedule import ClassList, DayOfWeek, Schedule
from services.llm.base import LLMProvider
from services.llm.key_pool import (
    GeminiKeyPool,
    KeyCooldownRegistry,
    OpenAIEndpointPool,
    _EndpointPool,
)
from services.llm.openai_chat import chat_with_image
from services.llm.openai_provider import _extract_json
from services.llm.prompts import DETECT_CLASSES_PROMPT, PARSE_SCHEDULE_PROMPT

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Slot:
    kind: str  # "gemini" | "openai"
    index: int
    label: str
    key_id: str


class HybridLLMProvider(LLMProvider):
    """Нативный Gemini + кастомные OpenAI-compatible слоты в одном пуле."""

    def __init__(self, settings: Settings):
        self._gemini_model = settings.gemini_model
        self._registry = KeyCooldownRegistry()
        self._slots: list[_Slot] = []
        self._gemini_pool: GeminiKeyPool | None = None
        self._openai_pool: OpenAIEndpointPool | None = None

        if settings.gemini_native_keys:
            self._gemini_pool = GeminiKeyPool(
                settings.gemini_native_keys,
                proxy=settings.proxy,
                registry=self._registry,
                model=settings.gemini_model,
            )
            for i, key in enumerate(settings.gemini_native_keys):
                masked = key[:4] + "…" + key[-4:] if len(key) > 8 else "***"
                label = (
                    f"generativelanguage.googleapis.com | {settings.gemini_model} | {masked}"
                )
                self._slots.append(_Slot("gemini", i, label, key))

        if settings.custom_endpoints:
            self._openai_pool = OpenAIEndpointPool(
                settings.custom_endpoints,
                proxy=settings.proxy,
                default_model=settings.openai_model,
                registry=self._registry,
            )
            for i, ep in enumerate(settings.custom_endpoints):
                self._slots.append(_Slot("openai", i, ep.label(), ep.api_key))

        if not self._slots:
            raise ValueError("Hybrid pool пуст")

        labels = [s.label for s in self._slots]
        key_ids = [s.key_id for s in self._slots]
        self._pool = _EndpointPool(labels, key_ids, registry=self._registry)

        logger.info(
            "Hybrid LLM pool: %d слотов, %d уник. ключей",
            len(self._slots),
            len(dict.fromkeys(key_ids)),
        )

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

    async def _openai_chat(self, slot: _Slot, *, prompt: str, image_bytes: bytes) -> str:
        assert self._openai_pool is not None

        async def _call(client, model: str) -> str:
            content = await chat_with_image(
                client,
                model,
                prompt=prompt,
                image_bytes=image_bytes,
                response_format={"type": "json_object"},
            )
            return _extract_json(content)

        return await self._openai_pool.call_at(slot.index, _call)

    async def detect_classes(self, image_bytes: bytes) -> ClassList:
        prompt = DETECT_CLASSES_PROMPT.format(today=date.today().isoformat())

        async def _call(slot_idx: int) -> ClassList:
            slot = self._slots[slot_idx]
            if slot.kind == "gemini":
                response = await self._gemini_generate(
                    slot, schema=ClassList, prompt=prompt, image_bytes=image_bytes
                )
                return ClassList.model_validate_json(response.text)
            raw = await self._openai_chat(slot, prompt=prompt, image_bytes=image_bytes)
            return ClassList.model_validate_json(raw)

        return await self._pool.run_with(
            _call,
            exhausted_message=(
                f"Все LLM слоты исчерпали квоту ({len(dict.fromkeys(s.key_id for s in self._slots))} ключей)."
            ),
        )

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

        async def _call(slot_idx: int) -> Schedule:
            slot = self._slots[slot_idx]
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

        return await self._pool.run_with(
            _call,
            exhausted_message=(
                f"Все LLM слоты исчерпали квоту ({len(dict.fromkeys(s.key_id for s in self._slots))} ключей)."
            ),
        )