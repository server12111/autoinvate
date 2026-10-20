"""Оплата по реквизитам: пользователь платит сам, админ подтверждает заявку вручную."""
from __future__ import annotations

import html
import json
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot import keyboards as kb
from bot.helpers import show
from bot.ui import e
from database import queries as q
from database.models import Order, OrderStatus, fmt_usd

router = Router(name="payments")
log = logging.getLogger(__name__)

MAX_PENDING_PER_USER = 3


# ───────────────────────────── тарифы ─────────────────────────────

@router.callback_query(F.data == "buy")
async def buy_list(cb: CallbackQuery, state: FSMContext) -> None:
    plans = await q.list_plans()
    if not plans:
        return await cb.answer("Тарифы пока не добавлены.", show_alert=True)
    user = await q.get_or_create_user(cb.from_user.id, cb.from_user.username)
    lines = [f"{e('fire')} <b>{html.escape(p.name)}</b> — {p.duration_days} дн. · "
             f"<b>{fmt_usd(p.price)}</b>" for p in plans]
    text = f"{e('card')} <b>Тарифы</b>\n\n<blockquote>{chr(10).join(lines)}</blockquote>"
    if user.discount:
        text += f"\n\n{e('tag')} Ваша скидка: <b>{fmt_usd(user.discount)}</b>"
    await show(cb, state, text + "\n\nВыберите тариф:", kb.plans_list(plans))


@router.callback_query(F.data.regexp(r"^buy:\d+$"))
async def buy_plan(cb: CallbackQuery, state: FSMContext) -> None:
    plan = await q.get_plan(int(cb.data.split(":")[1]))
    if plan is None:
        return await cb.answer("Тариф не найден.", show_alert=True)
    user = await q.get_or_create_user(cb.from_user.id, cb.from_user.username)
    price = plan.price - min(plan.price, user.discount)
    price_line = f"{e('money')} Цена: <b>{fmt_usd(price)}</b>"
    if price != plan.price:
        price_line += f" <s>{fmt_usd(plan.price)}</s>"
    invites = f"{e('infinity')} Инвайтинг: <b>без ограничений</b>"
    methods = await q.list_methods(enabled_only=True)
    footer = ("Выберите способ оплаты:" if methods
              else f"{e('warn')} Оплата временно недоступна — напишите в поддержку.")
    text = (f"{e('card')} <b>{html.escape(plan.name)}</b>\n\n"
            f"<blockquote>{e('calendar')} Срок: <b>{plan.duration_days} дн.</b>\n{invites}\n{price_line}"
            f"</blockquote>\n\n{footer}")
    await show(cb, state, text, kb.plan_methods(plan.id, methods))


# ───────────────────────────── заявка ─────────────────────────────

def order_details(order: Order) -> str:
    return (f"<blockquote>{e('fire')} Тариф: <b>{html.escape(order.plan_name)}</b>\n"
            f"{e('money')} К оплате: <b>{html.escape(order.amount_text)}</b>"
            f"{f' ({fmt_usd(order.amount_usd)})' if '$' not in order.amount_text else ''}\n"
            f"{e('wallet')} Реквизиты: <b>{html.escape(order.address)}</b></blockquote>")


@router.callback_query(F.data.regexp(r"^pay:\d+:\w+$"))
async def pay(cb: CallbackQuery, state: FSMContext) -> None:
    _, plan_id, method_key = cb.data.split(":")
    user = await q.get_or_create_user(cb.from_user.id, cb.from_user.username)
    order = await q.create_order(user.id, int(plan_id), method_key)
    if order is None:
        return await cb.answer("Этот способ оплаты сейчас недоступен.", show_alert=True)
    method = await q.get_method(method_key)
    warn = (f"{e('warn')} <b>Переведите ровно {html.escape(order.amount_text)}</b> — до копейки, "
            "по этой сумме мы найдём ваш платёж. Комиссию банка / сети оплачивайте сверху.")
    text = (f"{e('receipt')} <b>Заявка #{order.id}</b> · {html.escape(method.title)}\n\n"
            f"{order_details(order)}\n\n{warn}\n\n"
            "После перевода нажмите <b>«Я оплатил»</b> — администратор проверит платёж "
            "и активирует тариф.")
    await show(cb, state, text, kb.order_pay(order.id))


@router.callback_query(F.data.regexp(r"^order:cancel:\d+$"))
async def order_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    user = await q.get_or_create_user(cb.from_user.id)
    await q.cancel_order(int(cb.data.rsplit(":", 1)[1]), user.id)
    await buy_list(cb, state)


@router.callback_query(F.data.regexp(r"^order:paid:\d+$"))
async def order_paid(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    user = await q.get_or_create_user(cb.from_user.id, cb.from_user.username)
    if await q.count_pending_orders(user.id) >= MAX_PENDING_PER_USER:
        return await cb.answer("У вас уже есть заявки на проверке — дождитесь решения.", show_alert=True)
    order = await q.mark_order_paid(int(cb.data.rsplit(":", 1)[1]), user.id)
    if order is None:
        return await cb.answer("Заявка уже отправлена или отменена.", show_alert=True)
    await show(cb, state,
               f"{e('clock')} <b>Заявка #{order.id} отправлена на проверку</b>\n\n{order_details(order)}\n\n"
               "Как только администратор подтвердит оплату, тариф активируется и придёт уведомление.",
               kb.to_menu())
    await notify_admins(bot, order, cb.from_user)


# ───────────────────────────── уведомления админам ─────────────────────────────

def admin_order_text(order: Order, tg_user=None, user_tg_id: int | None = None) -> str:
    if tg_user is not None:
        who = f'<a href="tg://user?id={tg_user.id}">{html.escape(tg_user.full_name)}</a>'
        if tg_user.username:
            who += f" · @{html.escape(tg_user.username)}"
        who += f" · <code>{tg_user.id}</code>"
    else:
        who = f'<a href="tg://user?id={user_tg_id}">пользователь</a> · <code>{user_tg_id}</code>'
    status = {
        OrderStatus.PENDING: f"{e('clock')} ждёт проверки",
        OrderStatus.CONFIRMED: f"{e('ok')} подтверждена",
        OrderStatus.REJECTED: f"{e('cancel')} отклонена",
    }.get(order.status, order.status)
    paid = f"{order.paid_at:%d.%m %H:%M} UTC" if order.paid_at else "—"
    return (f"{e('receipt')} <b>Заявка #{order.id}</b> — {status}\n\n"
            f"{e('user')} {who}\n"
            f"{e('fire')} {html.escape(order.plan_name)} · {order.days} дн.\n"
            f"{e('money')} <b>{html.escape(order.amount_text)}</b> ({fmt_usd(order.amount_usd)}) · "
            f"{html.escape(order.method)}\n"
            f"{e('wallet')} {html.escape(order.address)}\n"
            f"{e('calendar')} Оплачено: {paid}")


async def notify_admins(bot: Bot, order: Order, tg_user) -> None:
    text = admin_order_text(order, tg_user)
    sent: list[list[int]] = []
    for admin_id in await q.admin_tg_ids():
        try:
            msg = await bot.send_message(admin_id, text, reply_markup=kb.order_review(order.id))
            sent.append([admin_id, msg.message_id])
        except (TelegramBadRequest, TelegramForbiddenError):
            pass  # админ не запускал бота / заблокировал его
    if not sent:
        log.error("Заявка #%s: ни одному админу не удалось отправить уведомление", order.id)
    await q.set_order_admin_msgs(order.id, sent)


async def finish_admin_msgs(bot: Bot, order: Order, admin_name: str) -> None:
    """Убирает кнопки из уведомлений у всех админов и пишет, кто принял решение."""
    user = await q.get_user(order.user_id)
    text = admin_order_text(order, user_tg_id=user.tg_id if user else 0) + f"\n{e('admin')} {html.escape(admin_name)}"
    for chat_id, msg_id in json.loads(order.admin_msgs or "[]"):
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
