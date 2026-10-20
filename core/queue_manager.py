from __future__ import annotations

import asyncio
import html
import logging
import random
import time
from dataclasses import dataclass, field

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from telethon.errors import FloodWaitError, PeerFloodError, UserBannedInChannelError

import config
from bot import keyboards as kb
from bot.ui import e
from core.telethon_engine import (
    AccountCannotInvite, AccountDead, AccountLimited, AccountPool, Outcome, Target, invite_user, resolve_channel,
    sleep_or_stop,
)
from database import queries as q
from database.models import TaskStatus

log = logging.getLogger(__name__)

EDIT_INTERVAL = 3.0  # не чаще, чем раз в N секунд правим сообщение с прогрессом


class NoAccounts(Exception):
    pass


@dataclass
class RunningTask:
    task_id: int
    user_id: int
    chat_id: int
    message_id: int
    target_chat: str
    targets: list[Target]
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    success: int = 0
    skipped: int = 0
    failed: int = 0
    done: int = 0
    note: str = "clock:В очереди…"
    last_error: str = ""
    started: bool = False
    bad_accounts: set[int] = field(default_factory=set)  # аккаунты, не подходящие для этой группы
    channels: dict[int, object] = field(default_factory=dict)  # account_id -> entity группы
    used_accounts: set[int] = field(default_factory=set)  # чьи клиенты закрыть после задачи
    _last_edit: float = 0.0


def _bar(done: int, total: int, width: int = 12) -> str:
    filled = round(width * done / total) if total else 0
    return "▓" * filled + "░" * (width - filled)


class QueueManager:
    def __init__(self, bot: Bot, pool: AccountPool) -> None:
        self.bot = bot
        self.pool = pool
        self._queue: asyncio.Queue[int | None] = asyncio.Queue()
        self._tasks: dict[int, RunningTask] = {}
        self._workers: list[asyncio.Task] = []
        self._starting: set[int] = set()

    # ───────── жизненный цикл ─────────

    def start(self) -> None:
        self._workers = [
            asyncio.create_task(self._worker(), name=f"invite-worker-{i}")
            for i in range(config.MAX_CONCURRENT_TASKS)
        ]

    async def shutdown(self) -> None:
        for rt in self._tasks.values():
            rt.stop.set()
        for _ in self._workers:
            self._queue.put_nowait(None)
        if self._workers:
            _, pending = await asyncio.wait(self._workers, timeout=30)
            for w in pending:
                w.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    # ───────── публичный API ─────────

    def has_task(self, user_id: int) -> bool:
        return user_id in self._starting or any(rt.user_id == user_id for rt in self._tasks.values())

    async def start_task(self, user_id: int, chat_id: int, message_id: int,
                         target_chat: str, targets: list[Target]) -> int | None:
        """None — у пользователя уже есть задача (защита от двойного нажатия)."""
        if self.has_task(user_id):  # проверка и резерв без await между ними
            return None
        self._starting.add(user_id)
        try:
            task = await q.create_task(user_id, target_chat, len(targets))
            rt = RunningTask(task.id, user_id, chat_id, message_id, target_chat, targets)
            self._tasks[task.id] = rt
        finally:
            self._starting.discard(user_id)
        self._queue.put_nowait(task.id)
        await self._edit(rt, force=True)
        return task.id

    def stop_task(self, task_id: int, user_id: int) -> bool:
        rt = self._tasks.get(task_id)
        if rt is None or rt.user_id != user_id:
            return False
        rt.stop.set()
        rt.note = "stop:Останавливаю…"
        return True

    # ───────── воркеры ─────────

    async def _worker(self) -> None:
        while True:
            task_id = await self._queue.get()
            if task_id is None:
                return
            rt = self._tasks.get(task_id)
            if rt is None:
                continue
            try:
                await self._run(rt)
            except Exception:
                log.exception("Задача %s упала", task_id)
            finally:
                self._tasks.pop(task_id, None)

    async def _run(self, rt: RunningTask) -> None:
        status, reason = TaskStatus.COMPLETED, ""
        rt.started = True
        rt.note = "rocket:Запускаю…"
        try:
            await self._edit(rt, force=True)
            last = len(rt.targets) - 1
            for idx, target in enumerate(rt.targets):
                if rt.stop.is_set():
                    status = TaskStatus.STOPPED
                    break
                user = await q.get_user(rt.user_id)
                if not q.is_active(user):
                    status, reason = TaskStatus.STOPPED, "Подписка закончилась"
                    break

                rt.note = f"refresh:Обрабатываю {html.escape(target[1])}"
                await self._edit(rt)
                try:
                    outcome, account_id = await self._process_target(rt, target)
                except NoAccounts:
                    status, reason = TaskStatus.STOPPED, "Нет доступных аккаунтов"
                    if rt.last_error:
                        reason += f" ({rt.last_error})"
                    break
                if outcome is Outcome.ABORTED:
                    status = TaskStatus.STOPPED
                    break

                if outcome is Outcome.OK:
                    rt.success += 1
                elif outcome is Outcome.FAILED:
                    rt.failed += 1
                else:
                    rt.skipped += 1
                rt.done += 1
                await q.add_log(rt.task_id, account_id, target[1], outcome.value)
                await q.update_task(rt.task_id, success_count=rt.success,
                                    skipped_count=rt.skipped, failed_count=rt.failed)
                if reason:
                    break

                if outcome.needs_pause and idx < last:
                    delay = random.uniform(*config.USER_PAUSE)
                    rt.note = f"clock:Пауза {int(delay)} сек"
                    await self._edit(rt, force=True)
                    if await sleep_or_stop(rt.stop, delay):
                        status = TaskStatus.STOPPED
                        break
        except asyncio.CancelledError:
            status = TaskStatus.STOPPED
            raise
        except Exception:
            log.exception("Ошибка в задаче %s", rt.task_id)
            status, reason = TaskStatus.STOPPED, "Внутренняя ошибка"
        finally:
            await asyncio.shield(self._finalize(rt, status, reason))

    async def _process_target(self, rt: RunningTask, target: Target) -> tuple[Outcome, int | None]:
        """Выполняет инвайт, при FloodWait/спамблоке ротирует аккаунты."""
        target_bad: set[int] = set()  # аккаунты с временными сбоями на этом юзере
        while not rt.stop.is_set():
            exclude = rt.bad_accounts | target_bad
            acc = await self.pool.acquire(exclude, rt.user_id)
            if acc is None:
                if await q.count_usable_accounts(exclude, rt.user_id) > 0:
                    rt.note = "clock:Жду свободный аккаунт"
                    await self._edit(rt)
                    if await sleep_or_stop(rt.stop, 15):
                        break
                    continue
                if target_bad and await q.count_usable_accounts(rt.bad_accounts, rt.user_id) > 0:
                    return Outcome.FAILED, None
                raise NoAccounts
            try:
                rt.used_accounts.add(acc.id)
                client = await self.pool.client_for(acc)
                if acc.id not in rt.channels:
                    rt.channels[acc.id] = await resolve_channel(client, rt.target_chat)
                outcome = await invite_user(client, rt.channels[acc.id], target, rt.stop)
                await q.touch_account(acc.id, invited=outcome is Outcome.OK)
                return outcome, acc.id
            except FloodWaitError as e:
                log.warning("FloodWait %ss на аккаунте %s", e.seconds, acc.id)
                await q.set_sleep(acc.id, e.seconds + 5)
            except AccountLimited as e:
                log.warning("Лимит импорта контактов на аккаунте %s", acc.id)
                await q.set_sleep(acc.id, e.seconds)
            except (PeerFloodError, UserBannedInChannelError):
                log.warning("Спамблок на аккаунте %s", acc.id)
                await q.mark_spamblocked(acc.id)
            except AccountDead as e:
                log.warning("Аккаунт %s мёртв: %s", acc.id, e)
                await q.mark_dead(acc.id)
                await self.pool.drop(acc.id)
            except AccountCannotInvite as e:
                rt.bad_accounts.add(acc.id)
                rt.last_error = str(e)
            except Exception as e:  # сеть и прочее — пробуем другой аккаунт
                log.exception("Сбой аккаунта %s", acc.id)
                target_bad.add(acc.id)
                rt.last_error = type(e).__name__
            finally:
                self.pool.release(acc.id)
        return Outcome.ABORTED, None

    # ───────── отображение ─────────

    def _render(self, rt: RunningTask, final: str | None = None) -> str:
        total = len(rt.targets)
        pct = round(100 * rt.done / total) if total else 0
        lines = [
            final or f"{e('rocket')} <b>Инвайтинг</b>",
            f"{e('link')} {html.escape(rt.target_chat)}",
            "",
            f"<code>{_bar(rt.done, total)}</code> <b>{pct}%</b> · {rt.done}/{total}",
            "",
            f"<blockquote>{e('ok')} Приглашено: <b>{rt.success}</b>\n"
            f"{e('skip')} Пропущено: <b>{rt.skipped}</b>\n"
            f"{e('warn')} Ошибок: <b>{rt.failed}</b></blockquote>",
        ]
        if final is None and rt.note:
            key, _, note = rt.note.partition(":")  # note = "emoji_key:текст"
            lines += ["", f"{e(key)} <i>{note}</i>"]
        return "\n".join(lines)

    async def _edit(self, rt: RunningTask, force: bool = False, text: str | None = None,
                    markup=None) -> None:
        now = time.monotonic()
        if not force and now - rt._last_edit < EDIT_INTERVAL:
            return
        rt._last_edit = now
        for attempt in range(2):
            try:
                await self.bot.edit_message_text(
                    text or self._render(rt), chat_id=rt.chat_id, message_id=rt.message_id,
                    reply_markup=markup or kb.task_progress(rt.task_id),
                )
                return
            except TelegramRetryAfter as e:
                rt._last_edit = now + e.retry_after
                if not force or attempt or e.retry_after > 30:
                    return
                await asyncio.sleep(e.retry_after)  # итоговое сообщение важно не потерять
            except (TelegramBadRequest, TelegramForbiddenError):
                return  # «not modified», сообщение удалено, бот заблокирован

    async def _finalize(self, rt: RunningTask, status: str, reason: str) -> None:
        await q.update_task(rt.task_id, status=status, success_count=rt.success,
                            skipped_count=rt.skipped, failed_count=rt.failed)
        if status == TaskStatus.COMPLETED:
            title = f"{e('ok')} <b>Инвайтинг завершён</b>"
        else:
            title = f"{e('stop')} <b>Задача остановлена</b>"
        text = self._render(rt, final=title)
        if reason:
            text += f"\n\n{e('warn')} Причина: <b>{html.escape(reason)}</b>"
        await self._edit(rt, force=True, text=text, markup=kb.to_menu())
        # закрываем соединения аккаунтов: иначе у сотен пользователей висят сотни клиентов
        for account_id in rt.used_accounts:
            if not self.pool.is_busy(account_id):
                await self.pool.drop(account_id)
