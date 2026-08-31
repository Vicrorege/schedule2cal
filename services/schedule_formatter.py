from datetime import date

from models.schedule import Lesson, Schedule, WeekType
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
) -> str:
    header = [f"📚 <b>{_esc(schedule.class_name)}</b>"]
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

    lessons = sorted(schedule.schedule, key=lambda x: x.lesson_number)
    if not lessons:
        header.append("Пусто — уроков не распознано.")
    else:
        for lesson in lessons:
            header.append(format_lesson_line(lesson, bells))
            header.append("")

    header.append(f"📊 Уроков: {len(lessons)}")
    return "\n".join(header).rstrip()


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
