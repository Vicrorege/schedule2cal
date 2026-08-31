# Schedule2Cal

Telegram-бот: распознаёт школьное расписание из PDF/фото (мультимодальный LLM) и записывает уроки в календарь **SOGo / Mailcow** по CalDAV.

Лицензия: [MIT](LICENSE)

## Возможности

- PDF / PNG / JPG → структурированное расписание (Gemini или OpenAI)
- Выбор класса и подгруппы, автоподстановка сохранённых настроек
- Дата из документа или подписи к файлу + календарь подтверждения
- Сетка звонков (уроки 1–9)
- Шаблоны названий событий (`sch {lesson}|[color=red]`) и словарь кастомных имён
- Запись разовых событий в CalDAV (на выбранную дату, без повторений)
- Whitelist пользователей, пароль CalDAV не светится в чате и стирается после записи

## Требования

- Python 3.11+ **или** Docker
- `poppler-utils` (для PDF)
- Telegram Bot Token
- API-ключ Gemini или OpenAI
- Доступ к SOGo CalDAV (по желанию, для записи в календарь)

## Быстрый старт

### 1. Клонирование и окружение

```bash
git clone <repo-url> schedule2cal
cd schedule2cal
cp .env.example .env
```

Заполни в `.env`:

| Переменная | Описание |
|---|---|
| `BOT_TOKEN` | токен от [@BotFather](https://t.me/BotFather) |
| `ALLOWED_USERS` | Telegram user id через запятую |
| `LLM_PROVIDER` | `gemini` или `openai` |
| `LLM_API_KEY` | ключ API |
| `PROXY` | опционально, SOCKS5 (например `socks5://127.0.0.1:10808`) |

### 2. Локально

```bash
# Debian/Ubuntu
sudo apt install poppler-utils

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m bot.main
```

### 3. Docker

```bash
docker compose up --build
```

По умолчанию `network_mode: host` — удобно, если SOCKS5-прокси слушает только `127.0.0.1` (Happ/Clash).

## Команды бота

| Команда | Описание |
|---|---|
| `/start` | Главное меню |
| `/help` | Инструкция |
| `/set_schedule` | Загрузить расписание |
| `/settings` | Звонки, шаблон, имена, CalDAV |
| `/menu` | Показать кнопки меню |

Или просто пришли PDF/фото в чат.

## Подпись к файлу

- Дата: `03.09.2026` / `03.09.26` / `03.09`
- Ручной выбор класса: `!`, `manual`, `ручной`
- Без пометки — используется сохранённый класс

## CalDAV (SOGo)

URL обычно такой:

```text
https://mail.example.com/SOGo/dav/user@example.com/Calendar/<calendar-id>/
```

В боте: **Настройки → CalDAV / SOGo**.  
После «Проверить подключение» бот может подставить рабочий URL календаря (часто это не `personal`).

## Структура проекта

```text
bot/           # aiogram: handlers, FSM, keyboards
db/            # SQLite (настройки пользователя, звонки, алиасы)
models/        # Pydantic-схемы расписания
services/      # PDF, LLM, CalDAV, шаблоны
```

Данные бота: `data/schedule2cal.db` (в `.gitignore`).  
Секреты: только `.env` (тоже в `.gitignore`).

## Безопасность

- Не коммить `.env` и `data/`
- Ограничь `ALLOWED_USERS`
- Пароль CalDAV удаляется из чата и очищается из БД после записи в календарь

## License

MIT — см. [LICENSE](LICENSE).
