from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def homework_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Записать ДЗ", callback_data="hw:confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="hw:cancel"),
    )
    return builder.as_markup()
