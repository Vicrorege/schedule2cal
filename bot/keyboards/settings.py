from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db.database import BellPeriod, CalendarPrefs


def settings_keyboard(prefs: CalendarPrefs | None = None) -> InlineKeyboardMarkup:
    naming_on = prefs.custom_naming if prefs else False
    extras_on = prefs.extra_classes_enabled if prefs else False
    builder = InlineKeyboardBuilder()
    builder.button(text="🔔 Сетка звонков", callback_data="settings:bells")
    builder.button(text="🏷 Шаблон названия", callback_data="settings:template")
    builder.button(
        text=("✅ Кастомные имена" if naming_on else "☐ Кастомные имена"),
        callback_data="settings:naming_toggle",
    )
    builder.button(
        text=("✅ Доп. классы" if extras_on else "☐ Доп. классы"),
        callback_data="settings:extra_toggle",
    )
    if extras_on:
        builder.button(text="➕ Управление доп. классами", callback_data="settings:extra_classes")
    builder.button(text="📖 Словарь имён", callback_data="settings:aliases")
    builder.button(text="📅 CalDAV / SOGo", callback_data="settings:caldav")
    builder.button(text="🗑 Сбросить класс", callback_data="settings:clear")
    builder.adjust(1)
    return builder.as_markup()


def extra_classes_settings_keyboard(
    extra_classes: list[str],
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i, name in enumerate(extra_classes):
        label = name if len(name) <= 50 else name[:47] + "…"
        builder.button(text=f"🗑 {label}", callback_data=f"extra_cfg:del:{i}")
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="➕ Добавить класс", callback_data="extra_cfg:add"),
        InlineKeyboardButton(text="🧹 Очистить", callback_data="extra_cfg:clear"),
    )
    builder.row(InlineKeyboardButton(text="↩️ К настройкам", callback_data="settings:home"))
    return builder.as_markup()


def bells_keyboard(bells: dict[int, BellPeriod]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for n in range(1, 10):
        period = bells[n]
        builder.button(
            text=f"{n}. {period.start}–{period.end}",
            callback_data=f"bell:edit:{n}",
        )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="↩️ К настройкам", callback_data="settings:home"),
        InlineKeyboardButton(text="↺ Дефолт", callback_data="bell:reset"),
    )
    return builder.as_markup()


def bell_edit_cancel_keyboard(lesson_number: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="bell:cancel")
    return builder.as_markup()


def template_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить шаблон", callback_data="settings:template_edit")
    builder.button(text="↺ Дефолт {lesson}", callback_data="settings:template_reset")
    builder.button(text="↩️ К настройкам", callback_data="settings:home")
    builder.adjust(1)
    return builder.as_markup()


def aliases_keyboard(aliases: dict[str, str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for i, (source, alias) in enumerate(sorted(aliases.items(), key=lambda x: x[0].casefold())):
        label = f"{source} → {alias}"
        if len(label) > 60:
            label = label[:57] + "…"
        builder.button(text=f"🗑 {label}", callback_data=f"alias:del:{i}")
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text="🧹 Очистить всё", callback_data="alias:clear"),
        InlineKeyboardButton(text="↩️ Назад", callback_data="settings:home"),
    )
    return builder.as_markup()


def naming_keyboard(
    suggestions: list[tuple[str, str]],
    *,
    known_aliases: list[str] | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Оставить как есть", callback_data="name:keep")

    for i, (source, alias) in enumerate(suggestions):
        label = f"≈ {alias}"
        if source != alias:
            label = f"≈ {alias} ({source})"
        if len(label) > 60:
            label = label[:57] + "…"
        builder.button(text=label, callback_data=f"name:sug:{i}")

    # готовые короткие имена из словаря (уникальные)
    if known_aliases:
        for i, alias in enumerate(known_aliases[:8]):
            label = f"📌 {alias}"
            if len(label) > 60:
                label = label[:57] + "…"
            builder.button(text=label, callback_data=f"name:pick:{i}")

    builder.adjust(1)
    return builder.as_markup()


def caldav_keyboard(*, has_creds: bool, has_password: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ URL / логин / пароль", callback_data="caldav:setup")
    if has_password:
        builder.button(text="🔑 Сменить только пароль", callback_data="caldav:password")
    if has_creds:
        builder.button(text="🔌 Проверить подключение", callback_data="caldav:test")
        builder.button(text="🗑 Удалить доступ", callback_data="caldav:delete")
    builder.button(text="↩️ К настройкам", callback_data="settings:home")
    builder.adjust(1)
    return builder.as_markup()
