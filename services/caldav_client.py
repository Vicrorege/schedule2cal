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

from db.database import BellPeriod, CalDavCredentials
from models.schedule import Schedule
from services.title_template import apply_title_template, resolve_lesson_name

logger = logging.getLogger(__name__)

BOT_MARKER = "schedule_bot_gen"
UID_PREFIX = "schedbot_"
DEFAULT_TZ = "Europe/Moscow"


@dataclass
class SyncResult:
    ok: bool
    created: int = 0
    deleted: int = 0
    error: str | None = None
    calendar_url: str | None = None


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
) -> str:
    digest = class_digest(user_id, class_name)
    return f"{UID_PREFIX}{digest}_{schedule_date.isoformat()}_{lesson_number}_{week_type}"


def _date_tag(schedule_date: date) -> str:
    return f"date:{schedule_date.isoformat()}"


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


def _class_tag(class_name: str) -> str:
    return f"class:{class_name}"


def _dav_root(url: str) -> str:
    """https://host/SOGo/dav/user/Calendar/xxx/ → https://host/SOGo/dav/"""
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
    """Находит рабочий календарь: сначала URL юзера, иначе список у principal."""
    preferred_id = (_calendar_id_from_url(preferred_url) or "").casefold()

    try:
        calendar = client.calendar(url=preferred_url)
        # Проверка, что коллекция существует
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

    # Первый доступный
    first = calendars[0]
    logger.info(
        "Using first available calendar %s. Available: %s",
        first.url,
        "; ".join(available),
    )
    return first


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
    tz_name: str = DEFAULT_TZ,
) -> SyncResult:
    if not creds.is_complete:
        return SyncResult(ok=False, error="Не заданы CalDAV URL / логин / пароль")

    if not schedule.date:
        return SyncResult(ok=False, error="В расписании нет даты")

    schedule_date = date.fromisoformat(schedule.date)
    tz = ZoneInfo(tz_name)
    tag = _class_tag(schedule.class_name)
    date_tag = _date_tag(schedule_date)
    digest = class_digest(user_id, schedule.class_name)
    root = _dav_root(creds.url)

    try:
        with DAVClient(url=root, username=creds.username, password=creds.password) as client:
            calendar = _resolve_calendar(client, creds.url)
            deleted = _delete_bot_events(
                calendar,
                class_tag=tag,
                digest=digest,
                schedule_date=schedule_date,
            )
            created = 0

            for lesson in sorted(schedule.schedule, key=lambda x: x.lesson_number):
                if lesson.lesson_number not in bells:
                    continue
                bell = bells[lesson.lesson_number]
                dtstart = datetime.combine(schedule_date, _parse_hhmm(bell.start), tzinfo=tz)
                dtend = datetime.combine(schedule_date, _parse_hhmm(bell.end), tzinfo=tz)

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
                    f"{tag}\n"
                    f"{date_tag}\n"
                    f"Предмет: {lesson.subject}\n"
                    f"Урок: {lesson.lesson_number}"
                )
                if lesson.room:
                    description += f"\nКабинет: {lesson.room}"

                calendar.save_event(
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


def _delete_bot_events(
    calendar,
    *,
    class_tag: str,
    digest: str,
    schedule_date: date,
) -> int:
    """Удаляет только события бота за конкретную дату (повторная загрузка того же дня)."""
    deleted = 0
    try:
        events = calendar.events()
    except Exception:
        logger.exception("Cannot list calendar events")
        return 0

    date_prefix = schedule_date.isoformat()
    needle_uid = f"{UID_PREFIX}{digest}_{date_prefix}_"
    date_tag = _date_tag(schedule_date)
    for event in events:
        try:
            data = event.data or ""
            is_bot_event = BOT_MARKER in data and class_tag in data
            if not is_bot_event and needle_uid not in data:
                continue
            same_date = (
                needle_uid in data
                or date_tag in data
                or _event_starts_on_date(data, schedule_date)
            )
            if is_bot_event and same_date:
                event.delete()
                deleted += 1
        except Exception:
            logger.exception("Failed to delete calendar event")
    return deleted
