from aiogram import Router

from bot.handlers import accounts, admin, payments, user


def get_router() -> Router:
    root = Router(name="root")
    # Команды первыми — иначе /start и /admin посреди мастера попадут в FSM как введённый текст
    root.include_routers(user.commands, admin.commands)
    root.include_router(admin.router)  # админ-фильтр отсекает чужих, остальное идёт дальше
    root.include_routers(accounts.router, payments.router, user.router)
    return root
