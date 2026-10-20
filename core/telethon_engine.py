from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyDuplicatedError, AuthKeyUnregisteredError, ChannelPrivateError, ChannelsTooMuchError,
    ChatAdminRequiredError, ChatWriteForbiddenError, FloodWaitError, InviteHashExpiredError,
    InviteHashInvalidError, PasswordHashInvalidError, PeerFloodError, PhoneCodeEmptyError,
    PhoneCodeExpiredError, PhoneCodeInvalidError, PhoneNumberBannedError, PhoneNumberInvalidError,
    PhoneNumberUnoccupiedError, RPCError, SessionPasswordNeededError, SessionExpiredError, SessionRevokedError,
    UserAlreadyParticipantError, UserBannedInChannelError, UserBotError, UserChannelsTooMuchError,
    UserDeactivatedBanError, UserDeactivatedError, UserKickedError, UserNotMutualContactError,
    UserPrivacyRestrictedError, UsernameInvalidError, UsernameNotOccupiedError, UsersTooMuchError,
)
from telethon.tl.functions.channels import InviteToChannelRequest, JoinChannelRequest
from telethon.tl.functions.contacts import AddContactRequest, ImportContactsRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import Channel, ChatInviteAlready, InputPhoneContact, User

import config
from database import queries as q

log = logging.getLogger(__name__)

# ───────────────────────────── цели ─────────────────────────────

_USERNAME_RE = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{3,31})$")
_TME_RE = re.compile(r"^(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,31})/?$")
_PHONE_RE = re.compile(r"^\+?(\d{7,15})$")
_INVITE_HASH_RE = re.compile(r"(?:t\.me/\+|t\.me/joinchat/|tg://join\?invite=)([\w-]+)")
CHAT_LINK_RE = re.compile(
    r"^(?:https?://)?t\.me/(?:\+|joinchat/)?[\w-]{4,}/?$|^@[A-Za-z][A-Za-z0-9_]{3,31}$"
)

Target = tuple[str, str]  # ("username", "@name") | ("phone", "+7999...")


def normalize_target(raw: str) -> Target | None:
    s = raw.strip()
    if not s:
        return None
    if mt := _TME_RE.match(s):
        return "username", "@" + mt.group(1)
    if mp := _PHONE_RE.match(re.sub(r"[\s\-()]", "", s)):
        return "phone", "+" + mp.group(1)
    if mu := _USERNAME_RE.match(s):
        return "username", "@" + mu.group(1)
    return None


def parse_targets(text: str) -> tuple[list[Target], int]:
    """Возвращает (уникальные цели, число нераспознанных строк)."""
    seen: set[Target] = set()
    result: list[Target] = []
    invalid = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        t = normalize_target(line)
        if t is None:
            invalid += 1
        elif t not in seen:
            seen.add(t)
            result.append(t)
    return result, invalid


# ───────────────────────────── исключения / результаты ─────────────────────────────

class Outcome(str, Enum):
    OK = "ok"
    PRIVACY = "privacy"      # приватность / не взаимный контакт — пропуск
    ALREADY = "already"      # уже в группе — пропуск
    NOT_FOUND = "not_found"  # аккаунт не найден — пропуск
    FAILED = "failed"        # прочая ошибка по этому юзеру
    ABORTED = "aborted"      # остановлено во время паузы

    @property
    def needs_pause(self) -> bool:
        return self not in (Outcome.NOT_FOUND, Outcome.ABORTED)


class AccountDead(Exception):
    """Сессия невалидна / аккаунт забанен."""


class AccountCannotInvite(Exception):
    """Этот аккаунт не может приглашать в данную группу (нет прав, не найдена и т.п.)."""


class AccountLimited(Exception):
    """Временный лимит без FloodWait (например, на импорт контактов)."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"limited for {seconds}s")
        self.seconds = seconds


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Спит `seconds`; True — если за это время запросили остановку."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


# ───────────────────────────── операции ─────────────────────────────

_DEAD_ERRORS = (
    AuthKeyUnregisteredError, AuthKeyDuplicatedError, UserDeactivatedError, UserDeactivatedBanError,
    SessionRevokedError, SessionExpiredError,
)
_NO_RIGHTS_ERRORS = (
    ChatAdminRequiredError, ChatWriteForbiddenError, ChannelPrivateError, ChannelsTooMuchError,
    UsersTooMuchError,
)


async def resolve_channel(client: TelegramClient, link: str) -> Any:
    """Находит супергруппу по ссылке/@username; при необходимости вступает аккаунтом."""
    link = link.strip()
    try:
        if mh := _INVITE_HASH_RE.search(link):
            invite_hash = mh.group(1)
            info = await client(CheckChatInviteRequest(invite_hash))
            if isinstance(info, ChatInviteAlready):
                entity = info.chat
            else:
                try:
                    entity = (await client(ImportChatInviteRequest(invite_hash))).chats[0]
                except UserAlreadyParticipantError:
                    entity = (await client(CheckChatInviteRequest(invite_hash))).chat
            if not isinstance(entity, Channel):
                raise AccountCannotInvite("Это обычная группа, нужна супергруппа")
            return entity

        entity = await client.get_entity(link)
        if not isinstance(entity, Channel):
            raise AccountCannotInvite("Это не супергруппа/канал")
        try:
            await client(JoinChannelRequest(entity))
        except UserAlreadyParticipantError:
            pass
        return entity
    except (InviteHashExpiredError, InviteHashInvalidError, UsernameNotOccupiedError,
            UsernameInvalidError, ValueError) as e:
        raise AccountCannotInvite(f"Группа недоступна: {type(e).__name__}") from e
    except (FloodWaitError, PeerFloodError):
        raise
    except _DEAD_ERRORS as e:
        raise AccountDead(type(e).__name__) from e
    except RPCError as e:  # нет прав, заявка на вступление (InviteRequestSent) и т.п.
        raise AccountCannotInvite(type(e).__name__) from e


async def invite_user(client: TelegramClient, channel: Any, target: Target, stop: asyncio.Event) -> Outcome:
    """Двухэтапный инвайт: контакты → пауза → InviteToChannel.

    Бросает FloodWaitError / PeerFloodError / UserBannedInChannelError (ротация аккаунта),
    AccountDead, AccountCannotInvite. Остальное превращает в Outcome.
    """
    kind, value = target
    try:
        # 1–2. Получаем юзера и добавляем в контакты
        if kind == "phone":
            # get_entity по номеру работает только для уже имеющихся контактов,
            # поэтому номер сначала импортируем — результат содержит User
            res = await client(ImportContactsRequest([InputPhoneContact(
                client_id=random.randrange(1, 2**62), phone=value,
                first_name=f"c{random.randrange(10**6, 10**7)}", last_name="")]))
            if not res.users:
                if res.retry_contacts:  # Telegram отложил импорт — лимит аккаунта
                    raise AccountLimited(3600)
                return Outcome.NOT_FOUND
            user = res.users[0]
        else:
            user = await client.get_entity(value)
            if not isinstance(user, User) or user.deleted:
                return Outcome.NOT_FOUND
            if user.bot:
                return Outcome.FAILED
            if user.phone:
                await client(ImportContactsRequest([InputPhoneContact(
                    client_id=random.randrange(1, 2**62), phone="+" + user.phone.lstrip("+"),
                    first_name=user.first_name or value, last_name=user.last_name or "")]))
            else:
                await client(AddContactRequest(
                    id=user, first_name=user.first_name or value.lstrip("@"),
                    last_name=user.last_name or "", phone=""))

        # 3. Пауза для синхронизации адресной книги
        if await sleep_or_stop(stop, random.uniform(*config.CONTACT_SYNC_PAUSE)):
            return Outcome.ABORTED

        # 4. Инвайт
        result = await client(InviteToChannelRequest(channel, [user]))
        if getattr(result, "missing_invitees", None):
            return Outcome.PRIVACY
        return Outcome.OK

    except UserPrivacyRestrictedError:
        return Outcome.PRIVACY
    except UserNotMutualContactError:
        return Outcome.PRIVACY
    except UserAlreadyParticipantError:
        return Outcome.ALREADY
    except (UsernameNotOccupiedError, UsernameInvalidError, ValueError):
        return Outcome.NOT_FOUND
    except (UserChannelsTooMuchError, UserBotError, UserKickedError):
        return Outcome.FAILED
    except (FloodWaitError, PeerFloodError, UserBannedInChannelError):
        raise
    except _DEAD_ERRORS as e:
        raise AccountDead(type(e).__name__) from e
    except _NO_RIGHTS_ERRORS as e:
        raise AccountCannotInvite(type(e).__name__) from e
    except RPCError as e:
        log.warning("RPC-ошибка при инвайте %s: %s", value, e)
        return Outcome.FAILED


# ───────────────────────────── пул аккаунтов ─────────────────────────────

class LoginError(Exception):
    """Понятная пользователю ошибка входа."""


@dataclass
class _PendingLogin:
    client: TelegramClient
    path: Path
    phone: str
    phone_code_hash: str
    started: float = field(default_factory=time.monotonic)


LOGIN_TTL = 600  # незавершённый вход живёт 10 минут


class AccountPool:
    def __init__(self) -> None:
        self._clients: dict[int, TelegramClient] = {}
        self._verified: set[int] = set()
        self._busy: set[int] = set()
        self._acquire_lock = asyncio.Lock()
        self._logins: dict[int, _PendingLogin] = {}  # owner_id -> незавершённый вход по номеру
        self._login_locks: dict[int, asyncio.Lock] = {}

    def _login_lock(self, owner_id: int) -> asyncio.Lock:
        return self._login_locks.setdefault(owner_id, asyncio.Lock())

    async def _purge_expired_logins(self) -> None:
        """Закрывает брошенные входы (человек ушёл, не введя код)."""
        now = time.monotonic()
        for owner_id, login in list(self._logins.items()):
            # занятый вход не трогаем: его владелец прямо сейчас вводит код/пароль
            if now - login.started > LOGIN_TTL and not self._login_lock(owner_id).locked():
                await self._cancel_unlocked(owner_id)

    @staticmethod
    def _make_client(path: Path) -> TelegramClient:
        return TelegramClient(str(path), config.API_ID, config.API_HASH)

    @staticmethod
    def _label(me) -> str:
        return f"@{me.username}" if me.username else (me.first_name or str(me.id))

    async def _register(self, client: TelegramClient, path: Path, owner_id: int) -> str:
        """Регистрирует авторизованный клиент в БД. Возвращает подпись аккаунта."""
        me = await client.get_me()
        label = self._label(me)
        if await q.account_exists(me.id):
            raise LoginError(f"{label} уже подключён")
        phone = f"+{me.phone}" if me.phone else None
        await q.add_account(path.name, me.id, owner_id, phone=phone, label=label)
        return label

    # ───────── загрузка .session ─────────

    async def add_session_file(self, path: Path, owner_id: int) -> tuple[bool, str]:
        try:
            client = self._make_client(path)  # битый файл падает уже здесь
        except Exception:
            return False, "файл повреждён или это не сессия Telethon"
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return False, "сессия не авторизована"
            return True, await self._register(client, path, owner_id)
        except LoginError as e:
            return False, str(e)
        except Exception as e:  # битый файл, нет сети и т.п.
            return False, f"ошибка: {type(e).__name__}"
        finally:
            await client.disconnect()

    # ───────── вход по номеру ─────────

    async def login_start(self, owner_id: int, phone: str) -> None:
        await self._purge_expired_logins()
        async with self._login_lock(owner_id):
            await self._cancel_unlocked(owner_id)
            await self._start_unlocked(owner_id, phone)

    async def _start_unlocked(self, owner_id: int, phone: str) -> None:
        config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        path = config.SESSIONS_DIR / f"u{owner_id}_{phone.lstrip('+')}_{int(time.time())}.session"
        client = self._make_client(path)
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
        except Exception as e:
            await client.disconnect()
            unlink_session(path)
            raise LoginError(_login_error_text(e)) from e
        self._logins[owner_id] = _PendingLogin(client, path, phone, sent.phone_code_hash)

    async def _pending(self, owner_id: int) -> _PendingLogin:
        login = self._logins.get(owner_id)
        if login is None or time.monotonic() - login.started > LOGIN_TTL:
            await self._cancel_unlocked(owner_id)
            raise LoginError("Время входа истекло, начните заново")
        return login

    async def login_code(self, owner_id: int, code: str) -> str | None:
        """Возвращает подпись аккаунта или None, если нужен пароль 2FA."""
        async with self._login_lock(owner_id):
            return await self._code_unlocked(owner_id, code)

    async def _code_unlocked(self, owner_id: int, code: str) -> str | None:
        login = await self._pending(owner_id)
        try:
            await login.client.sign_in(phone=login.phone, code=code, phone_code_hash=login.phone_code_hash)
        except SessionPasswordNeededError:
            return None
        except (PhoneCodeInvalidError, PhoneCodeEmptyError):
            raise LoginError("Неверный код, попробуйте ещё раз")
        except Exception as e:
            await self._cancel_unlocked(owner_id)
            raise LoginError(_login_error_text(e)) from e
        return await self._login_finish(owner_id)

    async def login_password(self, owner_id: int, password: str) -> str:
        async with self._login_lock(owner_id):
            return await self._password_unlocked(owner_id, password)

    async def _password_unlocked(self, owner_id: int, password: str) -> str:
        login = await self._pending(owner_id)
        try:
            await login.client.sign_in(password=password)
        except PasswordHashInvalidError:
            raise LoginError("Неверный пароль 2FA, попробуйте ещё раз")
        except Exception as e:
            await self._cancel_unlocked(owner_id)
            raise LoginError(_login_error_text(e)) from e
        return await self._login_finish(owner_id)

    async def _login_finish(self, owner_id: int) -> str:
        login = self._logins.pop(owner_id)
        try:
            return await self._register(login.client, login.path, owner_id)
        except LoginError:
            with contextlib.suppress(Exception):
                await login.client.log_out()  # дубль — не оставляем лишнюю сессию
            unlink_session(login.path)
            raise
        finally:
            await login.client.disconnect()

    async def login_cancel(self, owner_id: int) -> None:
        async with self._login_lock(owner_id):
            await self._cancel_unlocked(owner_id)

    async def _cancel_unlocked(self, owner_id: int) -> None:
        login = self._logins.pop(owner_id, None)
        if login is not None:
            try:
                await login.client.disconnect()
            finally:
                unlink_session(login.path)

    # ───────── работа с пулом ─────────

    async def acquire(self, exclude: set[int], owner_id: int):
        """Берёт свободный доступный аккаунт владельца и помечает его занятым."""
        async with self._acquire_lock:
            accounts = await q.get_available_accounts(exclude | self._busy, owner_id)
            if not accounts:
                return None
            acc = accounts[0]
            self._busy.add(acc.id)
            return acc

    def release(self, account_id: int) -> None:
        self._busy.discard(account_id)

    def is_busy(self, account_id: int) -> bool:
        return account_id in self._busy

    async def client_for(self, acc) -> TelegramClient:
        client = self._clients.get(acc.id)
        if client is None:
            client = self._clients[acc.id] = self._make_client(config.SESSIONS_DIR / acc.session_file)
        try:
            if not client.is_connected():
                await client.connect()
            if acc.id not in self._verified:
                if not await client.is_user_authorized():
                    raise AccountDead("not authorized")
                self._verified.add(acc.id)
        except _DEAD_ERRORS as e:
            raise AccountDead(type(e).__name__) from e
        return client

    async def drop(self, account_id: int) -> None:
        """Закрывает клиент аккаунта."""
        self._verified.discard(account_id)
        client = self._clients.pop(account_id, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                log.exception("Ошибка при закрытии сессии")

    async def remove(self, acc) -> None:
        """Удаляет аккаунт из пула и его файл сессии."""
        self._busy.add(acc.id)  # задача не должна взять его, пока удаляем
        try:
            await self.drop(acc.id)
            await q.delete_account(acc.id)
            unlink_session(config.SESSIONS_DIR / acc.session_file)
        finally:
            self._busy.discard(acc.id)

    async def disconnect_all(self) -> None:
        for owner_id in list(self._logins):
            await self._cancel_unlocked(owner_id)
        for client in self._clients.values():
            try:
                await client.disconnect()
            except Exception:
                log.exception("Ошибка при закрытии сессии")
        self._clients.clear()
        self._verified.clear()


def unlink_session(path: Path) -> None:
    """Удаляет файл сессии (и журнал SQLite)."""
    for p in (path, path.with_name(path.name + "-journal")):
        with contextlib.suppress(OSError):  # на Windows файл может быть ещё занят
            p.unlink(missing_ok=True)


def _login_error_text(e: Exception) -> str:
    if isinstance(e, FloodWaitError):
        return f"Слишком много попыток, подождите {e.seconds} сек"
    return {
        PhoneNumberInvalidError: "Неверный номер телефона",
        PhoneNumberBannedError: "Номер заблокирован в Telegram",
        PhoneNumberUnoccupiedError: "На этот номер нет аккаунта Telegram",
        PhoneCodeExpiredError: "Код истёк, начните заново",
    }.get(type(e), f"Ошибка входа: {type(e).__name__}")
