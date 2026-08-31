import logging
from datetime import date

from google.genai import types

from bot.config import Settings
from models.schedule import ClassList, DayOfWeek, Schedule
from services.llm.base import LLMProvider
from services.llm.key_pool import GeminiKeyPool
from services.llm.prompts import DETECT_CLASSES_PROMPT, PARSE_SCHEDULE_PROMPT

logger = logging.getLogger(__name__)


class GeminiProvider(LLMProvider):
    def __init__(self, settings: Settings):
        keys = settings.gemini_native_keys
        if not keys:
            raise ValueError("Нужен LLM_API_KEY для нативного Gemini")
        self._pool = GeminiKeyPool(keys, proxy=settings.proxy, model=settings.gemini_model)
        self._model = settings.gemini_model
        if settings.proxy:
            logger.info("Gemini через прокси: %s", settings.proxy)

    async def detect_classes(self, image_bytes: bytes) -> ClassList:
        prompt = DETECT_CLASSES_PROMPT.format(today=date.today().isoformat())

        async def _call(client):
            return await client.aio.models.generate_content(
                model=self._model,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ClassList,
                    temperature=0.1,
                ),
            )

        response = await self._pool.run(_call)
        return ClassList.model_validate_json(response.text)

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

        async def _call(client):
            return await client.aio.models.generate_content(
                model=self._model,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=Schedule,
                    temperature=0.1,
                ),
            )

        response = await self._pool.run(_call)
        result = Schedule.model_validate_json(response.text)
        result.class_name = class_name
        result.date = schedule_date.isoformat()
        for lesson in result.schedule:
            lesson.day_of_week = DayOfWeek(day_of_week)
        return result
