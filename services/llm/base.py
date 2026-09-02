from abc import ABC, abstractmethod
from datetime import date

from models.homework import HomeworkParseResult
from models.schedule import ClassList, Schedule


class LLMProvider(ABC):
    @abstractmethod
    async def detect_classes(self, image_bytes: bytes) -> ClassList:
        """Обнаруживает список классов и дату в документе расписания."""

    @abstractmethod
    async def parse_schedule(
        self,
        image_bytes: bytes,
        class_name: str,
        schedule_date: date,
        day_of_week: str,
    ) -> Schedule:
        """Парсит расписание для указанного класса на конкретную дату."""

    @abstractmethod
    async def parse_homework(self, text: str, *, today: date) -> HomeworkParseResult:
        """Разбирает текст домашнего задания на блоки."""
