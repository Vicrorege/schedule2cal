from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def class_selection_keyboard(
    classes: list[str],
    page: int = 0,
    per_page: int = 8,
    saved_class: str | None = None,
    saved_subgroup: int | None = None,
    remember: bool = True,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    remember_label = "✅ Запомнить класс" if remember else "☐ Запомнить класс"
    builder.button(text=remember_label, callback_data="remember:toggle")

    if saved_class:
        label = f"📌 {saved_class}"
        if saved_subgroup:
            label += f", п.г. {saved_subgroup}"
        builder.button(text=label, callback_data="use_saved")

    start = page * per_page
    end = start + per_page
    page_classes = classes[start:end]

    for i, cls in enumerate(page_classes):
        global_idx = start + i
        builder.button(text=cls, callback_data=f"cls:{global_idx}")

    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"cls_page:{page - 1}")
        )
    if end < len(classes):
        nav_buttons.append(
            InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"cls_page:{page + 1}")
        )
    if nav_buttons:
        builder.row(*nav_buttons)

    builder.adjust(1)
    return builder.as_markup()


def subgroup_keyboard(saved_subgroup: int | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"{'✓ ' if saved_subgroup == 1 else ''}Подгруппа 1",
        callback_data="subgroup:1",
    )
    builder.button(
        text=f"{'✓ ' if saved_subgroup == 2 else ''}Подгруппа 2",
        callback_data="subgroup:2",
    )
    builder.button(text="Пропустить", callback_data="subgroup:skip")
    builder.adjust(2, 1)
    return builder.as_markup()
