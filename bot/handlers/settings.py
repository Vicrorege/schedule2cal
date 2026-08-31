import logging

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards.settings import (
    aliases_keyboard,
    bell_edit_cancel_keyboard,
    bells_keyboard,
    caldav_keyboard,
    settings_keyboard,
    template_keyboard,
)
from bot.states.settings import SettingsStates
from db.database import DEFAULT_TITLE_TEMPLATE, Database, BellPeriod, parse_bell_range
from services.caldav_client import test_connection
from services.title_template import validate_title_template
import asyncio

logger = logging.getLogger(__name__)
router = Router()


def format_bells_text(bells: dict[int, BellPeriod]) -> str:
    lines = ["🔔 <b>Сетка звонков</b>\n"]
    for n in range(1, 10):
        p = bells[n]
        lines.append(f"<b>{n} урок</b>: {p.start} – {p.end}")
    lines.append("\nНажми на урок, чтобы изменить время.")
    lines.append("Формат: <code>08:00-08:45</code>")
    return "\n".join(lines)


async def format_settings_text(db: Database, user_id: int) -> str:
    saved = await db.get_user_settings(user_id)
    prefs = await db.get_calendar_prefs(user_id)
    aliases = await db.get_lesson_aliases(user_id)

    if saved:
        subgroup_text = f"Подгруппа: {saved.subgroup}" if saved.subgroup else "Подгруппа: не указана"
        class_block = f"Класс: <b>{saved.class_name}</b>\n{subgroup_text}"
    else:
        class_block = "Класс ещё не сохранён."

    naming = "вкл" if prefs.custom_naming else "выкл"
    creds = await db.get_caldav_credentials(user_id)
    if not creds:
        caldav_line = "📅 CalDAV: <b>не настроен</b>"
    elif creds.password:
        caldav_line = f"📅 CalDAV: <b>готов</b> ({creds.username})"
    else:
        caldav_line = f"📅 CalDAV: URL/логин есть, <b>пароль очищен</b> ({creds.username})"

    return (
        f"⚙️ <b>Настройки</b>\n\n"
        f"{class_block}\n\n"
        f"🏷 Шаблон: <code>{prefs.title_template}</code>\n"
        f"📖 Кастомные имена: <b>{naming}</b> ({len(aliases)} шт.)\n"
        f"{caldav_line}\n\n"
        "Плейсхолдеры шаблона: <code>{lesson}</code>, <code>{room}</code>, <code>{n}</code>\n"
        "Пример: <code>sch {lesson}|[color=red]</code>"
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext, db: Database):
    await state.clear()
    prefs = await db.get_calendar_prefs(message.from_user.id)
    await message.answer(
        await format_settings_text(db, message.from_user.id),
        reply_markup=settings_keyboard(prefs),
    )


@router.callback_query(F.data == "settings:home")
async def settings_home(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    prefs = await db.get_calendar_prefs(callback.from_user.id)
    await callback.message.edit_text(
        await format_settings_text(db, callback.from_user.id),
        reply_markup=settings_keyboard(prefs),
    )
    await callback.answer()


@router.callback_query(F.data == "settings:clear")
async def settings_clear_class(callback: CallbackQuery, db: Database):
    await db.clear_user_settings(callback.from_user.id)
    prefs = await db.get_calendar_prefs(callback.from_user.id)
    text = await format_settings_text(db, callback.from_user.id)
    await callback.message.edit_text(
        text + "\n\n🗑 Сохранённый класс сброшен.",
        reply_markup=settings_keyboard(prefs),
    )
    await callback.answer("Класс сброшен")


@router.callback_query(F.data == "settings:naming_toggle")
async def settings_naming_toggle(callback: CallbackQuery, db: Database):
    prefs = await db.get_calendar_prefs(callback.from_user.id)
    prefs = await db.save_calendar_prefs(
        callback.from_user.id, custom_naming=not prefs.custom_naming
    )
    await callback.message.edit_text(
        await format_settings_text(db, callback.from_user.id),
        reply_markup=settings_keyboard(prefs),
    )
    await callback.answer(
        "Кастомные имена включены" if prefs.custom_naming else "Кастомные имена выключены"
    )


@router.callback_query(F.data == "settings:template")
async def settings_template(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    prefs = await db.get_calendar_prefs(callback.from_user.id)
    await callback.message.edit_text(
        "🏷 <b>Шаблон названия события</b>\n\n"
        f"Сейчас: <code>{prefs.title_template}</code>\n\n"
        "Плейсхолдеры:\n"
        "• <code>{lesson}</code> — имя предмета (или кастомный алиас)\n"
        "• <code>{room}</code> — кабинет\n"
        "• <code>{n}</code> — номер урока\n\n"
        "Пример: <code>sch {lesson}|[color=red]</code>",
        reply_markup=template_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "settings:template_edit")
async def settings_template_edit(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsStates.editing_template)
    await callback.message.edit_text(
        "✏️ Пришли новый шаблон одной строкой.\n"
        "Пример: <code>sch {lesson}|[color=red]</code>",
        reply_markup=None,
    )
    await callback.answer()


@router.callback_query(F.data == "settings:template_reset")
async def settings_template_reset(callback: CallbackQuery, db: Database):
    await db.save_calendar_prefs(callback.from_user.id, title_template=DEFAULT_TITLE_TEMPLATE)
    prefs = await db.get_calendar_prefs(callback.from_user.id)
    await callback.message.edit_text(
        "🏷 <b>Шаблон названия события</b>\n\n"
        f"Сейчас: <code>{prefs.title_template}</code>\n\n"
        "↺ Восстановлен дефолт.",
        reply_markup=template_keyboard(),
    )
    await callback.answer("Дефолт")


@router.message(StateFilter(SettingsStates.editing_template), F.text)
async def settings_template_save(message: Message, state: FSMContext, db: Database):
    template = (message.text or "").strip()
    error = validate_title_template(template)
    if error:
        await message.answer(f"❌ {error}")
        return

    await db.save_calendar_prefs(message.from_user.id, title_template=template)
    await state.clear()
    prefs = await db.get_calendar_prefs(message.from_user.id)
    await message.answer(
        f"✅ Шаблон сохранён: <code>{template}</code>\n\n"
        + await format_settings_text(db, message.from_user.id),
        reply_markup=settings_keyboard(prefs),
    )


@router.callback_query(F.data == "settings:aliases")
async def settings_aliases(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    aliases = await db.get_lesson_aliases(callback.from_user.id)
    await state.update_data(alias_keys=sorted(aliases.keys(), key=str.casefold))
    if not aliases:
        text = "📖 <b>Словарь имён</b>\n\nПока пусто. Заполнится при разборе расписания."
    else:
        lines = ["📖 <b>Словарь имён</b>\n"]
        for src, alias in sorted(aliases.items(), key=lambda x: x[0].casefold()):
            lines.append(f"• <code>{src}</code> → <code>{alias}</code>")
        text = "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=aliases_keyboard(aliases))
    await callback.answer()


@router.callback_query(F.data.startswith("alias:del:"))
async def alias_delete(callback: CallbackQuery, state: FSMContext, db: Database):
    data = await state.get_data()
    keys = data.get("alias_keys") or []
    idx = int(callback.data.split(":")[2])
    if idx < 0 or idx >= len(keys):
        aliases = await db.get_lesson_aliases(callback.from_user.id)
        keys = sorted(aliases.keys(), key=str.casefold)
        await state.update_data(alias_keys=keys)
        if idx < 0 or idx >= len(keys):
            await callback.answer("Не найдено", show_alert=True)
            return

    source = keys[idx]
    await db.delete_lesson_alias(callback.from_user.id, source)
    aliases = await db.get_lesson_aliases(callback.from_user.id)
    await state.update_data(alias_keys=sorted(aliases.keys(), key=str.casefold))
    lines = ["📖 <b>Словарь имён</b>\n"] if aliases else ["📖 <b>Словарь имён</b>\n\nПусто."]
    for src, alias in sorted(aliases.items(), key=lambda x: x[0].casefold()):
        lines.append(f"• <code>{src}</code> → <code>{alias}</code>")
    await callback.message.edit_text("\n".join(lines), reply_markup=aliases_keyboard(aliases))
    await callback.answer(f"Удалено: {source}")


@router.callback_query(F.data == "alias:clear")
async def alias_clear(callback: CallbackQuery, state: FSMContext, db: Database):
    await db.clear_lesson_aliases(callback.from_user.id)
    await state.update_data(alias_keys=[])
    await callback.message.edit_text(
        "📖 <b>Словарь имён</b>\n\nОчищено.",
        reply_markup=aliases_keyboard({}),
    )
    await callback.answer("Очищено")


@router.callback_query(F.data == "settings:bells")
async def settings_bells(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    bells = await db.get_bells(callback.from_user.id)
    await callback.message.edit_text(
        format_bells_text(bells),
        reply_markup=bells_keyboard(bells),
    )
    await callback.answer()


@router.callback_query(F.data == "bell:reset")
async def bells_reset(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    bells = await db.reset_bells(callback.from_user.id)
    await callback.message.edit_text(
        format_bells_text(bells) + "\n\n↺ Восстановлены значения по умолчанию.",
        reply_markup=bells_keyboard(bells),
    )
    await callback.answer("Дефолт восстановлен")


@router.callback_query(F.data.startswith("bell:edit:"))
async def bell_edit_start(callback: CallbackQuery, state: FSMContext, db: Database):
    lesson = int(callback.data.split(":")[2])
    bells = await db.get_bells(callback.from_user.id)
    period = bells[lesson]

    await state.set_state(SettingsStates.editing_bell)
    await state.update_data(editing_lesson=lesson)

    await callback.message.edit_text(
        f"✏️ Урок <b>{lesson}</b>\n"
        f"Сейчас: <code>{period.start}-{period.end}</code>\n\n"
        "Пришли новое время в формате:\n"
        "<code>HH:MM-HH:MM</code>\n"
        "Например: <code>08:00-08:45</code>",
        reply_markup=bell_edit_cancel_keyboard(lesson),
    )
    await callback.answer()


@router.callback_query(F.data == "bell:cancel")
async def bell_edit_cancel(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    bells = await db.get_bells(callback.from_user.id)
    await callback.message.edit_text(
        format_bells_text(bells),
        reply_markup=bells_keyboard(bells),
    )
    await callback.answer("Отменено")


@router.message(StateFilter(SettingsStates.editing_bell), F.text)
async def bell_edit_save(message: Message, state: FSMContext, db: Database):
    data = await state.get_data()
    lesson = data.get("editing_lesson")
    if not lesson:
        await state.clear()
        await message.answer("Сессия сброшена. Открой /settings снова.")
        return

    parsed = parse_bell_range(message.text or "")
    if not parsed:
        await message.answer(
            "Не понял время. Пришли в формате <code>08:00-08:45</code>\n"
            "(начало раньше конца)."
        )
        return

    start, end = parsed
    bells = await db.set_bell_period(message.from_user.id, lesson, start, end)
    await state.clear()

    await message.answer(
        f"✅ Урок {lesson}: <b>{start}–{end}</b>\n\n" + format_bells_text(bells),
        reply_markup=bells_keyboard(bells),
    )


def _caldav_status_text(creds) -> str:
    if not creds:
        return (
            "📅 <b>CalDAV / SOGo</b>\n\n"
            "Доступ не настроен.\n"
            "Нужны URL календаря, логин и пароль.\n\n"
            "Пример URL:\n"
            "<code>https://mail.example.com/SOGo/dav/user@example.com/Calendar/personal/</code>\n\n"
            "⚠️ Сообщения с паролем удаляются из чата сразу после ввода.\n"
            "После записи в календарь пароль стирается из бота — его нужно ввести снова "
            "(URL и логин остаются)."
        )
    pwd = "задан" if creds.password else "очищен (нужно ввести снова)"
    return (
        "📅 <b>CalDAV / SOGo</b>\n\n"
        f"URL: <code>{creds.url}</code>\n"
        f"Логин: <code>{creds.username}</code>\n"
        f"Пароль: <b>{pwd}</b>\n\n"
        "⚠️ Сообщения с паролем удаляются из чата.\n"
        "После успешной записи расписания пароль стирается из хранилища бота."
    )


@router.callback_query(F.data == "settings:caldav")
async def settings_caldav(callback: CallbackQuery, state: FSMContext, db: Database):
    await state.clear()
    creds = await db.get_caldav_credentials(callback.from_user.id)
    await callback.message.edit_text(
        _caldav_status_text(creds),
        reply_markup=caldav_keyboard(
            has_creds=creds is not None,
            has_password=bool(creds and creds.password),
        ),
    )
    await callback.answer()


@router.callback_query(F.data == "caldav:setup")
async def caldav_setup_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SettingsStates.caldav_url)
    await callback.message.edit_text(
        "🔗 Пришли <b>URL календаря</b> SOGo/CalDAV одной строкой.\n\n"
        "Пример:\n"
        "<code>https://mail.example.com/SOGo/dav/user@example.com/Calendar/personal/</code>",
    )
    await callback.answer()


@router.callback_query(F.data == "caldav:password")
async def caldav_password_only(callback: CallbackQuery, state: FSMContext, db: Database):
    creds = await db.get_caldav_credentials(callback.from_user.id)
    if not creds:
        await callback.answer("Сначала задай URL и логин", show_alert=True)
        return
    await state.set_state(SettingsStates.caldav_password)
    await callback.message.edit_text(
        "🔑 Пришли <b>пароль</b> одним сообщением.\n"
        "Сообщение будет удалено сразу после получения.",
    )
    await callback.answer()


@router.message(StateFilter(SettingsStates.caldav_url), F.text)
async def caldav_save_url(message: Message, state: FSMContext):
    url = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    if not url.startswith("http"):
        await message.answer("URL должен начинаться с http:// или https:// — пришли ещё раз.")
        return
    await state.update_data(caldav_url=url)
    await state.set_state(SettingsStates.caldav_username)
    await message.answer("👤 Пришли <b>логин</b> (обычно email). Сообщение тоже удалю.")


@router.message(StateFilter(SettingsStates.caldav_username), F.text)
async def caldav_save_username(message: Message, state: FSMContext):
    username = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    if not username:
        await message.answer("Логин пустой — пришли ещё раз.")
        return
    await state.update_data(caldav_username=username)
    await state.set_state(SettingsStates.caldav_password)
    await message.answer("🔑 Пришли <b>пароль</b>. Сообщение будет удалено сразу.")


@router.message(StateFilter(SettingsStates.caldav_password), F.text)
async def caldav_save_password(message: Message, state: FSMContext, db: Database):
    password = (message.text or "").strip()
    try:
        await message.delete()
    except Exception:
        pass
    if not password:
        await message.answer("Пароль пустой — пришли ещё раз.")
        return

    data = await state.get_data()
    try:
        if data.get("caldav_url") and data.get("caldav_username"):
            await db.save_caldav_credentials(
                message.from_user.id,
                url=data["caldav_url"],
                username=data["caldav_username"],
                password=password,
            )
        else:
            await db.save_caldav_credentials(
                message.from_user.id,
                password=password,
            )
    except ValueError as exc:
        await state.clear()
        await message.answer(f"❌ {exc}")
        return

    await state.clear()
    creds = await db.get_caldav_credentials(message.from_user.id)
    await message.answer(
        "✅ Данные CalDAV сохранены. Пароль в чате удалён.\n\n" + _caldav_status_text(creds),
        reply_markup=caldav_keyboard(has_creds=True, has_password=True),
    )


@router.callback_query(F.data == "caldav:test")
async def caldav_test(callback: CallbackQuery, db: Database):
    creds = await db.get_caldav_credentials(callback.from_user.id)
    if not creds or not creds.is_complete:
        await callback.answer("Нужен пароль — задай доступ заново", show_alert=True)
        return
    await callback.answer("Проверяю…")
    try:
        resolved = await asyncio.to_thread(test_connection, creds)
        # test_connection returns "OK → <url>"
        calendar_url = resolved.split("→", 1)[-1].strip() if "→" in resolved else ""
        if calendar_url and calendar_url.rstrip("/") != creds.url.rstrip("/"):
            await db.save_caldav_credentials(
                callback.from_user.id,
                url=calendar_url,
                username=creds.username,
                password=creds.password,
            )
            await callback.message.answer(
                "✅ Подключение к CalDAV успешно.\n"
                f"Рабочий календарь: <code>{calendar_url}</code>\n"
                "URL в настройках обновлён (вместо несуществующего personal)."
            )
        else:
            await callback.message.answer(
                "✅ Подключение к CalDAV успешно.\n"
                f"Календарь: <code>{calendar_url or creds.url}</code>"
            )
    except Exception as exc:
        await callback.message.answer(f"❌ Не удалось подключиться:\n<code>{exc}</code>")


@router.callback_query(F.data == "caldav:delete")
async def caldav_delete(callback: CallbackQuery, state: FSMContext, db: Database):
    await db.delete_caldav_credentials(callback.from_user.id)
    await state.clear()
    await callback.message.edit_text(
        _caldav_status_text(None),
        reply_markup=caldav_keyboard(has_creds=False, has_password=False),
    )
    await callback.answer("Доступ удалён")
