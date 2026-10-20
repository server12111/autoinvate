from __future__ import annotations

import contextlib

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


async def show(event: Message | CallbackQuery, state: FSMContext, text: str,
               markup: InlineKeyboardMarkup) -> None:
    """Показывает экран «в одном сообщении»: из колбэка редактирует его, из /команды шлёт новое."""
    if isinstance(event, CallbackQuery):
        msg = event.message
        if isinstance(msg, Message):
            mid = msg.message_id
            try:
                await msg.edit_text(text, reply_markup=markup)
            except TelegramBadRequest as e:
                if "not modified" not in str(e):  # сообщение слишком старое / не редактируется
                    mid = (await msg.answer(text, reply_markup=markup)).message_id
        else:  # InaccessibleMessage (удалено / слишком старое) — редактировать нечего
            mid = (await event.bot.send_message(event.from_user.id, text, reply_markup=markup)).message_id
        await state.update_data(menu_mid=mid)
        with contextlib.suppress(TelegramBadRequest):
            await event.answer()
    else:
        msg = await event.answer(text, reply_markup=markup)
        await state.update_data(menu_mid=msg.message_id)


async def edit_menu(bot: Bot, state: FSMContext, chat_id: int, text: str,
                    markup: InlineKeyboardMarkup) -> None:
    """Правит сохранённое меню-сообщение (когда пользователь ответил текстом/файлом)."""
    mid = (await state.get_data()).get("menu_mid")
    if mid:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=mid, reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
    msg = await bot.send_message(chat_id, text, reply_markup=markup)
    await state.update_data(menu_mid=msg.message_id)


async def reset_state(state: FSMContext) -> None:
    """Сбрасывает FSM, сохраняя id меню-сообщения."""
    mid = (await state.get_data()).get("menu_mid")
    await state.clear()
    if mid:
        await state.update_data(menu_mid=mid)


async def safe_delete(message: Message) -> None:
    with contextlib.suppress(Exception):
        await message.delete()
