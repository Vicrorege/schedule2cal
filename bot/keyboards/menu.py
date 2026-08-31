from aiogram.types import BotCommand, KeyboardButton, ReplyKeyboardMarkup


BOT_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="help", description="Инструкция по использованию"),
    BotCommand(command="set_schedule", description="Загрузить расписание (PDF/фото)"),
    BotCommand(command="settings", description="Настройки: звонки, шаблон, CalDAV"),
    BotCommand(command="menu", description="Показать меню кнопок"),
]


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📎 Загрузить расписание")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="📖 Инструкция")],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="PDF, фото или команда…",
    )
