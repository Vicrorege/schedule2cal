from typing import Optional

from pydantic import BaseModel, Field

from models.schedule import DayOfWeek


class HomeworkBlock(BaseModel):
    """Один блок домашнего задания из сообщения пользователя."""

    subject: str = Field(description="Название предмета")
    text: str = Field(description="Текст домашнего задания")
    target_date: Optional[str] = Field(
        default=None,
        description="Дата YYYY-MM-DD, если пользователь указал конкретную дату",
    )
    day_of_week: Optional[DayOfWeek] = Field(
        default=None,
        description="День недели, если пользователь указал только день без даты",
    )


class HomeworkParseResult(BaseModel):
    blocks: list[HomeworkBlock] = Field(default_factory=list)
