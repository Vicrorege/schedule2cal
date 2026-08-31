from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from models.schedule import Schedule


def preview_keyboard(schedule: Schedule) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    lessons = sorted(schedule.schedule, key=lambda x: x.lesson_number)

    for lesson in lessons:
        label = f"✏️ {lesson.lesson_number}. {lesson.subject}"
        if len(label) > 60:
            label = label[:57] + "…"
        builder.button(
            text=label,
            callback_data=f"preview:edit:{lesson.lesson_number}",
        )

    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="🗑 Удалить урок", callback_data="preview:delete_menu"),
    )
    builder.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="preview:confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="preview:cancel"),
    )
    return builder.as_markup()


def preview_delete_keyboard(schedule: Schedule) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for lesson in sorted(schedule.schedule, key=lambda x: x.lesson_number):
        builder.button(
            text=f"🗑 {lesson.lesson_number}. {lesson.subject}",
            callback_data=f"preview:del:{lesson.lesson_number}",
        )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="↩️ Назад", callback_data="preview:back"),
    )
    return builder.as_markup()


def preview_edit_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена правки", callback_data="preview:back")
    return builder.as_markup()
