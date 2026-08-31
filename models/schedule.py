from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class DayOfWeek(str, Enum):
    MONDAY = "MONDAY"
    TUESDAY = "TUESDAY"
    WEDNESDAY = "WEDNESDAY"
    THURSDAY = "THURSDAY"
    FRIDAY = "FRIDAY"
    SATURDAY = "SATURDAY"
    SUNDAY = "SUNDAY"


class WeekType(str, Enum):
    ALL = "all"
    ODD = "odd"
    EVEN = "even"


class Lesson(BaseModel):
    day_of_week: DayOfWeek
    lesson_number: int = Field(ge=1, le=12)
    subject: str
    room: Optional[str] = None
    subgroup: Optional[int] = Field(default=None, ge=1, le=2)
    week_type: WeekType = WeekType.ALL


class Schedule(BaseModel):
    class_name: str
    date: Optional[str] = Field(
        default=None,
        description="Дата расписания в формате YYYY-MM-DD",
    )
    schedule: list[Lesson]


class ClassList(BaseModel):
    """Список классов и дата, обнаруженные в документе."""

    classes: list[str]
    schedule_date: Optional[str] = Field(
        default=None,
        description="Дата из заголовка документа в формате YYYY-MM-DD, если указана",
    )
    day_of_week: Optional[DayOfWeek] = Field(
        default=None,
        description="День недели из заголовка, если указан",
    )
