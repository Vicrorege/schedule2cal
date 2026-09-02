import calendar
from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.keyboards.calendar import MONTH_NAMES_RU, WEEKDAY_HEADERS, shift_month


def view_calendar_keyboard(
    year: int,
    month: int,
    *,
    selected: date | None = None,
    days_with_events: set[date] | None = None,
) -> InlineKeyboardMarkup:
    """Календарь просмотра расписания (без кнопки подтверждения загрузки)."""
    builder = InlineKeyboardBuilder()
    days_with_events = days_with_events or set()

    title = f"{MONTH_NAMES_RU[month]} {year}"
    builder.row(
        InlineKeyboardButton(text="◀️", callback_data=f"view:nav:{year}:{month}:-1"),
        InlineKeyboardButton(text=title, callback_data="view:noop"),
        InlineKeyboardButton(text="▶️", callback_data=f"view:nav:{year}:{month}:1"),
    )
    builder.row(
        *[InlineKeyboardButton(text=h, callback_data="view:noop") for h in WEEKDAY_HEADERS]
    )

    weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
    for week in weeks:
        row: list[InlineKeyboardButton] = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="view:noop"))
                continue
            current = date(year, month, day)
            label = str(day)
            if selected and current == selected:
                label = f"●{day}"
            elif current in days_with_events:
                label = f"·{day}·"
            row.append(
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"view:day:{year}:{month}:{day}",
                )
            )
        builder.row(*row)

    builder.row(
        InlineKeyboardButton(text="📅 Сегодня", callback_data="view:today"),
        InlineKeyboardButton(text="Закрыть", callback_data="view:close"),
    )
    return builder.as_markup()


def view_day_keyboard(selected: date, *, can_write: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if can_write:
        builder.row(
            InlineKeyboardButton(
                text="📤 Записать в SOGo",
                callback_data=f"view:write:{selected.year}:{selected.month}:{selected.day}",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="◀️ К календарю",
            callback_data=f"view:month:{selected.year}:{selected.month}",
        )
    )
    builder.row(InlineKeyboardButton(text="Закрыть", callback_data="view:close"))
    return builder.as_markup()


__all__ = ["view_calendar_keyboard", "view_day_keyboard", "shift_month"]
