from aiogram.fsm.state import State, StatesGroup


class InviteFSM(StatesGroup):
    chat = State()
    file = State()
    confirm = State()


class PromoFSM(StatesGroup):
    code = State()


class AddPlanFSM(StatesGroup):
    name = State()
    price = State()
    days = State()


class AddPromoFSM(StatesGroup):
    code = State()
    type = State()
    value = State()
    limit = State()


class AccountFSM(StatesGroup):
    phone = State()
    code = State()
    password = State()
    files = State()


class MethodFSM(StatesGroup):
    value = State()


class EmojiFSM(StatesGroup):
    wait = State()


class GrantFSM(StatesGroup):
    tg_id = State()
    amount = State()
