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
    status: str  # ok | not_found | ambiguous | skipped
    message: str


def _normalize_homework_text(text: str) -> str:
    return " ".join((text or "").casefold().split())


def is_same_homework(existing: str | None, new_text: str) -> bool:
    """True, если на уроке уже записано то же задание."""
    if not existing or not new_text.strip():
        return False
    return _normalize_homework_text(existing) == _normalize_homework_text(new_text)


def _make_ok_or_skipped(
    block: HomeworkBlock,
    event: BotCalendarEvent,
    *,
    local_homework: dict[tuple[str, int], str] | None = None,
) -> HomeworkAssignment:
    local_text = None
    if local_homework:
        local_text = local_homework.get((event.event_date.isoformat(), event.lesson_number))

    if is_same_homework(event.homework, block.text) or is_same_homework(
        local_text, block.text
    ):
        return HomeworkAssignment(
            block=block,
            event=event,
            status="skipped",
            message=(
                f"пропущено — такое же ДЗ уже на "
                f"{event.event_date.strftime('%d.%m.%Y')} · урок {event.lesson_number}"
            ),
        )

    return HomeworkAssignment(
        block=block,
        event=event,
        status="ok",
        message=(
            f"{event.subject} · {event.event_date.strftime('%d.%m.%Y')} "
            f"· урок {event.lesson_number}"
        ),
    )


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def _names_related(a: str, b: str, *, min_ratio: float = 0.86) -> bool:
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= min_ratio


def _alias_name_forms(text: str, aliases: dict[str, str] | None) -> set[str]:
    """Исходное имя + кастомный алиас для текста (и наоборот)."""
    n = _normalize(text)
    forms: set[str] = {n} if n else set()
    if not aliases:
        return forms

    for source, alias in aliases.items():
        ns = _normalize(source)
        na = _normalize(alias)
        hit = _names_related(n, ns) or _names_related(n, na)
        # кастомное имя внутри SUMMARY вроде «sch 6. math -»
        if not hit and na and len(na) >= 2 and na in n:
            hit = True
        if not hit and ns and len(ns) >= 3 and ns in n:
            hit = True
        if hit:
            if ns:
                forms.add(ns)
            if na:
                forms.add(na)
    return forms


def subject_match_score(
    query: str,
    candidate: str,
    *,
    aliases: dict[str, str] | None = None,
) -> float:
    """0..1 насколько query похож на subject/алиас/summary из календаря."""
    q = _normalize(query)
    c = _normalize(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if q in c or c in q:
        return 0.92

    q_forms = _alias_name_forms(query, aliases)
    c_forms = _alias_name_forms(candidate, aliases)
    if q_forms & c_forms:
        return 0.98

    # кастомное имя из словаря встречается в candidate (summary)
    for qf in q_forms:
        if qf and len(qf) >= 2 and qf in c:
            return 0.95

    best = difflib.SequenceMatcher(None, q, c).ratio()
    for qf in q_forms:
        for cf in c_forms:
            if not qf or not cf:
                continue
            best = max(best, difflib.SequenceMatcher(None, qf, cf).ratio())
            if qf in cf or cf in qf:
                best = max(best, 0.92)
    return best


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


def _event_match_score(
    subject: str,
    event: BotCalendarEvent,
    *,
    aliases: dict[str, str] | None = None,
) -> float:
    """Сравнение ДЗ с исходным предметом, кастомным именем и SUMMARY."""
    from services.title_template import resolve_lesson_name

    aliases = aliases or {}
    custom = resolve_lesson_name(event.subject, aliases)
    scores = [
        subject_match_score(subject, event.subject, aliases=aliases),
        subject_match_score(subject, custom, aliases=aliases),
    ]
    if event.summary:
        scores.append(subject_match_score(subject, event.summary, aliases=aliases))
    return max(scores)


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
    Учитывает кастомные имена из словаря алиасов.
    """
    scored: list[tuple[float, BotCalendarEvent]] = []
    for event in events:
        if event.is_extra_only:
            continue
        if on_date is not None and event.event_date != on_date:
            continue
        if from_date is not None and event.event_date < from_date:
            continue
        score = _event_match_score(subject, event, aliases=aliases)
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
    local_homework: dict[tuple[str, int], str] | None = None,
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
                    _make_ok_or_skipped(block, event, local_homework=local_homework)
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
                    _make_ok_or_skipped(block, event, local_homework=local_homework)
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
