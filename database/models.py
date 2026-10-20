from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, event,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Наивный UTC (SQLite не хранит tzinfo)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def today() -> date:
    return utcnow().date()


UNLIMITED = -1  # invite_limit: инвайтинг по подписке без лимита (колонка оставлена для совместимости)


def fmt_usd(cents: int) -> str:
    return f"${cents // 100}" if cents % 100 == 0 else f"${cents / 100:.2f}"


class PromoType:
    DISCOUNT = "discount_usd"  # value — в центах
    ADD_DAYS = "add_days"
    ALL = (DISCOUNT, ADD_DAYS)
    TITLES = {
        DISCOUNT: "Скидка ($)",
        ADD_DAYS: "Дни подписки",
    }


class TaskStatus:
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    STOPPED = "stopped"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    sub_until: Mapped[datetime | None] = mapped_column(DateTime)
    balance_invites: Mapped[int] = mapped_column(Integer, default=0)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # Разовая скидка из промокода в центах (применяется к следующей покупке)
    discount: Mapped[int] = mapped_column("discount_rub", Integer, default=0)
    # Безлимитные инвайты действуют до этой даты
    unlimited_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    price: Mapped[int] = mapped_column(Integer)  # в центах USD
    duration_days: Mapped[int] = mapped_column(Integer)
    invite_limit: Mapped[int] = mapped_column(Integer)  # UNLIMITED = безлимит



class Promocode(Base):
    __tablename__ = "promocodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    type: Mapped[str] = mapped_column(String(16))
    value: Mapped[int] = mapped_column(Integer)
    max_activations: Mapped[int] = mapped_column(Integer, default=0)  # 0 = без лимита
    used_count: Mapped[int] = mapped_column(Integer, default=0)


class PromocodeActivation(Base):
    __tablename__ = "promocode_activations"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promocodes.id", ondelete="CASCADE"), primary_key=True)
    activated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_file: Mapped[str] = mapped_column(String(255), unique=True)  # имя файла в SESSIONS_DIR
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # чей аккаунт: задачи пользователя работают только через его аккаунты
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    phone: Mapped[str | None] = mapped_column(String(32))
    label: Mapped[str | None] = mapped_column(String(64))
    is_alive: Mapped[bool] = mapped_column(Boolean, default=True)
    is_spamblocked: Mapped[bool] = mapped_column(Boolean, default=False)
    sleep_until: Mapped[datetime | None] = mapped_column(DateTime)  # FloodWait
    last_used: Mapped[datetime | None] = mapped_column(DateTime)
    daily_invites_count: Mapped[int] = mapped_column(Integer, default=0)
    daily_reset_on: Mapped[date | None] = mapped_column(Date)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    target_chat: Mapped[str] = mapped_column(String(255))
    total_count: Mapped[int] = mapped_column(Integer)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=TaskStatus.IN_PROGRESS, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PaymentMethod(Base):
    """Способ оплаты; реквизиты задаёт админ."""
    __tablename__ = "payment_methods"

    key: Mapped[str] = mapped_column(String(16), primary_key=True)
    title: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(8))
    # сколько единиц валюты за $1; 0 — показывать сумму в $ (оплата эквивалентом)
    rate: Mapped[float] = mapped_column(Float, default=0)
    # курс подтягивается автоматически (None = по умолчанию да, если есть источник — см. core/rates.py)
    auto_rate: Mapped[bool | None] = mapped_column(Boolean)
    rate_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    address: Mapped[str | None] = mapped_column(String(255))
    memo: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sort: Mapped[int] = mapped_column(Integer, default=0)


DEFAULT_METHODS = [
    # key, title, currency, rate, sort
    ("uah", "Карта (грн)", "грн", 41.0, 1),
    ("ton", "USDT · TON", "USDT", 1.0, 2),
    ("trc20", "USDT · TRC20", "USDT", 1.0, 3),
    ("erc20", "USDT · ERC20", "USDT", 1.0, 4),
    ("bep20", "USDT · BEP20", "USDT", 1.0, 5),
    ("btc", "BTC", "BTC", 0.0, 6),
]


class OrderStatus:
    NEW = "new"              # реквизиты показаны
    PENDING = "pending"      # пользователь нажал «Я оплатил», ждёт проверки
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    plan_id: Mapped[int] = mapped_column(Integer)
    # условия тарифа и реквизиты фиксируются на момент заказа
    plan_name: Mapped[str] = mapped_column(String(64))
    days: Mapped[int] = mapped_column(Integer)
    invites: Mapped[int] = mapped_column(Integer)
    discount: Mapped[int] = mapped_column(Integer, default=0)   # центы
    amount_usd: Mapped[int] = mapped_column(Integer)            # центы, уже со скидкой
    method: Mapped[str] = mapped_column(String(16))
    amount_text: Mapped[str] = mapped_column(String(64))
    address: Mapped[str] = mapped_column(String(255))
    memo: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default=OrderStatus.NEW, index=True)
    admin_msgs: Mapped[str | None] = mapped_column(String(1024))  # JSON [[chat_id, msg_id], ...]
    decided_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


class InviteLog(Base):
    __tablename__ = "invite_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)
    account_id: Mapped[int | None] = mapped_column(Integer)
    target: Mapped[str] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def session() -> AsyncSession:
    assert _sessionmaker is not None, "init_db() не вызван"
    return _sessionmaker()


async def init_db(path: Path) -> None:
    global _engine, _sessionmaker
    path.parent.mkdir(parents=True, exist_ok=True)
    _engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    @event.listens_for(_engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
        await conn.run_sync(_seed_methods)


def _seed_methods(conn) -> None:
    existing = {row[0] for row in conn.exec_driver_sql("SELECT key FROM payment_methods")}
    for key, title, currency, rate, sort in DEFAULT_METHODS:
        if key not in existing:
            conn.exec_driver_sql(
                "INSERT INTO payment_methods (key, title, currency, rate, enabled, sort) "
                "VALUES (?, ?, ?, ?, 0, ?)", (key, title, currency, rate, sort))


def _add_missing_columns(conn) -> None:
    """Простая миграция: добавляет новые nullable-колонки в уже существующие таблицы."""
    for table in Base.metadata.sorted_tables:
        existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table.name})")}
        for col in table.columns:
            if col.name not in existing and col.nullable:
                ddl = col.type.compile(dialect=conn.dialect)
                conn.exec_driver_sql(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl}")


async def close_db() -> None:
    if _engine is not None:
        await _engine.dispose()
