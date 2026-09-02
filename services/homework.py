from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass
from datetime import date, timedelta

from models.homework import HomeworkBlock
from models.schedule import DayOfWeek
from services.caldav_client import BotCalendarEvent
from services.schedule_postprocess import WEEKDAY_MAP, day_of_week_from_date

logger = logging.getLogger(__name__)

SEARCH_HORIZON_DAYS = 21


@dataclass
class HomeworkAssignment:
    block: HomeworkBlock
    event: BotCalendarEvent | None
    status: str  # ok | not_found | ambiguous
    message: str


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def subject_match_score(
    query: str,
    candidate: str,
    *,
    aliases: dict[str, str] | None = None,
) -> float:
    """0..1 насколько query похож на subject из календаря."""
    q = _normalize(query)
    c = _normalize(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if q in c or c in q:
        return 0.92

    aliases = aliases or {}
    # query может быть алиасом → исходное имя
    for source, alias in aliases.items():
        if _normalize(alias) == q and _normalize(source) == c:
            return 0.98
        if _normalize(source) == q and _normalize(alias) == c:
            return 0.98
        if _normalize(alias) == q and (
            _normalize(source) in c or c in _normalize(source)
        ):
            return 0.9

    return difflib.SequenceMatcher(None, q, c).ratio()


def next_weekday_on_or_after(start: date, weekday: DayOfWeek) -> date:
    target = {v: k for k, v in WEEKDAY_MAP.items()}[weekday]
    delta = (target - start.weekday()) % 7
    return start + timedelta(days=delta)


def resolve_block_anchor_date(block: HomeworkBlock, *, today: date) -> date | None:
    """Якорь поиска: точная дата, ближайший день недели, или None (= любой ближайший урок)."""
    if block.target_date:
        try:
            return date.fromisoformat(block.target_date)
        except ValueError:
            logger.warning("Bad target_date from LLM: %s", block.target_date)
    if block.day_of_week:
        return next_weekday_on_or_after(today, block.day_of_week)
    return None


def find_best_event_for_subject(
    events: list[BotCalendarEvent],
    subject: str,
    *,
    aliases: dict[str, str] | None = None,
    on_date: date | None = None,
    from_date: date | None = None,
    min_score: float = 0.72,
) -> tuple[BotCalendarEvent | None, str]:
    """
    Ищет урок предмета.
    - on_date: только этот день
    - иначе: ближайший урок с from_date (по умолчанию сегодня)
    """
    scored: list[tuple[float, BotCalendarEvent]] = []
    for event in events:
        if event.is_extra_only:
            continue
        if on_date is not None and event.event_date != on_date:
            continue
        if from_date is not None and event.event_date < from_date:
            continue
        score = subject_match_score(subject, event.subject, aliases=aliases)
        if score >= min_score:
            scored.append((score, event))

    if not scored:
        return None, "not_found"

    scored.sort(key=lambda x: (-x[0], x[1].event_date, x[1].lesson_number))
    best_score, best = scored[0]

    # несколько уроков одного предмета в один день — берём первый по номеру
    same_day = [
        e
        for s, e in scored
        if e.event_date == best.event_date and abs(s - best_score) < 0.05
    ]
    same_day.sort(key=lambda e: e.lesson_number)
    return same_day[0], "ok"


def assign_homework_blocks(
    blocks: list[HomeworkBlock],
    events: list[BotCalendarEvent],
    *,
    today: date,
    aliases: dict[str, str] | None = None,
) -> list[HomeworkAssignment]:
    results: list[HomeworkAssignment] = []
    for block in blocks:
        if not block.subject.strip() or not block.text.strip():
            continue

        anchor = resolve_block_anchor_date(block, today=today)
        if block.target_date or block.day_of_week:
            # точная дата / день недели
            event, status = find_best_event_for_subject(
                events,
                block.subject,
                aliases=aliases,
                on_date=anchor,
            )
            if event is None and block.day_of_week and anchor:
                # если в этот день предмета нет — попробуем ближайший такой день недели дальше
                for week in range(1, 4):
                    candidate = anchor + timedelta(days=7 * week)
                    event, status = find_best_event_for_subject(
                        events,
                        block.subject,
                        aliases=aliases,
                        on_date=candidate,
                    )
                    if event:
                        break
            if event is None:
                msg = (
                    f"Не найден урок «{block.subject}»"
                    + (f" на {anchor.strftime('%d.%m.%Y')}" if anchor else "")
                )
                results.append(
                    HomeworkAssignment(block=block, event=None, status="not_found", message=msg)
                )
            else:
                results.append(
                    HomeworkAssignment(
                        block=block,
                        event=event,
                        status="ok",
                        message=(
                            f"{event.subject} · {event.event_date.strftime('%d.%m.%Y')} "
                            f"· урок {event.lesson_number}"
                        ),
                    )
                )
        else:
            event, status = find_best_event_for_subject(
                events,
                block.subject,
                aliases=aliases,
                from_date=today,
            )
            if event is None:
                results.append(
                    HomeworkAssignment(
                        block=block,
                        event=None,
                        status="not_found",
                        message=f"Не найден ближайший урок «{block.subject}»",
                    )
                )
            else:
                results.append(
                    HomeworkAssignment(
                        block=block,
                        event=event,
                        status="ok",
                        message=(
                            f"{event.subject} · {event.event_date.strftime('%d.%m.%Y')} "
                            f"· урок {event.lesson_number}"
                        ),
                    )
                )
    return results


def search_range(*, today: date, blocks: list[HomeworkBlock]) -> tuple[date, date]:
    """Диапазон дат для загрузки событий из CalDAV."""
    end = today + timedelta(days=SEARCH_HORIZON_DAYS)
    for block in blocks:
        anchor = resolve_block_anchor_date(block, today=today)
        if anchor:
            end = max(end, anchor + timedelta(days=21))
    return today, end
