from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
from bot.ui import DANGER, EMOJI, PRIMARY, SUCCESS, btn, chunk, get_id
from database.models import Account, Order, PaymentMethod, Plan, Promocode, PromoType, fmt_usd


def _kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])  # пустые ряды Telegram не принимает


def back(cb: str = "menu", text: str = "Назад") -> InlineKeyboardMarkup:
    return _kb([btn(text, cb, emoji="back")])


def cancel(cb: str = "menu") -> InlineKeyboardMarkup:
    return _kb([btn("Отмена", cb, emoji="cancel", style=DANGER)])


def to_menu() -> InlineKeyboardMarkup:
    return _kb([btn("В главное меню", "menu", emoji="home", style=PRIMARY)])


# ───────────────────────────── пользователь ─────────────────────────────

def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [btn("Запустить инвайтинг", "inv:start", emoji="rocket", style=SUCCESS)],
        [btn("Мои аккаунты", "acc:menu", emoji="users", style=PRIMARY),
         btn("Подписка", "buy", emoji="card", style=PRIMARY)],
        [btn("Промокод", "promo", emoji="gift"), btn("Помощь", "help", emoji="info")],
    ]
    if is_admin:
        rows.append([btn("Админ-панель", "adm:menu", emoji="admin")])
    return _kb(*rows)


def invite_confirm() -> InlineKeyboardMarkup:
    return _kb([btn("Начать", "inv:go", emoji="rocket", style=SUCCESS),
                btn("Отмена", "menu", emoji="cancel", style=DANGER)])


def task_progress(task_id: int) -> InlineKeyboardMarkup:
    return _kb([btn("Остановить задачу", f"task:stop:{task_id}", emoji="stop", style=DANGER)])


def plans_list(plans: list[Plan]) -> InlineKeyboardMarkup:
    rows = [[btn(f"{p.name} · {fmt_usd(p.price)}", f"buy:{p.id}", emoji="fire", style=PRIMARY)] for p in plans]
    rows.append([btn("Назад", "menu", emoji="back")])
    return _kb(*rows)


def _method_emoji(m: PaymentMethod) -> str:
    return "bank" if m.key == "uah" else "coin"


def plan_methods(plan_id: int, methods: list[PaymentMethod]) -> InlineKeyboardMarkup:
    buttons = [btn(m.title, f"pay:{plan_id}:{m.key}", emoji=_method_emoji(m), style=PRIMARY) for m in methods]
    return _kb(*chunk(buttons), [btn("К тарифам", "buy", emoji="back")])


def order_pay(order_id: int) -> InlineKeyboardMarkup:
    return _kb([btn("Я оплатил", f"order:paid:{order_id}", emoji="ok", style=SUCCESS),
                btn("Отмена", f"order:cancel:{order_id}", emoji="cancel", style=DANGER)])


def order_review(order_id: int) -> InlineKeyboardMarkup:
    return _kb([btn("Подтвердить", f"adm:order_ok:{order_id}", emoji="ok", style=SUCCESS),
                btn("Отклонить", f"adm:order_no:{order_id}", emoji="cancel", style=DANGER)])


# ───────────────────────────── аккаунты пользователя ─────────────────────────────

def accounts_menu(accounts: list[Account]) -> InlineKeyboardMarkup:
    dels = [btn(a.label or a.phone or f"#{a.id}", f"acc:del:{a.id}", emoji="trash", style=DANGER)
            for a in accounts]
    return _kb(
        [btn("По номеру", "acc:phone", emoji="phone", style=SUCCESS),
         btn("Файл .session", "acc:file", emoji="clip", style=SUCCESS)],
        *chunk(dels),
        [btn("Сбросить сон / спамблок", "acc:reset", emoji="refresh")] if accounts else [],
        [btn("Назад", "menu", emoji="back")],
    )


def keypad(ok_text: str) -> InlineKeyboardMarkup:
    """Цифровая клавиатура для ввода номера / кода."""
    def digit(d: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=d, callback_data=f"acc:pad:{d}")

    return _kb(
        [digit("1"), digit("2"), digit("3")],
        [digit("4"), digit("5"), digit("6")],
        [digit("7"), digit("8"), digit("9")],
        [InlineKeyboardButton(text="⌫", callback_data="acc:pad:del"), digit("0"),
         btn(ok_text, "acc:pad:ok", emoji="ok", style=SUCCESS)],
        [btn("Отмена", "acc:menu", emoji="cancel", style=DANGER)],
    )


def accounts_done() -> InlineKeyboardMarkup:
    return _kb([btn("Готово", "acc:menu", emoji="ok", style=SUCCESS)])


def help_menu() -> InlineKeyboardMarkup:
    rows = []
    if config.SUPPORT_USERNAME:
        rows.append([btn("Написать в поддержку", url=f"https://t.me/{config.SUPPORT_USERNAME}",
                         emoji="chat", style=PRIMARY)])
    rows.append([btn("Назад", "menu", emoji="back")])
    return _kb(*rows)


# ───────────────────────────── админ ─────────────────────────────

def admin_menu(pending: int = 0) -> InlineKeyboardMarkup:
    return _kb(
        [btn("Тарифы", "adm:plans", emoji="tag", style=PRIMARY),
         btn("Промокоды", "adm:promos", emoji="gift", style=PRIMARY)],
        [btn("Реквизиты", "adm:methods", emoji="wallet", style=PRIMARY),
         btn(f"Заявки{f' ({pending})' if pending else ''}", "adm:orders", emoji="receipt",
             style=SUCCESS if pending else PRIMARY)],
        [btn("Выдать доступ", "adm:grant", emoji="user"),
         btn("Статистика", "adm:stats", emoji="stats")],
        [btn("Эмодзи", "adm:emoji", emoji="sparkles")],
        [btn("В главное меню", "menu", emoji="home")],
    )


def admin_back(cb: str = "adm:menu") -> InlineKeyboardMarkup:
    return back(cb)


def admin_cancel(cb: str = "adm:menu") -> InlineKeyboardMarkup:
    return cancel(cb)


def admin_plans(plans: list[Plan]) -> InlineKeyboardMarkup:
    dels = [btn(p.name, f"adm:plan_del:{p.id}", emoji="trash", style=DANGER) for p in plans]
    return _kb(*chunk(dels),
               [btn("Добавить тариф", "adm:plan_add", emoji="plus", style=SUCCESS)],
               [btn("Назад", "adm:menu", emoji="back")])


def admin_promos(promos: list[Promocode]) -> InlineKeyboardMarkup:
    dels = [btn(p.code, f"adm:promo_del:{p.id}", emoji="trash", style=DANGER) for p in promos]
    return _kb(*chunk(dels),
               [btn("Создать промокод", "adm:promo_add", emoji="plus", style=SUCCESS)],
               [btn("Назад", "adm:menu", emoji="back")])


def promo_types() -> InlineKeyboardMarkup:
    return _kb(
        [btn(PromoType.TITLES[PromoType.ADD_DAYS], f"adm:ptype:{PromoType.ADD_DAYS}", emoji="calendar", style=PRIMARY),
         btn(PromoType.TITLES[PromoType.DISCOUNT], f"adm:ptype:{PromoType.DISCOUNT}", emoji="money", style=PRIMARY)],
        [btn("Отмена", "adm:promos", emoji="cancel", style=DANGER)],
    )


def admin_methods(methods: list[PaymentMethod]) -> InlineKeyboardMarkup:
    buttons = [btn(f"{m.title}{' ✓' if m.enabled else ''}", f"adm:method:{m.key}", emoji=_method_emoji(m),
                   style=SUCCESS if m.enabled else None) for m in methods]
    return _kb(*chunk(buttons), [btn("Назад", "adm:menu", emoji="back")])


def admin_method(m: PaymentMethod, auto_supported: bool = False, auto_on: bool = False) -> InlineKeyboardMarkup:
    toggle = (btn("Выключить", f"adm:mtoggle:{m.key}", emoji="stop", style=DANGER) if m.enabled
              else btn("Включить", f"adm:mtoggle:{m.key}", emoji="ok", style=SUCCESS))
    rate_btn = btn("Свой курс" if auto_on else "Курс к $", f"adm:mset:{m.key}:rate", emoji="money", style=PRIMARY)
    auto_row = []
    if auto_supported:
        auto_row = [btn("Авто-курс: вкл" if auto_on else "Включить авто-курс", f"adm:mauto:{m.key}",
                        emoji="refresh", style=SUCCESS if auto_on else PRIMARY)]
    return _kb(
        [btn("Адрес / карта", f"adm:mset:{m.key}:address", emoji="wallet", style=PRIMARY), rate_btn],
        auto_row,
        [toggle],
        [btn("Назад", "adm:methods", emoji="back")],
    )


def admin_orders(orders: list[Order]) -> InlineKeyboardMarkup:
    buttons = [btn(f"#{o.id} · {fmt_usd(o.amount_usd)}", f"adm:order:{o.id}", emoji="receipt", style=PRIMARY)
               for o in orders]
    return _kb(*chunk(buttons), [btn("Назад", "adm:menu", emoji="back")])


def emoji_list() -> InlineKeyboardMarkup:
    buttons = [
        btn(f"{title}{' ✓' if get_id(key) else ''}", f"adm:emo:{key}", emoji=key,
            style=SUCCESS if get_id(key) else None)
        for key, (_, title) in EMOJI.items()
    ]
    return _kb(*chunk(buttons), [btn("Назад", "adm:menu", emoji="back")])


def emoji_edit(key: str) -> InlineKeyboardMarkup:
    rows = []
    if get_id(key):
        rows.append([btn("Сбросить на обычный", f"adm:emo_reset:{key}", emoji="trash", style=DANGER)])
    rows.append([btn("Назад", "adm:emoji", emoji="back")])
    return _kb(*rows)


def confirm_delete(yes_cb: str, back_cb: str) -> InlineKeyboardMarkup:
    return _kb([btn("Да, удалить", yes_cb, emoji="trash", style=DANGER),
                btn("Нет", back_cb, emoji="back")])
