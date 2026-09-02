import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import get_settings
from bot.handlers import (
    common,
    fallback,
    homework,
    schedule,
    settings as settings_handlers,
    view_calendar,
)from bot.keyboards.menu import BOT_COMMANDS
from bot.middlewares.auth import AuthMiddleware
from bot.middlewares.database import DatabaseMiddleware
from bot.middlewares.settings import SettingsMiddleware
from db.database import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def create_bot(settings) -> Bot:
    session = None
    if settings.proxy:
        session = AiohttpSession(proxy=settings.proxy)
        logger.info("Proxy: %s", settings.proxy)

    return Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def main():
    settings = get_settings()
    database = Database(settings.database_path)
    await database.init()

    bot = create_bot(settings)
    dp = Dispatcher(storage=MemoryStorage())

    settings_mw = SettingsMiddleware(settings)
    auth_mw = AuthMiddleware(settings)
    db_mw = DatabaseMiddleware(database)

    for observer in (dp.message, dp.callback_query):
        observer.middleware(settings_mw)
        observer.middleware(auth_mw)
        observer.middleware(db_mw)

    dp["settings"] = settings
    dp["db"] = database
    # common → settings → schedule → calendar view → homework → fallback
    dp.include_router(common.router)
    dp.include_router(settings_handlers.router)
    dp.include_router(schedule.router)
    dp.include_router(view_calendar.router)
    dp.include_router(homework.router)
    dp.include_router(fallback.router)

    await bot.set_my_commands(BOT_COMMANDS)

    logger.info(
        "Бот запущен (LLM: %s, whitelist: %s)",
        settings.llm_provider,
        sorted(settings.allowed_user_ids) or "выкл",
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
