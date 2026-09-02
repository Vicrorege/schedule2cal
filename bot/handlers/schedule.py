import asyncio
import logging
import json
from datetime import date

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.config import Settings
from bot.keyboards.calendar import calendar_keyboard, shift_month
from bot.keyboards.class_select import (
    class_selection_keyboard,
    subgroup_keyboard,
)
from bot.keyboards.extra_classes import extra_classes_keyboard
from bot.keyboards.preview import (
    parse_retry_keyboard,
    preview_delete_keyboard,
    preview_edit_cancel_keyboard,
    preview_keyboard,
)
from bot.keyboards.settings import naming_keyboard
from bot.states.schedule import ScheduleStates
from db.database import Database
from models.schedule import Schedule
from services.caldav_client import sync_schedule_to_caldav
from services.image_processor import fit_for_llm, process_upload
from services.llm import create_llm_provider
from services.schedule_formatter import format_schedule_preview
from services.schedule_postprocess import (
    WEEKDAY_RU,
    apply_subgroup,
    day_of_week_from_date,
    find_matching_extra_classes,
    find_saved_class,
    has_paired_lessons,
    is_manual_class_selection,
    normalize_paired_lessons,
    parse_caption_date,
    parse_iso_date,
    subjects_from_schedule,
)
from services.title_template import (
    format_calendar_events,
    suggest_aliases,
)

logger = logging.getLogger(__name__)
router = Router()


@router.message(Command("set_schedule"))
async def cmd_set_schedule(message: Message, state: FSMContext):
    await state.set_state(ScheduleStates.waiting_for_file)
    await message.answer(
        "📎 Отправь файл расписания (PDF, PNG, JPG).\n"
        "Можно переслать документ из Telegram."
    )


@router.message(F.document)
async def handle_document(message: Message, state: FSMContext, settings: Settings, db: Database):
    doc = message.document
    if not doc or not doc.file_name:
        await message.answer("Не удалось получить файл. Попробуй ещё раз.")
        return

    ext = doc.file_name.rsplit(".", 1)[-1].lower()
    if ext not in ("pdf", "png", "jpg", "jpeg", "webp"):
        await message.answer("Поддерживаются только PDF, PNG, JPG, WEBP.")
        return

    await state.set_state(ScheduleStates.waiting_for_file)
    await _process_file(
        message,
        state,
        settings,
        db,
        doc.file_id,
        doc.file_name,
        caption=message.caption,
    )


@router.message(F.photo)
async def handle_photo(message: Message, state: FSMContext, settings: Settings, db: Database):
    photo = message.photo[-1]
    await state.set_state(ScheduleStates.waiting_for_file)
    await _process_file(
        message,
        state,
        settings,
        db,
        photo.file_id,
        "photo.jpg",
        caption=message.caption,
    )


async def _process_file(
    message: Message,
    state: FSMContext,
    settings: Settings,
    db: Database,
    file_id: str,
    filename: str,
    caption: str | None = None,
):
    status_msg = await message.answer("⏳ Обрабатываю файл...")

    try:
        file = await message.bot.download(file_id)
        file_bytes = file.read()

        image_bytes = fit_for_llm(process_upload(file_bytes, filename))
        await state.update_data(image_bytes=image_bytes)

        await status_msg.edit_text("🔍 Ищу классы и дату в расписании...")

        llm = create_llm_provider(settings)
        class_list = await llm.detect_classes(image_bytes)

        if not class_list.classes:
            await status_msg.edit_text("❌ Не удалось найти классы в документе.")
            await state.clear()
            return

        caption_date = parse_caption_date(caption)
        llm_date = parse_iso_date(class_list.schedule_date)
        # Приоритет: дата из подписи → дата из LLM
        detected_date = caption_date or llm_date
        date_source = "подписи" if caption_date else ("документа" if llm_date else None)

        manual = is_manual_class_selection(caption)
        saved = await db.get_user_settings(message.from_user.id)
        matched_class = None
        if saved:
            matched_class = find_saved_class(saved.class_name, class_list.classes)

        prefs = await db.get_calendar_prefs(message.from_user.id)

        await state.update_data(
            classes=class_list.classes,
            saved_class=matched_class,
            saved_subgroup=saved.subgroup if saved else None,
            detected_date=detected_date.isoformat() if detected_date else None,
            selected_date=detected_date.isoformat() if detected_date else None,
            date_source=date_source,
            remember_class=True,
            extra_classes=[],
            extra_classes_enabled=prefs.extra_classes_enabled,
            extra_only_upload=False,
        )

        # Файл только с доп. классом(ами): основной класс уже сохранён, в файле его нет
        if (
            prefs.extra_classes_enabled
            and saved
            and saved.class_name
            and not manual
            and not matched_class
        ):
            matched_extras = find_matching_extra_classes(
                prefs.extra_classes or [],
                class_list.classes,
            )
            if matched_extras:
                await state.update_data(
                    selected_class=saved.class_name,
                    selected_subgroup=saved.subgroup,
                    extra_classes=matched_extras,
                    extra_only_upload=True,
                    remember_class=False,
                )
                date_line = ""
                if detected_date:
                    dow = WEEKDAY_RU[day_of_week_from_date(detected_date)]
                    date_line = (
                        f"\n📅 Дата из {date_source}: "
                        f"<b>{detected_date.strftime('%d.%m.%Y')}</b> ({dow})"
                    )
                extras_text = ", ".join(f"<b>{e}</b>" for e in matched_extras)
                await status_msg.edit_text(
                    f"➕ Распознал доп. класс: {extras_text}\n"
                    f"📚 Основной (куда пишем): <b>{saved.class_name}</b>"
                    f"{date_line}\n\n"
                    "Перехожу к выбору даты…",
                    parse_mode="HTML",
                )
                await _show_calendar_message(status_msg, state)
                return

        # Автопропуск выбора класса, если есть сохранённый и нет ручного режима
        if matched_class and not manual:
            await state.update_data(
                selected_class=matched_class,
                selected_subgroup=saved.subgroup if saved else None,
            )
            subgroup_text = (
                f", п.г. {saved.subgroup}" if saved and saved.subgroup else ""
            )
            date_line = ""
            if detected_date:
                dow = WEEKDAY_RU[day_of_week_from_date(detected_date)]
                date_line = (
                    f"\n📅 Дата из {date_source}: "
                    f"<b>{detected_date.strftime('%d.%m.%Y')}</b> ({dow})"
                )
            await status_msg.edit_text(
                f"✅ Класс подставлен: <b>{matched_class}</b>{subgroup_text}"
                f"{date_line}\n\n"
                "Перехожу к следующему шагу…",
                parse_mode="HTML",
            )
            await _continue_after_subgroup_message(
                status_msg, state, db, message.from_user.id
            )
            return

        await state.set_state(ScheduleStates.waiting_for_class)

        text = f"✅ Найдено классов: {len(class_list.classes)}\n"
        if manual:
            text += "🖐 Ручной выбор класса\n"
        if detected_date:
            dow = WEEKDAY_RU[day_of_week_from_date(detected_date)]
            text += (
                f"📅 Дата из {date_source}: "
                f"<b>{detected_date.strftime('%d.%m.%Y')}</b> ({dow})\n"
            )
        else:
            text += "📅 Дата не найдена — выберешь на следующем шаге\n"
        if matched_class:
            text += f"📌 Сохранённый класс найден: <b>{matched_class}</b>\n"
        text += "\nВыбери свой класс:"

        await status_msg.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=class_selection_keyboard(
                class_list.classes,
                saved_class=matched_class,
                saved_subgroup=saved.subgroup if saved else None,
                remember=True,
            ),
        )
    except Exception as exc:
        logger.exception("Ошибка обработки файла")
        detail = str(exc)
        if "исчерпали квоту" in detail.casefold() or "RESOURCE_EXHAUSTED" in detail:
            await status_msg.edit_text(
                "❌ Лимит Gemini исчерпан на всех ключах пула.\n"
                "Добавь ключи из других проектов в <code>LLM_API_KEY</code> "
                "или подожди сброса квоты.",
                parse_mode="HTML",
            )
        else:
            await status_msg.edit_text("❌ Ошибка при обработке файла. Попробуй ещё раз.")
        await state.clear()

async def _ensure_session(callback: CallbackQuery, state: FSMContext) -> dict | None:
    data = await state.get_data()
    if not data.get("image_bytes") or not data.get("classes"):
        await callback.answer("Сессия истекла — отправь файл заново", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await state.clear()
        return None
    return data


def _class_keyboard_from_state(data: dict, page: int = 0):
    return class_selection_keyboard(
        data.get("classes", []),
        page=page,
        saved_class=data.get("saved_class"),
        saved_subgroup=data.get("saved_subgroup"),
        remember=bool(data.get("remember_class", True)),
    )


@router.callback_query(F.data == "remember:toggle")
async def handle_remember_toggle(callback: CallbackQuery, state: FSMContext):
    data = await _ensure_session(callback, state)
    if data is None:
        return

    remember = not bool(data.get("remember_class", True))
    await state.update_data(remember_class=remember)
    data["remember_class"] = remember

    await callback.message.edit_reply_markup(reply_markup=_class_keyboard_from_state(data))
    await callback.answer("Запомним класс" if remember else "Не запоминать")


@router.callback_query(F.data == "use_saved")
async def handle_use_saved(callback: CallbackQuery, state: FSMContext, db: Database):
    data = await _ensure_session(callback, state)
    if data is None:
        return

    class_name = data.get("saved_class")
    subgroup = data.get("saved_subgroup")
    if not class_name:
        await callback.answer("Сохранённый класс не найден в этом расписании", show_alert=True)
        return

    await state.update_data(selected_class=class_name, selected_subgroup=subgroup)
    await callback.answer()
    await _continue_after_subgroup(callback, state, db, callback.from_user.id)


@router.callback_query(F.data.startswith("cls_page:"))
async def handle_class_page(callback: CallbackQuery, state: FSMContext):
    data = await _ensure_session(callback, state)
    if data is None:
        return

    page = int(callback.data.split(":")[1])
    await callback.message.edit_reply_markup(reply_markup=_class_keyboard_from_state(data, page=page))
    await callback.answer()


@router.callback_query(F.data.regexp(r"^cls:\d+$"))
async def handle_class_select(callback: CallbackQuery, state: FSMContext, db: Database):
    data = await _ensure_session(callback, state)
    if data is None:
        return

    idx = int(callback.data.split(":")[1])
    classes = data.get("classes", [])
    if idx < 0 or idx >= len(classes):
        await callback.answer("Класс не найден", show_alert=True)
        return

    class_name = classes[idx]
    saved = await db.get_user_settings(callback.from_user.id)
    saved_subgroup = (
        saved.subgroup
        if saved and find_saved_class(saved.class_name, [class_name])
        else data.get("saved_subgroup")
    )

    await state.update_data(selected_class=class_name)
    await state.set_state(ScheduleStates.waiting_for_subgroup)

    hint = ""
    if saved_subgroup:
        hint = f"\n\n📌 Сохранена подгруппа: {saved_subgroup}"

    await callback.message.edit_text(
        f"📚 Класс: <b>{class_name}</b>{hint}\n\n"
        "Укажи подгруппу (для спаренных уроков):",
        parse_mode="HTML",
        reply_markup=subgroup_keyboard(saved_subgroup),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("subgroup:"))
async def handle_subgroup_select(callback: CallbackQuery, state: FSMContext, settings: Settings, db: Database):
    data = await _ensure_session(callback, state)
    if data is None:
        return

    if not data.get("selected_class"):
        await callback.answer("Сначала выбери класс — отправь файл заново", show_alert=True)
        await state.clear()
        return

    subgroup_str = callback.data.split(":")[1]
    subgroup = None if subgroup_str == "skip" else int(subgroup_str)
    await state.update_data(selected_subgroup=subgroup)
    await callback.answer()

    if data.get("pending_subgroup_resolve"):
        await _resume_parse_after_subgroup(callback, state, settings, db, subgroup)
        return

    await _continue_after_subgroup(callback, state, db, callback.from_user.id)


async def _resume_parse_after_subgroup(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db: Database,
    subgroup: int | None,
):
    """Продолжение парсинга после выбора подгруппы на спаренных уроках."""
    data = await state.get_data()
    raw = data.get("schedule_json")
    if not raw:
        await callback.message.edit_text("Сессия истекла — отправь файл заново.")
        await state.clear()
        return

    schedule = Schedule.model_validate(raw)
    if subgroup is None and has_paired_lessons(schedule):
        await callback.message.edit_text(
            "Для спаренных уроков нужна подгруппа.\nВыбери 1 или 2:",
            reply_markup=subgroup_keyboard(None),
        )
        await state.set_state(ScheduleStates.waiting_for_subgroup)
        return

    schedule = apply_subgroup(schedule, subgroup)
    image_bytes = data["image_bytes"]
    schedule_date = parse_iso_date(schedule.date) or parse_iso_date(data.get("selected_date"))
    if not schedule_date:
        await callback.message.edit_text("Дата потеряна — выбери на календаре.")
        await _show_calendar(callback, state)
        return

    dow = day_of_week_from_date(schedule_date)
    extra_class_names: list[str] = list(data.get("extra_classes") or [])
    remember = bool(data.get("remember_class", True))
    class_name = data.get("selected_class") or schedule.class_name

    try:
        llm = create_llm_provider(settings)
        extra_schedules: dict[str, Schedule] = {}
        for i, extra_name in enumerate(extra_class_names):
            await callback.message.edit_text(
                f"⏳ Распознаю доп. класс <b>{extra_name}</b> "
                f"({i + 1}/{len(extra_class_names)})…",
                parse_mode="HTML",
            )
            extra_schedule = await llm.parse_schedule(
                image_bytes,
                extra_name,
                schedule_date,
                dow.value,
            )
            extra_schedule = normalize_paired_lessons(extra_schedule)
            extra_schedule.date = schedule_date.isoformat()
            extra_schedules[extra_name] = extra_schedule

        if remember:
            await db.save_user_settings(callback.from_user.id, class_name, subgroup)

        await state.update_data(
            schedule_json=schedule.model_dump(mode="json"),
            extra_schedules_json={
                name: s.model_dump(mode="json") for name, s in extra_schedules.items()
            },
            selected_subgroup=subgroup,
            pending_subgroup_resolve=False,
            session_aliases={},
            _user_id=callback.from_user.id,
        )

        prefs = await db.get_calendar_prefs(callback.from_user.id)
        if prefs.custom_naming:
            aliases = await db.get_lesson_aliases(callback.from_user.id)
            pending: list[str] = []
            seen: set[str] = set()
            for subj in subjects_from_schedule(schedule, expand_pairs=False):
                if subj.casefold() not in {k.casefold() for k in aliases} and subj.casefold() not in seen:
                    seen.add(subj.casefold())
                    pending.append(subj)
            for sched in extra_schedules.values():
                for subj in subjects_from_schedule(sched, expand_pairs=True):
                    if subj.casefold() not in {k.casefold() for k in aliases} and subj.casefold() not in seen:
                        seen.add(subj.casefold())
                        pending.append(subj)
            if pending:
                await state.update_data(naming_queue=pending)
                await state.set_state(ScheduleStates.naming_lessons)
                await _ask_next_name(
                    callback.message, state, db, callback.from_user.id, edit=True
                )
                return

        await _show_review(callback.message, state, db, callback.from_user.id, edit=True)
    except Exception:
        logger.exception("Ошибка продолжения парсинга после подгруппы")
        await state.set_state(ScheduleStates.waiting_for_date)
        await callback.message.edit_text(
            "❌ Ошибка при распознавании доп. классов.",
            reply_markup=parse_retry_keyboard(),
        )


def _resolved_extra_classes(
    saved: list[str],
    file_classes: list[str],
    main_class: str,
) -> list[str]:
    return [c for c in saved if c in file_classes and c != main_class]


async def _continue_after_subgroup(
    callback: CallbackQuery,
    state: FSMContext,
    db: Database,
    user_id: int,
):
    data = await state.get_data()
    prefs = await db.get_calendar_prefs(user_id)
    if not prefs.extra_classes_enabled:
        await _show_calendar(callback, state)
        return

    main = data.get("selected_class", "")
    saved = prefs.extra_classes or []
    file_classes = data.get("classes", [])
    resolved = _resolved_extra_classes(saved, file_classes, main)

    if resolved:
        await state.update_data(extra_classes=resolved)
        await _show_calendar(callback, state)
        return

    await _show_extra_classes(callback, state)


async def _continue_after_subgroup_message(
    message: Message,
    state: FSMContext,
    db: Database,
    user_id: int,
):
    data = await state.get_data()
    prefs = await db.get_calendar_prefs(user_id)
    if not prefs.extra_classes_enabled:
        await _show_calendar_message(message, state)
        return

    main = data.get("selected_class", "")
    saved = prefs.extra_classes or []
    file_classes = data.get("classes", [])
    resolved = _resolved_extra_classes(saved, file_classes, main)

    if resolved:
        await state.update_data(extra_classes=resolved)
        await _show_calendar_message(message, state)
        return

    await _show_extra_classes_message(message, state)


def _extra_class_candidates(data: dict) -> list[str]:
    main = data.get("selected_class")
    if not main:
        return []
    return [c for c in data.get("classes", []) if c != main]


def _extra_classes_keyboard_from_state(data: dict, page: int = 0):
    return extra_classes_keyboard(
        data.get("classes", []),
        main_class=data.get("selected_class", ""),
        selected=list(data.get("extra_classes") or []),
        page=page,
    )


def _extra_classes_text(data: dict) -> str:
    class_name = data.get("selected_class", "")
    subgroup = data.get("selected_subgroup")
    selected = list(data.get("extra_classes") or [])
    candidates = _extra_class_candidates(data)

    lines = [f"📚 Основной класс: <b>{class_name}</b>"]
    if subgroup:
        lines.append(f"Подгруппа: {subgroup}")
    lines.append("")
    lines.append(
        "Выбери дополнительные классы — их расписание попадёт "
        "в описание уроков основного класса."
    )
    lines.append(
        "Если у основного класса окно, а у доп. класса урок — "
        "создаётся <b>приватное</b> событие с пометкой <b>ДОП. КЛАСС</b>."
    )
    lines.append("")
    if not candidates:
        lines.append("Других классов в файле нет — нажми «Пропустить».")
    elif selected:
        lines.append(f"Выбрано: <b>{', '.join(selected)}</b>")
    else:
        lines.append("Можно выбрать несколько или пропустить.")
    return "\n".join(lines)


async def _show_extra_classes(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.set_state(ScheduleStates.waiting_for_extra_classes)
    await callback.message.edit_text(
        _extra_classes_text(data),
        parse_mode="HTML",
        reply_markup=_extra_classes_keyboard_from_state(data),
    )


async def _show_extra_classes_message(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(ScheduleStates.waiting_for_extra_classes)
    await message.edit_text(
        _extra_classes_text(data),
        parse_mode="HTML",
        reply_markup=_extra_classes_keyboard_from_state(data),
    )


@router.callback_query(F.data.startswith("extra:toggle:"))
async def handle_extra_toggle(callback: CallbackQuery, state: FSMContext):
    data = await _ensure_session(callback, state)
    if data is None:
        return

    global_idx = int(callback.data.split(":")[2])
    candidates = _extra_class_candidates(data)
    if global_idx < 0 or global_idx >= len(candidates):
        await callback.answer("Класс не найден", show_alert=True)
        return

    cls = candidates[global_idx]
    selected = list(data.get("extra_classes") or [])
    if cls in selected:
        selected.remove(cls)
    else:
        selected.append(cls)
    await state.update_data(extra_classes=selected)
    data["extra_classes"] = selected

    await callback.message.edit_text(
        _extra_classes_text(data),
        parse_mode="HTML",
        reply_markup=_extra_classes_keyboard_from_state(data),
    )
    await callback.answer(cls)


@router.callback_query(F.data.startswith("extra:page:"))
async def handle_extra_page(callback: CallbackQuery, state: FSMContext):
    data = await _ensure_session(callback, state)
    if data is None:
        return

    page = int(callback.data.split(":")[2])
    await callback.message.edit_reply_markup(
        reply_markup=_extra_classes_keyboard_from_state(data, page=page)
    )
    await callback.answer()


@router.callback_query(F.data == "extra:done")
async def handle_extra_done(callback: CallbackQuery, state: FSMContext, db: Database):
    data = await _ensure_session(callback, state)
    if data is None:
        return

    selected = list(data.get("extra_classes") or [])
    prefs = await db.get_calendar_prefs(callback.from_user.id)
    if prefs.extra_classes_enabled and selected:
        await db.save_calendar_prefs(callback.from_user.id, extra_classes=selected)

    await callback.answer()
    await _show_calendar(callback, state)


def _calendar_text(data: dict) -> str:
    class_name = data.get("selected_class", "")
    subgroup = data.get("selected_subgroup")
    detected = parse_iso_date(data.get("detected_date"))
    selected = parse_iso_date(data.get("selected_date"))
    date_source = data.get("date_source") or "документа"

    lines = [f"📚 Класс: <b>{class_name}</b>"]
    if data.get("extra_only_upload"):
        lines = [
            f"📚 Основной класс: <b>{class_name}</b>",
            "➕ Режим: только доп. класс (добавим к уже записанному расписанию)",
        ]
    if subgroup:
        lines.append(f"Подгруппа: {subgroup}")
    extra_classes = data.get("extra_classes") or []
    if extra_classes:
        lines.append(f"➕ Доп. классы: {', '.join(extra_classes)}")

    if detected:
        dow = WEEKDAY_RU[day_of_week_from_date(detected)]
        lines.append(
            f"\n📅 Дата из {date_source}: "
            f"<b>{detected.strftime('%d.%m.%Y')}</b> ({dow})"
        )
        lines.append("Она выделена как ·день·. Можно выбрать другую.")
    else:
        lines.append("\n📅 Дата не найдена.")
        lines.append("Выбери дату расписания на календаре.")

    if selected:
        dow = WEEKDAY_RU[day_of_week_from_date(selected)]
        lines.append(f"\nВыбрано: <b>{selected.strftime('%d.%m.%Y')}</b> ({dow})")
    else:
        lines.append("\nВыбери день, затем нажми «Подтвердить».")

    lines.append("\n<i>●день</i> — выбранный ·день· — из подписи/документа")
    return "\n".join(lines)


def _calendar_markup(data: dict, year: int | None = None, month: int | None = None):
    detected = parse_iso_date(data.get("detected_date"))
    selected = parse_iso_date(data.get("selected_date"))
    anchor = selected or detected or date.today()
    y = year if year is not None else anchor.year
    m = month if month is not None else anchor.month
    return calendar_keyboard(
        y,
        m,
        selected=selected,
        detected=detected,
        show_extra_button=bool(data.get("extra_classes_enabled")),
    )


async def _show_calendar(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.set_state(ScheduleStates.waiting_for_date)
    await callback.message.edit_text(
        _calendar_text(data),
        parse_mode="HTML",
        reply_markup=_calendar_markup(data),
    )


async def _show_calendar_message(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.set_state(ScheduleStates.waiting_for_date)
    await message.edit_text(
        _calendar_text(data),
        parse_mode="HTML",
        reply_markup=_calendar_markup(data),
    )


@router.callback_query(F.data == "cal:noop")
async def handle_cal_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("cal:nav:"))
async def handle_cal_nav(callback: CallbackQuery, state: FSMContext):
    data = await _ensure_session(callback, state)
    if data is None:
        return

    _, _, year_s, month_s, delta_s = callback.data.split(":")
    year, month = shift_month(int(year_s), int(month_s), int(delta_s))
    await callback.message.edit_reply_markup(
        reply_markup=_calendar_markup(data, year=year, month=month)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("cal:day:"))
async def handle_cal_day(callback: CallbackQuery, state: FSMContext):
    data = await _ensure_session(callback, state)
    if data is None:
        return

    _, _, year_s, month_s, day_s = callback.data.split(":")
    selected = date(int(year_s), int(month_s), int(day_s))
    await state.update_data(selected_date=selected.isoformat())
    data["selected_date"] = selected.isoformat()

    await callback.message.edit_text(
        _calendar_text(data),
        parse_mode="HTML",
        reply_markup=_calendar_markup(data, year=selected.year, month=selected.month),
    )
    await callback.answer(selected.strftime("%d.%m.%Y"))


@router.callback_query(F.data == "cal:change_class")
async def handle_cal_change_class(callback: CallbackQuery, state: FSMContext):
    data = await _ensure_session(callback, state)
    if data is None:
        return

    await state.update_data(selected_class=None, selected_subgroup=None, extra_classes=[])
    await state.set_state(ScheduleStates.waiting_for_class)

    text = f"✅ Найдено классов: {len(data.get('classes', []))}\n"
    detected = parse_iso_date(data.get("detected_date"))
    if detected:
        dow = WEEKDAY_RU[day_of_week_from_date(detected)]
        date_source = data.get("date_source") or "документа"
        text += (
            f"📅 Дата из {date_source}: "
            f"<b>{detected.strftime('%d.%m.%Y')}</b> ({dow})\n"
        )
    if data.get("saved_class"):
        text += f"📌 Сохранённый класс: <b>{data['saved_class']}</b>\n"
    text += "\nВыбери свой класс:"

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=_class_keyboard_from_state(data),
    )
    await callback.answer()


@router.callback_query(F.data == "cal:extra_classes")
async def handle_cal_extra_classes(callback: CallbackQuery, state: FSMContext):
    data = await _ensure_session(callback, state)
    if data is None:
        return
    if not data.get("extra_classes_enabled"):
        await callback.answer("Доп. классы выключены в настройках", show_alert=True)
        return
    await callback.answer()
    await _show_extra_classes(callback, state)


@router.callback_query(F.data == "cal:confirm")
async def handle_cal_confirm(callback: CallbackQuery, state: FSMContext, settings: Settings, db: Database):
    data = await _ensure_session(callback, state)
    if data is None:
        return

    selected = parse_iso_date(data.get("selected_date"))
    if not selected:
        await callback.answer("Сначала выбери дату на календаре", show_alert=True)
        return

    class_name = data.get("selected_class")
    if not class_name:
        await callback.answer("Класс не выбран — отправь файл заново", show_alert=True)
        await state.clear()
        return

    subgroup = data.get("selected_subgroup")
    await callback.answer()
    await _parse_and_preview(callback, state, settings, db, class_name, subgroup, selected)


async def _parse_and_preview(
    callback: CallbackQuery,
    state: FSMContext,
    settings: Settings,
    db: Database,
    class_name: str,
    subgroup: int | None,
    schedule_date: date,
):
    data = await state.get_data()
    image_bytes = data["image_bytes"]
    remember = bool(data.get("remember_class", True))
    dow = day_of_week_from_date(schedule_date)

    await callback.message.edit_text(
        f"⏳ Распознаю расписание на {schedule_date.strftime('%d.%m.%Y')} "
        f"({WEEKDAY_RU[dow]})..."
    )

    try:
        llm = create_llm_provider(settings)
        extra_only = bool(data.get("extra_only_upload"))
        extra_class_names: list[str] = list(data.get("extra_classes") or [])

        if extra_only:
            # Основной класс уже в календаре — из файла парсим только доп. классы
            schedule = Schedule(
                class_name=class_name,
                date=schedule_date.isoformat(),
                schedule=[],
            )
        else:
            schedule = await llm.parse_schedule(
                image_bytes,
                class_name,
                schedule_date,
                dow.value,
            )
            schedule = normalize_paired_lessons(schedule)
            schedule.date = schedule_date.isoformat()

            # Есть спаренные уроки, а подгруппу не выбрали — спросим перед именованием
            if has_paired_lessons(schedule) and subgroup is None:
                await state.update_data(
                    schedule_json=schedule.model_dump(mode="json"),
                    extra_schedules_json={},
                    pending_subgroup_resolve=True,
                    selected_subgroup=None,
                    remember_class=remember,
                )
                # extras ещё не спарсили — сохраним флаг и сначала спросим подгруппу,
                # потом повторим полный parse с подгруппой через parse:retry-подобное
                await state.set_state(ScheduleStates.waiting_for_subgroup)
                await callback.message.edit_text(
                    f"📚 Класс: <b>{class_name}</b>\n\n"
                    "В расписании есть спаренные уроки (через «/»).\n"
                    "Укажи <b>свою подгруппу</b> — иначе предметы останутся склеенными.",
                    parse_mode="HTML",
                    reply_markup=subgroup_keyboard(None),
                )
                return

            schedule = apply_subgroup(schedule, subgroup)

        extra_schedules: dict[str, Schedule] = {}
        for i, extra_name in enumerate(extra_class_names):
            label = (
                f"⏳ Распознаю доп. класс <b>{extra_name}</b> "
                f"({i + 1}/{len(extra_class_names)})…"
                if extra_only
                else (
                    f"⏳ Распознаю расписание на {schedule_date.strftime('%d.%m.%Y')} "
                    f"({WEEKDAY_RU[dow]})…\n"
                    f"Доп. класс <b>{extra_name}</b> ({i + 1}/{len(extra_class_names)})"
                )
            )
            await callback.message.edit_text(label, parse_mode="HTML")
            extra_schedule = await llm.parse_schedule(
                image_bytes,
                extra_name,
                schedule_date,
                dow.value,
            )
            # доп. классы — без подгрупп: оставляем оба предмета A/B
            extra_schedule = normalize_paired_lessons(extra_schedule)
            extra_schedule.date = schedule_date.isoformat()
            extra_schedules[extra_name] = extra_schedule

        if remember and not extra_only:
            await db.save_user_settings(callback.from_user.id, class_name, subgroup)

        await state.update_data(
            schedule_json=schedule.model_dump(mode="json"),
            extra_schedules_json={
                name: s.model_dump(mode="json") for name, s in extra_schedules.items()
            },
            selected_subgroup=subgroup,
            session_aliases={},
            pending_subgroup_resolve=False,
            _user_id=callback.from_user.id,
        )

        prefs = await db.get_calendar_prefs(callback.from_user.id)
        if prefs.custom_naming:
            aliases = await db.get_lesson_aliases(callback.from_user.id)
            pending: list[str] = []
            seen_subjects: set[str] = set()

            # основной — уже с разрезанной подгруппой
            for subj in subjects_from_schedule(schedule, expand_pairs=False):
                if subj.casefold() not in {k.casefold() for k in aliases}:
                    key = subj.casefold()
                    if key not in seen_subjects:
                        seen_subjects.add(key)
                        pending.append(subj)

            # доп. классы — без подгрупп: именуем каждый предмет из пары отдельно
            for sched in extra_schedules.values():
                for subj in subjects_from_schedule(sched, expand_pairs=True):
                    if subj.casefold() in {k.casefold() for k in aliases}:
                        continue
                    key = subj.casefold()
                    if key not in seen_subjects:
                        seen_subjects.add(key)
                        pending.append(subj)

            if pending:
                await state.update_data(naming_queue=pending)
                await state.set_state(ScheduleStates.naming_lessons)
                await _ask_next_name(
                    callback.message, state, db, callback.from_user.id, edit=True
                )
                return

        await _show_review(callback.message, state, db, callback.from_user.id, edit=True)
    except Exception as exc:
        logger.exception("Ошибка парсинга расписания")
        detail = str(exc)
        # сессию не чистим — можно повторить без повторной загрузки файла
        await state.set_state(ScheduleStates.waiting_for_date)
        if "исчерпали квоту" in detail.casefold() or "RESOURCE_EXHAUSTED" in detail:
            text = (
                "❌ Лимит LLM исчерпан на всех ключах пула.\n"
                "Подожди сброса или нажми «Повторить»."
            )
        elif "validation error" in detail.casefold() or "Field required" in detail:
            text = (
                "❌ LLM вернул кривой JSON (нет списка уроков).\n"
                "Нажми «Повторить» — попробуем другой слот."
            )
        else:
            short = detail if len(detail) <= 280 else detail[:277] + "…"
            text = f"❌ Ошибка при распознавании расписания.\n<code>{_esc_html(short)}</code>"

        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=parse_retry_keyboard(),
        )


def _esc_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


@router.callback_query(F.data == "parse:retry")
async def handle_parse_retry(
    callback: CallbackQuery, state: FSMContext, settings: Settings, db: Database
):
    data = await state.get_data()
    if not data.get("image_bytes") or not data.get("selected_class"):
        await callback.answer("Сессия истекла — отправь файл заново", show_alert=True)
        await state.clear()
        return

    selected = parse_iso_date(data.get("selected_date"))
    if not selected:
        await callback.answer("Сначала выбери дату", show_alert=True)
        await _show_calendar(callback, state)
        return

    await callback.answer("Повторяю…")
    await _parse_and_preview(
        callback,
        state,
        settings,
        db,
        data["selected_class"],
        data.get("selected_subgroup"),
        selected,
    )


@router.callback_query(F.data == "parse:change_date")
async def handle_parse_change_date(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("image_bytes"):
        await callback.answer("Сессия истекла — отправь файл заново", show_alert=True)
        await state.clear()
        return
    await callback.answer()
    await _show_calendar(callback, state)

async def _show_review(
    message: Message,
    state: FSMContext,
    db: Database,
    user_id: int,
    *,
    edit: bool = False,
):
    schedule = await _load_schedule(state)
    if not schedule:
        await state.clear()
        text = "Сессия истекла — отправь файл заново."
        if edit:
            await message.edit_text(text)
        else:
            await message.answer(text)
        return

    data = await state.get_data()
    bells = await db.get_bells(user_id)
    extra_schedules = await _load_extra_schedules(state)
    schedule_date = parse_iso_date(schedule.date) or parse_iso_date(data.get("selected_date"))
    weekday_ru = WEEKDAY_RU[day_of_week_from_date(schedule_date)] if schedule_date else None
    await state.update_data(_user_id=user_id)
    await state.set_state(ScheduleStates.waiting_for_review)
    text = format_schedule_preview(
        schedule,
        bells=bells,
        weekday_ru=weekday_ru,
        subgroup=data.get("selected_subgroup"),
        extra_schedules=extra_schedules or None,
    )
    markup = preview_keyboard(schedule)
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=markup)


async def _ask_next_name(
    message: Message,
    state: FSMContext,
    db: Database,
    user_id: int,
    *,
    edit: bool = False,
):
    data = await state.get_data()
    queue: list[str] = list(data.get("naming_queue") or [])
    if not queue:
        await _show_review(message, state, db, user_id, edit=edit)
        return

    subject = queue[0]
    aliases = await db.get_lesson_aliases(user_id)
    session_aliases = dict(data.get("session_aliases") or {})
    merged = {**aliases, **session_aliases}

    suggestions = suggest_aliases(subject, merged)
    known = sorted({v for v in merged.values()}, key=str.casefold)
    await state.update_data(
        naming_suggestions=[list(p) for p in suggestions],
        naming_known=known,
        naming_current=subject,
        _user_id=user_id,
    )

    left = len(queue)
    text = (
        f"🏷 Кастомное имя предмета ({left} осталось)\n\n"
        f"В расписании: <b>{subject}</b>\n\n"
        "Как назвать для шаблона <code>{lesson}</code>?\n"
        "Можно выбрать кнопку или написать текст (например <code>inf</code>)."
    )
    markup = naming_keyboard(suggestions, known_aliases=known)
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=markup)


async def _consume_name(
    *,
    message: Message,
    state: FSMContext,
    db: Database,
    user_id: int,
    alias: str,
    edit: bool,
):
    data = await state.get_data()
    queue: list[str] = list(data.get("naming_queue") or [])
    subject = data.get("naming_current") or (queue[0] if queue else None)
    if not subject:
        await state.clear()
        await message.answer("Сессия именования сброшена.")
        return

    await db.set_lesson_alias(user_id, subject, alias)
    session_aliases = dict(data.get("session_aliases") or {})
    session_aliases[subject] = alias
    queue = queue[1:]
    await state.update_data(naming_queue=queue, session_aliases=session_aliases)

    if queue:
        await _ask_next_name(message, state, db, user_id, edit=edit)
    else:
        await _show_review(message, state, db, user_id, edit=edit)


async def _load_extra_schedules(state: FSMContext) -> dict[str, Schedule]:
    data = await state.get_data()
    raw = data.get("extra_schedules_json") or {}
    return {name: Schedule.model_validate(payload) for name, payload in raw.items()}


async def _load_schedule(state: FSMContext) -> Schedule | None:
    data = await state.get_data()
    raw = data.get("schedule_json")
    if not raw:
        return None
    return Schedule.model_validate(raw)


async def _render_preview(callback: CallbackQuery, state: FSMContext, db: Database, schedule: Schedule):
    await state.update_data(schedule_json=schedule.model_dump(mode="json"))
    await _show_review(callback.message, state, db, callback.from_user.id, edit=True)


@router.callback_query(F.data == "name:keep", ScheduleStates.naming_lessons)
async def naming_keep(callback: CallbackQuery, state: FSMContext, db: Database):
    data = await state.get_data()
    subject = data.get("naming_current")
    if not subject:
        await callback.answer("Нет предмета", show_alert=True)
        return
    await callback.answer()
    await _consume_name(
        message=callback.message,
        state=state,
        db=db,
        user_id=callback.from_user.id,
        alias=subject,
        edit=True,
    )


@router.callback_query(F.data.startswith("name:sug:"), ScheduleStates.naming_lessons)
async def naming_suggestion(callback: CallbackQuery, state: FSMContext, db: Database):
    data = await state.get_data()
    suggestions = data.get("naming_suggestions") or []
    idx = int(callback.data.split(":")[2])
    if idx < 0 or idx >= len(suggestions):
        await callback.answer("Вариант устарел", show_alert=True)
        return
    alias = suggestions[idx][1]
    await callback.answer(alias)
    await _consume_name(
        message=callback.message,
        state=state,
        db=db,
        user_id=callback.from_user.id,
        alias=alias,
        edit=True,
    )


@router.callback_query(F.data.startswith("name:pick:"), ScheduleStates.naming_lessons)
async def naming_pick_known(callback: CallbackQuery, state: FSMContext, db: Database):
    data = await state.get_data()
    known = data.get("naming_known") or []
    idx = int(callback.data.split(":")[2])
    if idx < 0 or idx >= len(known):
        await callback.answer("Вариант устарел", show_alert=True)
        return
    alias = known[idx]
    await callback.answer(alias)
    await _consume_name(
        message=callback.message,
        state=state,
        db=db,
        user_id=callback.from_user.id,
        alias=alias,
        edit=True,
    )


@router.message(ScheduleStates.naming_lessons, F.text)
async def naming_text(message: Message, state: FSMContext, db: Database):
    alias = (message.text or "").strip()
    if not alias:
        await message.answer("Пришли короткое имя, например: <code>inf</code>")
        return
    await _consume_name(
        message=message,
        state=state,
        db=db,
        user_id=message.from_user.id,
        alias=alias,
        edit=False,
    )


@router.callback_query(F.data == "preview:back")
async def preview_back(callback: CallbackQuery, state: FSMContext, db: Database):
    schedule = await _load_schedule(state)
    if not schedule:
        await callback.answer("Сессия истекла", show_alert=True)
        await state.clear()
        return
    await callback.answer()
    await _render_preview(callback, state, db, schedule)


@router.callback_query(F.data == "preview:cancel")
async def preview_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Расписание отменено. Можешь отправить файл заново.")
    await callback.answer()


@router.callback_query(F.data == "preview:confirm")
async def preview_confirm(callback: CallbackQuery, state: FSMContext, db: Database):
    schedule = await _load_schedule(state)
    if not schedule:
        await callback.answer("Сессия истекла", show_alert=True)
        await state.clear()
        return

    data = await state.get_data()
    extra_schedules = await _load_extra_schedules(state)
    bells = await db.get_bells(callback.from_user.id)
    prefs = await db.get_calendar_prefs(callback.from_user.id)
    aliases = await db.get_lesson_aliases(callback.from_user.id)
    aliases = {**aliases, **dict(data.get("session_aliases") or {})}

    schedule_date = parse_iso_date(schedule.date) or parse_iso_date(data.get("selected_date"))
    weekday_ru = WEEKDAY_RU[day_of_week_from_date(schedule_date)] if schedule_date else None

    creds = await db.get_caldav_credentials(callback.from_user.id)
    if not creds or not creds.is_complete:
        preview = format_calendar_events(
            schedule,
            template=prefs.title_template,
            aliases=aliases,
            bells=bells,
            weekday_ru=weekday_ru,
            extra_schedules=extra_schedules or None,
        )
        await callback.message.edit_text(
            preview
            + "\n\n❌ <b>В календарь не записано</b>\n"
            "Причина: не настроен CalDAV (нужны URL, логин и пароль).\n"
            "Открой /settings → 📅 CalDAV / SOGo.",
            parse_mode="HTML",
        )
        await callback.answer("Нужен CalDAV", show_alert=True)
        await state.clear()
        return

    await callback.answer()
    await callback.message.edit_text("⏳ Записываю события в календарь SOGo…")

    result = await asyncio.to_thread(
        sync_schedule_to_caldav,
        creds,
        schedule,
        user_id=callback.from_user.id,
        template=prefs.title_template,
        aliases=aliases,
        bells=bells,
        extra_schedules=extra_schedules or None,
    )

    display_schedule = result.merged_schedule or schedule
    preview = format_calendar_events(
        display_schedule,
        template=prefs.title_template,
        aliases=aliases,
        bells=bells,
        weekday_ru=weekday_ru,
        extra_schedules=extra_schedules or None,
    )

    if result.ok:
        try:
            await db.save_day_schedule(
                callback.from_user.id,
                schedule_date=display_schedule.date or schedule.date or "",
                class_name=display_schedule.class_name,
                schedule_json=json.dumps(
                    display_schedule.model_dump(mode="json"), ensure_ascii=False
                ),
                extra_schedules_json=json.dumps(
                    {
                        name: s.model_dump(mode="json")
                        for name, s in (extra_schedules or {}).items()
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception:
            logger.exception("Failed to cache day schedule")
        cal_hint = f"\nКалендарь: <code>{result.calendar_url}</code>" if result.calendar_url else ""
        status = (
            f"\n\n✅ <b>Записано в календарь</b>\n"
            f"Создано: {result.created}, заменено за этот день: {result.deleted}"
            f"{cal_hint}"
        )
    else:
        status = (
            f"\n\n❌ <b>Не записано</b>\n"
            f"Причина: <code>{result.error}</code>"
        )

    await callback.message.edit_text(preview + status, parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data == "preview:delete_menu")
async def preview_delete_menu(callback: CallbackQuery, state: FSMContext):
    schedule = await _load_schedule(state)
    if not schedule:
        await callback.answer("Сессия истекла", show_alert=True)
        await state.clear()
        return
    await callback.message.edit_reply_markup(reply_markup=preview_delete_keyboard(schedule))
    await callback.answer("Выбери урок для удаления")


@router.callback_query(F.data.startswith("preview:del:"))
async def preview_delete_lesson(callback: CallbackQuery, state: FSMContext, db: Database):
    schedule = await _load_schedule(state)
    if not schedule:
        await callback.answer("Сессия истекла", show_alert=True)
        await state.clear()
        return

    lesson_no = int(callback.data.split(":")[2])
    schedule.schedule = [l for l in schedule.schedule if l.lesson_number != lesson_no]
    await callback.answer(f"Урок {lesson_no} удалён")
    await _render_preview(callback, state, db, schedule)


@router.callback_query(F.data.startswith("preview:edit:"))
async def preview_edit_start(callback: CallbackQuery, state: FSMContext):
    schedule = await _load_schedule(state)
    if not schedule:
        await callback.answer("Сессия истекла", show_alert=True)
        await state.clear()
        return

    lesson_no = int(callback.data.split(":")[2])
    lesson = next((l for l in schedule.schedule if l.lesson_number == lesson_no), None)
    if not lesson:
        await callback.answer("Урок не найден", show_alert=True)
        return

    await state.set_state(ScheduleStates.editing_lesson)
    await state.update_data(editing_lesson_number=lesson_no)

    example = lesson.subject
    if lesson.room:
        example += f" | {lesson.room}"

    await callback.message.edit_text(
        f"✏️ Правка урока <b>{lesson_no}</b>\n\n"
        f"Сейчас: <code>{example}</code>\n\n"
        "Пришли новую строку в формате:\n"
        "<code>Предмет | кабинет</code>\n"
        "Кабинет необязателен.\n\n"
        "Пример: <code>Физика | 103</code>",
        parse_mode="HTML",
        reply_markup=preview_edit_cancel_keyboard(),
    )
    await callback.answer()


@router.message(ScheduleStates.editing_lesson, F.text)
async def preview_edit_save(message: Message, state: FSMContext, db: Database):
    data = await state.get_data()
    lesson_no = data.get("editing_lesson_number")
    schedule = await _load_schedule(state)
    if not lesson_no or not schedule:
        await state.clear()
        await message.answer("Сессия истекла — отправь файл заново.")
        return

    parts = [p.strip() for p in (message.text or "").split("|")]
    if not parts or not parts[0]:
        await message.answer("Нужен хотя бы предмет. Пример: <code>Математика | 301</code>")
        return

    subject = parts[0]
    room = parts[1] if len(parts) > 1 and parts[1] else None

    updated = False
    new_lessons = []
    for lesson in schedule.schedule:
        if lesson.lesson_number == lesson_no:
            new_lessons.append(
                lesson.model_copy(update={"subject": subject, "room": room})
            )
            updated = True
        else:
            new_lessons.append(lesson)

    if not updated:
        await message.answer("Урок не найден в черновике.")
        return

    schedule.schedule = new_lessons
    await state.update_data(schedule_json=schedule.model_dump(mode="json"))
    await _show_review(message, state, db, message.from_user.id, edit=False)
