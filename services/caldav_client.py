from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
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
from services.title_template import (
    apply_title_template,
    resolve_lesson_name,
    resolve_paired_lesson_label,
)

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


def _lesson_number_from_uid(uid: str, schedule_date: date) -> int | None:
    """schedbot_{digest}_{YYYY-MM-DD}_{n}_{week}[_extra] — допускается @host."""
    bare = (uid or "").split("@", 1)[0]
    date_prefix = schedule_date.isoformat()
    match = re.search(
        rf"{re.escape(UID_PREFIX)}[0-9a-f]+_{re.escape(date_prefix)}_(\d+)_(?:all|odd|even)(?:_extra)?$",
        bare,
        re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def _lesson_number_from_description(data: str) -> int | None:
    match = re.search(r"^Урок: (\d+)$", data, re.MULTILINE)
    return int(match.group(1)) if match else None


def _lesson_number_from_summary(summary: str) -> int | None:
    match = re.search(r"(?i)\bsch\s*(\d+)\s*[\.\:]", summary or "")
    return int(match.group(1)) if match else None


def _resolve_lesson_number(
    data: str,
    uid: str,
    summary: str,
    schedule_date: date,
    bells: dict[int, BellPeriod] | None = None,
) -> int | None:
    bare = (uid or "").split("@", 1)[0]
    lesson_no = (
        _lesson_number_from_description(data)
        or _lesson_number_from_uid(bare, schedule_date)
        or _lesson_number_from_summary(summary)
    )
    if lesson_no is not None:
        return lesson_no
    if not bells:
        return None
    match = re.search(r"DTSTART[^:]*:(\d{8})T(\d{6})", data)
    if not match:
        return None
    raw = match.group(2)
    start = time(int(raw[0:2]), int(raw[2:4]))
    for number, bell in bells.items():
        bell_start = _parse_hhmm(bell.start)
        if bell_start.hour == start.hour and bell_start.minute == start.minute:
            return number
    return None


def _looks_like_bot_schedule_event(data: str, summary: str, uid: str = "") -> bool:
    bare = (uid or "").split("@", 1)[0]
    if BOT_MARKER in data or bare.startswith(UID_PREFIX):
        return True
    if "ДОП. КЛАСС" in (summary or ""):
        return True
    if re.match(r"(?i)^\s*sch\b", summary or ""):
        return True
    return False


def _safe_delete_event(event) -> bool:
    try:
        event.delete()
        return True
    except Exception:
        logger.exception("CalDAV delete failed for %s", getattr(event, "url", "?"))
        return False


def _summary_from_ical(data: str) -> str:
    match = re.search(r"^SUMMARY:(.+)$", data, re.MULTILINE | re.IGNORECASE)
    return match.group(1).strip() if match else ""


def _description_body_from_ical(data: str) -> str:
    match = re.search(
        r"^DESCRIPTION:((?:.*(?:\r?\n[ \t].*)*))",
        data,
        re.MULTILINE | re.IGNORECASE,
    )
    if not match:
        # fallback: уже развёрнутый текст с нашими тегами
        if "Предмет:" in data or BOT_MARKER in data:
            return data
        return ""
    raw = match.group(1)
    return re.sub(r"\r?\n[ \t]", "", raw).replace("\\n", "\n").strip()


def _lesson_from_main_event(data: str, lesson_no: int, schedule_date: date) -> Lesson | None:
    subject_match = re.search(r"^Предмет: (.+)$", data, re.MULTILINE)
    summary = _summary_from_ical(data)
    subject = subject_match.group(1).strip() if subject_match else (summary or f"Урок {lesson_no}")
    room_match = re.search(r"^Кабинет: (.+)$", data, re.MULTILINE)
    uid = _uid_from_ical(data)
    return Lesson(
        day_of_week=day_of_week_from_date(schedule_date),
        lesson_number=lesson_no,
        subject=subject,
        room=room_match.group(1).strip() if room_match else None,
        week_type=_week_type_from_uid(uid) if uid else WeekType.ALL,
    )


def _ensure_event_data(event) -> str:
    data = getattr(event, "data", None) or ""
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8", errors="replace")
        except Exception:
            data = ""
    if data and (BOT_MARKER in data or "BEGIN:VCALENDAR" in data or "UID:" in data):
        return data
    try:
        event.load()
        data = event.data or ""
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
    except Exception:
        logger.debug("event.load() failed", exc_info=True)
    return data or ""


def _uid_matches_schedule_date(uid: str, schedule_date: date) -> bool:
    """schedbot_{digest}_{YYYY-MM-DD}_{n}_{week}[_extra] — возможно с @host."""
    date_prefix = schedule_date.isoformat()
    return bool(
        re.search(
            rf"{re.escape(UID_PREFIX)}[0-9a-f]+_{re.escape(date_prefix)}_\d+_(?:all|odd|even)(?:_extra)?(?:@|\s|$)",
            uid,
            re.IGNORECASE,
        )
        or re.search(
            rf"^{re.escape(UID_PREFIX)}[0-9a-f]+_{re.escape(date_prefix)}_\d+_(?:all|odd|even)(?:_extra)?$",
            uid,
            re.IGNORECASE,
        )
    )


def _is_bot_main_uid_for_day(uid: str, schedule_date: date) -> bool:
    if not uid or uid.endswith("_extra") or "_extra@" in uid or "_extra." in uid:
        return False
    date_prefix = schedule_date.isoformat()
    return bool(
        re.search(
            rf"{re.escape(UID_PREFIX)}[0-9a-f]+_{re.escape(date_prefix)}_\d+_(?:all|odd|even)(?:@|\s|$)",
            uid,
            re.IGNORECASE,
        )
        or re.match(
            rf"^{re.escape(UID_PREFIX)}[0-9a-f]+_{re.escape(date_prefix)}_\d+_(?:all|odd|even)$",
            uid,
            re.IGNORECASE,
        )
    )


def _event_is_on_schedule_date(data: str, uid: str, schedule_date: date) -> bool:
    return (
        _date_tag(schedule_date) in data
        or _uid_matches_schedule_date(uid, schedule_date)
        or _event_starts_on_date(data, schedule_date)
    )


def _list_raw_events(calendar, schedule_date: date | None = None):
    """Список событий; для даты — широкий date_search + fallback на events()."""
    try:
        all_events = list(calendar.events())
    except Exception:
        logger.exception("Cannot list calendar events")
        all_events = []

    if schedule_date is None:
        return all_events

    searched: list = []
    try:
        start_dt = datetime.combine(schedule_date - timedelta(days=1), time.min)
        end_dt = datetime.combine(schedule_date + timedelta(days=1), time(23, 59, 59))
        searched = list(calendar.date_search(start=start_dt, end=end_dt))
    except Exception:
        logger.warning("date_search failed, using events()")

    # объединяем по URL, чтобы не потерять события из-за TZ/пустого date_search
    by_url: dict[str, object] = {}
    for ev in searched + all_events:
        try:
            key = str(getattr(ev, "url", None) or id(ev))
            by_url[key] = ev
        except Exception:
            continue
    return list(by_url.values())


def _index_bot_events_by_uid(calendar, schedule_date: date | None = None) -> dict[str, object]:
    indexed: dict[str, object] = {}
    for event in _list_raw_events(calendar, schedule_date):
        try:
            data = _ensure_event_data(event)
            if BOT_MARKER not in data:
                continue
            uid = _uid_from_ical(data)
            if not uid:
                continue
            if schedule_date is not None and not _event_is_on_schedule_date(
                data, uid, schedule_date
            ):
                continue
            indexed[uid] = event
            # без суффикса @host — для поиска по нашему _make_uid
            bare = uid.split("@", 1)[0]
            if bare != uid:
                indexed[bare] = event
        except Exception:
            logger.exception("Failed to index calendar event")
    return indexed


@dataclass
class ExistingMainEvent:
    uid: str
    lesson_number: int
    lesson: Lesson
    summary: str
    description: str
    resource: object


def _load_existing_main_events(
    calendar,
    *,
    main_class: str,
    main_digest: str,
    schedule_date: date,
    bells: dict[int, BellPeriod] | None = None,
) -> dict[int, ExistingMainEvent]:
    """Индекс основных событий бота за день по номеру урока (UID приоритетнее тега)."""
    found: dict[int, ExistingMainEvent] = {}
    duplicates: list = []
    main_tag = _class_tag(main_class)
    date_prefix = schedule_date.isoformat()
    needle_uid = f"{UID_PREFIX}{main_digest}_{date_prefix}_"

    for event in _list_raw_events(calendar, schedule_date):
        try:
            data = _ensure_event_data(event)
            summary = _summary_from_ical(data)
            uid = _uid_from_ical(data) or ""
            bare_uid = uid.split("@", 1)[0]

            if not _looks_like_bot_schedule_event(data, summary, uid):
                continue
            if EXTRA_KIND_TAG in data or bare_uid.endswith("_extra") or "ДОП. КЛАСС" in summary:
                continue
            if not _event_is_on_schedule_date(data, uid, schedule_date):
                continue

            is_uid_main = bare_uid.startswith(needle_uid)
            is_bot_uid_same_day = _is_bot_main_uid_for_day(bare_uid, schedule_date)
            # sch-события того же дня тоже считаем основными (даже без маркера)
            is_sch = bool(re.match(r"(?i)^\s*sch\b", summary or ""))
            if not (is_uid_main or main_tag in data or is_bot_uid_same_day or is_sch):
                continue

            lesson_no = _resolve_lesson_number(
                data, bare_uid, summary, schedule_date, bells=bells
            )
            if lesson_no is None:
                continue

            lesson = _lesson_from_main_event(data, lesson_no, schedule_date)
            if not lesson:
                continue

            candidate = ExistingMainEvent(
                uid=bare_uid or f"sch-{lesson_no}",
                lesson_number=lesson_no,
                lesson=lesson,
                summary=summary,
                description=_description_body_from_ical(data) or data,
                resource=event,
            )
            prev = found.get(lesson_no)
            if prev is None:
                found[lesson_no] = candidate
                continue

            keep_new = False
            if is_uid_main and not prev.uid.startswith(needle_uid):
                keep_new = True
            elif BOT_MARKER in data and BOT_MARKER not in (prev.description or ""):
                keep_new = True

            if keep_new:
                duplicates.append(prev.resource)
                found[lesson_no] = candidate
            else:
                duplicates.append(event)
        except Exception:
            logger.exception("Failed to parse existing main event")

    removed = 0
    for dup in duplicates:
        if _safe_delete_event(dup):
            removed += 1

    logger.info(
        "Найдено основных событий за %s для class=%s: %s (дубликатов удалено: %s)",
        schedule_date.isoformat(),
        main_class,
        sorted(found),
        removed,
    )
    return found


def _load_existing_main_lessons(
    calendar,
    *,
    main_class: str,
    main_digest: str,
    schedule_date: date,
) -> dict[int, Lesson]:
    events = _load_existing_main_events(
        calendar,
        main_class=main_class,
        main_digest=main_digest,
        schedule_date=schedule_date,
    )
    return {n: ev.lesson for n, ev in events.items()}


def _strip_extras_section(description: str) -> str:
    if "Доп. классы:" not in description:
        return description.rstrip()
    before, after = description.split("Доп. классы:", 1)
    lines = after.splitlines()
    i = 0
    # пропустить пустые и строки списка доп. классов
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("•"):
            i += 1
            continue
        break
    trailing = "\n".join(lines[i:]).strip()
    base = before.rstrip()
    if trailing:
        return f"{base}\n\n{trailing}"
    return base


def _append_extras_to_description(description: str, extra_lessons: dict[str, Lesson]) -> str:
    base = _strip_extras_section(description)
    extras = format_merged_description_extras(extra_lessons)
    if not extras.strip():
        return base

    # Домашка должна оставаться в конце описания
    if HOMEWORK_HEADER in base:
        head, hw = base.split(HOMEWORK_HEADER, 1)
        head = head.rstrip()
        hw_block = f"{HOMEWORK_HEADER}{hw}".rstrip()
        return f"{head}{extras}\n\n{hw_block}"
    return f"{base.rstrip()}{extras}"


def _save_existing_resource(resource) -> None:
    """Сохраняет уже загруженный/изменённый объект календаря.

    Важно: ``resource.save(ical_bytes)`` нельзя — первый аргумент это
    ``no_overwrite``, а не тело события (иначе ConsistencyError).
    """
    resource.save()


def _update_event_description(resource, *, description: str) -> None:
    resource.load()
    cal = resource.icalendar_component
    vevent = cal.walk("VEVENT")[0]
    vevent.pop("description", None)
    vevent.add("description", description)
    _save_existing_resource(resource)


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
        _save_existing_resource(resource)
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
    force_replace: bool = False,
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
    additive_extra = bool(extra_schedules) and not force_replace

    try:
        with DAVClient(url=root, username=creds.username, password=creds.password) as client:
            calendar = _resolve_calendar(client, creds.url)

            if additive_extra:
                existing_main_events = _load_existing_main_events(
                    calendar,
                    main_class=schedule.class_name,
                    main_digest=main_digest,
                    schedule_date=schedule_date,
                    bells=bells,
                )
                existing_main = {n: ev.lesson for n, ev in existing_main_events.items()}
                schedule = merge_main_schedule_with_existing(schedule, existing_main)
                deleted = _delete_extra_owned_events_for_day(
                    calendar,
                    owner_tag=owner_tag,
                    schedule_date=schedule_date,
                    extra_class_names=list(extra_schedules),
                )
                existing_by_uid = {
                    ev.uid: ev.resource for ev in existing_main_events.values()
                }
                # дополним индексом остальных bot-событий (на случай других UID)
                existing_by_uid.update(_index_bot_events_by_uid(calendar, schedule_date))
            else:
                existing_main_events = {}
                deleted = _delete_bot_events_for_day(
                    calendar,
                    main_class=schedule.class_name,
                    main_digest=main_digest,
                    owner_tag=owner_tag,
                    schedule_date=schedule_date,
                    bells=bells,
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

                # Если основной урок уже есть в календаре — доп. только в его описание
                existing_main_ev = existing_main_events.get(plan.lesson_number)
                if plan.extra_lessons and existing_main_ev is not None:
                    new_description = _append_extras_to_description(
                        existing_main_ev.description,
                        plan.extra_lessons,
                    )
                    try:
                        _update_event_description(
                            existing_main_ev.resource,
                            description=new_description,
                        )
                        created += 1
                        logger.info(
                            "Доп. классы вписаны в существующий урок %s (%s)",
                            plan.lesson_number,
                            existing_main_ev.uid,
                        )
                    except Exception:
                        logger.exception(
                            "Не удалось обновить описание урока %s",
                            plan.lesson_number,
                        )
                    continue

                if plan.is_extra_only:
                    extra_name = plan.extra_class_name or next(iter(plan.extra_lessons))
                    lesson = plan.extra_lessons[extra_name]
                    lesson_label = resolve_paired_lesson_label(lesson, aliases)
                    summary = f"🔸 ДОП. КЛАСС | {extra_name}: {lesson_label}"
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
                        f"Предмет: {lesson_label}\n"
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
                    # не перезаписываем уже существующий основной урок без новых доп.
                    if existing_main_ev is not None and not plan.extra_lessons:
                        continue
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


def _delete_schedule_looking_events_for_day(
    calendar,
    *,
    schedule_date: date,
    bells: dict[int, BellPeriod] | None = None,
) -> int:
    """Жёстко удаляет все sch / ДОП / bot-события за день (для полной перезаписи)."""
    deleted = 0
    for event in _list_raw_events(calendar, schedule_date):
        try:
            data = _ensure_event_data(event)
            summary = _summary_from_ical(data)
            uid = _uid_from_ical(data) or ""
            if not _event_is_on_schedule_date(data, uid, schedule_date):
                continue
            if not _looks_like_bot_schedule_event(data, summary, uid):
                continue
            if _safe_delete_event(event):
                deleted += 1
        except Exception:
            logger.exception("Failed to delete schedule-looking event")
    logger.info(
        "Удалено schedule-событий за %s: %s", schedule_date.isoformat(), deleted
    )
    return deleted


def _delete_extra_owned_events_for_day(
    calendar,
    *,
    owner_tag: str,
    schedule_date: date,
    extra_class_names: list[str] | None = None,
) -> int:
    """Удаляет private-события доп. классов за день (в т.ч. ошибочные дубликаты)."""
    deleted = 0
    for event in _list_raw_events(calendar, schedule_date):
        try:
            data = _ensure_event_data(event)
            summary = _summary_from_ical(data)
            uid = _uid_from_ical(data) or ""
            bare = uid.split("@", 1)[0]
            if not _event_is_on_schedule_date(data, uid, schedule_date):
                continue

            is_extra = (
                EXTRA_KIND_TAG in data
                or bare.endswith("_extra")
                or "ДОП. КЛАСС" in summary
            )
            if not is_extra:
                continue

            if _safe_delete_event(event):
                deleted += 1
        except Exception:
            logger.exception("Failed to delete extra-owned calendar event")
    logger.info("Удалено ДОП. КЛАСС за %s: %s", schedule_date.isoformat(), deleted)
    return deleted


def _delete_bot_events_for_day(
    calendar,
    *,
    main_class: str,
    main_digest: str,
    owner_tag: str,
    schedule_date: date,
    bells: dict[int, BellPeriod] | None = None,
) -> int:
    """Удаляет события бота / sch / ДОП за день перед полной записью."""
    return _delete_schedule_looking_events_for_day(
        calendar, schedule_date=schedule_date, bells=bells
    )


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


def _description_from_ical(data: str) -> str:
    return _description_body_from_ical(data)


def _parse_bot_event(data: str, resource=None) -> BotCalendarEvent | None:
    summary = _summary_from_ical(data)
    uid = _uid_from_ical(data) or ""
    if not _looks_like_bot_schedule_event(data, summary, uid):
        return None
    if not uid and not summary:
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

    bare = uid.split("@", 1)[0] if uid else ""
    lesson_no = _resolve_lesson_number(data, bare, summary, event_date)
    if lesson_no is None:
        return None

    subject_match = re.search(r"^Предмет: (.+)$", data, re.MULTILINE)
    subject = subject_match.group(1).strip() if subject_match else (summary or f"Урок {lesson_no}")
    room_match = re.search(r"^Кабинет: (.+)$", data, re.MULTILINE)
    description = _description_from_ical(data) or data

    return BotCalendarEvent(
        uid=bare or uid or f"sch-{event_date.isoformat()}-{lesson_no}",
        event_date=event_date,
        lesson_number=lesson_no,
        subject=subject,
        room=room_match.group(1).strip() if room_match else None,
        summary=summary,
        description=description,
        homework=extract_homework(description),
        is_extra_only=EXTRA_KIND_TAG in data or "ДОП. КЛАСС" in summary,
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
                data = _ensure_event_data(event)
                summary = _summary_from_ical(data)
                uid = _uid_from_ical(data) or ""
                if not _looks_like_bot_schedule_event(data, summary, uid):
                    continue
                is_extra = EXTRA_KIND_TAG in data or "ДОП. КЛАСС" in summary
                if is_extra and not include_extra:
                    continue
                if (
                    BOT_MARKER in data
                    and main_tag not in data
                    and f"{UID_PREFIX}{main_digest}_" not in data
                    and not is_extra
                    and not re.match(r"(?i)^\s*sch\b", summary)
                ):
                    if not (
                        include_extra
                        and EXTRA_KIND_TAG in data
                        and _owner_tag(class_name) in data
                    ):
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
class CleanupResult:
    ok: bool
    deleted: int = 0
    kept: int = 0
    error: str | None = None


def cleanup_day_duplicates(
    creds: CalDavCredentials,
    *,
    user_id: int,
    class_name: str,
    schedule_date: date,
    bells: dict[int, BellPeriod] | None = None,
) -> CleanupResult:
    """Удаляет дубли sch/bot и ДОП. КЛАСС за день, оставляя по одному основному уроку."""
    if not creds.is_complete:
        return CleanupResult(ok=False, error="Не заданы CalDAV URL / логин / пароль")

    main_digest = class_digest(user_id, class_name)
    root = _dav_root(creds.url)
    try:
        with DAVClient(url=root, username=creds.username, password=creds.password) as client:
            calendar = _resolve_calendar(client, creds.url)
            extras_deleted = _delete_extra_owned_events_for_day(
                calendar,
                owner_tag=_owner_tag(class_name),
                schedule_date=schedule_date,
            )
            before = _load_existing_main_events(
                calendar,
                main_class=class_name,
                main_digest=main_digest,
                schedule_date=schedule_date,
                bells=bells,
            )
            # повторный проход: всё лишнее sch без номера урока / без пары
            stray = 0
            kept_urls = {
                str(getattr(ev.resource, "url", "") or "") for ev in before.values()
            }
            for event in _list_raw_events(calendar, schedule_date):
                data = _ensure_event_data(event)
                summary = _summary_from_ical(data)
                uid = _uid_from_ical(data) or ""
                if not _event_is_on_schedule_date(data, uid, schedule_date):
                    continue
                if not _looks_like_bot_schedule_event(data, summary, uid):
                    continue
                url = str(getattr(event, "url", "") or "")
                if url and url in kept_urls:
                    continue
                if "ДОП. КЛАСС" in summary or EXTRA_KIND_TAG in data:
                    if _safe_delete_event(event):
                        stray += 1
                    continue
                lesson_no = _resolve_lesson_number(
                    data, uid, summary, schedule_date, bells=bells
                )
                if lesson_no is None or lesson_no in before:
                    if _safe_delete_event(event):
                        stray += 1
            return CleanupResult(
                ok=True,
                deleted=extras_deleted + stray,
                kept=len(before),
            )
    except Exception as exc:
        logger.exception("Day cleanup failed")
        return CleanupResult(ok=False, error=str(exc))


def write_homework_for_day(
    creds: CalDavCredentials,
    *,
    user_id: int,
    class_name: str,
    schedule_date: date,
    homework_by_lesson: dict[int, str],
) -> HomeworkWriteResult:
    """Пишет ДЗ в события дня по номеру урока (после перезаписи расписания)."""
    if not homework_by_lesson:
        return HomeworkWriteResult(ok=True, updated=0)
    if not creds.is_complete:
        return HomeworkWriteResult(ok=False, error="Не заданы CalDAV URL / логин / пароль")

    try:
        events = list_bot_events(
            creds,
            user_id=user_id,
            class_name=class_name,
            start_date=schedule_date,
            end_date=schedule_date,
            include_extra=False,
        )
        by_lesson = {e.lesson_number: e for e in events if not e.is_extra_only}
        assignments: list[tuple[BotCalendarEvent, str]] = []
        for lesson_no, text in homework_by_lesson.items():
            ev = by_lesson.get(lesson_no)
            if ev is None:
                logger.warning(
                    "No event for homework lesson %s on %s",
                    lesson_no,
                    schedule_date.isoformat(),
                )
                continue
            assignments.append((ev, text))
        return write_homework_to_events(creds, assignments)
    except Exception as exc:
        logger.exception("write_homework_for_day failed")
        return HomeworkWriteResult(ok=False, error=str(exc))


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
                    _save_existing_resource(resource)
                    updated += 1
                except Exception:
                    logger.exception("Failed to update homework on %s", event_view.uid)
            if updated == 0:
                return HomeworkWriteResult(
                    ok=False,
                    updated=0,
                    error="События найдены, но CalDAV не принял обновление описания",
                )
            return HomeworkWriteResult(ok=True, updated=updated)
    except Exception as exc:
        logger.exception("Homework write failed")
        return HomeworkWriteResult(ok=False, error=str(exc))
