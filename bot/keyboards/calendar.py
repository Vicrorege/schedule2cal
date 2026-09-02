import calendar
from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

WEEKDAY_HEADERS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
MONTH_NAMES_RU = (
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)


def calendar_keyboard(
    year: int,
    month: int,
    *,
    selected: date | None = None,
    detected: date | None = None,
    show_extra_button: bool = False,
) -> InlineKeyboardMarkup:
    """Календарь с заголовками дней недели, выделением найденной и выбранной даты."""
    builder = InlineKeyboardBuilder()

    title = f"{MONTH_NAMES_RU[month]} {year}"
    builder.row(
        InlineKeyboardButton(text="◀️", callback_data=f"cal:nav:{year}:{month}:-1"),
        InlineKeyboardButton(text=title, callback_data="cal:noop"),
        InlineKeyboardButton(text="▶️", callback_data=f"cal:nav:{year}:{month}:1"),
    )

    builder.row(
        *[InlineKeyboardButton(text=h, callback_data="cal:noop") for h in WEEKDAY_HEADERS]
    )

    weeks = calendar.Calendar(firstweekday=0).monthdayscalendar(year, month)
    for week in weeks:
        row: list[InlineKeyboardButton] = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="cal:noop"))
                continue

            current = date(year, month, day)
            label = str(day)
            if selected and current == selected:
                label = f"●{day}"
            elif detected and current == detected:
                label = f"·{day}·"

            row.append(
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"cal:day:{year}:{month}:{day}",
                )
            )
        builder.row(*row)

    confirm_label = "✅ Подтвердить"
    if selected:
        confirm_label = f"✅ Подтвердить {selected.strftime('%d.%m.%Y')}"

    builder.row(
        InlineKeyboardButton(text=confirm_label, callback_data="cal:confirm")
    )
    if show_extra_button:
        builder.row(
            InlineKeyboardButton(text="➕ Доп. классы", callback_data="cal:extra_classes")
        )
    builder.row(
        InlineKeyboardButton(text="📚 Выбрать другой класс", callback_data="cal:change_class")
    )
    return builder.as_markup()


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    month += delta
    while month < 1:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return year, month
