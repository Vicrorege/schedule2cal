import logging
from datetime import date

from google import genai
from google.genai import types

from bot.config import Settings
from models.schedule import ClassList, DayOfWeek, Schedule
from services.http_proxy import gemini_http_options
from services.llm.base import LLMProvider
from services.llm.prompts import DETECT_CLASSES_PROMPT, PARSE_SCHEDULE_PROMPT

logger = logging.getLogger(__name__)


class GeminiProvider(LLMProvider):
    def __init__(self, settings: Settings):
        client_kwargs: dict = {"api_key": settings.llm_api_key}
        if settings.proxy:
            client_kwargs["http_options"] = gemini_http_options(settings.proxy)
            logger.info("Gemini через прокси: %s", settings.proxy)

        self._client = genai.Client(**client_kwargs)
        self._model = settings.gemini_model

    async def detect_classes(self, image_bytes: bytes) -> ClassList:
        prompt = DETECT_CLASSES_PROMPT.format(today=date.today().isoformat())
        response = await self._client.aio.models.generate_content(
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

        response = await self._client.aio.models.generate_content(
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
        result = Schedule.model_validate_json(response.text)
        result.class_name = class_name
        result.date = schedule_date.isoformat()
        for lesson in result.schedule:
            lesson.day_of_week = DayOfWeek(day_of_week)
        return result
