from datetime import date

from models.schedule import Lesson, Schedule, WeekType
from services.schedule_merge import build_calendar_event_plans, format_extra_lesson_line
from db.database import BellPeriod


WEEK_TYPE_EMOJI = {
    WeekType.ALL: "",
    WeekType.ODD: " 🔢 числ.",
    WeekType.EVEN: " ➗ знам.",
}


def format_lesson_line(lesson: Lesson, bells: dict[int, BellPeriod] | None = None) -> str:
    time_part = ""
    if bells and lesson.lesson_number in bells:
        b = bells[lesson.lesson_number]
        time_part = f"  <i>{b.start}–{b.end}</i>"

    lines = [f"<b>{lesson.lesson_number}.</b>{time_part}  📘 <b>{_esc(lesson.subject)}</b>"]

    details: list[str] = []
    if lesson.room:
        details.append(f"🚪 {_esc(lesson.room)}")
    if lesson.subgroup:
        details.append(f"👥 п.г. {lesson.subgroup}")

    week_mark = WEEK_TYPE_EMOJI.get(lesson.week_type, "")
    if week_mark:
        details.append(week_mark.strip())

    if details:
        lines.append("   " + " · ".join(details))
    return "\n".join(lines)


def format_schedule_preview(
    schedule: Schedule,
    *,
    bells: dict[int, BellPeriod] | None = None,
    weekday_ru: str | None = None,
    subgroup: int | None = None,
    extra_schedules: dict[str, Schedule] | None = None,
) -> str:
    header = [f"📚 <b>Основной класс: {_esc(schedule.class_name)}</b>"]
    extras = extra_schedules or {}
    if extras:
        header.append(
            "➕ Доп. классы: "
            + ", ".join(_esc(name) for name in sorted(extras))
        )
    if schedule.date:
        try:
            d = date.fromisoformat(schedule.date)
            date_str = d.strftime("%d.%m.%Y")
        except ValueError:
            date_str = schedule.date
        date_line = f"📅 {date_str}"
        if weekday_ru:
            date_line += f" ({weekday_ru})"
        header.append(date_line)
    if subgroup:
        header.append(f"👥 Подгруппа {subgroup}")

    header.append("")
    header.append("⚠️ <b>Обязательно проверьте расписание</b> — ИИ может ошибаться.")
    header.append("")

    plans = build_calendar_event_plans(schedule, extras)
    if not plans:
        header.append("Пусто — уроков не распознано.")
    else:
        for plan in plans:
            if plan.is_extra_only:
                extra_name = plan.extra_class_name or "?"
                lesson = next(iter(plan.extra_lessons.values()))
                header.append(
                    f"<b>{plan.lesson_number}.</b>  🔸 <b>ДОП. КЛАСС</b> "
                    f"({_esc(extra_name)})"
                )
                header.append(
                    "   " + _esc(format_extra_lesson_line(extra_name, lesson).lstrip("• "))
                )
            else:
                lesson = plan.main_lesson
                if lesson:
                    header.append(format_lesson_line(lesson, bells))
                    for extra_name in sorted(plan.extra_lessons):
                        extra_lesson = plan.extra_lessons[extra_name]
                        header.append(
                            "   ↳ "
                            + _esc(format_extra_lesson_line(extra_name, extra_lesson).lstrip("• "))
                        )
            header.append("")

    header.append(f"📊 Событий в календаре: {len(plans)}")
    return "\n".join(header).rstrip()


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
