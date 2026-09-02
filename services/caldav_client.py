from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from caldav import DAVClient
from caldav.lib.error import NotFoundError
from icalendar import Calendar as ICalCalendar
from icalendar import Event as ICalEvent

from db.database import BellPeriod, CalDavCredentials
from models.schedule import Lesson, Schedule, WeekType
from services.schedule_merge import (
    build_calendar_event_plans,
    format_merged_description_extras,
    merge_main_schedule_with_existing,
)
from services.schedule_postprocess import day_of_week_from_date
from services.title_template import apply_title_template, resolve_lesson_name

logger = logging.getLogger(__name__)

BOT_MARKER = "schedule_bot_gen"
UID_PREFIX = "schedbot_"
EXTRA_KIND_TAG = "kind:extra_only"
HOMEWORK_HEADER = "Домашнее задание:"
DEFAULT_TZ = "Europe/Moscow"


@dataclass
class SyncResult:
    ok: bool
    created: int = 0
    deleted: int = 0
    error: str | None = None
    calendar_url: str | None = None
    merged_schedule: Schedule | None = None


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def class_digest(user_id: int, class_name: str) -> str:
    return hashlib.sha1(f"{user_id}:{class_name}".encode()).hexdigest()[:10]


def _make_uid(
    user_id: int,
    class_name: str,
    schedule_date: date,
    lesson_number: int,
    week_type: str,
    *,
    extra: bool = False,
) -> str:
    digest = class_digest(user_id, class_name)
    suffix = "_extra" if extra else ""
    return f"{UID_PREFIX}{digest}_{schedule_date.isoformat()}_{lesson_number}_{week_type}{suffix}"


def _date_tag(schedule_date: date) -> str:
    return f"date:{schedule_date.isoformat()}"


def _class_tag(class_name: str) -> str:
    return f"class:{class_name}"


def _owner_tag(main_class: str) -> str:
    return f"owner_class:{main_class}"


def _extra_class_tag(extra_class: str) -> str:
    return f"extra_class:{extra_class}"


def _event_starts_on_date(ical_data: str, schedule_date: date) -> bool:
    match = re.search(r"DTSTART[^:]*:(\d{8})", ical_data)
    if not match:
        return False
    raw = match.group(1)
    try:
        event_date = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return False
    return event_date == schedule_date


def _dav_root(url: str) -> str:
    match = re.search(r"(https?://[^/]+/.+?/dav/)", url, flags=re.IGNORECASE)
    if match:
        return match.group(1)
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/SOGo/dav/"


def _calendar_id_from_url(url: str) -> str | None:
    match = re.search(r"/Calendar/([^/]+)/?", url, flags=re.IGNORECASE)
    if not match:
        return None
    return unquote(match.group(1).rstrip("/"))


def _resolve_calendar(client: DAVClient, preferred_url: str):
    preferred_id = (_calendar_id_from_url(preferred_url) or "").casefold()

    try:
        calendar = client.calendar(url=preferred_url)
        list(calendar.events())
        return calendar
    except Exception as exc:
        logger.warning("Preferred calendar URL failed (%s), discovering…", exc)

    principal = client.principal()
    calendars = principal.calendars()
    if not calendars:
        raise ValueError(
            "Календари не найдены. Проверь URL вида "
            "https://mail.example.com/SOGo/dav/user@example.com/Calendar/<id>/"
        )

    available = []
    for cal in calendars:
        name = ""
        try:
            name = str(cal.get_display_name() or "")
        except Exception:
            name = ""
        cal_url = str(cal.url)
        available.append(f"{name or '?'} → {cal_url}")
        cal_id = _calendar_id_from_url(cal_url) or ""
        if preferred_id and cal_id.casefold() == preferred_id:
            return cal
        if preferred_id and preferred_id in cal_url.casefold():
            return cal
        if name.casefold() in {"personal", "личный", "private"}:
            return cal

    first = calendars[0]
    logger.info(
        "Using first available calendar %s. Available: %s",
        first.url,
        "; ".join(available),
    )
    return first


def _uid_from_ical(data: str) -> str | None:
    match = re.search(r"^UID:(.+)$", data, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else None


def _week_type_from_uid(uid: str) -> WeekType:
    match = re.search(r"_(all|odd|even)(?:_extra)?$", uid, re.IGNORECASE)
    if not match:
        return WeekType.ALL
    return WeekType(match.group(1).lower())


def _lesson_number_from_description(data: str) -> int | None:
    match = re.search(r"^Урок: (\d+)$", data, re.MULTILINE)
    return int(match.group(1)) if match else None


def _lesson_from_main_event(data: str, lesson_no: int, schedule_date: date) -> Lesson | None:
    subject_match = re.search(r"^Предмет: (.+)$", data, re.MULTILINE)
    if not subject_match:
        return None
    room_match = re.search(r"^Кабинет: (.+)$", data, re.MULTILINE)
    uid = _uid_from_ical(data)
    return Lesson(
        day_of_week=day_of_week_from_date(schedule_date),
        lesson_number=lesson_no,
        subject=subject_match.group(1).strip(),
        room=room_match.group(1).strip() if room_match else None,
        week_type=_week_type_from_uid(uid) if uid else WeekType.ALL,
    )


def _index_bot_events_by_uid(calendar) -> dict[str, object]:
    indexed: dict[str, object] = {}
    try:
        for event in calendar.events():
            data = event.data or ""
            if BOT_MARKER not in data:
                continue
            uid = _uid_from_ical(data)
            if uid:
                indexed[uid] = event
    except Exception:
        logger.exception("Cannot list calendar events for indexing")
    return indexed


def _load_existing_main_lessons(
    calendar,
    *,
    main_class: str,
    main_digest: str,
    schedule_date: date,
) -> dict[int, Lesson]:
    """Читает уроки основного класса из уже записанных событий бота за день."""
    lessons: dict[int, Lesson] = {}
    main_tag = _class_tag(main_class)
    date_prefix = schedule_date.isoformat()
    needle_uid = f"{UID_PREFIX}{main_digest}_{date_prefix}_"

    try:
        events = calendar.events()
    except Exception:
        logger.exception("Cannot list calendar events")
        return lessons

    for event in events:
        try:
            data = event.data or ""
            if BOT_MARKER not in data or main_tag not in data or EXTRA_KIND_TAG in data:
                continue
            if needle_uid not in data and not _event_starts_on_date(data, schedule_date):
                continue
            lesson_no = _lesson_number_from_description(data)
            if lesson_no is None:
                continue
            lesson = _lesson_from_main_event(data, lesson_no, schedule_date)
            if lesson:
                lessons[lesson_no] = lesson
        except Exception:
            logger.exception("Failed to parse existing main event")
    return lessons


def _save_or_update_event(
    calendar,
    existing_by_uid: dict[str, object],
    *,
    dtstart: datetime,
    dtend: datetime,
    summary: str,
    uid: str,
    description: str,
    private: bool = False,
) -> None:
    resource = existing_by_uid.get(uid)
    if resource is None:
        _save_event(
            calendar,
            dtstart=dtstart,
            dtend=dtend,
            summary=summary,
            uid=uid,
            description=description,
            private=private,
        )
        return

    try:
        if private:
            cal = ICalCalendar()
            event = ICalEvent()
            event.add("dtstart", dtstart)
            event.add("dtend", dtend)
            event.add("summary", summary)
            event.add("description", description)
            event.add("uid", uid)
            event.add("class", "PRIVATE")
            event.add("transp", "OPAQUE")
            cal.add_component(event)
            resource.put(cal.to_ical())
            return

        resource.load()
        cal = resource.icalendar_component
        vevent = cal.walk("VEVENT")[0]
        for key in ("summary", "description", "dtstart", "dtend"):
            vevent.pop(key, None)
        vevent.add("summary", summary)
        vevent.add("description", description)
        vevent.add("dtstart", dtstart)
        vevent.add("dtend", dtend)
        resource.save(cal.to_ical())
    except Exception:
        logger.exception("Failed to update event %s, recreating", uid)
        try:
            resource.delete()
        except Exception:
            logger.exception("Failed to delete stale event %s", uid)
        _save_event(
            calendar,
            dtstart=dtstart,
            dtend=dtend,
            summary=summary,
            uid=uid,
            description=description,
            private=private,
        )


def _save_event(
    calendar,
    *,
    dtstart: datetime,
    dtend: datetime,
    summary: str,
    uid: str,
    description: str,
    private: bool = False,
) -> None:
    if not private:
        calendar.save_event(
            dtstart=dtstart,
            dtend=dtend,
            summary=summary,
            uid=uid,
            description=description,
        )
        return

    cal = ICalCalendar()
    event = ICalEvent()
    event.add("dtstart", dtstart)
    event.add("dtend", dtend)
    event.add("summary", summary)
    event.add("description", description)
    event.add("uid", uid)
    event.add("class", "PRIVATE")
    event.add("transp", "OPAQUE")
    cal.add_component(event)
    calendar.add_event(cal.to_ical())


def test_connection(creds: CalDavCredentials) -> str:
    if not creds.is_complete:
        raise ValueError("Нужны URL, логин и пароль")

    root = _dav_root(creds.url)
    with DAVClient(url=root, username=creds.username, password=creds.password) as client:
        calendar = _resolve_calendar(client, creds.url)
        return f"OK → {calendar.url}"


def sync_schedule_to_caldav(
    creds: CalDavCredentials,
    schedule: Schedule,
    *,
    user_id: int,
    template: str,
    aliases: dict[str, str],
    bells: dict[int, BellPeriod],
    extra_schedules: dict[str, Schedule] | None = None,
    tz_name: str = DEFAULT_TZ,
) -> SyncResult:
    if not creds.is_complete:
        return SyncResult(ok=False, error="Не заданы CalDAV URL / логин / пароль")

    if not schedule.date:
        return SyncResult(ok=False, error="В расписании нет даты")

    schedule_date = date.fromisoformat(schedule.date)
    tz = ZoneInfo(tz_name)
    main_tag = _class_tag(schedule.class_name)
    main_digest = class_digest(user_id, schedule.class_name)
    owner_tag = _owner_tag(schedule.class_name)
    date_tag = _date_tag(schedule_date)
    root = _dav_root(creds.url)
    extra_schedules = extra_schedules or {}
    additive_extra = bool(extra_schedules)

    try:
        with DAVClient(url=root, username=creds.username, password=creds.password) as client:
            calendar = _resolve_calendar(client, creds.url)

            if additive_extra:
                existing_main = _load_existing_main_lessons(
                    calendar,
                    main_class=schedule.class_name,
                    main_digest=main_digest,
                    schedule_date=schedule_date,
                )
                schedule = merge_main_schedule_with_existing(schedule, existing_main)
                deleted = _delete_extra_owned_events_for_day(
                    calendar,
                    owner_tag=owner_tag,
                    schedule_date=schedule_date,
                    extra_class_names=list(extra_schedules),
                )
                existing_by_uid = _index_bot_events_by_uid(calendar)
            else:
                deleted = _delete_bot_events_for_day(
                    calendar,
                    main_class=schedule.class_name,
                    main_digest=main_digest,
                    owner_tag=owner_tag,
                    schedule_date=schedule_date,
                )
                existing_by_uid = {}

            plans = build_calendar_event_plans(schedule, extra_schedules)
            created = 0

            for plan in plans:
                if plan.lesson_number not in bells:
                    continue
                bell = bells[plan.lesson_number]
                dtstart = datetime.combine(schedule_date, _parse_hhmm(bell.start), tzinfo=tz)
                dtend = datetime.combine(schedule_date, _parse_hhmm(bell.end), tzinfo=tz)

                if plan.is_extra_only:
                    extra_name = plan.extra_class_name or next(iter(plan.extra_lessons))
                    lesson = plan.extra_lessons[extra_name]
                    lesson_name = resolve_lesson_name(lesson.subject, aliases)
                    room_part = f" ({lesson.room})" if lesson.room else ""
                    summary = f"🔸 ДОП. КЛАСС | {extra_name}: {lesson_name}{room_part}"
                    uid = _make_uid(
                        user_id,
                        extra_name,
                        schedule_date,
                        lesson.lesson_number,
                        lesson.week_type.value,
                        extra=True,
                    )
                    description = (
                        f"Бот-идентификатор: {BOT_MARKER}\n"
                        f"{EXTRA_KIND_TAG}\n"
                        f"{owner_tag}\n"
                        f"{_extra_class_tag(extra_name)}\n"
                        f"{date_tag}\n"
                        f"⚠️ Урок дополнительного класса (у основного класса окно)\n"
                        f"Основной класс: {schedule.class_name}\n"
                        f"Доп. класс: {extra_name}\n"
                        f"Предмет: {lesson.subject}\n"
                        f"Урок: {lesson.lesson_number}"
                    )
                    if lesson.room:
                        description += f"\nКабинет: {lesson.room}"
                    _save_or_update_event(
                        calendar,
                        existing_by_uid,
                        dtstart=dtstart,
                        dtend=dtend,
                        summary=summary,
                        uid=uid,
                        description=description,
                        private=True,
                    )
                    created += 1
                    continue

                lesson = plan.main_lesson
                if not lesson:
                    continue

                lesson_name = resolve_lesson_name(lesson.subject, aliases)
                summary = apply_title_template(
                    template,
                    lesson_name=lesson_name,
                    room=lesson.room,
                    lesson_number=lesson.lesson_number,
                )
                uid = _make_uid(
                    user_id,
                    schedule.class_name,
                    schedule_date,
                    lesson.lesson_number,
                    lesson.week_type.value,
                )
                description = (
                    f"Бот-идентификатор: {BOT_MARKER}\n"
                    f"{main_tag}\n"
                    f"{date_tag}\n"
                    f"Предмет: {lesson.subject}\n"
                    f"Урок: {lesson.lesson_number}"
                )
                if lesson.room:
                    description += f"\nКабинет: {lesson.room}"
                description += format_merged_description_extras(plan.extra_lessons)

                if additive_extra:
                    _save_or_update_event(
                        calendar,
                        existing_by_uid,
                        dtstart=dtstart,
                        dtend=dtend,
                        summary=summary,
                        uid=uid,
                        description=description,
                    )
                else:
                    _save_event(
                        calendar,
                        dtstart=dtstart,
                        dtend=dtend,
                        summary=summary,
                        uid=uid,
                        description=description,
                    )
                created += 1

        return SyncResult(
            ok=True,
            created=created,
            deleted=deleted,
            calendar_url=str(calendar.url),
            merged_schedule=schedule,
        )
    except NotFoundError as exc:
        logger.exception("CalDAV calendar not found")
        return SyncResult(
            ok=False,
            error=(
                f"Календарь не найден ({exc}). "
                "Укажи точный CalDAV URL или проверь имя календаря "
                "(часто не personal). В /settings → CalDAV нажми «Проверить подключение»."
            ),
        )
    except Exception as exc:
        logger.exception("CalDAV sync failed")
        return SyncResult(ok=False, error=str(exc))


def _delete_extra_owned_events_for_day(
    calendar,
    *,
    owner_tag: str,
    schedule_date: date,
    extra_class_names: list[str] | None = None,
) -> int:
    """Удаляет только private-события доп. классов для основного класса."""
    deleted = 0
    try:
        events = calendar.events()
    except Exception:
        logger.exception("Cannot list calendar events")
        return 0

    date_tag = _date_tag(schedule_date)

    for event in events:
        try:
            data = event.data or ""
            same_date = date_tag in data or _event_starts_on_date(data, schedule_date)
            if not same_date:
                continue

            is_extra_owned = (
                BOT_MARKER in data
                and EXTRA_KIND_TAG in data
                and owner_tag in data
            )
            if not is_extra_owned:
                continue

            if extra_class_names:
                has_class = any(
                    _extra_class_tag(name) in data for name in extra_class_names
                )
                if not has_class:
                    continue

            event.delete()
            deleted += 1
        except Exception:
            logger.exception("Failed to delete extra-owned calendar event")
    return deleted


def _delete_bot_events_for_day(
    calendar,
    *,
    main_class: str,
    main_digest: str,
    owner_tag: str,
    schedule_date: date,
) -> int:
    """Удаляет события бота за день: основной класс + доп. private от этой сессии."""
    deleted = 0
    try:
        events = calendar.events()
    except Exception:
        logger.exception("Cannot list calendar events")
        return 0

    main_tag = _class_tag(main_class)
    date_prefix = schedule_date.isoformat()
    needle_uid = f"{UID_PREFIX}{main_digest}_{date_prefix}_"
    date_tag = _date_tag(schedule_date)

    for event in events:
        try:
            data = event.data or ""
            same_date = (
                needle_uid in data
                or date_tag in data
                or _event_starts_on_date(data, schedule_date)
            )
            if not same_date:
                continue

            is_main = BOT_MARKER in data and main_tag in data
            is_extra_owned = (
                BOT_MARKER in data
                and EXTRA_KIND_TAG in data
                and owner_tag in data
            )
            if is_main or is_extra_owned:
                event.delete()
                deleted += 1
        except Exception:
            logger.exception("Failed to delete calendar event")
    return deleted


@dataclass
class BotCalendarEvent:
    uid: str
    event_date: date
    lesson_number: int
    subject: str
    room: str | None
    summary: str
    description: str
    homework: str | None
    is_extra_only: bool
    resource: object | None = None


def extract_homework(description: str) -> str | None:
    if HOMEWORK_HEADER not in description:
        return None
    after = description.split(HOMEWORK_HEADER, 1)[1]
    # обрезаем возможный следующий машинный блок
    lines = []
    for line in after.lstrip("\n").splitlines():
        if line.startswith("Бот-идентификатор:") or line.startswith("kind:"):
            break
        lines.append(line)
    text = "\n".join(lines).strip()
    return text or None


def set_homework_in_description(description: str, homework_text: str) -> str:
    base = description
    if HOMEWORK_HEADER in base:
        before = base.split(HOMEWORK_HEADER, 1)[0].rstrip()
        after_part = base.split(HOMEWORK_HEADER, 1)[1]
        # сохраняем хвост после ДЗ, если он есть (обычно нет)
        rest_lines = []
        started = False
        for line in after_part.lstrip("\n").splitlines():
            if started:
                rest_lines.append(line)
            elif line.startswith("Бот-идентификатор:") or line.startswith("kind:"):
                started = True
                rest_lines.append(line)
        rest = ("\n" + "\n".join(rest_lines)) if rest_lines else ""
        base = before + rest
    homework_text = homework_text.strip()
    if not homework_text:
        return base.rstrip()
    return f"{base.rstrip()}\n\n{HOMEWORK_HEADER}\n{homework_text}"


def _summary_from_ical(data: str) -> str:
    match = re.search(r"^SUMMARY:(.+)$", data, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _description_from_ical(data: str) -> str:
    # DESCRIPTION может быть многострочным с пробелами в начале строк
    match = re.search(
        r"^DESCRIPTION:((?:.*(?:\r?\n[ \t].*)*))",
        data,
        re.MULTILINE | re.IGNORECASE,
    )
    if not match:
        return ""
    raw = match.group(1)
    return re.sub(r"\r?\n[ \t]", "", raw).replace("\\n", "\n").strip()


def _parse_bot_event(data: str, resource=None) -> BotCalendarEvent | None:
    if BOT_MARKER not in data:
        return None
    uid = _uid_from_ical(data)
    if not uid:
        return None

    date_match = re.search(r"date:(\d{4}-\d{2}-\d{2})", data)
    if date_match:
        event_date = date.fromisoformat(date_match.group(1))
    else:
        dt_match = re.search(r"DTSTART[^:]*:(\d{8})", data)
        if not dt_match:
            return None
        raw = dt_match.group(1)
        event_date = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))

    lesson_no = _lesson_number_from_description(data)
    if lesson_no is None:
        # fallback из UID: schedbot_{digest}_{date}_{n}_{week}
        uid_parts = uid.split("_")
        try:
            lesson_no = int(uid_parts[-2] if not uid.endswith("_extra") else uid_parts[-3])
        except (ValueError, IndexError):
            return None

    subject_match = re.search(r"^Предмет: (.+)$", data, re.MULTILINE)
    subject = subject_match.group(1).strip() if subject_match else _summary_from_ical(data)
    room_match = re.search(r"^Кабинет: (.+)$", data, re.MULTILINE)
    description = _description_from_ical(data) or data

    return BotCalendarEvent(
        uid=uid,
        event_date=event_date,
        lesson_number=lesson_no,
        subject=subject,
        room=room_match.group(1).strip() if room_match else None,
        summary=_summary_from_ical(data),
        description=description,
        homework=extract_homework(description),
        is_extra_only=EXTRA_KIND_TAG in data,
        resource=resource,
    )


def list_bot_events(
    creds: CalDavCredentials,
    *,
    user_id: int,
    class_name: str,
    start_date: date,
    end_date: date,
    include_extra: bool = False,
) -> list[BotCalendarEvent]:
    """Список событий бота для класса в диапазоне дат (включительно)."""
    if not creds.is_complete:
        raise ValueError("Не заданы CalDAV URL / логин / пароль")

    main_tag = _class_tag(class_name)
    main_digest = class_digest(user_id, class_name)
    root = _dav_root(creds.url)
    results: list[BotCalendarEvent] = []

    with DAVClient(url=root, username=creds.username, password=creds.password) as client:
        calendar = _resolve_calendar(client, creds.url)
        try:
            start_dt = datetime.combine(start_date, time.min)
            end_dt = datetime.combine(end_date, time(23, 59, 59))
            try:
                raw_events = list(calendar.date_search(start=start_dt, end=end_dt))
            except Exception:
                logger.warning("date_search failed, falling back to events()")
                raw_events = list(calendar.events())
        except Exception:
            logger.exception("Cannot list calendar events")
            return []

        for event in raw_events:
            try:
                data = event.data or ""
                if BOT_MARKER not in data:
                    continue
                if main_tag not in data and f"{UID_PREFIX}{main_digest}_" not in data:
                    # extra-only owned by this class
                    if not (
                        include_extra
                        and EXTRA_KIND_TAG in data
                        and _owner_tag(class_name) in data
                    ):
                        continue
                elif EXTRA_KIND_TAG in data and not include_extra:
                    continue

                parsed = _parse_bot_event(data, resource=event)
                if not parsed:
                    continue
                if parsed.event_date < start_date or parsed.event_date > end_date:
                    continue
                results.append(parsed)
            except Exception:
                logger.exception("Failed to parse bot calendar event")

    results.sort(key=lambda e: (e.event_date, e.lesson_number))
    return results


@dataclass
class HomeworkWriteResult:
    ok: bool
    updated: int = 0
    error: str | None = None


def write_homework_to_events(
    creds: CalDavCredentials,
    assignments: list[tuple[BotCalendarEvent, str]],
) -> HomeworkWriteResult:
    """Записывает текст ДЗ в описания указанных событий (по UID)."""
    if not creds.is_complete:
        return HomeworkWriteResult(ok=False, error="Не заданы CalDAV URL / логин / пароль")
    if not assignments:
        return HomeworkWriteResult(ok=True, updated=0)

    root = _dav_root(creds.url)
    try:
        with DAVClient(url=root, username=creds.username, password=creds.password) as client:
            calendar = _resolve_calendar(client, creds.url)
            indexed = _index_bot_events_by_uid(calendar)
            updated = 0
            for event_view, homework_text in assignments:
                resource = indexed.get(event_view.uid) or event_view.resource
                if resource is None:
                    logger.warning("Event %s not found for homework update", event_view.uid)
                    continue
                try:
                    resource.load()
                    cal = resource.icalendar_component
                    vevent = cal.walk("VEVENT")[0]
                    old_desc = ""
                    if vevent.get("description") is not None:
                        old_desc = str(vevent.get("description"))
                    new_description = set_homework_in_description(old_desc, homework_text)
                    vevent.pop("description", None)
                    vevent.add("description", new_description)
                    try:
                        resource.save(cal.to_ical())
                    except TypeError:
                        resource.save()
                    updated += 1
                except Exception:
                    logger.exception("Failed to update homework on %s", event_view.uid)
            return HomeworkWriteResult(ok=True, updated=updated)
    except Exception as exc:
        logger.exception("Homework write failed")
        return HomeworkWriteResult(ok=False, error=str(exc))
