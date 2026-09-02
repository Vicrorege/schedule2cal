from __future__ import annotations

import asyncio
import calendar
import logging
from datetime import date
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards.view_calendar import (
    shift_month,
    view_calendar_keyboard,
    view_day_keyboard,
)
from db.database import Database
from services.caldav_client import list_bot_events
from services.schedule_postprocess import WEEKDAY_RU, day_of_week_from_date

logger = logging.getLogger(__name__)
router = Router()


def _esc(text: str) -> str:
    return escape(text, quote=False)


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


async def _days_with_events(db: Database, user_id: int, year: int, month: int) -> set[date]:
    saved = await db.get_user_settings(user_id)
    creds = await db.get_caldav_credentials(user_id)
    if not saved or not saved.class_name or not creds or not creds.is_complete:
        return set()
    start, end = _month_bounds(year, month)
    try:
        events = await asyncio.to_thread(
            list_bot_events,
            creds,
            user_id=user_id,
            class_name=saved.class_name,
            start_date=start,
            end_date=end,
            include_extra=True,
        )
    except Exception:
        logger.exception("Failed to load month events")
        return set()
    return {e.event_date for e in events}


async def _show_month(
    target: Message,
    *,
    db: Database,
    user_id: int,
    year: int,
    month: int,
    selected: date | None = None,
    edit: bool = True,
):
    saved = await db.get_user_settings(user_id)
    class_line = (
        f"Класс: <b>{_esc(saved.class_name)}</b>\n"
        if saved and saved.class_name
        else "Класс не сохранён — сначала загрузи расписание.\n"
    )
    days = await _days_with_events(db, user_id, year, month)
    text = (
        f"📅 <b>Календарь</b>\n{class_line}\n"
        "·день· — есть уроки бота. Нажми день, чтобы открыть."
    )
    markup = view_calendar_keyboard(
        year, month, selected=selected, days_with_events=days
    )
    if edit:
        await target.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=markup)


async def _show_day(
    message: Message,
    *,
    db: Database,
    user_id: int,
    day: date,
):
    saved = await db.get_user_settings(user_id)
    creds = await db.get_caldav_credentials(user_id)
    if not saved or not saved.class_name:
        await message.edit_text(
            "Сначала загрузи расписание и сохрани класс.",
            reply_markup=view_day_keyboard(day),
        )
        return
    if not creds or not creds.is_complete:
        await message.edit_text(
            "Нужен CalDAV: /settings → 📅 CalDAV / SOGo.",
            reply_markup=view_day_keyboard(day),
        )
        return

    bells = await db.get_bells(user_id)
    dow = WEEKDAY_RU[day_of_week_from_date(day)]
    try:
        events = await asyncio.to_thread(
            list_bot_events,
            creds,
            user_id=user_id,
            class_name=saved.class_name,
            start_date=day,
            end_date=day,
            include_extra=True,
        )
    except Exception as exc:
        logger.exception("Failed to load day events")
        await message.edit_text(
            f"❌ Ошибка чтения календаря: <code>{_esc(str(exc))}</code>",
            parse_mode="HTML",
            reply_markup=view_day_keyboard(day),
        )
        return

    lines = [
        f"📅 <b>{day.strftime('%d.%m.%Y')}</b> ({dow})",
        f"Класс: <b>{_esc(saved.class_name)}</b>",
        "",
    ]
    if not events:
        lines.append("На этот день уроков бота нет.")
    else:
        for ev in events:
            time_part = ""
            if ev.lesson_number in bells:
                b = bells[ev.lesson_number]
                time_part = f" <i>{b.start}–{b.end}</i>"
            mark = "🔸 " if ev.is_extra_only else ""
            lines.append(
                f"<b>{ev.lesson_number}.</b>{time_part}  {mark}<b>{_esc(ev.subject)}</b>"
            )
            if ev.room:
                lines.append(f"   🚪 {_esc(ev.room)}")
            if ev.homework:
                lines.append("   📝 <b>ДЗ:</b>")
                for hw_line in ev.homework.splitlines():
                    lines.append(f"   {_esc(hw_line)}")
            lines.append("")

    await message.edit_text(
        "\n".join(lines).rstrip(),
        parse_mode="HTML",
        reply_markup=view_day_keyboard(day),
    )


@router.message(Command("calendar"))
@router.message(F.text == "📅 Календарь")
async def cmd_calendar(message: Message, state: FSMContext, db: Database):
    await state.clear()
    today = date.today()
    await _show_month(
        message,
        db=db,
        user_id=message.from_user.id,
        year=today.year,
        month=today.month,
        selected=today,
        edit=False,
    )


@router.callback_query(F.data == "view:noop")
async def view_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "view:close")
async def view_close(callback: CallbackQuery):
    await callback.message.edit_text("Календарь закрыт.")
    await callback.answer()


@router.callback_query(F.data == "view:today")
async def view_today(callback: CallbackQuery, db: Database):
    today = date.today()
    await callback.answer()
    await _show_day(
        callback.message,
        db=db,
        user_id=callback.from_user.id,
        day=today,
    )


@router.callback_query(F.data.startswith("view:nav:"))
async def view_nav(callback: CallbackQuery, db: Database):
    _, _, year_s, month_s, delta_s = callback.data.split(":")
    year, month = shift_month(int(year_s), int(month_s), int(delta_s))
    await callback.answer()
    await _show_month(
        callback.message,
        db=db,
        user_id=callback.from_user.id,
        year=year,
        month=month,
    )


@router.callback_query(F.data.startswith("view:month:"))
async def view_month(callback: CallbackQuery, db: Database):
    _, _, year_s, month_s = callback.data.split(":")
    await callback.answer()
    await _show_month(
        callback.message,
        db=db,
        user_id=callback.from_user.id,
        year=int(year_s),
        month=int(month_s),
    )


@router.callback_query(F.data.startswith("view:day:"))
async def view_day(callback: CallbackQuery, db: Database):
    _, _, year_s, month_s, day_s = callback.data.split(":")
    day = date(int(year_s), int(month_s), int(day_s))
    await callback.answer(day.strftime("%d.%m.%Y"))
    await _show_day(
        callback.message,
        db=db,
        user_id=callback.from_user.id,
        day=day,
    )
