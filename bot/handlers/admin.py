from __future__ import annotations

import html
import json
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

import config
from bot import keyboards as kb
from bot import ui
from bot.helpers import edit_menu, reset_state, safe_delete, show
from bot.handlers import payments
from bot.states import AddPlanFSM, AddPromoFSM, EmojiFSM, GrantFSM, MethodFSM
from bot.ui import e
from core import rates as rates_mod
from database import queries as q
from database.models import OrderStatus, PromoType, fmt_usd


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        tg_id = event.from_user.id
        if tg_id in config.ADMIN_IDS:
            return True
        user = await q.get_user_by_tg(tg_id)
        return bool(user and user.is_admin)


router = Router(name="admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())
commands = Router(name="admin_commands")  # подключается раньше FSM-обработчиков
commands.message.filter(IsAdmin())

_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")


def _h(key: str, title: str, body: str = "") -> str:
    """Заголовок экрана админки."""
    head = f"{e(key)} <b>{title}</b>"
    return f"{head}\n\n{body}" if body else head


async def _int(message: Message, bot: Bot, state: FSMContext, screen: str, back_cb: str,
               minimum: int = 0) -> int | None:
    """Читает целое из сообщения; при ошибке правит меню и возвращает None."""
    await safe_delete(message)
    try:
        value = int((message.text or "").strip())
        if not minimum <= value <= 10**9:
            raise ValueError
        return value
    except ValueError:
        await edit_menu(bot, state, message.chat.id,
                        f"{screen}\n\n{e('warn')} Нужно целое число ≥ {minimum}. Попробуйте ещё раз:",
                        kb.admin_cancel(back_cb))
        return None


async def _money(message: Message, bot: Bot, state: FSMContext, screen: str, back_cb: str) -> int | None:
    """Читает сумму в $ («10», «9.99», «9,99») и возвращает центы."""
    await safe_delete(message)
    raw = (message.text or "").strip().lstrip("$").replace(",", ".")
    if re.fullmatch(r"\d{1,7}(\.\d{1,2})?", raw):
        cents = round(float(raw) * 100)
        if cents > 0:
            return cents
    await edit_menu(bot, state, message.chat.id,
                    f"{screen}\n\n{e('warn')} Введите сумму в долларах, например <code>9.99</code>:",
                    kb.admin_cancel(back_cb))
    return None


async def _admin_menu(event: Message | CallbackQuery, state: FSMContext) -> None:
    await reset_state(state)
    s = await q.global_stats()
    body = (f"<blockquote>{e('user')} Пользователей: <b>{s['total_users']}</b> "
            f"(активных: <b>{s['active_users']}</b>)\n"
            f"{e('mail')} Инвайтов сегодня: <b>{s['invites_today']}</b>\n"
            f"{e('users')} Аккаунтов у пользователей: <b>{s['alive'] - s['spam']}</b> рабочих из {s['total']}\n"
            f"{e('receipt')} Заявок на проверке: <b>{s['pending_orders']}</b>"
            f"</blockquote>\n\nВыберите раздел:")
    await show(event, state, _h("admin", "Админ-панель", body), kb.admin_menu(s["pending_orders"]))


@commands.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _admin_menu(message, state)


@router.callback_query(F.data == "adm:menu")
async def cb_admin(cb: CallbackQuery, state: FSMContext) -> None:
    await _admin_menu(cb, state)


# ───────────────────────────── тарифы ─────────────────────────────

def PLAN_HEAD() -> str:
    return f"{e('tag')} <b>Новый тариф</b>"


def _plan_line(p) -> str:
    return f"{e('fire')} <b>{html.escape(p.name)}</b> — {fmt_usd(p.price)} · {p.duration_days} дн."


async def _plans_screen(event, state: FSMContext, prefix: str = "") -> None:
    await reset_state(state)
    plans = await q.list_plans()
    if plans:
        lines = [_plan_line(p) for p in plans]
        body = f"<blockquote>{chr(10).join(lines)}</blockquote>\n\nНажмите на тариф, чтобы удалить его."
    else:
        body = "Тарифов пока нет — добавьте первый."
    text = _h("tag", "Тарифы", body)
    await show(event, state, f"{prefix}\n\n{text}" if prefix else text, kb.admin_plans(plans))


@router.callback_query(F.data == "adm:plans")
async def plans(cb: CallbackQuery, state: FSMContext) -> None:
    await _plans_screen(cb, state)


@router.callback_query(F.data.regexp(r"^adm:plan_del:\d+$"))
async def plan_del_ask(cb: CallbackQuery, state: FSMContext) -> None:
    plan = await q.get_plan(int(cb.data.rsplit(":", 1)[1]))
    if plan is None:
        return await _plans_screen(cb, state)
    await show(cb, state, _h("trash", "Удалить тариф?", _plan_line(plan)),
               kb.confirm_delete(f"adm:plan_delok:{plan.id}", "adm:plans"))


@router.callback_query(F.data.regexp(r"^adm:plan_delok:\d+$"))
async def plan_del(cb: CallbackQuery, state: FSMContext) -> None:
    await q.delete_plan(int(cb.data.rsplit(":", 1)[1]))
    await _plans_screen(cb, state, f"{e('trash')} Тариф удалён.")


@router.callback_query(F.data == "adm:plan_add")
async def plan_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddPlanFSM.name)
    await show(cb, state, f"{PLAN_HEAD()} · 1/3\n\nВведите <b>название</b>:", kb.admin_cancel("adm:plans"))


@router.message(AddPlanFSM.name, F.text)
async def plan_name(message: Message, state: FSMContext, bot: Bot) -> None:
    await safe_delete(message)
    await state.update_data(name=message.text.strip()[:64])
    await state.set_state(AddPlanFSM.price)
    await edit_menu(bot, state, message.chat.id, f"{PLAN_HEAD()} · 2/3\n\nЦена в <b>$</b> (например <code>9.99</code>):",
                    kb.admin_cancel("adm:plans"))


@router.message(AddPlanFSM.price, F.text)
async def plan_price(message: Message, state: FSMContext, bot: Bot) -> None:
    if (v := await _money(message, bot, state, f"{PLAN_HEAD()} · 2/3", "adm:plans")) is None:
        return
    await state.update_data(price=v)
    await state.set_state(AddPlanFSM.days)
    await edit_menu(bot, state, message.chat.id, f"{PLAN_HEAD()} · 3/3\n\nСрок подписки в <b>днях</b>:",
                    kb.admin_cancel("adm:plans"))


@router.message(AddPlanFSM.days, F.text)
async def plan_days(message: Message, state: FSMContext, bot: Bot) -> None:
    if (v := await _int(message, bot, state, f"{PLAN_HEAD()} · 3/3", "adm:plans", 1)) is None:
        return
    await state.update_data(days=v)
    await _finish_plan(bot, state, message.chat.id)


async def _finish_plan(bot: Bot, state: FSMContext, chat_id: int) -> None:
    d = await state.get_data()
    if d.get("plan_saved"):
        return
    await state.update_data(plan_saved=True)
    await q.add_plan(d["name"], d["price"], d["days"])
    await reset_state(state)
    plans_ = await q.list_plans()
    lines = [_plan_line(p) for p in plans_]
    await edit_menu(bot, state, chat_id,
                    f"{e('ok')} Тариф «{html.escape(d['name'])}» добавлен.\n\n"
                    + _h("tag", "Тарифы", f"<blockquote>{chr(10).join(lines)}</blockquote>"),
                    kb.admin_plans(plans_))


# ───────────────────────────── промокоды ─────────────────────────────

def PROMO_HEAD() -> str:
    return f"{e('gift')} <b>Новый промокод</b>"


def _promo_value(p) -> str:
    return fmt_usd(p.value) if p.type == PromoType.DISCOUNT else str(p.value)


def _promos_text(promos) -> str:
    if not promos:
        return _h("gift", "Промокоды", "Промокодов пока нет.")
    lines = [f"<code>{p.code}</code> — {PromoType.TITLES.get(p.type, p.type)}: <b>{_promo_value(p)}</b> · "
             f"{p.used_count}/{p.max_activations or '∞'}" for p in promos]
    return _h("gift", "Промокоды",
              f"<blockquote>{chr(10).join(lines)}</blockquote>\n\nНажмите на код, чтобы удалить его.")


async def _promos_screen(event, state: FSMContext, prefix: str = "") -> None:
    await reset_state(state)
    promos = await q.list_promos()
    text = _promos_text(promos)
    await show(event, state, f"{prefix}\n\n{text}" if prefix else text, kb.admin_promos(promos))


@router.callback_query(F.data == "adm:promos")
async def promos(cb: CallbackQuery, state: FSMContext) -> None:
    await _promos_screen(cb, state)


@router.callback_query(F.data.regexp(r"^adm:promo_del:\d+$"))
async def promo_del_ask(cb: CallbackQuery, state: FSMContext) -> None:
    pid = int(cb.data.rsplit(":", 1)[1])
    await show(cb, state, _h("trash", "Удалить промокод?", "Активации по нему тоже удалятся."),
               kb.confirm_delete(f"adm:promo_delok:{pid}", "adm:promos"))


@router.callback_query(F.data.regexp(r"^adm:promo_delok:\d+$"))
async def promo_del(cb: CallbackQuery, state: FSMContext) -> None:
    await q.delete_promo(int(cb.data.rsplit(":", 1)[1]))
    await _promos_screen(cb, state, f"{e('trash')} Промокод удалён.")


@router.callback_query(F.data == "adm:promo_add")
async def promo_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddPromoFSM.code)
    await show(cb, state, f"{PROMO_HEAD()} · 1/4\n\nВведите <b>код</b> (латиница, цифры, 3–32 символа):",
               kb.admin_cancel("adm:promos"))


@router.message(AddPromoFSM.code, F.text)
async def promo_code(message: Message, state: FSMContext, bot: Bot) -> None:
    await safe_delete(message)
    code = message.text.strip()
    error = None
    if not _CODE_RE.match(code):
        error = "Допустимы A–Z, 0–9, _ и -, длина 3–32."
    elif await q.promo_exists(code):
        error = "Такой код уже существует."
    if error:
        return await edit_menu(bot, state, message.chat.id,
                               f"{PROMO_HEAD()} · 1/4\n\n{e('warn')} {error} Введите другой код:",
                               kb.admin_cancel("adm:promos"))
    await state.update_data(code=code.upper())
    await state.set_state(AddPromoFSM.type)
    await edit_menu(bot, state, message.chat.id,
                    f"{PROMO_HEAD()} · 2/4\n\nКод: <code>{code.upper()}</code>\nВыберите <b>тип бонуса</b>:",
                    kb.promo_types())


@router.callback_query(AddPromoFSM.type, F.data.regexp(r"^adm:ptype:\w+$"))
async def promo_type(cb: CallbackQuery, state: FSMContext) -> None:
    type_ = cb.data.rsplit(":", 1)[1]
    if type_ not in PromoType.ALL:
        return await cb.answer()
    await state.update_data(type=type_)
    await state.set_state(AddPromoFSM.value)
    hint = {PromoType.DISCOUNT: "сумму скидки в $ (например 2.50)", PromoType.ADD_DAYS: "количество дней",
}[type_]
    await show(cb, state, f"{PROMO_HEAD()} · 3/4\n\nВведите <b>{hint}</b>:", kb.admin_cancel("adm:promos"))


@router.message(AddPromoFSM.value, F.text)
async def promo_value(message: Message, state: FSMContext, bot: Bot) -> None:
    if (await state.get_data()).get("type") == PromoType.DISCOUNT:
        v = await _money(message, bot, state, f"{PROMO_HEAD()} · 3/4", "adm:promos")
    else:
        v = await _int(message, bot, state, f"{PROMO_HEAD()} · 3/4", "adm:promos", 1)
    if v is None:
        return
    await state.update_data(value=v)
    await state.set_state(AddPromoFSM.limit)
    await edit_menu(bot, state, message.chat.id,
                    f"{PROMO_HEAD()} · 4/4\n\n<b>Лимит активаций</b> (0 — без лимита):",
                    kb.admin_cancel("adm:promos"))


@router.message(AddPromoFSM.limit, F.text)
async def promo_limit(message: Message, state: FSMContext, bot: Bot) -> None:
    if (v := await _int(message, bot, state, f"{PROMO_HEAD()} · 4/4", "adm:promos")) is None:
        return
    d = await state.get_data()
    await q.create_promo(d["code"], d["type"], d["value"], v)
    await reset_state(state)
    promos_ = await q.list_promos()
    await edit_menu(bot, state, message.chat.id,
                    f"{e('ok')} Промокод <code>{d['code']}</code> создан.\n\n" + _promos_text(promos_),
                    kb.admin_promos(promos_))


# ───────────────────────────── реквизиты ─────────────────────────────

_FIELD_PROMPTS = {
    "address": "Отправьте <b>адрес кошелька</b> или <b>номер карты</b> для этого способа:",
    "rate": "Сколько <b>{currency}</b> брать за <b>$1</b>? Например <code>41.5</code>.",
}


def _method_kb(method):
    return kb.admin_method(method, method.key in rates_mod.AUTO_SOURCES, rates_mod.is_auto(method))


def _method_card(method, prefix: str = "") -> str:
    status = f"{e('ok')} включён" if method.enabled else f"{e('stop')} выключен"
    rate = (f"{method.rate:.8g} {method.currency} за $1" if method.rate > 0 else "сумма в $ (эквивалент)")
    if rates_mod.is_auto(method):
        when = f", {method.rate_updated_at:%d.%m %H:%M} UTC" if method.rate_updated_at else ", ещё не загружен"
        rate += f" · авто ({rates_mod.SOURCE_NAMES[method.key]}{when})"
    elif method.key in rates_mod.AUTO_SOURCES:
        rate += " · вручную"
    example = q.format_amount(method, 1000)
    example += " (минус 1–99 мельчайших единиц)"
    body = (f"<blockquote>Статус: <b>{status}</b>\n"
            f"{e('wallet')} Адрес: <code>{html.escape(method.address or '—')}</code>\n"
            f"{e('tag')} Опознание платежа: <b>уникальная сумма</b>\n"
            f"{e('money')} Курс: <b>{rate}</b>\n"
            f"{e('fire')} Тариф за $10 → <b>{html.escape(example)}</b></blockquote>")
    if not method.address:
        body += f"\n\n{e('warn')} Укажите адрес — без него способ не показывается клиентам."
    if method.rate <= 0:
        body += (f"\n\n{e('warn')} Укажите курс: платёж опознаётся по точной сумме в {method.currency}.")
    text = _h("wallet", method.title, body)
    return f"{prefix}\n\n{text}" if prefix else text


async def _methods_screen(event, state: FSMContext, prefix: str = "") -> None:
    await reset_state(state)
    methods = await q.list_methods()
    on = sum(1 for x in methods if x.enabled)
    text = _h("wallet", "Реквизиты для оплаты",
              f"Включено способов: <b>{on}/{len(methods)}</b>\n\n"
              "Нажмите на способ, чтобы указать адрес и курс.\n\n"
              f"{e('money')} Бот выставляет каждой заявке "
              "уникальную сумму — на несколько копеек / центов меньше (450.78, 450.77…). "
              "По точной сумме видно, от кого платёж.")
    await show(event, state, f"{prefix}\n\n{text}" if prefix else text, kb.admin_methods(methods))


@router.callback_query(F.data == "adm:methods")
async def methods(cb: CallbackQuery, state: FSMContext) -> None:
    await _methods_screen(cb, state)


@router.callback_query(F.data.regexp(r"^adm:method:\w+$"))
async def method_card(cb: CallbackQuery, state: FSMContext) -> None:
    method = await q.get_method(cb.data.rsplit(":", 1)[1])
    if method is None:
        return await _methods_screen(cb, state)
    await reset_state(state)
    await show(cb, state, _method_card(method), _method_kb(method))


@router.callback_query(F.data.regexp(r"^adm:mtoggle:\w+$"))
async def method_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    method = await q.get_method(cb.data.rsplit(":", 1)[1])
    if method is None:
        return await _methods_screen(cb, state)
    if not method.enabled and not method.address:
        return await cb.answer("Сначала укажите адрес.", show_alert=True)
    if not method.enabled and method.rate <= 0:
        # без курса клиент сам пересчитает $ → сумма на кошельке не совпадёт с заявкой
        return await cb.answer(f"Сначала укажите курс: сколько {method.currency} за $1. "
                               "В этой сети платёж опознаётся по точной сумме.", show_alert=True)
    method = await q.update_method(method.key, enabled=not method.enabled)
    await show(cb, state, _method_card(method), _method_kb(method))


@router.callback_query(F.data.regexp(r"^adm:mauto:\w+$"))
async def method_auto(cb: CallbackQuery, state: FSMContext) -> None:
    method = await q.get_method(cb.data.rsplit(":", 1)[1])
    if method is None or method.key not in rates_mod.AUTO_SOURCES:
        return await _methods_screen(cb, state)
    if rates_mod.is_auto(method):
        return await cb.answer("Авто-курс уже включён. Чтобы задать свой — нажмите «Свой курс».",
                               show_alert=True)
    await q.update_method(method.key, auto_rate=True)
    got = await rates_mod.update_rates(only=method.key)
    method = await q.get_method(method.key)
    prefix = (f"{e('ok')} Авто-курс включён." if got
              else f"{e('warn')} Авто-курс включён, но источник сейчас недоступен — повторю через 15 минут.")
    await show(cb, state, _method_card(method, prefix), _method_kb(method))


@router.callback_query(F.data.regexp(r"^adm:mset:\w+:(address|rate)$"))
async def method_set(cb: CallbackQuery, state: FSMContext) -> None:
    _, _, key, field = cb.data.split(":")
    method = await q.get_method(key)
    if method is None:
        return await _methods_screen(cb, state)
    await state.set_state(MethodFSM.value)
    await state.update_data(method_key=key, method_field=field)
    prompt = _FIELD_PROMPTS[field].format(currency=method.currency)
    if field == "rate" and method.currency == "BTC":
        prompt = ("Сколько <b>BTC</b> брать за <b>$1</b>? Например <code>0.0000105</code> "
                  "(при курсе $95 000 за 1 BTC). Обновляйте курс время от времени.")
    await show(cb, state, _h("wallet", method.title, prompt),
               kb.admin_cancel(f"adm:method:{key}"))


@router.message(MethodFSM.value, F.text)
async def method_value(message: Message, state: FSMContext, bot: Bot) -> None:
    await safe_delete(message)
    d = await state.get_data()
    key, field = d.get("method_key"), d.get("method_field")
    method = await q.get_method(key or "")
    if method is None or field not in _FIELD_PROMPTS:
        await reset_state(state)
        return await edit_menu(bot, state, message.chat.id, _h("wallet", "Реквизиты"), kb.admin_back())
    raw = message.text.strip()
    if field == "rate":
        try:
            value = float(raw.replace(",", "."))
            if not 0 <= value <= 10**7:
                raise ValueError
            if value == 0:
                return await edit_menu(bot, state, message.chat.id,
                                       _h("wallet", method.title,
                                          f"{e('warn')} Нужен точный курс, иначе платёж не опознать по сумме. "
                                          f"Сколько {method.currency} за $1?"),
                                       kb.admin_cancel(f"adm:method:{key}"))
        except ValueError:
            return await edit_menu(bot, state, message.chat.id,
                                   _h("wallet", method.title, f"{e('warn')} Нужно число, например <code>41.5</code>"),
                                   kb.admin_cancel(f"adm:method:{key}"))
    else:
        if len(raw) > 255:
            return await edit_menu(bot, state, message.chat.id,
                                   _h("wallet", method.title, f"{e('warn')} Слишком длинно (макс. 255 символов)"),
                                   kb.admin_cancel(f"adm:method:{key}"))
        value = raw
    extra = {"auto_rate": False} if field == "rate" else {}  # свой курс отключает автообновление
    method = await q.update_method(key, **{field: value}, **extra)
    await reset_state(state)
    await edit_menu(bot, state, message.chat.id, _method_card(method, f"{e('ok')} Сохранено."),
                    _method_kb(method))


# ───────────────────────────── заявки на оплату ─────────────────────────────

async def _orders_screen(event, state: FSMContext, prefix: str = "") -> None:
    await reset_state(state)
    orders = await q.list_pending_orders()
    if orders:
        lines = [f"#{o.id} · <b>{html.escape(o.amount_text)}</b> · {html.escape(o.plan_name)} · "
                 f"{o.paid_at:%d.%m %H:%M}" for o in orders]
        body = f"<blockquote>{chr(10).join(lines)}</blockquote>\n\nНажмите на заявку, чтобы проверить её."
    else:
        body = "Новых заявок нет."
    text = _h("receipt", "Заявки на оплату", body)
    await show(event, state, f"{prefix}\n\n{text}" if prefix else text, kb.admin_orders(orders))


@router.callback_query(F.data == "adm:orders")
async def orders(cb: CallbackQuery, state: FSMContext) -> None:
    await _orders_screen(cb, state)


@router.callback_query(F.data.regexp(r"^adm:order:\d+$"))
async def order_card(cb: CallbackQuery, state: FSMContext) -> None:
    order = await q.get_order(int(cb.data.rsplit(":", 1)[1]))
    if order is None or order.status != OrderStatus.PENDING:
        return await _orders_screen(cb, state, f"{e('warn')} Заявка уже обработана.")
    user = await q.get_user(order.user_id)
    markup = kb.order_review(order.id)
    markup.inline_keyboard.append(kb.admin_back("adm:orders").inline_keyboard[0])
    await show(cb, state, payments.admin_order_text(order, user_tg_id=user.tg_id), markup)


@router.callback_query(F.data.regexp(r"^adm:order_(ok|no):\d+$"))
async def order_decide(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    confirm = cb.data.startswith("adm:order_ok")
    res = await q.decide_order(int(cb.data.rsplit(":", 1)[1]), cb.from_user.id, confirm)
    if res is None:
        await cb.answer("Заявка уже обработана другим админом.", show_alert=True)
        return await _orders_screen(cb, state)
    order, user = res
    admin_name = f"@{cb.from_user.username}" if cb.from_user.username else cb.from_user.full_name
    await payments.finish_admin_msgs(bot, order, admin_name)

    if confirm:
        note = (f"{e('ok')} <b>Оплата подтверждена!</b>\n\nЗаявка #{order.id}: тариф "
                f"«{html.escape(order.plan_name)}» активирован — +{order.days} дн. подписки.")
    else:
        note = (f"{e('cancel')} <b>Заявка #{order.id} отклонена.</b>\n\nПлатёж не найден. "
                "Если вы оплатили — напишите в поддержку и приложите чек.")
    try:
        await bot.send_message(user.tg_id, note, reply_markup=kb.to_menu())
    except (TelegramBadRequest, TelegramForbiddenError):
        pass

    # нажали в уведомлении — оно уже обновлено; нажали из списка заявок — возвращаемся к списку
    notified = {tuple(x) for x in json.loads(order.admin_msgs or "[]")}
    if (cb.message.chat.id, cb.message.message_id) in notified:
        await cb.answer("Подтверждено" if confirm else "Отклонено")
    else:
        await _orders_screen(cb, state, f"{e('ok')} Заявка #{order.id} "
                                        f"{'подтверждена' if confirm else 'отклонена'}.")


# ───────────────────────────── выдача доступа ─────────────────────────────

def GRANT_HEAD() -> str:
    return f"{e('user')} <b>Выдать доступ</b>"


def _sub_line(user) -> str:
    sub = f"до {user.sub_until:%d.%m.%Y}" if q.is_active(user) else "не активна"
    return f"{e('calendar')} Подписка: <b>{sub}</b>"


@router.callback_query(F.data == "adm:grant")
async def grant(cb: CallbackQuery, state: FSMContext) -> None:
    await reset_state(state)
    await state.set_state(GrantFSM.tg_id)
    await show(cb, state, f"{GRANT_HEAD()}\n\nВведите <b>Telegram ID</b> пользователя:", kb.admin_cancel())


@router.message(GrantFSM.tg_id, F.text)
async def grant_id(message: Message, state: FSMContext, bot: Bot) -> None:
    if (v := await _int(message, bot, state, GRANT_HEAD(), "adm:menu", 1)) is None:
        return
    user = await q.get_or_create_user(v)
    await state.update_data(user_id=user.id, tg_id=v)
    await state.set_state(GrantFSM.amount)
    await edit_menu(bot, state, message.chat.id,
                    f"{GRANT_HEAD()}\n\n<blockquote><code>{v}</code>"
                    f"{' · @' + html.escape(user.username) if user.username else ''}\n"
                    f"{_sub_line(user)}</blockquote>\n\nСколько <b>дней подписки</b> начислить?",
                    kb.admin_cancel())


@router.message(GrantFSM.amount, F.text)
async def grant_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    if (v := await _int(message, bot, state, GRANT_HEAD(), "adm:menu", 1)) is None:
        return
    d = await state.get_data()
    user = await q.grant(d["user_id"], days=v)
    await reset_state(state)
    await edit_menu(bot, state, message.chat.id,
                    f"{e('ok')} <b>Начислено {v} дн.!</b>\n\n<blockquote><code>{d['tg_id']}</code>\n"
                    f"{_sub_line(user)}</blockquote>",
                    kb.admin_back())


# ───────────────────────────── статистика ─────────────────────────────

@router.callback_query(F.data == "adm:stats")
async def stats(cb: CallbackQuery, state: FSMContext) -> None:
    s = await q.global_stats()
    body = (f"<blockquote>{e('user')} Пользователей: <b>{s['total_users']}</b>\n"
            f"{e('ok')} С активной подпиской: <b>{s['active_users']}</b>\n"
            f"{e('rocket')} Задач в работе: <b>{s['running_tasks']}</b></blockquote>\n\n"
            f"<blockquote>{e('mail')} Инвайтов сегодня: <b>{s['invites_today']}</b>\n"
            f"{e('fire')} Остаток пула на сегодня: <b>{s['remaining']}</b></blockquote>\n\n"
            f"<blockquote>{e('users')} Аккаунтов у пользователей: <b>{s['total']}</b>\n"
            f"{e('ok')} Живых: <b>{s['alive']}</b>\n"
            f"{e('stop')} В спамблоке: <b>{s['spam']}</b></blockquote>")
    await show(cb, state, _h("stats", "Статистика", body), kb.admin_back())


# ───────────────────────────── премиум-эмодзи ─────────────────────────────

def _emoji_text(prefix: str = "") -> str:
    configured = sum(1 for k in ui.EMOJI if ui.get_id(k))
    body = (f"Настроено: <b>{configured}/{len(ui.EMOJI)}</b>\n\n"
            "Выберите элемент и пришлите для него <b>премиум-эмодзи</b>. Оно заменит обычный "
            "эмодзи в текстах и станет иконкой на кнопках.\n\n"
            f"{e('warn')} Работает, только если у владельца бота есть Telegram Premium.")
    text = _h("sparkles", "Премиум-эмодзи", body)
    return f"{prefix}\n\n{text}" if prefix else text


@router.callback_query(F.data == "adm:emoji")
async def emoji_menu(cb: CallbackQuery, state: FSMContext) -> None:
    await reset_state(state)
    await show(cb, state, _emoji_text(), kb.emoji_list())


@router.callback_query(F.data.regexp(r"^adm:emo:\w+$"))
async def emoji_pick(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data.rsplit(":", 1)[1]
    if key not in ui.EMOJI:
        return await cb.answer()
    await state.set_state(EmojiFSM.wait)
    await state.update_data(emoji_key=key)
    fallback, title = ui.EMOJI[key]
    await show(cb, state,
               _h("sparkles", f"Эмодзи «{title}»",
                  f"Сейчас: {e(key)}  (обычный: {fallback})\n\nПришлите одно <b>премиум-эмодзи</b> сообщением:"),
               kb.emoji_edit(key))


@router.callback_query(F.data.regexp(r"^adm:emo_reset:\w+$"))
async def emoji_reset(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data.rsplit(":", 1)[1]
    if key in ui.EMOJI:
        ui.set_id(key, None)
    await reset_state(state)
    await show(cb, state, _emoji_text(f"{e('refresh')} Эмодзи сброшено."), kb.emoji_list())


@router.message(EmojiFSM.wait)
async def emoji_set(message: Message, state: FSMContext, bot: Bot) -> None:
    await safe_delete(message)
    key = (await state.get_data()).get("emoji_key")
    if key not in ui.EMOJI:  # состояние потеряно (рестарт)
        await reset_state(state)
        return await edit_menu(bot, state, message.chat.id, _emoji_text(), kb.emoji_list())
    ids = [ent.custom_emoji_id for ent in (message.entities or []) if ent.type == "custom_emoji"]
    if not ids:
        return await edit_menu(bot, state, message.chat.id,
                               _h("sparkles", "Премиум-эмодзи",
                                  f"{e('warn')} В сообщении нет премиум-эмодзи. Пришлите его ещё раз:"),
                               kb.emoji_edit(key))
    emoji_id = ids[0]
    # Проверяем, что бот действительно может использовать это эмодзи (и в тексте, и на кнопке)
    old = ui.get_id(key)
    ui.set_id(key, emoji_id)
    try:
        test = await bot.send_message(
            message.chat.id, f"Проверка: {ui.e(key)}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[ui.btn("Проверка", "noop", emoji=key)]]))
        await safe_delete(test)
        # без прав Telegram может молча отдать обычный эмодзи вместо премиум
        error = None if any(ent.type == "custom_emoji" for ent in (test.entities or [])) else \
            "эмодзи заменено на обычное — у владельца бота нет Telegram Premium?"
    except TelegramBadRequest as err:
        error = err.message
    if error:
        ui.set_id(key, old)
        return await edit_menu(bot, state, message.chat.id,
                               _h("sparkles", "Премиум-эмодзи",
                                  f"{e('warn')} Telegram не разрешил боту это эмодзи:\n"
                                  f"<code>{html.escape(error)}</code>"),
                               kb.emoji_edit(key))
    await reset_state(state)
    await edit_menu(bot, state, message.chat.id,
                    _emoji_text(f"{e('ok')} Эмодзи «{ui.EMOJI[key][1]}» обновлено: {ui.e(key)}"),
                    kb.emoji_list())
