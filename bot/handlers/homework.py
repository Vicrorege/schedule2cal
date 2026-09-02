from __future__ import annotations

import asyncio
import logging
from datetime import date
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, Message

from bot.config import Settings
from bot.keyboards.homework import homework_confirm_keyboard
from bot.states.homework import HomeworkStates
from db.database import Database
from models.homework import HomeworkBlock, HomeworkParseResult
from services.caldav_client import BotCalendarEvent, list_bot_events, write_homework_to_events
from services.homework import assign_homework_blocks, search_range
from services.llm import create_llm_provider
from services.schedule_postprocess import WEEKDAY_RU, day_of_week_from_date

logger = logging.getLogger(__name__)
router = Router()

MENU_BUTTONS = {
    "📎 Загрузить расписание",
    "⚙️ Настройки",
    "📖 Инструкция",
    "📅 Календарь",
    "📝 Домашка",
}


def _esc(text: str) -> str:
    return escape(text, quote=False)


def _looks_like_homework(text: str) -> bool:
    """Грубая эвристика: не перехватывать короткие случайные сообщения."""
    stripped = text.strip()
    if len(stripped) < 8:
        return False
    if stripped.startswith("/"):
        return False
    # хотя бы одна буква и перенос/двоеточие/типичные маркеры
    has_letter = any(ch.isalpha() for ch in stripped)
    if not has_letter:
        return False
    markers = ("\n", ":", "номер", "параграф", "на ", "дз", "домаш")
    return any(m in stripped.casefold() for m in markers) or stripped.count(" ") >= 3


def _format_preview(assignments) -> str:
    lines = ["📝 <b>Домашнее задание — превью</b>", ""]
    ok = [a for a in assignments if a.status == "ok" and a.event]
    fail = [a for a in assignments if a.status != "ok"]

    for a in ok:
        ev = a.event
        assert ev is not None
        dow = WEEKDAY_RU[day_of_week_from_date(ev.event_date)]
        lines.append(
            f"✅ <b>{_esc(ev.subject)}</b> · "
            f"{ev.event_date.strftime('%d.%m.%Y')} ({dow}) · урок {ev.lesson_number}"
        )
        for hw_line in a.block.text.strip().splitlines() or [a.block.text]:
            lines.append(f"   {_esc(hw_line)}")
        lines.append("")

    for a in fail:
        lines.append(f"⚠️ {_esc(a.message)}")
        lines.append(f"   <i>{_esc(a.block.subject)}</i>: {_esc(a.block.text[:120])}")
        lines.append("")

    if ok:
        lines.append("Записать в календарь?")
    else:
        lines.append("Нечего записывать — проверь предметы/даты или загрузи расписание.")
    return "\n".join(lines).rstrip()


@router.message(Command("homework"))
@router.message(F.text == "📝 Домашка")
async def cmd_homework_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "📝 <b>Домашка</b>\n\n"
        "Просто пришли текст без команды — бот разберёт блоки и запишет ДЗ "
        "в описание ближайших уроков.\n\n"
        "Пример:\n"
        "<code>математика:\n"
        "номера 12–15\n"
        "параграф 3\n\n"
        "русский\n"
        "упр. 45\n\n"
        "на четверг\n"
        "математика\n"
        "другие номера…\n\n"
        "на 28.09\n"
        "русский\n"
        "…</code>\n\n"
        "Без даты — на ближайший урок этого предмета.",
        parse_mode="HTML",
    )


@router.message(StateFilter(default_state), F.text)
async def handle_plain_homework(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db: Database,
):
    text = (message.text or "").strip()
    if not text or text in MENU_BUTTONS:
        return
    if text.startswith("/"):
        return
    if not _looks_like_homework(text):
        return

    saved = await db.get_user_settings(message.from_user.id)
    if not saved or not saved.class_name:
        await message.answer(
            "Сначала загрузи расписание и сохрани класс "
            "(📎 Загрузить расписание), иначе некуда писать ДЗ."
        )
        return

    creds = await db.get_caldav_credentials(message.from_user.id)
    if not creds or not creds.is_complete:
        await message.answer(
            "Нужен CalDAV: /settings → 📅 CalDAV / SOGo.\n"
            "Без календаря ДЗ записать некуда."
        )
        return

    status = await message.answer("⏳ Разбираю домашнее задание…")

    try:
        llm = create_llm_provider(settings)
        today = date.today()
        parsed: HomeworkParseResult = await llm.parse_homework(text, today=today)
        blocks = [
            b
            for b in parsed.blocks
            if isinstance(b, HomeworkBlock) and b.subject.strip() and b.text.strip()
        ]
        if not blocks:
            await status.edit_text(
                "Не нашёл блоков ДЗ. Укажи предмет и текст задания."
            )
            return

        start, end = search_range(today=today, blocks=blocks)
        events = await asyncio.to_thread(
            list_bot_events,
            creds,
            user_id=message.from_user.id,
            class_name=saved.class_name,
            start_date=start,
            end_date=end,
            include_extra=False,
        )
        aliases = await db.get_lesson_aliases(message.from_user.id)
        assignments = assign_homework_blocks(
            blocks, events, today=today, aliases=aliases
        )
        if not assignments:
            await status.edit_text("Нечего записывать.")
            return

        payload = [
            {
                "subject": a.block.subject,
                "text": a.block.text,
                "target_date": a.block.target_date,
                "day_of_week": a.block.day_of_week.value if a.block.day_of_week else None,
                "status": a.status,
                "message": a.message,
                "uid": a.event.uid if a.event else None,
                "event_date": a.event.event_date.isoformat() if a.event else None,
                "lesson_number": a.event.lesson_number if a.event else None,
                "event_subject": a.event.subject if a.event else None,
                "description": a.event.description if a.event else None,
            }
            for a in assignments
        ]
        await state.update_data(homework_assignments=payload)
        await state.set_state(HomeworkStates.waiting_for_confirm)

        ok_count = sum(1 for a in assignments if a.status == "ok")
        markup = homework_confirm_keyboard() if ok_count else None
        await status.edit_text(
            _format_preview(assignments),
            parse_mode="HTML",
            reply_markup=markup,
        )
    except Exception as exc:
        logger.exception("Homework parse failed")
        detail = str(exc)
        if "исчерпали квоту" in detail.casefold() or "RESOURCE_EXHAUSTED" in detail:
            await status.edit_text("❌ Лимит LLM исчерпан. Попробуй позже.")
        else:
            await status.edit_text("❌ Не удалось разобрать ДЗ. Попробуй ещё раз.")


@router.callback_query(F.data == "hw:cancel", HomeworkStates.waiting_for_confirm)
async def homework_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Запись ДЗ отменена.")
    await callback.answer()


@router.callback_query(F.data == "hw:confirm", HomeworkStates.waiting_for_confirm)
async def homework_confirm(callback: CallbackQuery, state: FSMContext, db: Database):
    data = await state.get_data()
    payload = data.get("homework_assignments") or []
    ok_items = [p for p in payload if p.get("status") == "ok" and p.get("uid")]
    if not ok_items:
        await callback.answer("Нечего записывать", show_alert=True)
        await state.clear()
        return

    creds = await db.get_caldav_credentials(callback.from_user.id)
    if not creds or not creds.is_complete:
        await callback.answer("CalDAV не настроен", show_alert=True)
        await state.clear()
        return

    await callback.answer()
    await callback.message.edit_text("⏳ Записываю ДЗ в календарь…")

    assignments = []
    for item in ok_items:
        event = BotCalendarEvent(
            uid=item["uid"],
            event_date=date.fromisoformat(item["event_date"]),
            lesson_number=item["lesson_number"],
            subject=item["event_subject"],
            room=None,
            summary=item["event_subject"],
            description=item.get("description") or "",
            homework=None,
            is_extra_only=False,
        )
        assignments.append((event, item["text"]))

    result = await asyncio.to_thread(write_homework_to_events, creds, assignments)
    await state.clear()

    if result.ok:
        await callback.message.edit_text(
            f"✅ Домашка записана в календарь.\nОбновлено событий: {result.updated}"
        )
    else:
        await callback.message.edit_text(
            f"❌ Не удалось записать ДЗ.\n<code>{_esc(result.error or 'ошибка')}</code>",
            parse_mode="HTML",
        )
