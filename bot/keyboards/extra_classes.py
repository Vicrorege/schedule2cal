from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def extra_classes_keyboard(
    classes: list[str],
    main_class: str,
    selected: list[str],
    page: int = 0,
    per_page: int = 8,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    candidates = [c for c in classes if c != main_class]

    start = page * per_page
    end = start + per_page
    page_items = candidates[start:end]

    for i, cls in enumerate(page_items):
        global_idx = start + i
        mark = "✓ " if cls in selected else ""
        builder.button(text=f"{mark}{cls}", callback_data=f"extra:toggle:{global_idx}")

    nav_buttons = []
    if page > 0:
        nav_buttons.append(
            InlineKeyboardButton(text="◀️", callback_data=f"extra:page:{page - 1}")
        )
    if end < len(candidates):
        nav_buttons.append(
            InlineKeyboardButton(text="▶️", callback_data=f"extra:page:{page + 1}")
        )
    if nav_buttons:
        builder.row(*nav_buttons)

    selected_label = f"Готово ({len(selected)} доп.)" if selected else "Пропустить"
    builder.button(text=selected_label, callback_data="extra:done")
    builder.adjust(1)
    return builder.as_markup()
