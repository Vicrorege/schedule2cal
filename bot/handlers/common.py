from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.keyboards.menu import main_menu_keyboard
from bot.states.schedule import ScheduleStates
from db.database import Database

router = Router()

HELP_TEXT = """📖 <b>Инструкция Schedule2Cal</b>

Бот распознаёт школьное расписание из PDF/фото (LLM) и записывает уроки в календарь SOGo по CalDAV.

<b>Быстрый сценарий</b>
1. Настрой CalDAV: /settings → 📅 CalDAV / SOGo
2. Настрой сетку звонков (уроки 1–9)
3. При желании — шаблон названия и кастомные имена предметов
4. Отправь PDF или фото расписания
5. Выбери основной класс, подгруппу и при необходимости <b>доп. классы</b>
6. Проверь превью → ✅ Подтвердить → запись в календарь

<b>Домашка</b>
Просто напиши текст без команды — бот разобьёт на блоки и запишет ДЗ
в описание урока в календаре.
• предмет без даты → ближайший урок этого предмета
• «на четверг» / «на 28.09» → на этот день (если урок есть)
Превью → ✅ Записать ДЗ. Подсказка: /homework

<b>Календарь в боте</b>
📅 Календарь или /calendar — дни с уроками, по клику: расписание и ДЗ.

<b>Доп. классы</b>
В /settings включи галочку <b>Доп. классы</b> и укажи список в «Управление доп. классами».
При загрузке расписания они подставятся автоматически (если есть в файле).
Можно изменить на шаге календаря кнопкой «➕ Доп. классы».
Их уроки попадут в <i>описание</i> событий основного класса.
Если у основного класса окно, а у доп. класса урок — создаётся <b>приватное</b> событие с пометкой <b>🔸 ДОП. КЛАСС</b>.

<b>Подпись к файлу</b>
• Дата: <code>03.09.2026</code> / <code>03.09.26</code> / <code>03.09</code>
• Ручной выбор класса: <code>!</code>, <code>manual</code> или <code>ручной</code>
• Без пометки — подставится сохранённый класс (если есть)

<b>Шаблон названия</b>
Пример: <code>sch {lesson}|[color=red]</code>
Плейсхолдеры: <code>{lesson}</code> <code>{room}</code> <code>{n}</code>

<b>Безопасность</b>
• Сообщения с паролем CalDAV удаляются из чата
• Пароль хранится в локальной SQLite и переживает рестарт (удалить можно в /settings)
• Доступ только пользователям из whitelist (<code>ALLOWED_USERS</code>)
• Текст ДЗ для LLM обёрнут маркерами и не может переопределить системные инструкции

<b>Команды</b>
/start — меню
/help — эта инструкция
/set_schedule — загрузить расписание
/calendar — календарь уроков
/homework — как писать домашку
/settings — настройки
/menu — показать кнопки меню
"""


def _start_text(*, class_name: str | None = None, subgroup: int | None = None) -> str:
    text = (
        "📅 <b>Schedule2Cal</b>\n\n"
        "Распознаю школьное расписание и пишу его в календарь SOGo.\n"
        "Домашку можно прислать обычным текстом — без команды.\n\n"
        "Выбери действие в меню ниже или просто пришли PDF/фото / текст ДЗ."
    )
    if class_name:
        subgroup_text = f", п.г. {subgroup}" if subgroup else ""
        text += f"\n\n📌 Сохранённый класс: <b>{class_name}</b>{subgroup_text}"
    text += "\n\nПодробнее: /help"
    return text


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, db: Database):
    await state.clear()
    saved = await db.get_user_settings(message.from_user.id)
    await message.answer(
        _start_text(
            class_name=saved.class_name if saved else None,
            subgroup=saved.subgroup if saved else None,
        ),
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message):
    await message.answer("Главное меню:", reply_markup=main_menu_keyboard())


@router.message(Command("help"))
@router.message(F.text == "📖 Инструкция")
async def cmd_help(message: Message):
    await message.answer(HELP_TEXT, reply_markup=main_menu_keyboard())


@router.message(F.text == "📎 Загрузить расписание")
async def menu_set_schedule(message: Message, state: FSMContext):
    await state.set_state(ScheduleStates.waiting_for_file)
    await message.answer(
        "📎 Пришли файл расписания (PDF, PNG, JPG).\n"
        "Можно сразу с подписью: дата и/или <code>!</code> для ручного выбора класса."
    )


@router.message(F.text == "⚙️ Настройки")
async def menu_settings(message: Message, state: FSMContext, db: Database):
    # делегируем логике /settings через повторный вызов — импорт ленивый, чтобы избежать циклов
    from bot.handlers.settings import cmd_settings

    await cmd_settings(message, state, db)
