"""«Мои аккаунты»: каждый пользователь подключает свои аккаунты Telegram для инвайтинга."""
from __future__ import annotations

import asyncio
import html
import re
import time
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
from bot import keyboards as kb
from bot.helpers import edit_menu, reset_state, safe_delete, show
from bot.states import AccountFSM
from bot.ui import e
from core.telethon_engine import AccountPool, LoginError, unlink_session
from database import models as m
from database import queries as q

router = Router(name="accounts")

_PHONE_RE = re.compile(r"^\+?\d{7,15}$")
MAX_SHOWN = 30
_files_locks: dict[int, asyncio.Lock] = {}  # файлы альбомом приходят параллельно — по очереди на пользователя


def _status(acc) -> str:
    if not acc.is_alive:
        return f"{e('cancel')} не работает"
    if acc.is_spamblocked:
        return f"{e('stop')} спамблок"
    if acc.sleep_until and acc.sleep_until > m.utcnow():
        return f"{e('clock')} пауза до {acc.sleep_until:%H:%M} UTC"
    if acc.daily_invites_count >= config.DAILY_INVITE_LIMIT:
        return f"{e('clock')} лимит на сегодня"
    return f"{e('ok')} {acc.daily_invites_count}/{config.DAILY_INVITE_LIMIT} сегодня"


async def _screen(event: Message | CallbackQuery, state: FSMContext, prefix: str = "") -> None:
    await reset_state(state)
    user = await q.get_or_create_user(event.from_user.id, event.from_user.username)
    await q.pool_stats(user.id)  # обнуляет суточные счётчики
    accounts = await q.list_accounts(user.id)
    hidden = max(0, len(accounts) - MAX_SHOWN)
    accounts = accounts[:MAX_SHOWN]  # лимиты Telegram: 4096 символов и 100 кнопок
    if accounts:
        lines = [f"<b>{html.escape(a.label or a.phone or f'#{a.id}')}</b>"
                 f"{f' · {a.phone}' if a.phone and a.phone != a.label else ''}\n{_status(a)}" for a in accounts]
        more = f"\n\n…и ещё {hidden}" if hidden else ""
        body = (f"<blockquote>{(chr(10) * 2).join(lines)}{more}</blockquote>\n\n"
                f"Нажмите на {e('trash')}, чтобы отключить аккаунт.")
    else:
        body = ("У вас пока нет аккаунтов. Через них бот будет приглашать людей в вашу группу.\n\n"
                f"<blockquote>{e('info')} Аккаунт должен состоять в группе с правом приглашать "
                f"(лучше — админ с правом «Добавлять участников»).\n"
                f"{e('fire')} Больше аккаунтов — быстрее инвайтинг: на каждый до "
                f"{config.DAILY_INVITE_LIMIT} приглашений в сутки.</blockquote>")
    text = f"{e('users')} <b>Мои аккаунты</b>\n\n{body}"
    await show(event, state, f"{prefix}\n\n{text}" if prefix else text, kb.accounts_menu(accounts))


@router.callback_query(F.data == "acc:menu")
async def acc_menu(cb: CallbackQuery, state: FSMContext, pool: AccountPool) -> None:
    user = await q.get_or_create_user(cb.from_user.id)
    await pool.login_cancel(user.id)  # «Отмена» во время входа по номеру
    await _screen(cb, state)


# ───────────────────────────── вход по номеру ─────────────────────────────
# Номер вводится сообщением, код — на цифровой клавиатуре из кнопок. Код не уходит
# сообщением, поэтому Telegram не считает его «переданным» и не аннулирует.

def _login_head(step: str) -> str:
    return f"{e('phone')} <b>Подключение по номеру</b> · {step}"


def _phone_screen(error: str = "") -> str:
    err = f"{e('warn')} {html.escape(error)}\n\n" if error else ""
    return (f"{_login_head('1/3')}\n\n{err}Отправьте номер телефона аккаунта сообщением "
            "в международном формате:\n\n"
            "<blockquote><code>+380991234567</code></blockquote>")


def _code_screen(phone: str, pad: str, error: str = "") -> str:
    err = f"{e('warn')} {html.escape(error)}\n\n" if error else ""
    cells = " ".join(pad[i] if i < len(pad) else "○" for i in range(max(5, len(pad))))
    return (f"{_login_head('2/3')}\n\n{err}Код отправлен в Telegram на <b>{phone}</b>.\n"
            f"{e('key')} Наберите его кнопками ниже:\n\n"
            f"<blockquote><b><code>{cells}</code></b></blockquote>\n"
            "<i>Не отправляйте код сообщением — Telegram сочтёт, что им поделились, и аннулирует его.</i>")


def _password_screen(error: str = "") -> str:
    err = f"{e('warn')} {html.escape(error)}\n\n" if error else ""
    return (f"{_login_head('3/3')}\n\n{err}{e('key')} На аккаунте включена двухэтапная защита.\n"
            "Отправьте <b>облачный пароль</b> сообщением — оно сразу удалится.")


@router.callback_query(F.data == "acc:phone")
async def acc_phone(cb: CallbackQuery, state: FSMContext) -> None:
    await reset_state(state)
    await state.set_state(AccountFSM.phone)
    await show(cb, state, _phone_screen(), kb.cancel("acc:menu"))


@router.callback_query(AccountFSM.code, F.data.regexp(r"^acc:pad:(\d|del|ok)$"))
async def acc_pad(cb: CallbackQuery, state: FSMContext, bot: Bot, pool: AccountPool) -> None:
    key = cb.data.rsplit(":", 1)[1]
    data = await state.get_data()
    pad = data.get("pad", "")
    if key == "ok":
        await cb.answer()
        return await _once(state, _submit_code(bot, state, cb.message.chat.id, cb.from_user.id, pad, pool))
    pad = pad[:-1] if key == "del" else (pad + key if len(pad) < 6 else pad)
    await state.update_data(pad=pad)  # без await между чтением и записью — быстрые нажатия не теряются
    await cb.answer()
    # рисуем актуальное состояние (могли успеть нажать ещё раз)
    pad = (await state.get_data()).get("pad", "")
    await edit_menu(bot, state, cb.message.chat.id, _code_screen(data.get("phone", ""), pad), kb.keypad("Войти"))


@router.message(AccountFSM.phone, F.text)
async def acc_phone_input(message: Message, state: FSMContext, bot: Bot, pool: AccountPool) -> None:
    await safe_delete(message)
    await _once(state, _submit_phone(bot, state, message.chat.id, message.from_user.id, message.text, pool))


@router.message(AccountFSM.code, F.text)
async def acc_code_input(message: Message, state: FSMContext, bot: Bot, pool: AccountPool) -> None:
    await safe_delete(message)
    await _once(state, _submit_code(bot, state, message.chat.id, message.from_user.id, message.text, pool))


async def _once(state: FSMContext, coro) -> None:
    """Пропускает повторную отправку, пока предыдущая ещё обрабатывается."""
    if (await state.get_data()).get("busy"):  # чтение и запись без переключения задач
        coro.close()
        return
    await state.update_data(busy=True)
    try:
        await coro
    finally:
        await state.update_data(busy=False)


async def _submit_phone(bot: Bot, state: FSMContext, chat_id: int, tg_id: int, raw: str,
                        pool: AccountPool) -> None:
    phone = re.sub(r"[\s\-()+]", "", raw)
    if not _PHONE_RE.match(phone):
        await state.update_data(pad="")
        return await edit_menu(bot, state, chat_id, _phone_screen("Неверный номер. Пример: +380991234567"),
                               kb.cancel("acc:menu"))
    phone = "+" + phone
    await edit_menu(bot, state, chat_id, f"{_login_head('1/3')}\n\n{e('clock')} Отправляю код на {phone}…",
                    kb.cancel("acc:menu"))
    user = await q.get_or_create_user(tg_id)
    try:
        await pool.login_start(user.id, phone)
    except LoginError as err:
        await state.update_data(pad="")
        return await edit_menu(bot, state, chat_id, _phone_screen(str(err)), kb.cancel("acc:menu"))
    await state.set_state(AccountFSM.code)
    await state.update_data(phone=phone, pad="")
    await edit_menu(bot, state, chat_id, _code_screen(phone, ""), kb.keypad("Войти"))


async def _submit_code(bot: Bot, state: FSMContext, chat_id: int, tg_id: int, raw: str,
                       pool: AccountPool) -> None:
    code = re.sub(r"\D", "", raw)
    phone = (await state.get_data()).get("phone", "ваш номер")
    if not 5 <= len(code) <= 6:
        return await edit_menu(bot, state, chat_id, _code_screen(phone, code, "Код — это 5–6 цифр"),
                               kb.keypad("Войти"))
    await edit_menu(bot, state, chat_id, f"{_login_head('2/3')}\n\n{e('clock')} Проверяю код…",
                    kb.cancel("acc:menu"))
    user = await q.get_or_create_user(tg_id)
    try:
        label = await pool.login_code(user.id, code)
    except LoginError as err:
        if "Неверный код" in str(err):
            await state.update_data(pad="")
            return await edit_menu(bot, state, chat_id, _code_screen(phone, "", str(err)), kb.keypad("Войти"))
        return await _screen_after(bot, state, chat_id, tg_id, f"{e('warn')} {html.escape(str(err))}")
    if label is None:
        await state.set_state(AccountFSM.password)
        return await edit_menu(bot, state, chat_id, _password_screen(), kb.cancel("acc:menu"))
    await _screen_after(bot, state, chat_id, tg_id, f"{e('ok')} Аккаунт <b>{html.escape(label)}</b> подключён!")


@router.message(AccountFSM.password, F.text)
async def acc_password_input(message: Message, state: FSMContext, bot: Bot, pool: AccountPool) -> None:
    await safe_delete(message)
    await _once(state, _submit_password(bot, state, message, pool))


async def _submit_password(bot: Bot, state: FSMContext, message: Message, pool: AccountPool) -> None:
    user = await q.get_or_create_user(message.from_user.id)
    await edit_menu(bot, state, message.chat.id, f"{_login_head('3/3')}\n\n{e('clock')} Проверяю пароль…",
                    kb.cancel("acc:menu"))
    try:
        label = await pool.login_password(user.id, message.text)
    except LoginError as err:
        if "Неверный пароль" in str(err):
            return await edit_menu(bot, state, message.chat.id, _password_screen(str(err)),
                                   kb.cancel("acc:menu"))
        return await _screen_after(bot, state, message.chat.id, message.from_user.id,
                                   f"{e('warn')} {html.escape(str(err))}")
    await _screen_after(bot, state, message.chat.id, message.from_user.id,
                        f"{e('ok')} Аккаунт <b>{html.escape(label)}</b> подключён!")


async def _screen_after(bot: Bot, state: FSMContext, chat_id: int, tg_id: int, prefix: str) -> None:
    """Возвращает на экран аккаунтов, редактируя меню-сообщение."""
    await reset_state(state)
    user = await q.get_or_create_user(tg_id)
    accounts = await q.list_accounts(user.id)
    await edit_menu(bot, state, chat_id, f"{prefix}\n\n{e('users')} <b>Мои аккаунты</b>: {len(accounts)}",
                    kb.accounts_menu(accounts[:MAX_SHOWN]))


# ───────────────────────────── загрузка .session ─────────────────────────────

def _files_head() -> str:
    return (f"{e('clip')} <b>Загрузка .session</b>\n\nПришлите файлы <code>.session</code> (Telethon) — "
            "можно несколько сразу. Когда закончите, нажмите «Готово».")


@router.callback_query(F.data == "acc:file")
async def acc_file(cb: CallbackQuery, state: FSMContext) -> None:
    await reset_state(state)
    await state.set_state(AccountFSM.files)
    await state.update_data(log=[])
    await show(cb, state, _files_head(), kb.accounts_done())


@router.message(AccountFSM.files, F.document)
async def acc_file_input(message: Message, state: FSMContext, bot: Bot, pool: AccountPool) -> None:
    await safe_delete(message)
    async with _files_locks.setdefault(message.from_user.id, asyncio.Lock()):
        user = await q.get_or_create_user(message.from_user.id)
        doc = message.document
        name = Path(doc.file_name or "").name
        log = (await state.get_data()).get("log", [])
        if not name.lower().endswith(".session") or (doc.file_size or 0) > 5 * 1024 * 1024:
            log.append(f"{e('warn')} {html.escape(name or '?')}: нужен файл .session")
        else:
            config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            # своё имя файла: пользовательское может пересечься с чужим
            path = config.SESSIONS_DIR / f"u{user.id}_{int(time.time() * 1000)}.session"
            await bot.download(doc, destination=path)
            ok, info = await pool.add_session_file(path, user.id)
            if ok:
                log.append(f"{e('ok')} {html.escape(name)} → {html.escape(info)}")
            else:
                unlink_session(path)
                log.append(f"{e('cancel')} {html.escape(name)}: {html.escape(info)}")
        await state.update_data(log=log)
        await edit_menu(bot, state, message.chat.id,
                        f"{_files_head()}\n\n<blockquote>{chr(10).join(log[-15:])}</blockquote>",
                        kb.accounts_done())


# ───────────────────────────── управление ─────────────────────────────

@router.callback_query(F.data.regexp(r"^acc:del:\d+$"))
async def acc_delete_ask(cb: CallbackQuery, state: FSMContext) -> None:
    user = await q.get_or_create_user(cb.from_user.id)
    acc = await q.get_account(int(cb.data.rsplit(":", 1)[1]))
    if acc is None or acc.owner_id != user.id:
        return await _screen(cb, state)
    await show(cb, state,
               f"{e('trash')} <b>Отключить аккаунт {html.escape(acc.label or acc.phone or '')}?</b>\n\n"
               "Файл сессии будет удалён с сервера.",
               kb.confirm_delete(f"acc:delok:{acc.id}", "acc:menu"))


@router.callback_query(F.data.regexp(r"^acc:delok:\d+$"))
async def acc_delete(cb: CallbackQuery, state: FSMContext, pool: AccountPool) -> None:
    user = await q.get_or_create_user(cb.from_user.id)
    acc = await q.get_account(int(cb.data.rsplit(":", 1)[1]))
    if acc is None or acc.owner_id != user.id:
        return await _screen(cb, state)
    if pool.is_busy(acc.id):
        return await cb.answer("Аккаунт сейчас приглашает — остановите задачу и попробуйте снова.",
                               show_alert=True)
    await pool.remove(acc)
    await _screen(cb, state, f"{e('trash')} Аккаунт отключён.")


@router.callback_query(F.data == "acc:reset")
async def acc_reset(cb: CallbackQuery, state: FSMContext) -> None:
    user = await q.get_or_create_user(cb.from_user.id)
    n = await q.reset_account_flags(user.id)
    await _screen(cb, state, f"{e('refresh')} Сброшено: <b>{n}</b>")
