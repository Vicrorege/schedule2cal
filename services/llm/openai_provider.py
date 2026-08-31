import base64
import logging
from datetime import date

from openai import AsyncOpenAI

from bot.config import Settings
from models.schedule import ClassList, DayOfWeek, Schedule
from services.http_proxy import openai_http_client
from services.llm.base import LLMProvider
from services.llm.prompts import DETECT_CLASSES_PROMPT, PARSE_SCHEDULE_PROMPT

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    def __init__(self, settings: Settings):
        client_kwargs: dict = {"api_key": settings.llm_api_key}
        if settings.proxy:
            client_kwargs["http_client"] = openai_http_client(settings.proxy)
            logger.info("OpenAI через прокси: %s", settings.proxy)

        self._client = AsyncOpenAI(**client_kwargs)
        self._model = settings.openai_model

    async def detect_classes(self, image_bytes: bytes) -> ClassList:
        prompt = DETECT_CLASSES_PROMPT.format(today=date.today().isoformat())
        b64 = base64.standard_b64encode(image_bytes).decode()
        response = await self._client.chat.completions.create(
            model=self._model,
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
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "class_list",
                    "strict": True,
                    "schema": ClassList.model_json_schema(),
                },
            },
            temperature=0.1,
        )
        return ClassList.model_validate_json(response.choices[0].message.content)

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
        b64 = base64.standard_b64encode(image_bytes).decode()
        response = await self._client.chat.completions.create(
            model=self._model,
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
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "schedule",
                    "strict": True,
                    "schema": Schedule.model_json_schema(),
                },
            },
            temperature=0.1,
        )
        result = Schedule.model_validate_json(response.choices[0].message.content)
        result.class_name = class_name
        result.date = schedule_date.isoformat()
        for lesson in result.schedule:
            lesson.day_of_week = DayOfWeek(day_of_week)
        return result
