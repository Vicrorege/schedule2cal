import re
from datetime import date

from models.schedule import DayOfWeek, Lesson, Schedule

WEEKDAY_MAP = {
    0: DayOfWeek.MONDAY,
    1: DayOfWeek.TUESDAY,
    2: DayOfWeek.WEDNESDAY,
    3: DayOfWeek.THURSDAY,
    4: DayOfWeek.FRIDAY,
    5: DayOfWeek.SATURDAY,
    6: DayOfWeek.SUNDAY,
}

WEEKDAY_RU = {
    DayOfWeek.MONDAY: "понедельник",
    DayOfWeek.TUESDAY: "вторник",
    DayOfWeek.WEDNESDAY: "среда",
    DayOfWeek.THURSDAY: "четверг",
    DayOfWeek.FRIDAY: "пятница",
    DayOfWeek.SATURDAY: "суббота",
    DayOfWeek.SUNDAY: "воскресенье",
}

# dd.mm.yyyy | dd.mm.yy | dd.mm
_CAPTION_DATE_RE = re.compile(
    r"(?<!\d)(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?(?!\d)"
)


def day_of_week_from_date(d: date) -> DayOfWeek:
    return WEEKDAY_MAP[d.weekday()]


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def parse_caption_date(caption: str | None, *, today: date | None = None) -> date | None:
    """Достаёт дату из подписи: dd.mm.yyyy / dd.mm.yy / dd.mm."""
    if not caption:
        return None

    today = today or date.today()
    match = _CAPTION_DATE_RE.search(caption)
    if not match:
        return None

    day = int(match.group(1))
    month = int(match.group(2))
    year_raw = match.group(3)

    if year_raw is None:
        year = today.year
    elif len(year_raw) == 2:
        year = 2000 + int(year_raw)
    else:
        year = int(year_raw)

    try:
        return date(year, month, day)
    except ValueError:
        return None


def is_manual_class_selection(caption: str | None) -> bool:
    """Ручной выбор класса: в подписи есть !, manual или ручной."""
    if not caption:
        return False
    if "!" in caption:
        return True
    lower = caption.casefold()
    return any(marker in lower for marker in ("manual", "ручной"))


def _split_rooms(room: str, count: int) -> list[str]:
    room = room.strip()
    if not room:
        return [""] * count

    if "," in room:
        parts = [p.strip() for p in room.split(",")]
    elif "/" in room:
        parts = [p.strip() for p in room.split("/")]
    else:
        parts = [room]

    while len(parts) < count:
        parts.append(parts[-1])
    return parts[:count]


def _is_paired_lesson(lesson: Lesson) -> bool:
    return "/" in lesson.subject


def apply_subgroup(schedule: Schedule, subgroup: int | None) -> Schedule:
    """Разрешает спаренные уроки: предмет и кабинет по индексу подгруппы."""
    if subgroup is None:
        return schedule

    resolved: list[Lesson] = []
    for lesson in schedule.schedule:
        if not _is_paired_lesson(lesson):
            resolved.append(lesson)
            continue

        subjects = [s.strip() for s in lesson.subject.split("/") if s.strip()]
        rooms = _split_rooms(lesson.room or "", len(subjects))
        idx = min(subgroup - 1, len(subjects) - 1)

        resolved.append(
            lesson.model_copy(
                update={
                    "subject": subjects[idx],
                    "room": rooms[idx],
                    "subgroup": subgroup,
                }
            )
        )

    return schedule.model_copy(update={"schedule": resolved})


def find_saved_class(saved: str, detected: list[str]) -> str | None:
    """Ищет сохранённый класс среди обнаруженных (точное или частичное совпадение)."""
    saved_norm = _normalize_class_name(saved)
    for cls in detected:
        if _normalize_class_name(cls) == saved_norm:
            return cls

    for cls in detected:
        cls_norm = _normalize_class_name(cls)
        if saved_norm in cls_norm or cls_norm in saved_norm:
            return cls

    return None


def find_matching_extra_classes(
    saved_extras: list[str],
    detected: list[str],
) -> list[str]:
    """Возвращает названия классов из файла, совпавшие с сохранёнными доп. классами."""
    matched: list[str] = []
    seen: set[str] = set()
    for extra in saved_extras:
        hit = find_saved_class(extra, detected)
        if hit and hit.casefold() not in seen:
            seen.add(hit.casefold())
            matched.append(hit)
    return matched


def _normalize_class_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())
