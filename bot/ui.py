"""Эмодзи (обычные + премиум) и конструктор цветных кнопок."""
from __future__ import annotations

import html
import json
import logging

from aiogram.types import InlineKeyboardButton

import config

log = logging.getLogger(__name__)

# Цвета кнопок (Bot API 9.4)
PRIMARY = "primary"   # синяя
SUCCESS = "success"   # зелёная
DANGER = "danger"     # красная

# ключ -> (обычный эмодзи-фолбэк, название в админке)
EMOJI: dict[str, tuple[str, str]] = {
    "wave": ("👋", "Приветствие"),
    "rocket": ("🚀", "Запуск"),
    "card": ("💳", "Подписка"),
    "gift": ("🎁", "Промокод"),
    "info": ("ℹ️", "Помощь"),
    "admin": ("🛠", "Админка"),
    "home": ("🏠", "Меню"),
    "back": ("⬅️", "Назад"),
    "cancel": ("❌", "Отмена"),
    "ok": ("✅", "Успех"),
    "stop": ("🛑", "Стоп"),
    "calendar": ("📅", "Срок"),
    "mail": ("📨", "Инвайты"),
    "tag": ("🏷", "Тарифы"),
    "users": ("👥", "Аккаунты"),
    "user": ("👤", "Пользователь"),
    "stats": ("📊", "Статистика"),
    "plus": ("➕", "Добавить"),
    "trash": ("🗑", "Удалить"),
    "refresh": ("♻️", "Сброс"),
    "link": ("🔗", "Ссылка"),
    "file": ("📄", "Файл"),
    "warn": ("⚠️", "Ошибка"),
    "star": ("⭐", "Звёзды"),
    "sparkles": ("✨", "Эмодзи"),
    "clock": ("⏳", "Ожидание"),
    "skip": ("⏭", "Пропуск"),
    "chat": ("💬", "Поддержка"),
    "money": ("💰", "Цена"),
    "clip": ("📎", "Вложение"),
    "list": ("📋", "Список"),
    "fire": ("🔥", "Акцент"),
    "infinity": ("♾", "Безлимит"),
    "phone": ("📱", "Телефон"),
    "key": ("🔑", "Код / пароль"),
    "receipt": ("🧾", "Заявки"),
    "wallet": ("👛", "Реквизиты"),
    "coin": ("🪙", "Крипта"),
    "bank": ("🏦", "Карта"),
}

_PATH = config.DB_PATH.parent / "emoji.json"
_ids: dict[str, str] = {}


def load() -> None:
    global _ids
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        _ids = {k: str(v) for k, v in data.items() if k in EMOJI and v}
    except FileNotFoundError:
        _ids = {}
    except Exception:
        log.exception("Не удалось прочитать %s", _PATH)
        _ids = {}


def _save() -> None:
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(_ids, ensure_ascii=False, indent=2), encoding="utf-8")


def set_id(key: str, emoji_id: str | None) -> None:
    if emoji_id:
        _ids[key] = emoji_id
    else:
        _ids.pop(key, None)
    _save()


def get_id(key: str) -> str | None:
    return _ids.get(key)


def e(key: str) -> str:
    """Эмодзи для текста сообщения (HTML): премиум, если настроен, иначе обычный."""
    fallback = EMOJI[key][0]
    emoji_id = _ids.get(key)
    if emoji_id:
        return f'<tg-emoji emoji-id="{html.escape(emoji_id)}">{fallback}</tg-emoji>'
    return fallback


def btn(text: str, cb: str | None = None, *, url: str | None = None, emoji: str | None = None,
        style: str | None = None) -> InlineKeyboardButton:
    """Кнопка: премиум-эмодзи идёт иконкой, обычный — в начале текста."""
    icon_id = _ids.get(emoji) if emoji else None
    label = f"{EMOJI[emoji][0]} {text}" if emoji and not icon_id else text
    return InlineKeyboardButton(text=label, callback_data=cb, url=url, style=style,
                                icon_custom_emoji_id=icon_id)


def chunk(buttons: list[InlineKeyboardButton], size: int = 2) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + size] for i in range(0, len(buttons), size)]


load()
