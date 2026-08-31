import base64
import logging
import re
from datetime import date

from openai import AsyncOpenAI

from bot.config import Settings
from models.schedule import ClassList, DayOfWeek, Schedule
from services.llm.base import LLMProvider
from services.llm.key_pool import OpenAIEndpointPool
from services.llm.prompts import DETECT_CLASSES_PROMPT, PARSE_SCHEDULE_PROMPT

logger = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _extract_json(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("Пустой ответ LLM")
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        return fence.group(1).strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


class OpenAIProvider(LLMProvider):
    """OpenAI API или OpenAI-compatible endpoint (base_url)."""

    def __init__(self, settings: Settings):
        endpoints = settings.custom_endpoints
        if not endpoints:
            raise ValueError("Нужен LLM_POOL или LLM_BASE_URL + LLM_API_KEY")
        self._pool = OpenAIEndpointPool(
            endpoints,
            proxy=settings.proxy,
            default_model=settings.openai_model,
        )
        # Кастомные прокси часто не тянут json_schema — json_object надёжнее
        self._use_json_object = any(ep.base_url for ep in endpoints)
        if settings.proxy:
            logger.info("OpenAI-compatible через прокси: %s", settings.proxy)
        if self._use_json_object:
            logger.info("OpenAI pool: response_format=json_object (custom base_url)")

    def _response_format(self, name: str, schema: dict) -> dict:
        if self._use_json_object:
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": name,
                "strict": True,
                "schema": schema,
            },
        }

    async def _chat(
        self,
        client: AsyncOpenAI,
        model: str,
        *,
        prompt: str,
        image_bytes: bytes,
        format_name: str,
        schema: dict,
    ):
        b64 = base64.standard_b64encode(image_bytes).decode()
        if self._use_json_object:
            prompt = prompt + "\n\nОтветь ТОЛЬКО валидным JSON-объектом, без markdown и пояснений."
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
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            response_format=self._response_format(format_name, schema),
            temperature=0.1,
        )
        content = response.choices[0].message.content
        return _extract_json(content or "")

    async def detect_classes(self, image_bytes: bytes) -> ClassList:
        prompt = DETECT_CLASSES_PROMPT.format(today=date.today().isoformat())
        schema = ClassList.model_json_schema()

        async def _call(client: AsyncOpenAI, model: str) -> ClassList:
            raw = await self._chat(
                client,
                model,
                prompt=prompt,
                image_bytes=image_bytes,
                format_name="class_list",
                schema=schema,
            )
            return ClassList.model_validate_json(raw)

        return await self._pool.run(_call)

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
        schema = Schedule.model_json_schema()

        async def _call(client: AsyncOpenAI, model: str) -> Schedule:
            raw = await self._chat(
                client,
                model,
                prompt=prompt,
                image_bytes=image_bytes,
                format_name="schedule",
                schema=schema,
            )
            result = Schedule.model_validate_json(raw)
            result.class_name = class_name
            result.date = schedule_date.isoformat()
            for lesson in result.schedule:
                lesson.day_of_week = DayOfWeek(day_of_week)
            return result

        return await self._pool.run(_call)
