from __future__ import annotations

import json
import logging
from typing import Any

from models.schedule import DayOfWeek, Lesson, Schedule, WeekType

logger = logging.getLogger(__name__)

_SCHEDULE_ALIASES = ("schedule", "lessons", "items", "уроки", "classes_schedule")


def _as_dict(raw: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    text = (raw or "").strip()
    if not text:
        raise ValueError("Пустой JSON расписания")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"Ожидался JSON-объект, получено {type(data).__name__}")
    return data


def _coerce_lesson(item: dict[str, Any], day_of_week: str) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    subject = item.get("subject") or item.get("name") or item.get("предмет")
    lesson_no = item.get("lesson_number") or item.get("number") or item.get("n") or item.get("урок")
    if subject is None or lesson_no is None:
        return None
    try:
        lesson_no = int(lesson_no)
    except (TypeError, ValueError):
        return None

    room = item.get("room") or item.get("кабинет")
    subgroup = item.get("subgroup") or item.get("подгруппа")
    week_type = item.get("week_type") or item.get("week") or "all"
    if isinstance(week_type, str):
        week_type = week_type.strip().lower()
        if week_type in {"числитель", "odd", "1"}:
            week_type = WeekType.ODD.value
        elif week_type in {"знаменатель", "even", "2"}:
            week_type = WeekType.EVEN.value
        else:
            week_type = WeekType.ALL.value

    return {
        "day_of_week": item.get("day_of_week") or day_of_week,
        "lesson_number": lesson_no,
        "subject": str(subject).strip(),
        "room": str(room).strip() if room not in (None, "") else None,
        "subgroup": int(subgroup) if subgroup not in (None, "") else None,
        "week_type": week_type,
    }


def normalize_schedule_payload(
    raw: str | dict[str, Any],
    *,
    class_name: str,
    schedule_date: str,
    day_of_week: str,
) -> Schedule:
    """Приводит кривой JSON от LLM к валидной Schedule."""
    data = _as_dict(raw)

    lessons_raw = None
    for key in _SCHEDULE_ALIASES:
        if key in data:
            lessons_raw = data[key]
            break
    if lessons_raw is None:
        # иногда модель кладёт список уроков в корень как "schedule_items"
        for key, value in data.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                if any(k in value[0] for k in ("subject", "lesson_number", "предмет", "урок")):
                    lessons_raw = value
                    break
    if lessons_raw is None:
        lessons_raw = []
        logger.warning(
            "В ответе LLM нет списка уроков (keys=%s) — пустое расписание",
            list(data.keys()),
        )

    if not isinstance(lessons_raw, list):
        raise ValueError("Поле schedule должно быть списком")

    lessons: list[Lesson] = []
    for item in lessons_raw:
        if not isinstance(item, dict):
            continue
        coerced = _coerce_lesson(item, day_of_week)
        if not coerced:
            continue
        try:
            lessons.append(Lesson.model_validate(coerced))
        except Exception:
            logger.warning("Пропуск урока из LLM: %s", item)

    result = Schedule(
        class_name=str(data.get("class_name") or class_name),
        date=str(data.get("date") or schedule_date),
        schedule=lessons,
    )
    result.class_name = class_name
    result.date = schedule_date
    for lesson in result.schedule:
        lesson.day_of_week = DayOfWeek(day_of_week)
    return result
