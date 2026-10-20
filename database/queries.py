from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

import config
from database import models as m
from database.models import (
    UNLIMITED, Account, InviteLog, Order, OrderStatus, PaymentMethod, Plan, Promocode,
    PromocodeActivation, PromoType, Task, TaskStatus, User, fmt_usd,
)


# ───────────────────────────── users ─────────────────────────────

def is_active(user: User) -> bool:
    return bool(user.sub_until and user.sub_until > m.utcnow())


async def get_or_create_user(tg_id: int, username: str | None = None) -> User:
    async with m.session() as s:
        user = (await s.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()
        changed = False
        if user is None:
            user = User(tg_id=tg_id, username=username, is_admin=tg_id in config.ADMIN_IDS)
            s.add(user)
            changed = True
        else:
            if username and user.username != username:
                user.username = username
                changed = True
            if tg_id in config.ADMIN_IDS and not user.is_admin:
                user.is_admin = True
                changed = True
        if changed:
            await s.commit()
        return user


async def get_user(user_id: int) -> User | None:
    async with m.session() as s:
        return await s.get(User, user_id)


async def get_user_by_tg(tg_id: int) -> User | None:
    async with m.session() as s:
        return (await s.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()


def _extend(user: User, days: int) -> None:
    """Продлевает подписку на days дней (от текущей даты окончания, если она в будущем)."""
    now = m.utcnow()
    if days:
        user.sub_until = max(user.sub_until or now, now) + timedelta(days=days)


async def grant(user_id: int, days: int) -> User:
    async with m.session() as s:
        user = await s.get(User, user_id)
        _extend(user, days)
        await s.commit()
        return user


# ───────────────────────────── plans ─────────────────────────────

async def list_plans() -> list[Plan]:
    async with m.session() as s:
        return list((await s.execute(select(Plan).order_by(Plan.price))).scalars())


async def get_plan(plan_id: int) -> Plan | None:
    async with m.session() as s:
        return await s.get(Plan, plan_id)


async def add_plan(name: str, price: int, days: int) -> Plan:
    async with m.session() as s:
        plan = Plan(name=name, price=price, duration_days=days, invite_limit=UNLIMITED)
        s.add(plan)
        await s.commit()
        return plan


async def delete_plan(plan_id: int) -> None:
    async with m.session() as s:
        await s.execute(delete(Plan).where(Plan.id == plan_id))
        await s.commit()


# ─────────────────────────── promocodes ───────────────────────────

async def list_promos(limit: int = 20) -> list[Promocode]:
    async with m.session() as s:
        return list((await s.execute(select(Promocode).order_by(Promocode.id.desc()).limit(limit))).scalars())


async def promo_exists(code: str) -> bool:
    async with m.session() as s:
        return (await s.execute(select(Promocode.id).where(Promocode.code == code.upper()))).first() is not None


async def create_promo(code: str, type_: str, value: int, max_activations: int) -> Promocode:
    async with m.session() as s:
        promo = Promocode(code=code.upper(), type=type_, value=value, max_activations=max_activations)
        s.add(promo)
        await s.commit()
        return promo


async def delete_promo(promo_id: int) -> None:
    async with m.session() as s:
        await s.execute(delete(Promocode).where(Promocode.id == promo_id))
        await s.commit()


async def activate_promocode(user_id: int, code: str) -> tuple[bool, str]:
    async with m.session() as s:
        promo = (await s.execute(
            select(Promocode).where(Promocode.code == code.strip().upper())
        )).scalar_one_or_none()
        if promo is None:
            return False, "Промокод не найден."
        if promo.type not in PromoType.ALL:  # старый тип (например, «инвайты»)
            return False, "Этот промокод больше не действует."
        if await s.get(PromocodeActivation, (user_id, promo.id)):
            return False, "Вы уже активировали этот промокод."
        # атомарно занимаем слот активации
        res = await s.execute(
            update(Promocode)
            .where(Promocode.id == promo.id,
                   or_(Promocode.max_activations <= 0, Promocode.used_count < Promocode.max_activations))
            .values(used_count=Promocode.used_count + 1)
        )
        if res.rowcount == 0:
            await s.rollback()
            return False, "Лимит активаций промокода исчерпан."

        user = await s.get(User, user_id)
        if promo.type == PromoType.ADD_DAYS:
            _extend(user, promo.value)
            text = f"Начислено {promo.value} дн. подписки."
        else:
            user.discount += promo.value
            text = f"Скидка {fmt_usd(promo.value)} будет применена к следующей покупке."
        s.add(PromocodeActivation(user_id=user_id, promo_id=promo.id))
        try:
            await s.commit()
        except IntegrityError:  # тот же код прислали дважды параллельно
            await s.rollback()
            return False, "Вы уже активировали этот промокод."
        return True, text


# ───────────────────────────── accounts ─────────────────────────────

async def account_exists(tg_user_id: int) -> bool:
    async with m.session() as s:
        return (await s.execute(
            select(Account.id).where(Account.tg_user_id == tg_user_id, Account.is_alive.is_(True))
        )).first() is not None


async def add_account(session_file: str, tg_user_id: int | None, owner_id: int,
                      phone: str | None = None, label: str | None = None) -> Account:
    async with m.session() as s:
        acc = Account(session_file=session_file, tg_user_id=tg_user_id, owner_id=owner_id,
                      phone=phone, label=label, daily_reset_on=m.today())
        s.add(acc)
        await s.commit()
        return acc


async def get_account(account_id: int) -> Account | None:
    async with m.session() as s:
        return await s.get(Account, account_id)


async def delete_account(account_id: int) -> None:
    async with m.session() as s:
        await s.execute(delete(Account).where(Account.id == account_id))
        await s.commit()


async def list_accounts(owner_id: int | None = None) -> list[Account]:
    """Аккаунты пользователя; owner_id=None — все."""
    async with m.session() as s:
        q = select(Account).order_by(Account.id)
        if owner_id is not None:
            q = q.where(Account.owner_id == owner_id)
        return list((await s.execute(q)).scalars())


async def _reset_daily(s) -> None:
    await s.execute(
        update(Account)
        .where(or_(Account.daily_reset_on.is_(None), Account.daily_reset_on < m.today()))
        .values(daily_invites_count=0, daily_reset_on=m.today())
    )


def _usable_filters(exclude: set[int], owner_id: int):
    return [
        Account.owner_id == owner_id,
        Account.is_alive.is_(True),
        Account.is_spamblocked.is_(False),
        Account.daily_invites_count < config.DAILY_INVITE_LIMIT,
        Account.id.notin_(list(exclude)),
    ]


async def get_available_accounts(exclude: set[int], owner_id: int) -> list[Account]:
    """Живые, без спамблока, не спящие, с остатком суточного лимита; самые «отдохнувшие» первыми."""
    async with m.session() as s:
        await _reset_daily(s)
        await s.commit()
        q = (
            select(Account)
            .where(*_usable_filters(exclude, owner_id),
                   or_(Account.sleep_until.is_(None), Account.sleep_until <= m.utcnow()))
            .order_by(Account.last_used.asc())
        )
        return list((await s.execute(q)).scalars())


MAX_ACCOUNT_WAIT = timedelta(minutes=30)


async def count_usable_accounts(exclude: set[int], owner_id: int) -> int:
    """Аккаунты, которые освободятся в обозримое время (занятые или спящие не дольше 30 мин)."""
    async with m.session() as s:
        await _reset_daily(s)
        await s.commit()
        q = select(func.count()).select_from(Account).where(
            *_usable_filters(exclude, owner_id),
            or_(Account.sleep_until.is_(None), Account.sleep_until <= m.utcnow() + MAX_ACCOUNT_WAIT),
        )
        return (await s.execute(q)).scalar_one()


async def touch_account(account_id: int, invited: bool) -> None:
    async with m.session() as s:
        values = {"last_used": m.utcnow()}
        if invited:
            values["daily_invites_count"] = Account.daily_invites_count + 1
        await s.execute(update(Account).where(Account.id == account_id).values(**values))
        await s.commit()


async def mark_spamblocked(account_id: int) -> None:
    async with m.session() as s:
        await s.execute(update(Account).where(Account.id == account_id).values(is_spamblocked=True))
        await s.commit()


async def mark_dead(account_id: int) -> None:
    async with m.session() as s:
        await s.execute(update(Account).where(Account.id == account_id).values(is_alive=False))
        await s.commit()


async def set_sleep(account_id: int, seconds: int) -> None:
    async with m.session() as s:
        await s.execute(
            update(Account).where(Account.id == account_id)
            .values(sleep_until=m.utcnow() + timedelta(seconds=seconds))
        )
        await s.commit()


async def reset_account_flags(owner_id: int) -> int:
    async with m.session() as s:
        res = await s.execute(
            update(Account)
            .where(Account.owner_id == owner_id,
                   or_(Account.is_spamblocked.is_(True), Account.sleep_until.is_not(None)))
            .values(is_spamblocked=False, sleep_until=None)
        )
        await s.commit()
        return res.rowcount


async def pool_stats(owner_id: int | None = None) -> dict:
    """Статистика аккаунтов пользователя (owner_id=None — всех)."""
    async with m.session() as s:
        await _reset_daily(s)
        await s.commit()
    accounts = await list_accounts(owner_id)
    working = [a for a in accounts if a.is_alive and not a.is_spamblocked]
    return {
        "total": len(accounts),
        "alive": sum(a.is_alive for a in accounts),
        "spam": sum(a.is_alive and a.is_spamblocked for a in accounts),
        "remaining": sum(max(0, config.DAILY_INVITE_LIMIT - a.daily_invites_count) for a in working),
    }


# ───────────────────────────── tasks ─────────────────────────────

async def create_task(user_id: int, target_chat: str, total: int) -> Task:
    async with m.session() as s:
        task = Task(user_id=user_id, target_chat=target_chat, total_count=total)
        s.add(task)
        await s.commit()
        return task


async def get_task(task_id: int) -> Task | None:
    async with m.session() as s:
        return await s.get(Task, task_id)


async def get_active_task(user_id: int) -> Task | None:
    async with m.session() as s:
        return (await s.execute(
            select(Task).where(Task.user_id == user_id, Task.status == TaskStatus.IN_PROGRESS)
        )).scalars().first()


async def update_task(task_id: int, **values) -> None:
    async with m.session() as s:
        await s.execute(update(Task).where(Task.id == task_id).values(**values))
        await s.commit()


async def stop_stale_tasks() -> None:
    """После падения/рестарта задачи in_progress уже никто не выполняет."""
    async with m.session() as s:
        await s.execute(
            update(Task).where(Task.status == TaskStatus.IN_PROGRESS).values(status=TaskStatus.STOPPED)
        )
        await s.commit()


async def add_log(task_id: int, account_id: int | None, target: str, result: str) -> None:
    async with m.session() as s:
        s.add(InviteLog(task_id=task_id, account_id=account_id, target=target[:64], result=result))
        await s.commit()


# ─────────────────────────── оплата ───────────────────────────

def format_amount(method: PaymentMethod, cents: int) -> str:
    if method.rate > 0:
        return _fmt(cents / 100 * method.rate, method)
    return f"{fmt_usd(cents)} в {method.currency}"


async def list_methods(enabled_only: bool = False) -> list[PaymentMethod]:
    async with m.session() as s:
        q = select(PaymentMethod).order_by(PaymentMethod.sort)
        if enabled_only:
            q = q.where(PaymentMethod.enabled.is_(True), PaymentMethod.address.is_not(None))
        return list((await s.execute(q)).scalars())


async def get_method(key: str) -> PaymentMethod | None:
    async with m.session() as s:
        return await s.get(PaymentMethod, key)


async def update_method(key: str, **values) -> PaymentMethod | None:
    async with m.session() as s:
        method = await s.get(PaymentMethod, key)
        if method is None:
            return None
        for k, v in values.items():
            setattr(method, k, v)
        if not method.address:
            method.enabled = False
        await s.commit()
        return method


async def create_order(user_id: int, plan_id: int, method_key: str) -> Order | None:
    """Создаёт заявку с зафиксированной ценой (со скидкой) и реквизитами."""
    async with m.session() as s:
        user = await s.get(User, user_id)
        plan = await s.get(Plan, plan_id)
        method = await s.get(PaymentMethod, method_key)
        if not (user and plan and method and method.enabled and method.address):
            return None
        if method.rate <= 0:
            return None  # без курса точную сумму не посчитать (курс ещё не подтянулся)
        discount = min(plan.price, user.discount)
        amount = plan.price - discount
        order = Order(user_id=user_id, plan_id=plan.id, plan_name=plan.name, days=plan.duration_days,
                      invites=plan.invite_limit, discount=discount, amount_usd=amount,
                      method=method.key, amount_text=await _unique_amount_text(s, method, amount),
                      address=method.address)
        s.add(order)
        await s.commit()
        return order


def _decimals(method: PaymentMethod) -> int:
    return 8 if method.currency == "BTC" else 2


def _fmt(value: float, method: PaymentMethod) -> str:
    d = _decimals(method)
    text = f"{value:.{d}f}"
    if d > 2:
        text = text.rstrip("0").rstrip(".")
    return f"{text} {method.currency}"


async def _unique_amount_text(s, method: PaymentMethod, cents: int) -> str:
    """Сумма в валюте оплаты, сдвинутая на 1–99 мельчайших единиц (копеек / центов / сатоши),
    чтобы не совпасть ни с одной открытой заявкой: по точной сумме видно, от кого платёж.
    Комментарий к переводу не нужен."""
    taken = set((await s.execute(
        select(Order.amount_text).where(
            Order.method == method.key,
            or_(Order.status == OrderStatus.PENDING,
                # брошенные заявки (реквизиты показали, «Я оплатил» не нажали) держат сумму сутки
                (Order.status == OrderStatus.NEW) & (Order.created_at > m.utcnow() - timedelta(days=1))))
    )).scalars())
    d = _decimals(method)
    unit = 10 ** d
    base = round(cents / 100 * method.rate * unit)  # в мельчайших единицах
    sign = -1 if base > 100 else 1  # обычно чуть дешевле; у копеечных сумм — чуть дороже
    for off in range(1, 100):  # ближайшая свободная: 450.78, 450.77, …
        text = _fmt((base + sign * off) / unit, method)
        if text not in taken:
            return text
    return _fmt((base + sign * 99) / unit, method)  # 99 открытых заявок с одной ценой — маловероятно


async def get_order(order_id: int) -> Order | None:
    async with m.session() as s:
        return await s.get(Order, order_id)


async def cancel_order(order_id: int, user_id: int) -> bool:
    async with m.session() as s:
        res = await s.execute(
            update(Order)
            .where(Order.id == order_id, Order.user_id == user_id, Order.status == OrderStatus.NEW)
            .values(status=OrderStatus.CANCELLED))
        await s.commit()
        return res.rowcount > 0


async def mark_order_paid(order_id: int, user_id: int) -> Order | None:
    """new → pending. None — заявка не найдена или уже отправлена."""
    async with m.session() as s:
        res = await s.execute(
            update(Order)
            .where(Order.id == order_id, Order.user_id == user_id, Order.status == OrderStatus.NEW)
            .values(status=OrderStatus.PENDING, paid_at=m.utcnow()))
        await s.commit()
        if res.rowcount == 0:
            return None
        return await s.get(Order, order_id)


async def set_order_admin_msgs(order_id: int, msgs: list[list[int]]) -> None:
    async with m.session() as s:
        await s.execute(update(Order).where(Order.id == order_id).values(admin_msgs=json.dumps(msgs)))
        await s.commit()


async def decide_order(order_id: int, admin_tg_id: int, confirm: bool) -> tuple[Order, User] | None:
    """pending → confirmed/rejected; при подтверждении начисляет тариф.
    None — заявку уже обработал кто-то другой."""
    async with m.session() as s:
        res = await s.execute(
            update(Order)
            .where(Order.id == order_id, Order.status == OrderStatus.PENDING)
            .values(status=OrderStatus.CONFIRMED if confirm else OrderStatus.REJECTED,
                    decided_by=admin_tg_id, decided_at=m.utcnow()))
        if res.rowcount == 0:
            await s.rollback()
            return None
        order = await s.get(Order, order_id)
        user = await s.get(User, order.user_id)
        if confirm:
            _extend(user, order.days)
            user.discount = max(0, user.discount - order.discount)
        await s.commit()
        return order, user


async def count_pending_orders(user_id: int) -> int:
    async with m.session() as s:
        return (await s.execute(
            select(func.count()).select_from(Order)
            .where(Order.user_id == user_id, Order.status == OrderStatus.PENDING))).scalar_one()


async def list_pending_orders(limit: int = 20) -> list[Order]:
    async with m.session() as s:
        return list((await s.execute(
            select(Order).where(Order.status == OrderStatus.PENDING).order_by(Order.paid_at).limit(limit)
        )).scalars())


async def admin_tg_ids() -> set[int]:
    async with m.session() as s:
        ids = set((await s.execute(select(User.tg_id).where(User.is_admin.is_(True)))).scalars())
    return ids | config.ADMIN_IDS


# ───────────────────────────── stats ─────────────────────────────

async def global_stats() -> dict:
    now = m.utcnow()
    day_start = datetime.combine(m.today(), datetime.min.time())
    async with m.session() as s:
        active_users = (await s.execute(
            select(func.count()).select_from(User).where(User.sub_until > now))).scalar_one()
        total_users = (await s.execute(select(func.count()).select_from(User))).scalar_one()
        invites_today = (await s.execute(
            select(func.count()).select_from(InviteLog)
            .where(InviteLog.result == "ok", InviteLog.created_at >= day_start))).scalar_one()
        running = (await s.execute(
            select(func.count()).select_from(Task).where(Task.status == TaskStatus.IN_PROGRESS))).scalar_one()
        pending = (await s.execute(
            select(func.count()).select_from(Order).where(Order.status == OrderStatus.PENDING))).scalar_one()
    pool = await pool_stats()
    return {"active_users": active_users, "total_users": total_users,
            "invites_today": invites_today, "running_tasks": running, "pending_orders": pending, **pool}
