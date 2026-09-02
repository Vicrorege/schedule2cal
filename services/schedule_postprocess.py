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
        parts = [p.strip() for p in room.split(",") if p.strip()]
    elif "/" in room:
        parts = [p.strip() for p in room.split("/") if p.strip()]
    elif "\n" in room:
        parts = [p.strip() for p in room.splitlines() if p.strip()]
    else:
        parts = [room]

    while len(parts) < count:
        parts.append(parts[-1] if parts else "")
    return parts[:count]


def _is_paired_lesson(lesson: Lesson) -> bool:
    return len(split_paired_subjects(lesson.subject)) >= 2


def split_paired_subjects(subject: str) -> list[str]:
    """Разбивает «Англ/Физика» или многострочный предмет на части."""
    text = (subject or "").strip()
    if not text:
        return []
    # вертикальная запись в ячейке → строки
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" in text:
        parts = [p.strip() for p in text.split("\n") if p.strip()]
        if len(parts) >= 2:
            return parts
    # слэш / вертикальная черта / плюс как разделитель подгрупп
    if any(sep in text for sep in ("/", "|", "+")):
        parts = [p.strip() for p in re.split(r"[/|+]", text) if p.strip()]
        if len(parts) >= 2:
            return parts
    return [text]


def has_paired_lessons(schedule: Schedule) -> bool:
    return any(_is_paired_lesson(lesson) for lesson in schedule.schedule)


def normalize_paired_lessons(schedule: Schedule) -> Schedule:
    """Приводит спаренные уроки к виду subject='A/B', room='101,102'."""
    resolved: list[Lesson] = []
    for lesson in schedule.schedule:
        subjects = split_paired_subjects(lesson.subject)
        if len(subjects) < 2:
            resolved.append(lesson)
            continue
        rooms = _split_rooms(lesson.room or "", len(subjects))
        room_value = ",".join(r for r in rooms if r) or None
        resolved.append(
            lesson.model_copy(
                update={
                    "subject": "/".join(subjects),
                    "room": room_value,
                    "subgroup": None,
                }
            )
        )
    return schedule.model_copy(update={"schedule": resolved})


def apply_subgroup(schedule: Schedule, subgroup: int | None) -> Schedule:
    """Разрешает спаренные уроки: предмет и кабинет по индексу подгруппы."""
    schedule = normalize_paired_lessons(schedule)
    if subgroup is None:
        return schedule

    resolved: list[Lesson] = []
    for lesson in schedule.schedule:
        subjects = split_paired_subjects(lesson.subject)
        if len(subjects) < 2:
            resolved.append(lesson)
            continue

        rooms = _split_rooms(lesson.room or "", len(subjects))
        idx = min(subgroup - 1, len(subjects) - 1)

        resolved.append(
            lesson.model_copy(
                update={
                    "subject": subjects[idx],
                    "room": rooms[idx] or None,
                    "subgroup": subgroup,
                }
            )
        )

    return schedule.model_copy(update={"schedule": resolved})


def format_lesson_subjects_plain(lesson: Lesson) -> str:
    """Текст предмета без маркера подгруппы: для доп. классов оба варианта."""
    subjects = split_paired_subjects(lesson.subject)
    if len(subjects) < 2:
        return lesson.subject
    rooms = _split_rooms(lesson.room or "", len(subjects))
    chunks: list[str] = []
    for subject, room in zip(subjects, rooms):
        if room:
            chunks.append(f"{subject} ({room})")
        else:
            chunks.append(subject)
    return " / ".join(chunks)


def subjects_from_schedule(schedule: Schedule, *, expand_pairs: bool = False) -> list[str]:
    """Уникальные предметы; expand_pairs=True режет A/B на отдельные имена."""
    seen: set[str] = set()
    result: list[str] = []
    for lesson in sorted(schedule.schedule, key=lambda x: x.lesson_number):
        parts = (
            split_paired_subjects(lesson.subject)
            if expand_pairs
            else [lesson.subject.strip()]
        )
        for part in parts:
            key = part.strip()
            if not key or key.casefold() in seen:
                continue
            seen.add(key.casefold())
            result.append(key)
    return result


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
