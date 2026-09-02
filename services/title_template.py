from __future__ import annotations

import difflib
from string import Formatter

from models.schedule import Schedule
from services.schedule_merge import build_calendar_event_plans, format_merged_description_extras
from db.database import BellPeriod

ALLOWED_PLACEHOLDERS = {"lesson", "room", "n", "number"}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def validate_title_template(template: str) -> str | None:
    """Возвращает текст ошибки или None, если шаблон ок."""
    if not template.strip():
        return "Шаблон не может быть пустым"
    try:
        fields = [name for _, name, _, _ in Formatter().parse(template) if name]
    except ValueError as exc:
        return f"Ошибка в шаблоне: {exc}"
    unknown = [f for f in fields if f not in ALLOWED_PLACEHOLDERS]
    if unknown:
        return (
            "Неизвестные плейсхолдеры: "
            + ", ".join(f"{{{u}}}" for u in unknown)
            + ". Доступны: {lesson}, {room}, {n}"
        )
    return None


def apply_title_template(
    template: str,
    *,
    lesson_name: str,
    room: str | None = None,
    lesson_number: int | None = None,
) -> str:
    return template.format_map(
        _SafeDict(
            lesson=lesson_name,
            room=room or "",
            n=str(lesson_number or ""),
            number=str(lesson_number or ""),
        )
    )


def resolve_lesson_name(subject: str, aliases: dict[str, str]) -> str:
    if subject in aliases:
        return aliases[subject]
    # case-insensitive exact
    lower = {k.casefold(): v for k, v in aliases.items()}
    return lower.get(subject.casefold(), subject)


def unique_subjects(schedule: Schedule) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for lesson in sorted(schedule.schedule, key=lambda x: x.lesson_number):
        key = lesson.subject.strip()
        if key and key.casefold() not in seen:
            seen.add(key.casefold())
            result.append(key)
    return result


def subjects_needing_alias(schedule: Schedule, aliases: dict[str, str]) -> list[str]:
    known = {k.casefold() for k in aliases}
    return [s for s in unique_subjects(schedule) if s.casefold() not in known]


def suggest_aliases(subject: str, aliases: dict[str, str], limit: int = 5) -> list[tuple[str, str]]:
    """Близкие исходные названия → (source, alias)."""
    if not aliases:
        return []
    keys = list(aliases.keys())
    close = difflib.get_close_matches(subject, keys, n=limit, cutoff=0.55)
    # также по самим alias
    alias_keys = list({v: k for k, v in aliases.items()}.keys())
    close_alias = difflib.get_close_matches(subject, alias_keys, n=limit, cutoff=0.55)
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for src in close:
        pair = (src, aliases[src])
        if pair[1] not in seen:
            seen.add(pair[1])
            result.append(pair)
    inv = {v: k for k, v in aliases.items()}
    for al in close_alias:
        src = inv[al]
        if al not in seen:
            seen.add(al)
            result.append((src, al))
    return result[:limit]


def format_calendar_events(
    schedule: Schedule,
    *,
    template: str,
    aliases: dict[str, str],
    bells: dict[int, BellPeriod],
    weekday_ru: str | None = None,
    extra_schedules: dict[str, Schedule] | None = None,
) -> str:
    lines: list[str] = ["🗓 <b>События для календаря</b>"]
    if schedule.date:
        try:
            from datetime import date as date_cls

            d = date_cls.fromisoformat(schedule.date)
            date_str = d.strftime("%d.%m.%Y")
        except ValueError:
            date_str = schedule.date
        line = f"📅 {date_str}"
        if weekday_ru:
            line += f" ({weekday_ru})"
        lines.append(line)
    lines.append(f"Шаблон: <code>{_esc(template)}</code>")
    lines.append("")

    plans = build_calendar_event_plans(schedule, extra_schedules)
    for plan in plans:
        time_part = "??:??–??:??"
        if plan.lesson_number in bells:
            b = bells[plan.lesson_number]
            time_part = f"{b.start}–{b.end}"

        if plan.is_extra_only:
            extra_name = plan.extra_class_name or "?"
            lesson = next(iter(plan.extra_lessons.values()))
            name = resolve_lesson_name(lesson.subject, aliases)
            lines.append(f"<b>{plan.lesson_number}.</b> ⏰ {time_part}")
            lines.append(
                f"   🔸 <code>ДОП. КЛАСС | {_esc(extra_name)}: {_esc(name)}</code> (private)"
            )
            lines.append("")
            continue

        lesson = plan.main_lesson
        if not lesson:
            continue
        name = resolve_lesson_name(lesson.subject, aliases)
        title = apply_title_template(
            template,
            lesson_name=name,
            room=lesson.room,
            lesson_number=lesson.lesson_number,
        )
        lines.append(f"<b>{plan.lesson_number}.</b> ⏰ {time_part}")
        lines.append(f"   📌 <code>{_esc(title)}</code>")
        extras_text = format_merged_description_extras(plan.extra_lessons)
        if extras_text.strip():
            for extra_line in extras_text.strip().splitlines():
                lines.append(f"   <i>{_esc(extra_line)}</i>")
        lines.append("")

    return "\n".join(lines).rstrip()


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
