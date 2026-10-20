from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as TgUser

import config
from bot import keyboards as kb
from bot.helpers import edit_menu, reset_state, safe_delete, show
from bot.states import InviteFSM, PromoFSM
from bot.ui import e
from core.queue_manager import QueueManager
from core.telethon_engine import CHAT_LINK_RE, parse_targets
from database import queries as q
from database.models import User, fmt_usd

router = Router(name="user")
commands = Router(name="user_commands")  # подключается первым: /start работает из любого состояния
log = logging.getLogger(__name__)

MAX_FILE_SIZE = 1024 * 1024


async def _user(tg: TgUser) -> User:
    return await q.get_or_create_user(tg.id, tg.username)


def _menu_text(user: User, first_name: str, prefix: str = "") -> str:
    if q.is_active(user):
        sub = f"{e('ok')} Подписка активна до <b>{user.sub_until:%d.%m.%Y}</b>"
    else:
        sub = f"{e('cancel')} Подписка <b>не активна</b>"
    lines = [sub]
    if q.is_active(user):
        lines.append(f"{e('infinity')} Инвайтинг: <b>без ограничений</b>")
    if user.discount:
        lines.append(f"{e('tag')} Скидка на покупку: <b>{fmt_usd(user.discount)}</b>")
    text = (f"{e('wave')} <b>Привет, {html.escape(first_name or 'друг')}!</b>\n\n"
            f"<blockquote>{chr(10).join(lines)}</blockquote>\n\n"
            f"Приглашайте аудиторию в свою группу в пару кликов {e('rocket')}")
    return f"{prefix}\n\n{text}" if prefix else text


async def _main_menu(event: Message | CallbackQuery, state: FSMContext, prefix: str = "") -> None:
    user = await _user(event.from_user)
    await show(event, state, _menu_text(user, event.from_user.first_name, prefix),
               kb.main_menu(user.is_admin))


# ───────────────────────────── меню ─────────────────────────────

@commands.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _main_menu(message, state)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await reset_state(state)
    await _main_menu(cb, state)


@router.callback_query(F.data == "help")
async def cb_help(cb: CallbackQuery, state: FSMContext) -> None:
    text = (
        f"{e('info')} <b>Как это работает</b>\n\n"
        f"<blockquote><b>1.</b> Оформите подписку или активируйте промокод\n"
        f"<b>2.</b> Подключите свои аккаунты Telegram в «Мои аккаунты» — по номеру или файлом .session\n"
        f"<b>3.</b> Нажмите «Запустить инвайтинг» и пришлите ссылку на супергруппу\n"
        f"<b>4.</b> Отправьте <code>.txt</code> со списком — <code>@username</code> "
        f"или <code>+телефон</code>, по одному в строке\n"
        f"<b>5.</b> Следите за прогрессом в реальном времени</blockquote>\n\n"
        f"{e('warn')} Дайте аккаунтам сервиса право приглашать участников в вашей группе."
    )
    await show(cb, state, text, kb.help_menu())


# ───────────────────────────── инвайтинг ─────────────────────────────

def _step(n: int, title: str) -> str:
    return f"{e('rocket')} <b>Новая задача</b> · шаг {n}/3\n\n{title}"


@router.callback_query(F.data == "inv:start")
async def inv_start(cb: CallbackQuery, state: FSMContext) -> None:
    user = await _user(cb.from_user)
    if not q.is_active(user):
        return await cb.answer("Подписка не активна — оформите её в разделе «Подписка».", show_alert=True)
    if await q.get_active_task(user.id):
        return await cb.answer("У вас уже есть активная задача.", show_alert=True)
    pool = await q.pool_stats(user.id)
    if pool["total"] == 0:
        return await cb.answer("Сначала подключите свой аккаунт Telegram в разделе «Мои аккаунты».",
                               show_alert=True)
    if pool["remaining"] <= 0:
        return await cb.answer("Все ваши аккаунты сейчас недоступны: лимит на сегодня, спамблок или "
                               "отключены. Подключите ещё аккаунт или попробуйте завтра.", show_alert=True)
    await reset_state(state)
    await state.set_state(InviteFSM.chat)
    await show(cb, state,
               _step(1, f"{e('link')} Пришлите ссылку на вашу <b>супергруппу</b>:\n\n"
                        "<blockquote><code>https://t.me/name</code>\n<code>https://t.me/+hash</code>\n"
                        "<code>@name</code></blockquote>"),
               kb.cancel())


@router.message(InviteFSM.chat, F.text)
async def inv_chat(message: Message, state: FSMContext, bot: Bot) -> None:
    await safe_delete(message)
    link = message.text.strip()
    if not CHAT_LINK_RE.match(link):
        return await edit_menu(bot, state, message.chat.id,
                               _step(1, f"{e('warn')} Это не похоже на ссылку на группу.\n"
                                        "Пришлите ссылку вида <code>https://t.me/name</code>"),
                               kb.cancel())
    await state.update_data(chat=link)
    await state.set_state(InviteFSM.file)
    await edit_menu(bot, state, message.chat.id,
                    _step(2, f"{e('ok')} Группа: <b>{html.escape(link)}</b>\n\n"
                             f"{e('file')} Теперь отправьте <code>.txt</code> файл со списком:\n"
                             "<blockquote><code>@username</code>\n<code>+79991234567</code></blockquote>"),
                    kb.cancel())


@router.message(InviteFSM.file, F.document)
async def inv_file(message: Message, state: FSMContext, bot: Bot) -> None:
    await safe_delete(message)
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".txt") or (doc.file_size or 0) > MAX_FILE_SIZE:
        return await edit_menu(bot, state, message.chat.id,
                               _step(2, f"{e('warn')} Нужен <code>.txt</code> файл размером до 1 МБ."),
                               kb.cancel())
    buf = await bot.download(doc)
    raw = buf.read()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("cp1251", errors="ignore")
    targets, invalid = parse_targets(content)
    if not targets:
        return await edit_menu(bot, state, message.chat.id,
                               _step(2, f"{e('warn')} В файле нет ни одного <code>@username</code> "
                                        "или <code>+телефона</code>."),
                               kb.cancel())

    user = await _user(message.from_user)
    limit = config.MAX_TARGETS_PER_TASK
    notes = []
    if len(targets) > limit:
        notes.append(f"{e('warn')} Список обрезан до {limit} — максимум на одну задачу")
        targets = targets[:limit]
    if invalid:
        notes.append(f"{e('skip')} Нераспознанных строк: {invalid}")

    data = await state.get_data()
    await state.update_data(targets=[list(t) for t in targets])
    await state.set_state(InviteFSM.confirm)
    pool = await q.pool_stats(user.id)
    text = _step(3, f"{e('list')} <b>Проверьте задачу</b>\n\n"
                    f"<blockquote>{e('link')} Группа: <b>{html.escape(data['chat'])}</b>\n"
                    f"{e('users')} Пользователей: <b>{len(targets)}</b>\n"
                    f"{e('fire')} Ваших рабочих аккаунтов: <b>{pool['alive'] - pool['spam']}</b> "
                    f"(≈{pool['remaining']} приглашений сегодня)</blockquote>")
    if notes:
        text += "\n\n" + "\n".join(notes)
    await edit_menu(bot, state, message.chat.id, text, kb.invite_confirm())


@router.message(InviteFSM.file)
async def inv_file_wrong(message: Message, state: FSMContext, bot: Bot) -> None:
    await safe_delete(message)
    await edit_menu(bot, state, message.chat.id,
                    _step(2, f"{e('warn')} Пришлите именно <code>.txt</code> файл со списком."),
                    kb.cancel())


@router.callback_query(InviteFSM.confirm, F.data == "inv:go")
async def inv_go(cb: CallbackQuery, state: FSMContext, manager: QueueManager) -> None:
    user = await _user(cb.from_user)
    if not q.is_active(user):
        return await cb.answer("Подписка закончилась — продлите её в разделе «Подписка».", show_alert=True)
    if manager.has_task(user.id) or await q.get_active_task(user.id):
        return await cb.answer("У вас уже есть активная задача.", show_alert=True)
    data = await state.get_data()
    if "targets" not in data or "chat" not in data:  # FSM потерян (рестарт бота)
        await reset_state(state)
        return await cb.answer("Сессия устарела, начните заново.", show_alert=True)
    targets = [tuple(t) for t in data["targets"]]
    await reset_state(state)
    task_id = await manager.start_task(user.id, cb.message.chat.id, cb.message.message_id,
                                       data["chat"], targets)
    await cb.answer("Задача запущена 🚀" if task_id else "У вас уже есть активная задача.")


@router.callback_query(F.data.startswith("task:stop:"))
async def task_stop(cb: CallbackQuery, manager: QueueManager) -> None:
    user = await _user(cb.from_user)
    ok = manager.stop_task(int(cb.data.rsplit(":", 1)[1]), user.id)
    await cb.answer("Останавливаю…" if ok else "Задача уже завершена.")


# ───────────────────────────── промокод ─────────────────────────────

@router.callback_query(F.data == "promo")
async def promo_start(cb: CallbackQuery, state: FSMContext) -> None:
    await reset_state(state)
    await state.set_state(PromoFSM.code)
    await show(cb, state, f"{e('gift')} <b>Промокод</b>\n\nОтправьте код сообщением:", kb.cancel())


@router.message(PromoFSM.code, F.text)
async def promo_code(message: Message, state: FSMContext, bot: Bot) -> None:
    await safe_delete(message)
    user = await _user(message.from_user)
    ok, text = await q.activate_promocode(user.id, message.text)
    if not ok:
        return await edit_menu(bot, state, message.chat.id,
                               f"{e('gift')} <b>Промокод</b>\n\n{e('warn')} {text}\n\nПопробуйте другой код:",
                               kb.cancel())
    await reset_state(state)
    user = await _user(message.from_user)
    await edit_menu(bot, state, message.chat.id,
                    _menu_text(user, message.from_user.first_name, f"{e('ok')} <b>{text}</b>"),
                    kb.main_menu(user.is_admin))


# ───────────────────────────── устаревшие кнопки ─────────────────────────────
# Регистрируется последним: ловит кнопки, которые ни один обработчик не принял
# (например, мастер прервался из-за перезапуска бота).

@router.callback_query()
async def stale_callback(cb: CallbackQuery, state: FSMContext) -> None:
    await cb.answer("Эта кнопка устарела — открываю меню.")
    await reset_state(state)
    await _main_menu(cb, state)
