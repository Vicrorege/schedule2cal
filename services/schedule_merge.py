from __future__ import annotations

from dataclasses import dataclass

from models.schedule import Lesson, Schedule


@dataclass(frozen=True)
class CalendarEventPlan:
    lesson_number: int
    is_extra_only: bool
    main_lesson: Lesson | None
    extra_lessons: dict[str, Lesson]
    extra_class_name: str | None = None  # для extra-only: какой доп. класс


def lessons_by_number(schedule: Schedule) -> dict[int, Lesson]:
    return {lesson.lesson_number: lesson for lesson in schedule.schedule}


def merge_main_schedule_with_existing(
    main: Schedule,
    existing: dict[int, Lesson],
) -> Schedule:
    """Сохраняет уроки основного класса из календаря, если в новом парсе слот пуст."""
    if not existing:
        return main
    new_map = lessons_by_number(main)
    merged: list[Lesson] = []
    for lesson_no in sorted(set(new_map) | set(existing)):
        lesson = new_map.get(lesson_no) or existing.get(lesson_no)
        if lesson:
            merged.append(lesson)
    return main.model_copy(update={"schedule": merged})


def build_calendar_event_plans(
    main: Schedule,
    extra_schedules: dict[str, Schedule] | None = None,
) -> list[CalendarEventPlan]:
    """Планы событий: основной класс + доп. классы в описании / отдельные private."""
    extra_schedules = extra_schedules or {}
    main_map = lessons_by_number(main)
    extra_maps = {name: lessons_by_number(s) for name, s in extra_schedules.items()}

    lesson_numbers: set[int] = set(main_map)
    for emap in extra_maps.values():
        lesson_numbers.update(emap)

    plans: list[CalendarEventPlan] = []
    for lesson_no in sorted(lesson_numbers):
        main_lesson = main_map.get(lesson_no)
        extras_at_slot = {
            class_name: emap[lesson_no]
            for class_name, emap in extra_maps.items()
            if lesson_no in emap
        }

        if main_lesson:
            plans.append(
                CalendarEventPlan(
                    lesson_number=lesson_no,
                    is_extra_only=False,
                    main_lesson=main_lesson,
                    extra_lessons=extras_at_slot,
                )
            )
            continue

        for extra_name, extra_lesson in extras_at_slot.items():
            plans.append(
                CalendarEventPlan(
                    lesson_number=lesson_no,
                    is_extra_only=True,
                    main_lesson=None,
                    extra_lessons={extra_name: extra_lesson},
                    extra_class_name=extra_name,
                )
            )

    return plans


def format_extra_lesson_line(class_name: str, lesson: Lesson) -> str:
    parts = [f"• {class_name}: {lesson.subject}"]
    if lesson.room:
        parts.append(f"каб. {lesson.room}")
    return " ".join(parts)


def format_merged_description_extras(extra_lessons: dict[str, Lesson]) -> str:
    if not extra_lessons:
        return ""
    lines = ["", "Доп. классы:"]
    for class_name in sorted(extra_lessons):
        lines.append(format_extra_lesson_line(class_name, extra_lessons[class_name]))
    return "\n".join(lines)
