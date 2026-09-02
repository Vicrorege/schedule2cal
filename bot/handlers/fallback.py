from aiogram import Router
from aiogram.types import CallbackQuery

router = Router()


@router.callback_query()
async def handle_unknown_callback(callback: CallbackQuery):
    await callback.answer("Кнопка устарела — отправь файл заново", show_alert=True)
