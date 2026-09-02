from __future__ import annotations

import asyncio
import calendar
import json
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
from models.schedule import Schedule
from services.caldav_client import (
    cleanup_day_duplicates,
    list_bot_events,
    sync_schedule_to_caldav,
    write_homework_for_day,
)
from services.schedule_postprocess import WEEKDAY_RU, day_of_week_from_date
from services.title_template import resolve_lesson_name

logger = logging.getLogger(__name__)
router = Router()


def _esc(text: str) -> str:
    return escape(text, quote=False)


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


async def _days_with_events(db: Database, user_id: int, year: int, month: int) -> set[date]:
    start, end = _month_bounds(year, month)
    days: set[date] = set()

    try:
        cached = await db.list_cached_schedule_dates(
            user_id, start.isoformat(), end.isoformat()
        )
        for iso in cached:
            try:
                days.add(date.fromisoformat(iso))
            except ValueError:
                continue
    except Exception:
        logger.exception("Failed to list cached schedule dates")

    saved = await db.get_user_settings(user_id)
    creds = await db.get_caldav_credentials(user_id)
    if saved and saved.class_name and creds and creds.is_complete:
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
            days.update(e.event_date for e in events)
        except Exception:
            logger.exception("Failed to load month events from SOGo")

    return days


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
        "·день· — есть уроки (локально и/или в SOGo).\n"
        "Расписание и ДЗ хранятся <b>в боте</b>; SOGo — копия.\n"
        "«Записать в SOGo» восстанавливает день без LLM."
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
    can_write = bool(creds and creds.is_complete)
    day_kb = view_day_keyboard(day, can_write=can_write)
    day_iso = day.isoformat()

    if not saved or not saved.class_name:
        await message.edit_text(
            "Сначала загрузи расписание и сохрани класс.",
            reply_markup=day_kb,
        )
        return

    bells = await db.get_bells(user_id)
    aliases = await db.get_lesson_aliases(user_id)
    dow = WEEKDAY_RU[day_of_week_from_date(day)]
    cached = await db.get_day_schedule(user_id, day_iso)
    local_hw = await db.get_day_homework(user_id, day_iso)

    sogo_events = []
    sogo_error = None
    if creds and creds.is_complete:
        try:
            sogo_events = await asyncio.to_thread(
                list_bot_events,
                creds,
                user_id=user_id,
                class_name=saved.class_name,
                start_date=day,
                end_date=day,
                include_extra=True,
            )
        except Exception as exc:
            logger.exception("Failed to load day events from SOGo")
            sogo_error = str(exc)

    lines = [
        f"📅 <b>{day.strftime('%d.%m.%Y')}</b> ({dow})",
        f"Класс: <b>{_esc(saved.class_name)}</b>",
        "",
    ]

    shown = False
    if cached:
        try:
            schedule = Schedule.model_validate(json.loads(cached["schedule_json"]))
            lines.append("💾 <b>Локально в боте</b>")
            for lesson in sorted(schedule.schedule, key=lambda x: x.lesson_number):
                time_part = ""
                if lesson.lesson_number in bells:
                    b = bells[lesson.lesson_number]
                    time_part = f" <i>{b.start}–{b.end}</i>"
                name = resolve_lesson_name(lesson.subject, aliases)
                lines.append(
                    f"<b>{lesson.lesson_number}.</b>{time_part}  "
                    f"<b>{_esc(name)}</b>"
                )
                if lesson.room:
                    lines.append(f"   🚪 {_esc(lesson.room)}")
                hw = local_hw.get(lesson.lesson_number)
                if hw and hw.get("homework_text"):
                    lines.append("   📝 <b>ДЗ:</b>")
                    for hw_line in hw["homework_text"].splitlines():
                        lines.append(f"   {_esc(hw_line)}")
                lines.append("")
            shown = True
        except Exception:
            logger.exception("Failed to render cached schedule")

    if local_hw and not cached:
        lines.append("💾 <b>Локальное ДЗ</b> (расписание дня ещё не кэшировано)")
        for lesson_no, hw in sorted(local_hw.items()):
            subj = hw.get("subject") or f"Урок {lesson_no}"
            lines.append(f"<b>{lesson_no}.</b>  <b>{_esc(subj)}</b>")
            lines.append("   📝 <b>ДЗ:</b>")
            for hw_line in (hw.get("homework_text") or "").splitlines():
                lines.append(f"   {_esc(hw_line)}")
            lines.append("")
        shown = True

    if not shown and sogo_events:
        lines.append("☁️ <b>Только в SOGo</b> (локального кэша нет)")
        for ev in sogo_events:
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
        shown = True

    if not shown:
        lines.append("На этот день расписания нет ни локально, ни в SOGo.")
        if sogo_error:
            lines.append(f"SOGo: <code>{_esc(sogo_error)}</code>")

    sogo_count = len([e for e in sogo_events if not e.is_extra_only])
    if cached or local_hw:
        if sogo_count == 0 and not sogo_error:
            lines.append("⚠️ В SOGo пусто — нажми «Записать в SOGo», чтобы восстановить.")
        elif sogo_count:
            lines.append(f"☁️ В SOGo сейчас уроков: {sogo_count}")

    if can_write and (cached or local_hw):
        lines.append("📤 Запишет локальную копию в SOGo без LLM.")
    elif can_write:
        lines.append(
            "📤 Почистит дубли в SOGo. Полное восстановление — после записи дня из PDF."
        )
    elif not creds or not creds.is_complete:
        lines.append("Для записи в SOGo настрой CalDAV в /settings.")

    await message.edit_text(
        "\n".join(lines).rstrip(),
        parse_mode="HTML",
        reply_markup=day_kb,
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


@router.callback_query(F.data.startswith("view:write:"))
async def view_write_sogo(callback: CallbackQuery, db: Database):
    _, _, year_s, month_s, day_s = callback.data.split(":")
    day = date(int(year_s), int(month_s), int(day_s))
    day_iso = day.isoformat()

    creds = await db.get_caldav_credentials(callback.from_user.id)
    if not creds or not creds.is_complete:
        await callback.answer("CalDAV не настроен", show_alert=True)
        return

    saved = await db.get_user_settings(callback.from_user.id)
    if not saved or not saved.class_name:
        await callback.answer("Сначала сохрани класс", show_alert=True)
        return

    cached = await db.get_day_schedule(callback.from_user.id, day_iso)
    local_hw = await db.get_day_homework(callback.from_user.id, day_iso)
    bells = await db.get_bells(callback.from_user.id)
    await callback.answer()

    if cached:
        await callback.message.edit_text(
            f"⏳ Восстанавливаю {day.strftime('%d.%m.%Y')} в SOGo из локальной копии…"
        )
        try:
            schedule = Schedule.model_validate(json.loads(cached["schedule_json"]))
            schedule.date = day_iso
            extras_raw = json.loads(cached.get("extra_schedules_json") or "{}")
            extra_schedules = {
                name: Schedule.model_validate(payload)
                for name, payload in extras_raw.items()
            }
            for extra in extra_schedules.values():
                extra.date = day_iso

            prefs = await db.get_calendar_prefs(callback.from_user.id)
            aliases = await db.get_lesson_aliases(callback.from_user.id)

            result = await asyncio.to_thread(
                sync_schedule_to_caldav,
                creds,
                schedule,
                user_id=callback.from_user.id,
                template=prefs.title_template,
                aliases=aliases,
                bells=bells,
                extra_schedules=extra_schedules or None,
                force_replace=True,
            )
            hw_updated = 0
            if result.ok and local_hw:
                hw_map = {
                    n: item["homework_text"]
                    for n, item in local_hw.items()
                    if item.get("homework_text")
                }
                hw_result = await asyncio.to_thread(
                    write_homework_for_day,
                    creds,
                    user_id=callback.from_user.id,
                    class_name=schedule.class_name,
                    schedule_date=day,
                    homework_by_lesson=hw_map,
                )
                if hw_result.ok:
                    hw_updated = hw_result.updated
        except Exception as exc:
            logger.exception("Rewrite day to SOGo failed")
            await callback.message.edit_text(
                f"❌ Не удалось записать: <code>{_esc(str(exc))}</code>",
                parse_mode="HTML",
                reply_markup=view_day_keyboard(day, can_write=True),
            )
            return

        if result.ok:
            text = (
                f"✅ <b>{day.strftime('%d.%m.%Y')}</b> восстановлен в SOGo\n"
                f"Удалено старых: {result.deleted}, создано: {result.created}"
            )
            if hw_updated:
                text += f"\nДЗ восстановлено: {hw_updated}"
            if result.calendar_url:
                text += f"\n<code>{_esc(result.calendar_url)}</code>"
        else:
            text = f"❌ Не записано: <code>{_esc(result.error or 'ошибка')}</code>"
    elif local_hw:
        await callback.message.edit_text(
            f"⏳ Восстанавливаю ДЗ за {day.strftime('%d.%m.%Y')} в SOGo…"
        )
        hw_map = {
            n: item["homework_text"]
            for n, item in local_hw.items()
            if item.get("homework_text")
        }
        hw_result = await asyncio.to_thread(
            write_homework_for_day,
            creds,
            user_id=callback.from_user.id,
            class_name=saved.class_name,
            schedule_date=day,
            homework_by_lesson=hw_map,
        )
        if hw_result.ok:
            text = (
                f"✅ ДЗ за <b>{day.strftime('%d.%m.%Y')}</b> записано в SOGo\n"
                f"Обновлено: {hw_result.updated}\n"
                "Расписания в локальном кэше нет — уроки должны уже быть в SOGo."
            )
        else:
            text = f"❌ ДЗ не записано: <code>{_esc(hw_result.error or 'ошибка')}</code>"
    else:
        await callback.message.edit_text(
            f"⏳ Чищу дубли за {day.strftime('%d.%m.%Y')} в SOGo…"
        )
        result = await asyncio.to_thread(
            cleanup_day_duplicates,
            creds,
            user_id=callback.from_user.id,
            class_name=saved.class_name,
            schedule_date=day,
            bells=bells,
        )
        if result.ok:
            text = (
                f"✅ Дубли за <b>{day.strftime('%d.%m.%Y')}</b> почищены\n"
                f"Удалено: {result.deleted}, оставлено уроков: {result.kept}\n\n"
                "Локального кэша нет — загрузи PDF один раз, чтобы дальше "
                "восстанавливать день без LLM."
            )
        else:
            text = f"❌ Очистка не удалась: <code>{_esc(result.error or 'ошибка')}</code>"

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=view_day_keyboard(day, can_write=True),
    )
