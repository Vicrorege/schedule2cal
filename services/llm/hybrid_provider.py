from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from google.genai import types

from bot.config import Settings
from models.homework import HomeworkParseResult
from models.schedule import ClassList, DayOfWeek, Schedule
from services.llm.base import LLMProvider
from services.llm.key_pool import (
    GeminiKeyPool,
    KeyCooldownRegistry,
    OpenAIEndpointPool,
    _EndpointPool,
)
from services.llm.openai_chat import chat_with_image, chat_with_text
from services.llm.openai_provider import _extract_json
from services.llm.prompts import (
    DETECT_CLASSES_PROMPT,
    PARSE_HOMEWORK_SYSTEM,
    PARSE_SCHEDULE_PROMPT,
    wrap_homework_user_text,
)
from services.schedule_postprocess import WEEKDAY_RU, day_of_week_from_date
from services.schedule_normalize import normalize_schedule_payload

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

        if settings.custom_endpoints:
            self._openai_pool = OpenAIEndpointPool(
                settings.custom_endpoints,
                proxy=settings.proxy,
                default_model=settings.openai_model,
                registry=self._registry,
            )
            for i, ep in enumerate(settings.custom_endpoints):
                self._slots.append(_Slot("openai", i, ep.label(), ep.api_key))

        # Нативный Gemini — запасной: free-tier часто таймаутит
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

    async def _gemini_generate(
        self,
        slot: _Slot,
        *,
        schema,
        prompt: str,
        image_bytes: bytes | None = None,
    ):
        assert self._gemini_pool is not None

        async def _call(client):
            if image_bytes is None:
                contents = [prompt]
            else:
                contents = [
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    prompt,
                ]
            return await client.aio.models.generate_content(
                model=self._gemini_model,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0.0 if image_bytes is None else 0.1,
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

    async def _openai_text(
        self, slot: _Slot, *, system_prompt: str, user_prompt: str
    ) -> str:
        assert self._openai_pool is not None

        async def _call(client, model: str) -> str:
            content = await chat_with_text(
                client,
                model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
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
                result = normalize_schedule_payload(
                    response.text,
                    class_name=class_name,
                    schedule_date=schedule_date.isoformat(),
                    day_of_week=day_of_week,
                )
            else:
                raw = await self._openai_chat(slot, prompt=prompt, image_bytes=image_bytes)
                result = normalize_schedule_payload(
                    raw,
                    class_name=class_name,
                    schedule_date=schedule_date.isoformat(),
                    day_of_week=day_of_week,
                )
            return result

        return await self._pool.run_with(
            _call,
            exhausted_message=(
                f"Все LLM слоты исчерпали квоту ({len(dict.fromkeys(s.key_id for s in self._slots))} ключей)."
            ),
        )

    async def parse_homework(self, text: str, *, today: date) -> HomeworkParseResult:
        system = PARSE_HOMEWORK_SYSTEM.format(
            today=today.isoformat(),
            weekday_ru=WEEKDAY_RU[day_of_week_from_date(today)],
        )
        user = wrap_homework_user_text(text)
        gemini_prompt = f"{system}\n\n{user}"

        async def _call(slot_idx: int) -> HomeworkParseResult:
            slot = self._slots[slot_idx]
            if slot.kind == "gemini":
                response = await self._gemini_generate(
                    slot,
                    schema=HomeworkParseResult,
                    prompt=gemini_prompt,
                    image_bytes=None,
                )
                return HomeworkParseResult.model_validate_json(response.text)
            raw = await self._openai_text(
                slot, system_prompt=system, user_prompt=user
            )
            return HomeworkParseResult.model_validate_json(raw)

        return await self._pool.run_with(
            _call,
            exhausted_message=(
                f"Все LLM слоты исчерпали квоту ({len(dict.fromkeys(s.key_id for s in self._slots))} ключей)."
            ),
        )
