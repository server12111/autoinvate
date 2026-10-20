import asyncio
import contextlib
import logging
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import config
from bot.handlers import get_router
from core.queue_manager import QueueManager
from core.rates import RateUpdater
from core.telethon_engine import AccountPool
from database import models
from database import queries as q

log = logging.getLogger("main")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    log.info("Данные: база %s, сессии %s", config.DB_PATH, config.SESSIONS_DIR)

    await models.init_db(config.DB_PATH)
    await q.stop_stale_tasks()

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    pool = AccountPool()
    manager = QueueManager(bot, pool)
    manager.start()
    rates = RateUpdater()
    rates.start()  # курсы BTC и гривны — сразу и далее каждые 15 минут

    dp = Dispatcher(storage=MemoryStorage(), manager=manager, pool=pool)
    dp.include_router(get_router())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows: SIGINT придёт как KeyboardInterrupt
            loop.add_signal_handler(sig, stop.set)

    polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    waiter = asyncio.create_task(stop.wait())
    try:
        log.info("Бот запущен")
        await asyncio.wait({polling, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        log.info("Останавливаюсь…")
        waiter.cancel()
        polling.cancel()
        await asyncio.gather(polling, waiter, return_exceptions=True)
        await rates.stop()
        await asyncio.shield(_shutdown(bot, manager, pool))
    if polling.done() and not polling.cancelled() and polling.exception():
        raise polling.exception()


async def _shutdown(bot: Bot, manager: QueueManager, pool: AccountPool) -> None:
    await manager.shutdown()      # останавливает задачи и правит их сообщения (бот ещё жив)
    await pool.disconnect_all()   # корректно закрывает Telethon-сессии
    await bot.session.close()
    await models.close_db()
    log.info("Остановлено")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
