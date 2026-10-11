"""Auto VIP Roblox v17 для FunPay Cardinal.

Автоматическая сдача VIP-серверов Roblox в аренду на FunPay:
автовыдача ссылки после оплаты, таймер аренды, выключение сервера и сброс
ссылки по окончании, пулы и категории серверов, несколько аренд у одного
покупателя, друзья и платное добавление в друзья, бонусы, два режима цен.
Вся настройка — в Telegram: /vip_config.
"""
from __future__ import annotations

import html
import logging
import math
import os
import queue
import random
import re
import sqlite3
import string
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from string import Formatter
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

try:
    from telebot.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Keyboard
except Exception:  # pragma: no cover - Cardinal всегда содержит pyTelegramBotAPI
    Button = Keyboard = None

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - bs4 входит в зависимости FunPayAPI
    BeautifulSoup = None

try:
    from tg_bot import CBT
except Exception:  # pragma: no cover
    CBT = None

try:
    from FunPayAPI.common.enums import OrderStatuses
except Exception:  # pragma: no cover
    OrderStatuses = None


NAME = "Auto VIP Roblox"
VERSION = "17.0.0"
DESCRIPTION = (
    "Автоматическая аренда VIP-серверов Roblox на FunPay: выдача ссылки после оплаты, "
    "таймер, выключение сервера и сброс ссылки по окончании, категории серверов, "
    "друзья и платное добавление, бонусы, режимы цен. Панель: /vip_config."
)
CREDITS = "@LuckyFunPay_Support"
UUID = "dbd0fc30-4033-4afc-8e36-0edcaa517466"
SETTINGS_PAGE = True

LOG = logging.getLogger("FPC.AutoVIPRoblox")

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PLUGIN_DIR, "AutoVIPRoblox_v17.db")
OLD_DB_PATH = os.path.join(PLUGIN_DIR, "AutoVIPRoblox_Monolith.db")

DEFAULT_PLACE_ID = "109983668079237"
DEFAULT_GAME_NAME = "Steal a Brainrot"
WARN_BEFORE = 15 * 60
SELECTION_TTL = 5 * 60
LOT_SYNC_INTERVAL = 5 * 60
CANCEL_WORDS = ("отмена", "cancel", "стоп")
TAG_RE = re.compile(r"#FR[A-Z0-9]{5}")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

DEFAULTS = {
    "event_mode": "0",
    "lot_management": "1",
    "global_proxy": "",
    "bonus_active": "1",
    "friends_module_active": "1",
    "friends_auto_remove": "1",
    "friends_default_limit": "0",
    "review_bonus_active": "1",
    "review_bonus_hours": "1",
    "paid_active": "0",
    "paid_price": "50",
    "paid_lifetime": "10",
    "paid_node": "402",
}

# Текстовые настройки платного добавления (сбрасываются кнопкой «♻️ Сбросить тексты»).
PAID_TEXT_DEFAULTS = {
    "paid_game": "[Прочие]",
    "paid_game_name_ru": "Roblox VIP сервер",
    "paid_game_name_en": "Roblox VIP server",
    "paid_type": "Услуги",
    "paid_title_ru": "Добавление друга на арендованный VIP-сервер Roblox",
    "paid_title_en": "Add a friend to the rented Roblox VIP server",
    "paid_desc_ru": (
        "Доплата за дополнительное добавление друга на ваш арендованный VIP-сервер Roblox.\n"
        "1 шт. = 1 друг. После оплаты бот сам добавит игрока на сервер."
    ),
    "paid_desc_en": (
        "Payment for an additional friend slot on your rented Roblox VIP server. "
        "1 pcs = 1 friend. After payment the bot adds the player to the server automatically."
    ),
    "paid_pay_msg_ru": "Спасибо за оплату! Бот уже добавляет друга на ваш VIP-сервер — смотрите сообщения в чате.",
    "paid_pay_msg_en": "Thank you! The bot is adding your friend to the VIP server — see the chat messages.",
}
DEFAULTS.update(PAID_TEXT_DEFAULTS)

EN_DESC_FILLER = (
    " This listing was created automatically for a specific buyer of a Roblox VIP server rental. "
    "If you did not request it, please do not pay for it - the payment will be refunded automatically."
)

# ---------------------------------------------------------------------------
# Шаблоны сообщений покупателю. vars — допустимые переменные и их описание.
# fallback — упрощённый текст, если в пользовательском шаблоне ошибка.
# ---------------------------------------------------------------------------
TEMPLATES: Dict[str, Dict[str, Any]] = {
    "post_payment": {
        "title": "Сообщение после выдачи сервера",
        "vars": {"link": "ссылка на сервер", "hours": "часы аренды (с учётом бонуса)", "order_id": "номер заказа",
                 "price": "цена заказа", "buyer_id": "ID покупателя", "server_id": "ID сервера", "chat_id": "ID чата"},
        "default": (
            "🎮 Ваш VIP-сервер Roblox готов!\n\n"
            "🔗 Ссылка: {link}\n"
            "⏰ Время аренды: {hours} ч.\n"
            "📦 Заказ: #{order_id}\n\n"
            "📋 Команды:\n"
            "!время — сколько осталось\n"
            "!ссылка — новая ссылка на сервер\n"
            "!друзья ник — добавить в друзья и на сервер\n"
            "!отзыв — бонусное время за отзыв\n"
            "!команды — все команды"
        ),
        "fallback": "🎮 VIP-сервер выдан!\n🔗 Ссылка: {link}\n⏰ Время: {hours} ч.\n📦 Заказ: #{order_id}\nКоманды: !команды",
    },
    "post_expire": {
        "title": "Сообщение об истечении аренды",
        "vars": {"order_id": "номер заказа", "hours": "часы аренды", "buyer_id": "ID покупателя",
                 "server_id": "ID сервера", "chat_id": "ID чата"},
        "default": (
            "⌛ Аренда по заказу #{order_id} завершена. Сервер отключён, ссылка больше не работает.\n\n"
            "🙏 Пожалуйста, подтвердите выполнение заказа: https://funpay.com/orders/{order_id}/\n"
            "Спасибо за покупку!"
        ),
        "fallback": "⌛ Аренда по заказу #{order_id} завершена. Подтвердите заказ: https://funpay.com/orders/{order_id}/",
    },
    "friend_invite": {
        "title": "Сообщение-приглашение",
        "vars": {"owner_profile_link": "ссылка на профиль владельца", "owner_username": "ник владельца",
                 "order_id": "номер заказа"},
        "default": (
            "🤝 Если вам меньше 13 лет или ссылка не пускает на сервер:\n"
            "1) Отправьте заявку в друзья владельцу сервера {owner_username}: {owner_profile_link}\n"
            "2) Напишите сюда: !друзья ВашНик\n"
            "Бот примет заявку и добавит вас на VIP-сервер (заказ #{order_id}).\n"
            "Играете компанией? Каждый друг отправляет заявку, а вы пишете !друзья НикДруга."
        ),
        "fallback": "🤝 Отправьте заявку в друзья: {owner_profile_link} и напишите !друзья ВашНик",
    },
    "friend_added": {
        "title": "Сообщение о добавлении",
        "vars": {"friend_name": "ник добавленного игрока", "order_id": "номер заказа"},
        "default": "✅ {friend_name} добавлен(а) в друзья и на VIP-сервер (заказ #{order_id}). Можно заходить!",
        "fallback": "✅ {friend_name} добавлен(а) на VIP-сервер.",
    },
    "friend_not_found": {
        "title": "Сообщение 'не найдено'",
        "vars": {"input": "то, что написал покупатель", "owner_profile_link": "ссылка на профиль владельца"},
        "default": (
            "❌ Не нашёл игрока или входящую заявку в друзья от «{input}».\n"
            "Отправьте заявку владельцу сервера: {owner_profile_link}\n"
            "Подождите минуту и повторите команду !друзья ник."
        ),
        "fallback": "❌ Заявка от «{input}» не найдена. Отправьте заявку: {owner_profile_link}",
    },
    "friend_limit": {
        "title": "Сообщение о лимите",
        "vars": {"limit": "установленный лимит", "order_id": "номер заказа"},
        "default": "❌ Достигнут лимит добавлений для этой аренды: {limit} (заказ #{order_id}).",
        "fallback": "❌ Достигнут лимит добавлений: {limit}.",
    },
    "paid_limit_reached": {
        "title": "Лимит исчерпан (после добавления)",
        "vars": {"limit": "лимит", "price": "цена за 1 добавление", "order_id": "номер заказа аренды"},
        "default": (
            "ℹ️ Бесплатные добавления для заказа #{order_id} закончились (лимит: {limit}).\n"
            "Следующих друзей можно добавить за доплату — {price} ₽ за одного. Просто напишите !друзья ник."
        ),
        "fallback": "ℹ️ Бесплатный лимит ({limit}) исчерпан. Следующие добавления — {price} ₽.",
    },
    "paid_lot_created": {
        "title": "Лот создан",
        "vars": {"link": "ссылка на лот", "price": "цена", "minutes": "через сколько минут лот удалится",
                 "friend_name": "ник игрока из команды", "limit": "лимит", "order_id": "номер заказа аренды"},
        "default": (
            "💳 Создал лот для доплаты за добавление {friend_name}: {link}\n"
            "Цена: {price} ₽ за 1 добавление.\n"
            "В поле «Количество» укажите, сколько друзей хотите добавить (1 шт. = 1 друг).\n"
            "⏳ Без оплаты лот удалится через {minutes} мин."
        ),
        "fallback": "💳 Лот для доплаты: {link} — {price} ₽ за 1 добавление. Удалится через {minutes} мин.",
    },
    "paid_lot_exists": {
        "title": "Лот уже есть",
        "vars": {"link": "ссылка на лот", "minutes": "сколько минут осталось до удаления",
                 "price": "цена", "friend_name": "ник игрока"},
        "default": (
            "ℹ️ У вас уже есть неоплаченный лот доплаты: {link}\n"
            "Цена: {price} ₽ за 1 добавление. Лот удалится через {minutes} мин."
        ),
        "fallback": "ℹ️ Ваш лот доплаты: {link} (удалится через {minutes} мин.)",
    },
    "paid_received": {
        "title": "Оплата получена",
        "vars": {"amount": "сколько добавлений куплено", "order_id": "номер заказа аренды"},
        "default": "✅ Оплата получена! Начислено добавлений: {amount} (аренда #{order_id}).",
        "fallback": "✅ Оплата получена. Добавлений: {amount}.",
    },
    "review_bonus": {
        "title": "Сообщение о бонусе за отзыв",
        "vars": {"bonus_hours": "начисленные часы", "order_id": "номер заказа",
                 "new_time": "новое время окончания (ДД.ММ ЧЧ:ММ)"},
        "default": "⭐ Спасибо за отзыв! К аренде по заказу #{order_id} добавлено {bonus_hours} ч. Новое окончание: {new_time}.",
        "fallback": "⭐ Спасибо за отзыв! Добавлено {bonus_hours} ч.",
    },
}
FRIEND_TEMPLATES = ("friend_invite", "friend_added", "friend_not_found", "friend_limit")
MAIN_TEMPLATES = ("post_payment", "post_expire")
PAID_TEMPLATES = ("paid_limit_reached", "paid_lot_created", "paid_lot_exists", "paid_received")

# Глобальное состояние
S: "Store | None" = None
CARDINAL: Any = None
STOP = threading.Event()
EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix="AutoVIP")
ORDER_QUEUE: "queue.Queue" = queue.Queue()
THREADS: List[threading.Thread] = []
GUARD = threading.RLock()
RUNNING: set = set()
LOCKS: Dict[str, threading.RLock] = {}
SELECTIONS: Dict[Tuple[str, str], Dict[str, Any]] = {}
HANDLED: Dict[str, float] = {}
FP_LOCK = threading.Lock()
SYNC_REQUEST = threading.Event()
API_CACHE: Dict[int, Tuple[str, str, Any]] = {}


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def now() -> float:
    return time.time()


def norm_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def parse_number(raw: Any) -> float:
    try:
        value = float(str(raw).strip().replace(",", "."))
    except (TypeError, ValueError):
        raise ValueError("нужно число") from None
    if not math.isfinite(value):
        raise ValueError("нужно число")
    return value


def fmt_num(value: Any) -> str:
    try:
        return f"{round(float(value), 2):g}"
    except (TypeError, ValueError):
        return str(value)


def fmt_left(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes = rest // 60
    return f"{hours}ч {minutes}м" if hours else f"{minutes}м"


def fmt_money(value: Any) -> str:
    try:
        return f"{float(value):,.2f}".replace(",", " ")
    except (TypeError, ValueError):
        return "0.00"


def fmt_dt(ts: float, with_year: bool = False) -> str:
    return time.strftime("%d.%m.%Y %H:%M" if with_year else "%d.%m %H:%M", time.localtime(ts))


def plural_hours(value: float) -> str:
    if float(value) != int(value):
        return "часа"
    n = int(value) % 100
    if 11 <= n <= 14:
        return "часов"
    n %= 10
    return "час" if n == 1 else ("часа" if 2 <= n <= 4 else "часов")


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]*>", "", str(text or ""))


def currency_symbol() -> str:
    try:
        cur = getattr(CARDINAL.account, "currency", None)
        text = str(cur) if cur is not None else ""
        if text and "." not in text and len(text) <= 3 and "UNKNOWN" not in text:
            return text
    except Exception:
        pass
    return "₽"


def short(text: str, size: int = 60) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= size else text[: size - 1] + "…"


# --- прокси ---------------------------------------------------------------
PROXY_RE = re.compile(r"^(?:(?P<user>[^:@\s/]+):(?P<pwd>[^@\s]+)@)?(?P<host>[A-Za-z0-9.\-]+):(?P<port>\d{1,5})$")


def normalize_proxy(raw: str) -> str:
    value = re.sub(r"^(?:https?|socks4a?|socks5h?)://", "", str(raw or "").strip(), flags=re.I).rstrip("/")
    match = PROXY_RE.match(value)
    if not match or not 1 <= int(match.group("port")) <= 65535:
        raise ValueError("bad proxy")
    return value


def proxy_dict(proxy: str) -> Optional[Dict[str, str]]:
    if not proxy:
        return None
    url = "http://" + proxy
    return {"http": url, "https": url}


def mask_proxy(proxy: str) -> str:
    match = PROXY_RE.match(proxy or "")
    if not match:
        return proxy or ""
    if match.group("user"):
        return f"{match.group('user')}:***@{match.group('host')}:{match.group('port')}"
    return proxy


# --- шаблоны --------------------------------------------------------------
def template_fields(text: str) -> Optional[List[str]]:
    """Список переменных шаблона или None, если в скобках ошибка."""
    try:
        result = []
        for _, field, spec, conv in Formatter().parse(text):
            if field is None:
                continue
            if spec or conv:
                return None
            result.append(field)
        return result
    except (ValueError, IndexError):
        return None


def template_problems(key: str, text: str) -> List[str]:
    fields = template_fields(text)
    if fields is None:
        return ["ошибка в фигурных скобках"]
    allowed = TEMPLATES[key]["vars"]
    return ["{" + f + "}" for f in fields if f not in allowed]


def _format(text: str, allowed: Dict[str, str], values: Dict[str, Any]) -> Optional[str]:
    fields = template_fields(text)
    if fields is None or any(f not in allowed for f in fields):
        return None
    try:
        return text.format_map({k: "" if values.get(k) is None else str(values.get(k)) for k in allowed})
    except (ValueError, KeyError, IndexError):
        return None


def get_template(key: str) -> str:
    value = S.get("tpl_" + key, "") if S else ""
    return value if value.strip() else TEMPLATES[key]["default"]


def render(key: str, **values: Any) -> str:
    info = TEMPLATES[key]
    text = _format(get_template(key), info["vars"], values)
    if text is None:
        text = _format(info["fallback"], info["vars"], values) or info["fallback"]
    return strip_html(text).strip()


# ---------------------------------------------------------------------------
# База данных
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, cookie TEXT NOT NULL, username TEXT,
    roblox_id TEXT, created_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, game_id TEXT, game_name TEXT);
CREATE TABLE IF NOT EXISTS servers (
    server_id TEXT PRIMARY KEY, category_id INTEGER, owner_account_id INTEGER,
    added_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS lots (
    lot_id INTEGER PRIMARY KEY, name TEXT NOT NULL, hours REAL NOT NULL,
    category_id INTEGER, server_id TEXT,
    price_normal REAL NOT NULL DEFAULT 50, price_event REAL NOT NULL DEFAULT 100,
    friend_limit INTEGER, last_active INTEGER, added_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS bonus_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT, buy_hours REAL NOT NULL, get_hours REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rentals (
    rental_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, buyer_id TEXT NOT NULL, buyer_name TEXT,
    chat_id TEXT NOT NULL, lot_id INTEGER, lot_name TEXT, server_id TEXT NOT NULL,
    account_id INTEGER, place_id TEXT, hours REAL NOT NULL, price REAL NOT NULL DEFAULT 0,
    start_time REAL NOT NULL, end_time REAL NOT NULL, status TEXT NOT NULL,
    end_reason TEXT, link TEXT, warned INTEGER NOT NULL DEFAULT 0,
    paid_slots INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT,
    created_at REAL NOT NULL, finished_at REAL);
CREATE INDEX IF NOT EXISTS idx_rentals_status ON rentals(status, end_time);
CREATE INDEX IF NOT EXISTS idx_rentals_buyer ON rentals(buyer_id, status);
CREATE TABLE IF NOT EXISTS rental_friends (
    rental_id TEXT NOT NULL, roblox_id TEXT NOT NULL, name TEXT, account_id INTEGER,
    on_server INTEGER NOT NULL DEFAULT 0, unfriend INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'added', added_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (rental_id, roblox_id));
CREATE TABLE IF NOT EXISTS processed_orders (
    order_id TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reviews (order_id TEXT PRIMARY KEY, given_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS friend_lots (
    tag TEXT PRIMARY KEY, offer_id TEXT, rental_id TEXT NOT NULL, buyer_id TEXT NOT NULL,
    chat_id TEXT NOT NULL, friend_input TEXT, friend_name TEXT, price REAL NOT NULL,
    created_at REAL NOT NULL, expires_at REAL NOT NULL, state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS friend_payments (
    order_id TEXT PRIMARY KEY, tag TEXT, rental_id TEXT, amount INTEGER NOT NULL,
    price REAL NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, chat_name TEXT, text TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL);
"""

LIVE = "('active','ending')"  # аренда занимает сервер


class Store:
    """Одно соединение SQLite под общей блокировкой. Сетевые запросы под ней не выполняются."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.con = sqlite3.connect(path, timeout=30, check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=30000")
        self.con.executescript(SCHEMA)
        for key, value in DEFAULTS.items():
            self.con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))

    def q(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        with self.lock:
            return self.con.execute(sql, params).fetchall()

    def q1(self, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.con.execute(sql, params).fetchone()

    def ex(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.con.execute(sql, params)

    def val(self, sql: str, params: tuple = (), default: Any = 0) -> Any:
        row = self.q1(sql, params)
        return row[0] if row and row[0] is not None else default

    @contextmanager
    def tx(self):
        with self.lock:
            if self.con.in_transaction:
                yield self
                return
            self.con.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self.con.execute("ROLLBACK")
                raise
            self.con.execute("COMMIT")

    def get(self, key: str, default: Optional[str] = None) -> str:
        row = self.q1("SELECT value FROM settings WHERE key=?", (key,))
        if row is not None:
            return str(row["value"])
        return DEFAULTS.get(key, "") if default is None else default

    def set(self, key: str, value: Any) -> None:
        self.ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)))

    def flag(self, key: str) -> bool:
        return self.get(key) == "1"


# ---------------------------------------------------------------------------
# Перенос данных из прежней базы (v11/v15/v16 «AutoVIPRoblox_Monolith.db»)
# ---------------------------------------------------------------------------
OLD_DEFAULT_TEMPLATES = {
    "msg_friend_added": "✅ {friend_name} добавлен в доступ к вашему VIP-серверу.",
    "msg_friend_not_found": "❌ Не удалось найти/принять заявку от {input}. Сначала отправьте заявку владельцу сервера.",
    "msg_friend_limit": "❌ Лимит друзей для этой аренды: {limit}.",
    "msg_review_bonus": "⭐ Спасибо за отзыв 5★! Добавлено {bonus_hours} ч. к активной аренде.",
    "msg_post_expire": "⏳ Аренда по заказу #{order_id} завершена. Сервер освобождён.",
}
OLD_TEMPLATE_MAP = {
    "msg_post_payment": "post_payment", "msg_post_expire": "post_expire",
    "msg_friend_added": "friend_added", "msg_friend_not_found": "friend_not_found",
    "msg_friend_limit": "friend_limit", "msg_review_bonus": "review_bonus",
}


def _g(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    try:
        return row[key] if key in row.keys() else default
    except (IndexError, KeyError):
        return default


def migrate_old_database(store: Store, old_path: str) -> Optional[str]:
    """Копирует аккаунты, серверы, лоты, настройки и аренды прежней версии. Старая база не меняется."""
    if not os.path.exists(old_path) or store.get("migrated_from_old", "0") == "1":
        return None
    try:
        old = sqlite3.connect(f"file:{old_path}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        return None
    old.row_factory = sqlite3.Row
    stats = {"accounts": 0, "servers": 0, "lots": 0, "rentals": 0}
    try:
        tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def rows(table: str, order: str = "") -> List[sqlite3.Row]:
            if table not in tables:
                return []
            return old.execute(f"SELECT * FROM {table} {order}").fetchall()

        t = now()
        with store.tx():
            # Настройки
            settings = {r["key"]: r["value"] for r in rows("settings")}
            for key in ("event_mode", "lot_management", "global_proxy", "friends_module_active",
                        "friends_auto_remove", "friends_default_limit", "review_bonus_active", "review_bonus_hours"):
                if key in settings and settings[key] is not None:
                    value = str(settings[key])
                    if key == "global_proxy" and value:
                        try:
                            value = normalize_proxy(value)
                        except ValueError:
                            value = ""
                    store.set(key, value)
            for old_key, new_key in OLD_TEMPLATE_MAP.items():
                value = settings.get(old_key)
                if not value or value == OLD_DEFAULT_TEMPLATES.get(old_key):
                    continue
                if not template_problems(new_key, value):
                    store.set("tpl_" + new_key, strip_html(html.unescape(value)))
            # Аккаунты
            for r in rows("accounts", "ORDER BY id"):
                if (_g(r, "id", 0) or 0) <= 0 or not _g(r, "cookie") or _g(r, "enabled", 1) in (0, "0"):
                    continue
                store.ex("INSERT OR IGNORE INTO accounts(id,cookie,username,roblox_id,created_at) VALUES(?,?,?,?,?)",
                         (r["id"], r["cookie"], _g(r, "username"), _g(r, "roblox_id"), _g(r, "created_at", t) or t))
                stats["accounts"] += 1
            # Категории
            for r in rows("categories", "ORDER BY id"):
                game = _g(r, "game_id")
                store.ex("INSERT OR IGNORE INTO categories(id,name,game_id,game_name) VALUES(?,?,?,?)",
                         (r["id"], r["name"], str(game) if game else None,
                          DEFAULT_GAME_NAME if str(game) == DEFAULT_PLACE_ID else (str(game) if game else None)))
            # Серверы
            for r in rows("servers"):
                if _g(r, "enabled", 1) in (0, "0"):
                    continue
                owner = _g(r, "owner_account_id")
                store.ex("INSERT OR IGNORE INTO servers(server_id,category_id,owner_account_id,added_at) VALUES(?,?,?,?)",
                         (str(r["server_id"]), _g(r, "category_id"), owner if owner and owner > 0 else None, t))
                stats["servers"] += 1
            # Лоты
            for r in rows("lots", "ORDER BY lot_id"):
                bind = _g(r, "bind_type")
                category = _g(r, "category_id") if bind in (None, "category") else None
                server = _g(r, "server_id") if bind in (None, "server") else None
                if category:
                    server = None
                limit = _g(r, "friend_limit")
                limit = None if limit is None or int(limit) < 0 else int(limit)
                store.ex("""INSERT OR IGNORE INTO lots(lot_id,name,hours,category_id,server_id,price_normal,price_event,friend_limit,added_at)
                            VALUES(?,?,?,?,?,?,?,?,?)""",
                         (int(r["lot_id"]), str(_g(r, "name") or f"Лот {r['lot_id']}"), float(_g(r, "hours", 1) or 1),
                          category, str(server) if server else None, float(_g(r, "price_normal", 0) or 0),
                          float(_g(r, "price_event", 0) or 0), limit, t))
                stats["lots"] += 1
            # Бонусы
            for r in rows("wholesale_bonuses", "ORDER BY id"):
                store.ex("INSERT INTO bonus_rules(buy_hours,get_hours) VALUES(?,?)", (r["buy_hours"], r["get_hours"]))
            # Обработанные заказы и отзывы
            for r in rows("payments"):
                store.ex("INSERT OR IGNORE INTO processed_orders VALUES(?,?,?)", (str(r["order_id"]), "migrated", t))
                if _g(r, "review_bonus_given", 0):
                    store.ex("INSERT OR IGNORE INTO reviews VALUES(?,?)", (str(r["order_id"]), t))
            for r in rows("incoming_orders"):
                store.ex("INSERT OR IGNORE INTO processed_orders VALUES(?,?,?)", (str(r["order_id"]), "migrated", t))
            # Аренды
            lot_names = {r["lot_id"]: r["name"] for r in store.q("SELECT lot_id,name FROM lots")}
            payments_by_rental: Dict[Any, List[sqlite3.Row]] = {}
            for p in rows("payments"):
                payments_by_rental.setdefault(_g(p, "rental_id"), []).append(p)
            if "rentals" in tables:
                for r in rows("rentals"):
                    if str(_g(r, "buyer_id", "0")) in ("0", "") or not _g(r, "chat_id"):
                        status_old = _g(r, "status")
                        if status_old in ("cleanup_failed", "cleaning", "issuing", "refunding"):
                            _migrate_rental(store, r, None, 0.0, "quiet", lot_names, t)
                        continue
                    pays = list(payments_by_rental.get(r["rental_id"], []))
                    pays.sort(key=lambda p: (_g(p, "created_at", 0) or 0, str(p["order_id"])))
                    order_id = str(pays[0]["order_id"]) if pays else str(r["rental_id"]).replace("legacy-", "")[:12]
                    price = sum(float(_g(p, "price", 0) or 0) for p in pays if _g(p, "state") != "refunded")
                    status_old = _g(r, "status")
                    if status_old in ("active", "delivering"):
                        reason = None
                    elif status_old in ("cleanup_failed", "cleaning", "issuing", "refunding"):
                        reason = {"manual": "manual", "cancelled": "cancelled"}.get(_g(r, "cleanup_reason"), "quiet")
                        if _g(r, "cleanup_reason") in (None, "expired"):
                            reason = "expired"
                    else:
                        reason = "history"
                    _migrate_rental(store, r, order_id, price, reason, lot_names, t)
                    stats["rentals"] += 1
                for f in rows("rental_friends"):
                    rental = store.q1("SELECT account_id,status FROM rentals WHERE rental_id=?", (f["rental_id"],))
                    if not rental or rental["status"] not in ("active", "ending"):
                        continue
                    state = _g(f, "state", "added")
                    if state == "removed":
                        continue
                    store.ex("""INSERT OR IGNORE INTO rental_friends(rental_id,roblox_id,name,account_id,on_server,unfriend,state,added_at)
                                VALUES(?,?,?,?,1,?,'added',?)""",
                             (f["rental_id"], str(f["roblox_id"]), _g(f, "name"), rental["account_id"],
                              int(_g(f, "unfriend_on_cleanup", 0) or 0), t))
            elif "orders" in tables:  # v11: только таблица orders
                for r in rows("orders"):
                    reason = None if _g(r, "status") == "active" else "history"
                    fake = {"rental_id": "legacy-" + str(r["order_id"]), "buyer_id": _g(r, "buyer_id"),
                            "chat_id": _g(r, "chat_id"), "lot_id": None, "server_id": _g(r, "server_id"),
                            "owner_account_id": None, "hours": _g(r, "hours", 0), "start_time": _g(r, "start_time", t),
                            "end_time": _g(r, "end_time", t), "link": None}
                    _migrate_rental(store, fake, str(r["order_id"]), float(_g(r, "price", 0) or 0), reason, lot_names, t)
                    stats["rentals"] += 1
            store.set("migrated_from_old", "1")
    except Exception:
        LOG.exception("AutoVIP: не удалось перенести данные прежней версии")
        return "⚠️ Не удалось перенести данные прежней версии: подробности в логе Cardinal."
    finally:
        old.close()
    return ("✅ Данные прежней версии перенесены: аккаунтов {accounts}, серверов {servers}, лотов {lots}, "
            "аренд {rentals}.").format(**stats)


def _migrate_rental(store: Store, r: Any, order_id: Optional[str], price: float, reason: Optional[str],
                    lot_names: Dict[int, str], t: float) -> None:
    get = (lambda k, d=None: r.get(k, d)) if isinstance(r, dict) else (lambda k, d=None: _g(r, k, d))
    start = float(get("start_time", 0) or 0)
    end = float(get("end_time", 0) or 0)
    hours = float(get("hours", 0) or 0)
    if reason is None:  # активная аренда
        if not start:
            start, end = t, t + hours * 3600
        status, end_reason = "active", None
    elif reason == "history":
        status, end_reason = ("cancelled" if get("status") in ("cancelled", "refunded", "issue_failed") else "completed"), None
    else:
        status, end_reason = "ending", reason
        end = min(end or t, t)
    owner = get("owner_account_id")
    lot_id = get("lot_id")
    store.ex("""INSERT OR IGNORE INTO rentals(rental_id,order_id,buyer_id,chat_id,lot_id,lot_name,server_id,account_id,
                hours,price,start_time,end_time,status,end_reason,link,warned,created_at,finished_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             (str(get("rental_id")), order_id or str(get("rental_id"))[:8], str(get("buyer_id") or "0"),
              str(get("chat_id") or ""), lot_id, lot_names.get(lot_id), str(get("server_id")),
              owner if owner and owner > 0 else None, hours, price, start or t, end or t, status, end_reason,
              get("link"), 1 if status != "active" else 0, start or t, None if status in ("active", "ending") else t))


# ---------------------------------------------------------------------------
# Roblox API
# ---------------------------------------------------------------------------
class RobloxError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class RobloxAPI:
    def __init__(self, cookie: str, proxy: str = ""):
        cookie = str(cookie or "").strip()
        if cookie.startswith(".ROBLOSECURITY="):
            cookie = cookie.split("=", 1)[1].split(";", 1)[0]
        if not cookie or "\n" in cookie or "\r" in cookie:
            raise ValueError("Cookie должен быть одной строкой")
        self.cookie = cookie
        self.proxies = proxy_dict(proxy)
        self.session = requests.Session()
        self.session.trust_env = False
        self.csrf: Optional[str] = None
        self.user_id: Optional[str] = None

    def request(self, method: str, url: str, write: bool = False, **kwargs) -> requests.Response:
        last: Optional[Exception] = None
        for attempt in range(4):
            headers = {"Cookie": ".ROBLOSECURITY=" + self.cookie, "User-Agent": UA,
                       "Accept": "application/json", "Origin": "https://www.roblox.com",
                       "Referer": "https://www.roblox.com/"}
            if write and self.csrf:
                headers["x-csrf-token"] = self.csrf
            try:
                response = self.session.request(method, url, headers=headers, proxies=self.proxies,
                                                timeout=(10, 25), allow_redirects=False, **kwargs)
            except requests.RequestException as exc:
                # Текст исключения не выводим: в нём может быть пароль прокси.
                last = RobloxError("нет соединения с Roblox (сеть/прокси: " + type(exc).__name__ + ")")
                time.sleep(1 + attempt)
                continue
            token = response.headers.get("x-csrf-token")
            if write and response.status_code == 403 and token and token != headers.get("x-csrf-token"):
                self.csrf = token
                continue
            if response.status_code == 429 and attempt < 3:
                time.sleep(2 + attempt * 2)
                continue
            return response
        raise last or RobloxError("Roblox не ответил")

    @staticmethod
    def _errors(response: requests.Response) -> str:
        try:
            errors = response.json().get("errors") or []
            return "; ".join(str(e.get("message")) for e in errors if isinstance(e, dict))[:200]
        except Exception:
            return ""

    def call(self, method: str, url: str, write: bool = False, **kwargs) -> Dict[str, Any]:
        response = self.request(method, url, write=write, **kwargs)
        if response.status_code == 401:
            raise RobloxError("куки недействительны (401)", 401)
        if response.status_code not in (200, 201, 204):
            detail = self._errors(response)
            raise RobloxError(f"HTTP {response.status_code}" + (": " + detail if detail else ""), response.status_code)
        if response.status_code == 204 or not response.content:
            return {}
        try:
            data = response.json()
        except ValueError:
            raise RobloxError("некорректный ответ Roblox") from None
        return data if isinstance(data, dict) else {"data": data}

    # --- аккаунт ---
    def authenticated(self) -> Tuple[str, str]:
        data = self.call("GET", "https://users.roblox.com/v1/users/authenticated")
        if not data.get("id"):
            raise RobloxError("Roblox не вернул пользователя")
        self.user_id = str(data["id"])
        return str(data["id"]), str(data.get("name") or data["id"])

    # --- VIP-сервер ---
    def details(self, server_id: str) -> Dict[str, Any]:
        data = self.call("GET", f"https://games.roblox.com/v1/vip-servers/{server_id}")
        if str(data.get("id")) != str(server_id):
            raise RobloxError("Roblox вернул другой VIP-сервер")
        return data

    @staticmethod
    def place_of(data: Dict[str, Any]) -> Optional[str]:
        game = data.get("game") or {}
        root = game.get("rootPlace") or {}
        place = root.get("id") or game.get("placeId")
        return str(place) if place else None

    def set_active(self, server_id: str, active: bool) -> bool:
        data = self.call("PATCH", f"https://games.roblox.com/v1/vip-servers/{server_id}", write=True,
                         json={"active": bool(active)})
        state = data.get("active") if str(data.get("id")) == str(server_id) else None
        if state is None:
            state = self.details(server_id).get("active")
        return state is bool(active)

    def new_link(self, server_id: str, place_fallback: Optional[str] = None) -> str:
        data = self.call("PATCH", f"https://games.roblox.com/v1/vip-servers/{server_id}", write=True,
                         json={"newJoinCode": True})
        code, place = data.get("joinCode"), self.place_of(data)
        if not code or not place:
            info = self.details(server_id)
            code, place = code or info.get("joinCode"), place or self.place_of(info)
        place = place or place_fallback
        if not code or not place:
            raise RobloxError("Roblox не вернул код ссылки")
        return f"https://www.roblox.com/games/{place}?privateServerLinkCode={quote(str(code), safe='')}"

    def shutdown_instances(self, server_id: str, place_id: Optional[str]) -> None:
        """Пытается закрыть запущенные экземпляры сервера (игроки вылетают). Ошибки не критичны."""
        if not place_id:
            return
        try:
            data = self.call("GET", f"https://games.roblox.com/v1/games/{place_id}/private-servers",
                             params={"excludeFriendServers": "true", "limit": 100})
            for row in data.get("data") or []:
                if str(row.get("vipServerId")) == str(server_id) and row.get("id") and (row.get("playing") or 0) > 0:
                    self.request("POST", "https://apis.roblox.com/matchmaking-api/v1/game-instances/shutdown",
                                 write=True, json={"placeId": int(place_id), "gameId": str(row["id"]),
                                                   "privateServerId": int(server_id)})
        except Exception:
            LOG.debug("AutoVIP: shutdown экземпляров не выполнен", exc_info=True)

    def set_permission(self, server_id: str, user_id: str, add: bool) -> bool:
        key = "usersToAdd" if add else "usersToRemove"
        url = f"https://games.roblox.com/v1/vip-servers/{server_id}/permissions"
        try:
            self.call("PATCH", url, write=True, json={key: [int(user_id)]})
        except RobloxError as exc:
            if exc.status != 400:
                raise
            perms = self.details(server_id).get("permissions") or {}
            body: Dict[str, Any] = {key: [int(user_id)], "friendsAllowed": bool(perms.get("friendsAllowed")),
                                    "clanAllowed": bool(perms.get("clanAllowed"))}
            if perms.get("enemyClanId") is not None:
                body["enemyClanId"] = perms["enemyClanId"]
            self.call("PATCH", url, write=True, json=body)
        users = {str(u.get("id")) for u in ((self.details(server_id).get("permissions") or {}).get("users") or [])}
        return (str(user_id) in users) == bool(add)

    # --- пользователи и друзья ---
    def resolve_user(self, raw: str) -> Optional[Tuple[str, str]]:
        raw = str(raw or "").strip().strip("<>").rstrip("/")
        match = re.search(r"roblox\.com/users/(\d+)", raw, re.I)
        user_id = match.group(1) if match else (raw if raw.isdigit() else None)
        if user_id:
            try:
                data = self.call("GET", f"https://users.roblox.com/v1/users/{user_id}")
            except RobloxError as exc:
                if exc.status in (400, 404):
                    return None
                raise
            return str(data["id"]), str(data.get("name") or user_id)
        name = raw.lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{3,20}", name):
            return None
        data = self.call("POST", "https://users.roblox.com/v1/usernames/users",
                         json={"usernames": [name], "excludeBannedUsers": True})
        rows = data.get("data") or []
        return (str(rows[0]["id"]), str(rows[0].get("name") or name)) if rows else None

    def friend_status(self, owner_id: str, user_id: str) -> str:
        data = self.call("GET", f"https://friends.roblox.com/v1/users/{owner_id}/friends/statuses",
                         params={"userIds": str(user_id)})
        for row in data.get("data") or []:
            if str(row.get("id")) == str(user_id):
                status = row.get("status")
                if isinstance(status, int):
                    status = {0: "NotFriends", 1: "Friends", 2: "RequestSent", 3: "RequestReceived"}.get(status)
                return str(status or "NotFriends")
        return "NotFriends"

    def accept_friend(self, user_id: str) -> None:
        self.call("POST", f"https://friends.roblox.com/v1/users/{user_id}/accept-friend-request", write=True)

    def unfriend(self, user_id: str) -> None:
        self.call("POST", f"https://friends.roblox.com/v1/users/{user_id}/unfriend", write=True)


def api_for(account: sqlite3.Row) -> RobloxAPI:
    proxy = S.get("global_proxy")
    key = int(account["id"])
    with GUARD:
        cached = API_CACHE.get(key)
        if cached and cached[0] == account["cookie"] and cached[1] == proxy:
            return cached[2]
        api = RobloxAPI(account["cookie"], proxy)
        api.user_id = str(account["roblox_id"] or "") or None
        API_CACHE[key] = (account["cookie"], proxy, api)
        return api


def owner_of(server_id: str, prefer: Optional[int] = None) -> Tuple[sqlite3.Row, RobloxAPI, Dict[str, Any]]:
    """Находит добавленный аккаунт, которому Roblox даёт управлять сервером."""
    accounts = S.q("SELECT * FROM accounts ORDER BY id")
    if not accounts:
        raise RobloxError("не добавлено ни одного аккаунта Roblox")
    cached = S.val("SELECT owner_account_id FROM servers WHERE server_id=?", (server_id,), None)
    first = [x for x in (prefer, cached) if x]
    accounts.sort(key=lambda a: (first.index(a["id"]) if a["id"] in first else len(first), a["id"]))
    errors = []
    for account in accounts:
        try:
            api = api_for(account)
            data = api.details(server_id)
        except Exception as exc:
            errors.append(f"{account['username'] or account['id']}: {exc}")
            continue
        S.ex("UPDATE servers SET owner_account_id=? WHERE server_id=?", (account["id"], server_id))
        return account, api, data
    raise RobloxError("ни один аккаунт не управляет сервером " + server_id + " (" + "; ".join(errors)[:300] + ")")


def server_place_fallback(server_id: str) -> str:
    row = S.q1("""SELECT c.game_id FROM servers s JOIN categories c ON c.id=s.category_id
                  WHERE s.server_id=? AND c.game_id IS NOT NULL""", (server_id,))
    return str(row["game_id"]) if row else DEFAULT_PLACE_ID


def issue_link(server_id: str) -> Tuple[sqlite3.Row, str, str]:
    """До 3 попыток с перебором аккаунтов: включает сервер и создаёт новую ссылку."""
    last: Optional[Exception] = None
    for attempt in range(3):
        try:
            account, api, data = owner_of(server_id)
            place = RobloxAPI.place_of(data) or server_place_fallback(server_id)
            if data.get("active") is False and not api.set_active(server_id, True):
                raise RobloxError("Roblox не включил сервер (проверьте подписку)")
            return account, api.new_link(server_id, place), place
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(2)
    raise RobloxError(str(last))


# ---------------------------------------------------------------------------
# FunPay и Telegram
# ---------------------------------------------------------------------------
def account_ready() -> bool:
    account = getattr(CARDINAL, "account", None)
    return bool(account is not None and getattr(account, "is_initiated", True) and getattr(account, "id", None))


def plugin_enabled() -> bool:
    plugin = (getattr(CARDINAL, "plugins", None) or {}).get(UUID)
    return plugin is None or bool(getattr(plugin, "enabled", True))


def _send_now(chat_id: Any, text: str, chat_name: Optional[str]) -> bool:
    target = int(chat_id) if str(chat_id).isdigit() else chat_id
    try:
        sender = getattr(CARDINAL, "send_message", None)
        if callable(sender):
            return bool(sender(target, text, chat_name))
        return CARDINAL.account.send_message(target, text, chat_name) is not None
    except Exception:
        LOG.warning("AutoVIP: не удалось отправить сообщение в чат %s", chat_id, exc_info=True)
        return False


def send_fp(chat_id: Any, text: str, chat_name: Optional[str] = None, retry: bool = True) -> bool:
    text = strip_html(text).strip()
    if not text or not chat_id:
        return True
    if _send_now(chat_id, text, chat_name):
        return True
    if retry:
        S.ex("INSERT INTO outbox(chat_id,chat_name,text,next_at,created_at) VALUES(?,?,?,?,?)",
             (str(chat_id), chat_name, text, now() + 15, now()))
    return False


def admin_ids() -> List[int]:
    telegram = getattr(CARDINAL, "telegram", None)
    raw = getattr(telegram, "authorized_users", None) or []
    values = list(raw.keys()) if isinstance(raw, dict) else list(raw)
    result = []
    for value in values:
        try:
            result.append(int(value))
        except (TypeError, ValueError):
            pass
    return result


def is_admin(user_id: Any) -> bool:
    try:
        return int(user_id) in admin_ids()
    except (TypeError, ValueError):
        return False


def notify_admins(text: str) -> None:
    bot = getattr(getattr(CARDINAL, "telegram", None), "bot", None)
    if bot is None:
        return
    for user_id in admin_ids():
        try:
            bot.send_message(user_id, text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            LOG.debug("AutoVIP: не удалось отправить уведомление в Telegram", exc_info=True)


def blacklisted(username: Any) -> bool:
    if not username:
        return False
    try:
        return str(username) in (getattr(CARDINAL, "blacklist", None) or [])
    except Exception:
        return False


def rental_lock(rental_id: str) -> threading.RLock:
    with GUARD:
        return LOCKS.setdefault("rental:" + rental_id, threading.RLock())


def named_lock(name: str) -> threading.RLock:
    with GUARD:
        return LOCKS.setdefault(name, threading.RLock())


def run_once(key: str, func, *args) -> bool:
    with GUARD:
        if key in RUNNING:
            return False
        RUNNING.add(key)

    def job():
        try:
            func(*args)
        except Exception:
            LOG.exception("AutoVIP: ошибка операции %s", key)
        finally:
            with GUARD:
                RUNNING.discard(key)
    try:
        EXECUTOR.submit(job)
    except RuntimeError:
        with GUARD:
            RUNNING.discard(key)
        return False
    return True


def request_lot_sync() -> None:
    SYNC_REQUEST.set()


# --- работа с лотами FunPay ---------------------------------------------
def renew_and_save(fields: Any) -> None:
    renew = getattr(fields, "renew_fields", None)
    if callable(renew):
        renew()
    CARDINAL.account.save_lot(fields)


def lot_title_from_funpay(lot_id: int) -> str:
    fields = CARDINAL.account.get_lot_fields(int(lot_id))
    raw = getattr(fields, "fields", None) or {}
    title = getattr(fields, "title_ru", None) or raw.get("fields[summary][ru]") or getattr(fields, "title_en", None) \
        or raw.get("fields[summary][en]")
    title = re.sub(r"\s+", " ", str(title or "")).strip()
    if not title:
        raise ValueError("пустое название")
    return title[:250]


def fp_post_offer(fields: Dict[str, str]) -> Dict[str, Any]:
    headers = {"accept": "*/*", "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
               "x-requested-with": "XMLHttpRequest"}
    payload = dict(fields)
    payload.setdefault("location", "trade")
    response = CARDINAL.account.method("post", "lots/offerSave", headers, payload, raise_not_200=True)
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError("FunPay вернул некорректный ответ") from None
    errors = data.get("errors")
    if data.get("error") or errors:
        details = []
        if isinstance(errors, list):
            details = [": ".join(str(x) for x in e) if isinstance(e, (list, tuple)) else str(e) for e in errors]
        elif isinstance(errors, dict):
            details = [f"{k}: {v}" for k, v in errors.items()]
        message = str(data.get("error") or "") + (" " + "; ".join(details) if details else "")
        raise RuntimeError(("FunPay: " + message.strip())[:400] or "FunPay отклонил сохранение лота")
    return data


def _offer_form(node_id: str) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
    if BeautifulSoup is None:
        raise RuntimeError("не установлен bs4")
    response = CARDINAL.account.method("get", f"lots/offerEdit?node={node_id}", {"accept": "text/html"}, {},
                                       raise_not_200=True)
    soup = BeautifulSoup(response.text, "html.parser")
    form = soup.select_one("form.form-offer-editor") or soup.find("form", attrs={"action": re.compile("offerSave")})
    if form is None:
        lead = soup.find("p", class_="lead")
        raise RuntimeError("FunPay не отдал форму создания лота" + (": " + lead.get_text(strip=True) if lead else ""))
    fields: Dict[str, str] = {}
    meta: List[Dict[str, Any]] = []
    for el in form.find_all(["input", "textarea", "select"]):
        name = el.get("name")
        if not name:
            continue
        group = el.find_parent(class_="form-group")
        label_el = group.find("label") if group else None
        label = label_el.get_text(" ", strip=True) if label_el else ""
        options = None
        if el.name == "input":
            kind = (el.get("type") or "text").lower()
            if kind in ("submit", "button", "file", "image", "reset"):
                continue
            if kind in ("checkbox", "radio"):
                if el.has_attr("checked"):
                    fields[name] = el.get("value") or "on"
                continue
            fields[name] = el.get("value") or ""
        elif el.name == "textarea":
            fields[name] = el.get_text() or ""
        else:
            options = [(o.get("value") or "", o.get_text(" ", strip=True)) for o in el.find_all("option")]
            selected = el.find("option", selected=True)
            fields[name] = (selected.get("value") if selected else (options[0][0] if options else "")) or ""
        meta.append({"name": name, "label": label, "tag": el.name, "options": options})
    return fields, meta


def _pick_option(options: List[Tuple[str, str]], wanted: str, fallbacks: Tuple[str, ...]) -> Optional[str]:
    def clean(x: str) -> str:
        return norm_name(re.sub(r"[\[\]()]", "", x))
    choices = [(v, t) for v, t in options if v]
    target = clean(wanted)
    for value, text in choices:
        if clean(text) == target or clean(value) == target:
            return value
    for value, text in choices:
        if target and (target in clean(text) or clean(text) in target):
            return value
    for word in fallbacks:
        for value, text in choices:
            if word in clean(text):
                return value
    return choices[0][0] if choices else None


def _find_offer_by_tag(node_id: str, tag: str) -> Optional[str]:
    if BeautifulSoup is None:
        return None
    response = CARDINAL.account.method("get", f"lots/{node_id}/trade", {"accept": "text/html"}, {}, raise_not_200=True)
    soup = BeautifulSoup(response.text, "html.parser")
    for item in soup.select("a.tc-item, a[href*='offerEdit?offer=']"):
        if tag in item.get_text(" ", strip=True):
            match = re.search(r"offer(?:Edit\?offer|\?id)=(\d+)", item.get("href") or "")
            return item.get("data-offer") or (match.group(1) if match else None)
    return None


def create_friend_offer(tag: str, price: float) -> str:
    """Создаёт временный лот доплаты. Возвращает ID созданного лота."""
    node = S.get("paid_node").strip() or "402"
    fields, meta = _offer_form(node)
    title_ru = short(S.get("paid_title_ru"), 58).rstrip("…") + " " + tag
    title_en = short(S.get("paid_title_en"), 58).rstrip("…") + " " + tag
    warning_ru = (f"\n\nКод лота: {tag}. Лот создан для конкретного покупателя аренды. "
                  "Если вы его не запрашивали — не оплачивайте: деньги будут автоматически возвращены.")
    warning_en = f"\n\nCode: {tag}." + EN_DESC_FILLER
    desc_en = S.get("paid_desc_en").strip()
    if len(desc_en) < 120:
        desc_en = (desc_en + EN_DESC_FILLER).strip()
    values = {
        "fields[summary][ru]": title_ru[:70], "fields[summary][en]": title_en[:70],
        "fields[desc][ru]": S.get("paid_desc_ru").strip() + warning_ru,
        "fields[desc][en]": desc_en + warning_en,
        "fields[payment_msg][ru]": S.get("paid_pay_msg_ru"), "fields[payment_msg][en]": S.get("paid_pay_msg_en"),
        "price": fmt_num(price), "amount": "100", "active": "on", "offer_id": "0", "node_id": node,
    }
    fields.update(values)
    fields.pop("deactivate_after_sale", None)
    for item in meta:
        label = norm_name(item["label"])
        name = item["name"]
        if item["tag"] == "select" and item["options"]:
            if "игр" in label and "назван" not in label:
                choice = _pick_option(item["options"], S.get("paid_game"), ("прочие", "другое"))
            elif label.startswith("тип") or " тип" in label:
                choice = _pick_option(item["options"], S.get("paid_type"), ("услуг", "прочее"))
            elif not fields.get(name):
                choice = _pick_option(item["options"], "", ("прочие", "услуг"))
            else:
                choice = None
            if choice is not None:
                fields[name] = choice
        elif item["tag"] == "input" and "назван" in label and "игр" in label:
            fields[name] = S.get("paid_game_name_en") if "[en]" in name else S.get("paid_game_name_ru")
    data = fp_post_offer(fields)
    match = re.search(r"offer(?:Edit\?offer|\?id)=(\d+)", str(data.get("url") or ""))
    offer_id = match.group(1) if match else None
    for _ in range(3):
        if offer_id:
            break
        time.sleep(1.5)
        try:
            offer_id = _find_offer_by_tag(node, tag)
        except Exception:
            LOG.debug("AutoVIP: поиск созданного лота", exc_info=True)
    if not offer_id:
        raise RuntimeError(f"лот создан, но FunPay не вернул его ID — удалите вручную лот с тегом {tag}")
    return str(offer_id)


def delete_offer(offer_id: Any) -> None:
    account = CARDINAL.account
    deleter = getattr(account, "delete_lot", None)
    if callable(deleter):
        try:
            deleter(int(offer_id))
            return
        except Exception:
            LOG.debug("AutoVIP: delete_lot не сработал, удаляю через форму", exc_info=True)
    lot_fields = account.get_lot_fields(int(offer_id))
    fields = dict(getattr(lot_fields, "fields", None) or {})
    fields.update({"deleted": "1", "offer_id": str(offer_id)})
    fp_post_offer(fields)


# ---------------------------------------------------------------------------
# Серверы, лоты, бонусы
# ---------------------------------------------------------------------------
FREE_SQL = f"server_id NOT IN (SELECT server_id FROM rentals WHERE status IN {LIVE})"
GENERAL_SQL = "category_id IS NULL AND server_id NOT IN (SELECT server_id FROM lots WHERE server_id IS NOT NULL)"


def lot_mode(lot: sqlite3.Row) -> str:
    if lot["category_id"] and S.q1("SELECT 1 FROM categories WHERE id=?", (lot["category_id"],)):
        return "category"
    if lot["server_id"]:
        return "server"
    return "random"


def free_server_for(lot: sqlite3.Row) -> Tuple[Optional[str], str]:
    mode = lot_mode(lot)
    if mode == "category":
        row = S.q1(f"SELECT server_id FROM servers WHERE category_id=? AND {FREE_SQL} ORDER BY added_at, server_id",
                   (lot["category_id"],))
    elif mode == "server":
        row = S.q1(f"SELECT server_id FROM servers WHERE server_id=? AND {FREE_SQL}", (str(lot["server_id"]),))
    else:
        row = S.q1(f"SELECT server_id FROM servers WHERE {GENERAL_SQL} AND {FREE_SQL} ORDER BY added_at, server_id")
    return (row["server_id"] if row else None), mode


def free_general_count() -> int:
    return int(S.val(f"SELECT COUNT(*) FROM servers WHERE {GENERAL_SQL} AND {FREE_SQL}"))


def duplicate_lots() -> Dict[str, List[sqlite3.Row]]:
    groups: Dict[str, List[sqlite3.Row]] = {}
    for lot in S.q("SELECT * FROM lots ORDER BY lot_id"):
        groups.setdefault(norm_name(lot["name"]), []).append(lot)
    return {k: v for k, v in groups.items() if k and len(v) > 1}


def name_taken(name: str, except_lot: Optional[int] = None) -> Optional[sqlite3.Row]:
    key = norm_name(name)
    for lot in S.q("SELECT * FROM lots"):
        if norm_name(lot["name"]) == key and lot["lot_id"] != except_lot:
            return lot
    return None


def match_lot(description: str) -> Tuple[Optional[sqlite3.Row], bool]:
    """Ищет лот, название которого входит в описание заказа: сначала полное совпадение, затем самое длинное."""
    desc = norm_name(description)
    base = re.sub(r",?\s*\d+\s*шт\.?$", "", desc).strip()
    candidates = [lot for lot in S.q("SELECT * FROM lots") if norm_name(lot["name"]) and norm_name(lot["name"]) in desc]
    if not candidates:
        return None, False
    exact = [lot for lot in candidates if norm_name(lot["name"]) in (desc, base)]
    chosen = (exact or sorted(candidates, key=lambda l: len(norm_name(l["name"])), reverse=True))[0]
    twins = [lot for lot in S.q("SELECT * FROM lots")
             if lot["lot_id"] != chosen["lot_id"] and norm_name(lot["name"]) == norm_name(chosen["name"])]
    return chosen, bool(twins)


def volume_bonus(hours: float) -> float:
    if not S.flag("bonus_active"):
        return 0.0
    for rule in S.q("SELECT * FROM bonus_rules ORDER BY id"):
        if hours >= float(rule["buy_hours"]):
            return float(rule["get_hours"])
    return 0.0


def friend_limit_for(rental: sqlite3.Row) -> int:
    lot = S.q1("SELECT friend_limit FROM lots WHERE lot_id=?", (rental["lot_id"],)) if rental["lot_id"] else None
    if lot is not None and lot["friend_limit"] is not None:
        return max(0, int(lot["friend_limit"]))
    try:
        return max(0, int(S.get("friends_default_limit") or 0))
    except ValueError:
        return 0


def friends_used(rental_id: str) -> int:
    return int(S.val("SELECT COUNT(*) FROM rental_friends WHERE rental_id=? AND state='added'", (rental_id,)))


def active_rentals(buyer_id: Any) -> List[sqlite3.Row]:
    return S.q("SELECT * FROM rentals WHERE buyer_id=? AND status='active' AND end_time>? ORDER BY start_time, created_at, rowid",
               (str(buyer_id), now()))


def get_active(rental_id: str) -> Optional[sqlite3.Row]:
    return S.q1("SELECT * FROM rentals WHERE rental_id=? AND status='active' AND end_time>?", (rental_id, now()))


def rental_title(rental: sqlite3.Row) -> str:
    if rental["lot_name"]:
        return str(rental["lot_name"])
    row = S.q1("""SELECT c.game_name,c.name FROM servers s JOIN categories c ON c.id=s.category_id
                  WHERE s.server_id=?""", (rental["server_id"],))
    if row:
        return str(row["game_name"] or row["name"])
    return "VIP-сервер " + str(rental["server_id"])


def owner_profile(rental: sqlite3.Row) -> Tuple[str, str]:
    account = S.q1("SELECT * FROM accounts WHERE id=?", (rental["account_id"],)) if rental["account_id"] else None
    if not account:
        return "https://www.roblox.com/", "владелец сервера"
    return (f"https://www.roblox.com/users/{account['roblox_id']}/profile",
            str(account["username"] or account["roblox_id"]))


def earned_total() -> float:
    return float(S.val("SELECT COALESCE(SUM(price),0) FROM rentals")) + \
        float(S.val("SELECT COALESCE(SUM(price),0) FROM friend_payments WHERE state='applied'"))


# ---------------------------------------------------------------------------
# Заказы
# ---------------------------------------------------------------------------
def on_new_order(cardinal: Any, event: Any) -> None:
    order = getattr(event, "order", None)
    if order is not None:
        ORDER_QUEUE.put(order)


def order_loop() -> None:
    while not STOP.is_set():
        try:
            order = ORDER_QUEUE.get(timeout=1)
        except queue.Empty:
            continue
        try:
            process_order(order)
        except Exception:
            LOG.exception("AutoVIP: ошибка обработки заказа")


def process_order(order: Any) -> None:
    order_id = str(getattr(order, "id", "") or "").lstrip("#")
    if not order_id:
        return
    status = getattr(order, "status", None)
    if OrderStatuses is not None and status is not None and status != getattr(OrderStatuses, "PAID", status):
        return
    with S.tx():
        if S.q1("SELECT 1 FROM processed_orders WHERE order_id=?", (order_id,)):
            return
        S.ex("INSERT INTO processed_orders VALUES(?,?,?)", (order_id, "processing", now()))
    try:
        kind = handle_order(order, order_id)
    except Exception as exc:
        LOG.exception("AutoVIP: заказ %s", order_id)
        notify_admins(f"⚠️ <b>Auto VIP: ошибка обработки заказа</b> #{esc(order_id)}\n{esc(exc)}")
        kind = "error"
    S.ex("UPDATE processed_orders SET kind=? WHERE order_id=?", (kind, order_id))


def order_chat(order: Any, order_id: str) -> Tuple[str, str, str]:
    chat_id = str(getattr(order, "chat_id", "") or "")
    buyer_id = str(getattr(order, "buyer_id", "") or "")
    buyer_name = str(getattr(order, "buyer_username", "") or "")
    if not chat_id or not buyer_id:
        try:
            full = CARDINAL.account.get_order(order_id)
            chat_id = chat_id or str(getattr(full, "chat_id", "") or "")
            buyer_id = buyer_id or str(getattr(full, "buyer_id", "") or "")
            buyer_name = buyer_name or str(getattr(full, "buyer_username", "") or "")
        except Exception:
            LOG.debug("AutoVIP: get_order", exc_info=True)
    if not chat_id and buyer_name:
        try:
            chat = CARDINAL.account.get_chat_by_name(buyer_name, True)
            chat_id = str(getattr(chat, "id", "") or "")
        except Exception:
            LOG.debug("AutoVIP: get_chat_by_name", exc_info=True)
    return chat_id, buyer_id, buyer_name


def refund(order_id: str) -> bool:
    try:
        CARDINAL.account.refund(order_id)
        return True
    except Exception:
        LOG.warning("AutoVIP: возврат заказа %s не выполнен", order_id, exc_info=True)
        return False


def handle_order(order: Any, order_id: str) -> str:
    description = str(getattr(order, "description", "") or "")
    if blacklisted(getattr(order, "buyer_username", None)):
        return "blacklist"
    tag = TAG_RE.search(description)
    if tag:
        return handle_friend_payment(order, order_id, tag.group(0))
    lot, has_twin = match_lot(description)
    if lot is None:
        return "ignored"
    chat_id, buyer_id, buyer_name = order_chat(order, order_id)
    if has_twin:
        notify_admins(f"⚠️ <b>Одинаковые названия лотов</b>\nЗаказ #{esc(order_id)} выдан по лоту «{esc(lot['name'])}» "
                      f"(ID {lot['lot_id']}), но в плагине есть лот с таким же названием. Проверьте заказ и "
                      "переименуйте один из лотов на FunPay.")
    try:
        amount = max(1, int(getattr(order, "amount", 1) or 1))
    except (TypeError, ValueError):
        amount = 1
    try:
        price = float(getattr(order, "price", 0) or 0)
    except (TypeError, ValueError):
        price = 0.0
    hours = float(lot["hours"]) * amount
    bonus = volume_bonus(hours)
    total = hours + bonus
    server_id, mode = free_server_for(lot)
    if not server_id:
        busy = {"category": "❌ Все серверы для этого лота сейчас заняты.",
                "server": "❌ Сервер для этого лота сейчас занят.",
                "random": "❌ Все серверы сейчас заняты."}[mode]
        refunded = refund(order_id)
        busy += "\n💸 Деньги возвращены автоматически." if refunded else "\nПродавец уведомлён и вернёт деньги."
        send_fp(chat_id, busy, buyer_name)
        notify_admins(f"⚠️ <b>Нет свободного сервера</b> для заказа #{esc(order_id)} (лот «{esc(lot['name'])}»). "
                      + ("Сделан автовозврат." if refunded else "<b>Автовозврат не удался — верните деньги вручную.</b>"))
        request_lot_sync()
        return "no_server"
    try:
        account, link, place = issue_link(server_id)
    except Exception as exc:
        send_fp(chat_id, "❌ Ошибка генерации ссылки. Обратитесь к администратору.", buyer_name)
        notify_admins(f"⚠️ <b>Ошибка генерации ссылки</b>\nЗаказ #{esc(order_id)}, сервер <code>{esc(server_id)}</code>\n"
                      f"Причина: {esc(exc)}\nАренда не создана, автовозврата нет — обработайте заказ вручную.")
        return "link_failed"
    start = now()
    rental_id = str(uuid.uuid4())
    S.ex("""INSERT INTO rentals(rental_id,order_id,buyer_id,buyer_name,chat_id,lot_id,lot_name,server_id,account_id,place_id,
            hours,price,start_time,end_time,status,link,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?)""",
         (rental_id, order_id, buyer_id, buyer_name, chat_id, lot["lot_id"], lot["name"], server_id, account["id"], place,
          total, price, start, start + total * 3600, link, start))
    text = render("post_payment", link=link, hours=fmt_num(total), order_id=order_id, price=fmt_num(price),
                  buyer_id=buyer_id, server_id=server_id, chat_id=chat_id)
    if bonus:
        text += f"\n🎁 Бонус: +{fmt_num(bonus)} {plural_hours(bonus)} в подарок!"
    send_fp(chat_id, text, buyer_name)
    if S.flag("friends_module_active"):
        profile, owner_name = f"https://www.roblox.com/users/{account['roblox_id']}/profile", account["username"]
        send_fp(chat_id, render("friend_invite", owner_profile_link=profile, owner_username=owner_name,
                                order_id=order_id), buyer_name)
    request_lot_sync()
    return "rental"


# ---------------------------------------------------------------------------
# Завершение аренды
# ---------------------------------------------------------------------------
def end_rental(rental_id: str, reason: Optional[str] = None) -> Tuple[bool, str]:
    """reason: expired | manual | quiet | cancelled. Возвращает (успех, ошибка)."""
    with rental_lock(rental_id):
        rental = S.q1("SELECT * FROM rentals WHERE rental_id=?", (rental_id,))
        if not rental or rental["status"] not in ("active", "ending"):
            return False, "not_active"
        if rental["status"] == "active":
            reason = reason or "expired"
            S.ex("UPDATE rentals SET status='ending',end_reason=?,attempts=0 WHERE rental_id=?", (reason, rental_id))
        else:
            reason = rental["end_reason"] or reason or "expired"
        server_id = rental["server_id"]
        try:
            account, api, data = owner_of(server_id, rental["account_id"])
            place = RobloxAPI.place_of(data) or rental["place_id"] or server_place_fallback(server_id)
            api.shutdown_instances(server_id, place)
            if data.get("active") is not False and not api.set_active(server_id, False):
                raise RobloxError("Roblox не подтвердил выключение сервера")
        except Exception as exc:
            attempts = int(rental["attempts"] or 0) + 1
            delay = min(900, 30 * 2 ** min(attempts - 1, 5))
            S.ex("UPDATE rentals SET attempts=?,next_attempt=?,last_error=? WHERE rental_id=?",
                 (attempts, now() + delay, str(exc)[:500], rental_id))
            if attempts == 1:
                notify_admins(f"⚠️ <b>Не удалось выключить сервер</b> <code>{esc(server_id)}</code> "
                              f"(заказ #{esc(rental['order_id'])}).\nПричина: {esc(exc)}\n"
                              "Сервер не выдаётся, бот повторит попытку автоматически.")
            return False, str(exc)
        try:
            api.new_link(server_id, place)  # старая ссылка перестаёт работать
        except Exception:
            LOG.debug("AutoVIP: сброс ссылки после аренды", exc_info=True)
        remove_rental_friends(rental, api)
        final = "cancelled" if reason == "cancelled" else "completed"
        S.ex("""UPDATE rentals SET status=?,end_reason=?,finished_at=?,last_error=NULL,link=NULL,
                end_time=MIN(end_time,?) WHERE rental_id=?""", (final, reason, now(), now(), rental_id))
    values = dict(order_id=rental["order_id"], hours=fmt_num(rental["hours"]), buyer_id=rental["buyer_id"],
                  server_id=server_id, chat_id=rental["chat_id"])
    if reason == "expired":
        send_fp(rental["chat_id"], render("post_expire", **values), rental["buyer_name"])
        notify_admins(f"🏁 <b>Аренда завершена</b>\nЗаказ #{esc(rental['order_id'])}, сервер <code>{esc(server_id)}</code>.\n"
                      "Сервер выключен, ссылка обновлена.")
    elif reason in ("manual", "cancelled"):
        send_fp(rental["chat_id"], f"⛔ Аренда по заказу #{rental['order_id']} досрочно завершена администратором. "
                                   "Сервер отключён, ссылка больше не работает.", rental["buyer_name"])
    for lot in S.q("SELECT * FROM friend_lots WHERE rental_id=? AND state='active'", (rental_id,)):
        remove_friend_lot(lot)
    request_lot_sync()
    return True, ""


def remove_rental_friends(rental: sqlite3.Row, api: RobloxAPI) -> None:
    friends = S.q("SELECT * FROM rental_friends WHERE rental_id=? AND state='added'", (rental["rental_id"],))
    if not friends:
        return
    if not S.flag("friends_auto_remove"):
        S.ex("UPDATE rental_friends SET state='kept' WHERE rental_id=? AND state='added'", (rental["rental_id"],))
        return
    for friend in friends:
        if friend["on_server"]:
            try:
                api.set_permission(rental["server_id"], friend["roblox_id"], False)
            except Exception:
                LOG.debug("AutoVIP: удаление доступа %s", friend["roblox_id"], exc_info=True)
        S.ex("UPDATE rental_friends SET state='removed' WHERE rental_id=? AND roblox_id=?",
             (rental["rental_id"], friend["roblox_id"]))
    for friend in friends:
        if not friend["unfriend"]:
            continue
        still_needed = S.q1(f"""SELECT 1 FROM rental_friends f JOIN rentals r ON r.rental_id=f.rental_id
            WHERE f.roblox_id=? AND f.account_id IS ? AND f.state='added' AND r.status IN {LIVE} AND f.rental_id!=?""",
                            (friend["roblox_id"], friend["account_id"], rental["rental_id"]))
        if still_needed:
            continue
        try:
            api.unfriend(friend["roblox_id"])
        except Exception:
            LOG.debug("AutoVIP: удаление из друзей %s", friend["roblox_id"], exc_info=True)


def extend_rental(rental_id: str, hours: float) -> sqlite3.Row:
    with rental_lock(rental_id):
        rental = get_active(rental_id)
        if not rental:
            raise ValueError("Аренда уже не активна")
        new_end = float(rental["end_time"]) + hours * 3600
        S.ex("UPDATE rentals SET end_time=?,hours=hours+?,warned=CASE WHEN ?-?>? THEN 0 ELSE warned END WHERE rental_id=?",
             (new_end, hours, new_end, now(), WARN_BEFORE, rental_id))
    send_fp(rental["chat_id"], f"🔄 Аренда продлена! К заказу #{rental['order_id']} добавлено {fmt_num(hours)} ч.\n"
                               f"⏰ Теперь осталось: {fmt_left(new_end - now())}.", rental["buyer_name"])
    return rental


# ---------------------------------------------------------------------------
# Друзья
# ---------------------------------------------------------------------------
def rental_api(rental: sqlite3.Row) -> Tuple[sqlite3.Row, RobloxAPI]:
    account = S.q1("SELECT * FROM accounts WHERE id=?", (rental["account_id"],)) if rental["account_id"] else None
    if account:
        return account, api_for(account)
    account, api, _ = owner_of(rental["server_id"])
    S.ex("UPDATE rentals SET account_id=? WHERE rental_id=?", (account["id"], rental["rental_id"]))
    return account, api


def cmd_friends(rental: sqlite3.Row, raw: str, paid_context: bool = False) -> str:
    """Возвращает итог: added | exists | not_found | limit | paid | error."""
    chat, name = rental["chat_id"], rental["buyer_name"]
    with rental_lock(rental["rental_id"]):
        rental = get_active(rental["rental_id"])
        if not rental:
            send_fp(chat, "❌ Эта аренда уже закончилась.", name)
            return "error"
        limit = friend_limit_for(rental)
        used = friends_used(rental["rental_id"])
        allowed = limit + int(rental["paid_slots"] or 0)
        if limit and used >= allowed:
            if S.flag("paid_active"):
                return start_paid_flow(rental, raw)
            send_fp(chat, render("friend_limit", limit=limit, order_id=rental["order_id"]), name)
            return "limit"
        profile, _ = owner_profile(rental)
        try:
            account, api = rental_api(rental)
            profile = f"https://www.roblox.com/users/{account['roblox_id']}/profile"
            target = api.resolve_user(raw)
            if not target:
                if not paid_context:
                    send_fp(chat, render("friend_not_found", input=raw, owner_profile_link=profile), name)
                return "not_found"
            user_id, user_name = target
            if user_id == str(account["roblox_id"]):
                send_fp(chat, "ℹ️ Это аккаунт владельца сервера — укажите свой ник Roblox.", name)
                return "error"
            if S.q1("SELECT 1 FROM rental_friends WHERE rental_id=? AND roblox_id=? AND state='added'",
                    (rental["rental_id"], user_id)):
                send_fp(chat, f"ℹ️ {user_name} уже добавлен(а) в друзья и на сервер.", name)
                return "exists"
            status = api.friend_status(str(account["roblox_id"]), user_id)
            if status == "RequestReceived":
                api.accept_friend(user_id)
                if api.friend_status(str(account["roblox_id"]), user_id) != "Friends":
                    raise RobloxError("Roblox не подтвердил принятие заявки")
                unfriend = 1
            elif status == "Friends":
                unfriend = 1 if S.q1(f"""SELECT 1 FROM rental_friends f JOIN rentals r ON r.rental_id=f.rental_id
                    WHERE f.roblox_id=? AND f.account_id=? AND f.unfriend=1 AND f.state='added' AND r.status IN {LIVE}""",
                                     (user_id, account["id"])) else 0
            else:
                if not paid_context:
                    send_fp(chat, render("friend_not_found", input=raw, owner_profile_link=profile), name)
                return "not_found"
            on_server = 0
            try:
                on_server = int(api.set_permission(rental["server_id"], user_id, True))
            except Exception:
                LOG.info("AutoVIP: Roblox не добавил %s в доступ сервера (приватность?)", user_id, exc_info=True)
            S.ex("""INSERT INTO rental_friends(rental_id,roblox_id,name,account_id,on_server,unfriend,state,added_at)
                    VALUES(?,?,?,?,?,?,'added',?) ON CONFLICT(rental_id,roblox_id) DO UPDATE SET
                    name=excluded.name,account_id=excluded.account_id,on_server=excluded.on_server,
                    unfriend=MAX(rental_friends.unfriend,excluded.unfriend),state='added'""",
                 (rental["rental_id"], user_id, user_name, account["id"], on_server, unfriend, now()))
        except RobloxError as exc:
            LOG.warning("AutoVIP: !друзья: %s", exc)
            send_fp(chat, "❌ Roblox сейчас не отвечает. Повторите команду через пару минут.", name)
            return "error"
    send_fp(chat, render("friend_added", friend_name=user_name, order_id=rental["order_id"]), name)
    if limit and S.flag("paid_active") and used + 1 == limit and not rental["paid_slots"]:
        send_fp(chat, render("paid_limit_reached", limit=limit, price=fmt_num(S.get("paid_price")),
                             order_id=rental["order_id"]), name)
    return "added"


def start_paid_flow(rental: sqlite3.Row, raw: str) -> str:
    chat, name = rental["chat_id"], rental["buyer_name"]
    price = parse_number(S.get("paid_price") or 50)
    existing = S.q1("SELECT * FROM friend_lots WHERE buyer_id=? AND state='active' ORDER BY created_at DESC LIMIT 1",
                    (rental["buyer_id"],))
    if existing:
        send_fp(chat, render("paid_lot_exists", link=f"https://funpay.com/lots/offer?id={existing['offer_id']}",
                             minutes=max(1, math.ceil((existing["expires_at"] - now()) / 60)),
                             price=fmt_num(existing["price"]), friend_name=existing["friend_name"] or ""), name)
        return "paid"
    if not account_ready():
        send_fp(chat, "⏳ Бот ещё подключается к FunPay. Повторите команду через пару минут.", name)
        return "error"
    try:
        account, api = rental_api(rental)
        profile = f"https://www.roblox.com/users/{account['roblox_id']}/profile"
        target = api.resolve_user(raw)
    except RobloxError:
        send_fp(chat, "❌ Roblox сейчас не отвечает. Повторите команду через пару минут.", name)
        return "error"
    if not target:
        send_fp(chat, render("friend_not_found", input=raw, owner_profile_link=profile), name)
        return "not_found"
    user_id, user_name = target
    if S.q1("SELECT 1 FROM rental_friends WHERE rental_id=? AND roblox_id=? AND state='added'",
            (rental["rental_id"], user_id)):
        send_fp(chat, f"ℹ️ {user_name} уже добавлен(а) в друзья и на сервер.", name)
        return "exists"
    tag = "#FR" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    while S.q1("SELECT 1 FROM friend_lots WHERE tag=?", (tag,)):
        tag = "#FR" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
    minutes = max(1, min(1440, int(parse_number(S.get("paid_lifetime") or 10))))
    try:
        with FP_LOCK:
            offer_id = create_friend_offer(tag, price)
    except Exception as exc:
        LOG.warning("AutoVIP: лот доплаты не создан: %s", exc)
        send_fp(chat, "❌ Не удалось создать лот для доплаты. Попробуйте позже или напишите продавцу.", name)
        notify_admins(f"⚠️ <b>Не удалось создать лот доплаты за друзей</b>\nАренда #{esc(rental['order_id'])}, "
                      f"игрок {esc(user_name)}.\nПричина: {esc(exc)}")
        return "error"
    created = now()
    S.ex("""INSERT INTO friend_lots(tag,offer_id,rental_id,buyer_id,chat_id,friend_input,friend_name,price,created_at,
            expires_at,state) VALUES(?,?,?,?,?,?,?,?,?,?,'active')""",
         (tag, offer_id, rental["rental_id"], rental["buyer_id"], chat, raw, user_name, price, created,
          created + minutes * 60))
    link = f"https://funpay.com/lots/offer?id={offer_id}"
    send_fp(chat, render("paid_lot_created", link=link, price=fmt_num(price), minutes=minutes, friend_name=user_name,
                         limit=friend_limit_for(rental), order_id=rental["order_id"]), name)
    notify_admins(f"🧾 <b>Создан лот доплаты за друзей</b>\nАренда #{esc(rental['order_id'])}, друг {esc(user_name)}, "
                  f"цена {fmt_num(price)} {currency_symbol()}\n{esc(link)}")
    return "paid"


def remove_friend_lot(lot: sqlite3.Row, final_state: str = "deleted") -> bool:
    """Удаляет временный лот доплаты с FunPay. При ошибке повторяет попытку через минуту (до 5 раз)."""
    try:
        if lot["offer_id"]:
            with FP_LOCK:
                delete_offer(lot["offer_id"])
        S.ex("UPDATE friend_lots SET state=? WHERE tag=?", (final_state, lot["tag"]))
        return True
    except Exception as exc:
        attempts = int(lot["attempts"] or 0) + 1
        if attempts >= 5:
            S.ex("UPDATE friend_lots SET attempts=?,state=? WHERE tag=?", (attempts, final_state, lot["tag"]))
            notify_admins(f"⚠️ Не удалось удалить лот доплаты {esc(lot['tag'])} (ID {esc(lot['offer_id'])}): {esc(exc)}. "
                          "Удалите его на FunPay вручную.")
        else:
            S.ex("UPDATE friend_lots SET attempts=?,state=?,expires_at=? WHERE tag=?",
                 (attempts, "deleting" if final_state == "paid" else "active", now() + 60, lot["tag"]))
        return False


def handle_friend_payment(order: Any, order_id: str, tag: str) -> str:
    chat_id, buyer_id, buyer_name = order_chat(order, order_id)
    lot = S.q1("SELECT * FROM friend_lots WHERE tag=?", (tag,))
    try:
        amount = max(1, int(getattr(order, "amount", 1) or 1))
    except (TypeError, ValueError):
        amount = 1
    try:
        price = float(getattr(order, "price", 0) or 0)
    except (TypeError, ValueError):
        price = 0.0
    if not lot:
        notify_admins(f"⚠️ <b>Оплачен лот доплаты, но запись о нём не найдена</b>\nЗаказ #{esc(order_id)}, тег {esc(tag)}. "
                      "Обработайте заказ вручную.")
        return "friend_unknown"
    if lot["state"] in ("active", "deleting"):
        remove_friend_lot(lot, "paid")
    else:
        S.ex("UPDATE friend_lots SET state='paid' WHERE tag=?", (tag,))
    rental = get_active(lot["rental_id"])
    problem = None
    if str(buyer_id) != str(lot["buyer_id"]):
        problem = ("лот купил другой покупатель", "❌ Этот лот был создан для другого покупателя, поэтому добавление не начислено.")
    elif not rental:
        problem = ("аренда уже не активна", "❌ Аренда, для которой создан лот, уже закончилась, поэтому добавление не начислено.")
    if problem:
        refunded = refund(order_id)
        send_fp(chat_id, problem[1] + ("\n💸 Деньги возвращены автоматически." if refunded else "\nПродавец уведомлён."),
                buyer_name)
        S.ex("INSERT OR REPLACE INTO friend_payments VALUES(?,?,?,?,?,?,?)",
             (order_id, tag, lot["rental_id"], amount, price, "refunded" if refunded else "rejected", now()))
        notify_admins(f"⚠️ <b>Оплата лота доплаты за друзей не применена</b>\nЗаказ #{esc(order_id)}: {problem[0]}. "
                      + ("Автовозврат выполнен." if refunded else "<b>Автовозврат не удался — верните деньги вручную.</b>"))
        return "friend_rejected"
    with rental_lock(rental["rental_id"]):
        S.ex("UPDATE rentals SET paid_slots=paid_slots+? WHERE rental_id=?", (amount, rental["rental_id"]))
        S.ex("INSERT OR REPLACE INTO friend_payments VALUES(?,?,?,?,?,?,?)",
             (order_id, tag, rental["rental_id"], amount, price, "applied", now()))
    send_fp(rental["chat_id"], render("paid_received", amount=amount, order_id=rental["order_id"]), rental["buyer_name"])
    notify_admins(f"💳 <b>Оплачено добавление в друзья</b>\nАренда #{esc(rental['order_id'])}, количество {amount}, "
                  f"сумма {fmt_num(price)} {currency_symbol()}, заказ FunPay #{esc(order_id)}.")
    result = cmd_friends(rental, lot["friend_input"] or "", paid_context=True)
    if result == "not_found":
        profile, _ = owner_profile(rental)
        send_fp(rental["chat_id"], f"ℹ️ Оплаченное добавление сохранено. Заявка от {lot['friend_name'] or lot['friend_input']} "
                                   f"пока не найдена: отправьте заявку в друзья владельцу ({profile}) и повторите "
                                   f"!друзья {lot['friend_input']}", rental["buyer_name"])
    fresh = S.q1("SELECT * FROM rentals WHERE rental_id=?", (rental["rental_id"],))
    left = friend_limit_for(fresh) + int(fresh["paid_slots"] or 0) - friends_used(fresh["rental_id"])
    send_fp(rental["chat_id"], f"ℹ️ Для этого сервера осталось добавлений: {max(0, left)}. "
                               "Остальных друзей добавляйте командой !друзья ник.", rental["buyer_name"])
    return "friend_payment"


# ---------------------------------------------------------------------------
# Отзывы, ссылка
# ---------------------------------------------------------------------------
def cmd_review(order_id: str, chat_id: str, chat_name: Optional[str]) -> None:
    if S.q1("SELECT 1 FROM reviews WHERE order_id=?", (order_id,)):
        send_fp(chat_id, f"ℹ️ Бонус за заказ #{order_id} уже получен.", chat_name)
        return
    try:
        order = CARDINAL.account.get_order(order_id)
    except Exception:
        send_fp(chat_id, "❌ Ошибка проверки отзыва. Попробуйте позже.", chat_name)
        return
    review = getattr(order, "review", None)
    has_review = review is not None and bool(getattr(review, "stars", None) or str(getattr(review, "text", "") or "").strip())
    if not has_review:
        send_fp(chat_id, "❌ Отзыв не найден. Оставьте отзыв и напишите !отзыв снова.", chat_name)
        return
    try:
        bonus = parse_number(S.get("review_bonus_hours"))
    except ValueError:
        bonus = 1.0
    new_time = ""
    with S.tx():
        if S.q1("SELECT 1 FROM reviews WHERE order_id=?", (order_id,)):
            return
        S.ex("INSERT INTO reviews VALUES(?,?)", (order_id, now()))
    rental = S.q1("SELECT * FROM rentals WHERE order_id=? AND status='active' AND end_time>?", (order_id, now()))
    if rental and bonus > 0:
        with rental_lock(rental["rental_id"]):
            new_end = float(rental["end_time"]) + bonus * 3600
            S.ex("UPDATE rentals SET end_time=?,hours=hours+?,warned=CASE WHEN ?-?>? THEN 0 ELSE warned END WHERE rental_id=?",
                 (new_end, bonus, new_end, now(), WARN_BEFORE, rental["rental_id"]))
            new_time = fmt_dt(new_end)
    send_fp(chat_id, render("review_bonus", bonus_hours=fmt_num(bonus), order_id=order_id, new_time=new_time), chat_name)


def cmd_link(rental: sqlite3.Row) -> None:
    with rental_lock(rental["rental_id"]):
        rental = get_active(rental["rental_id"])
        if not rental:
            return
        try:
            _, api = rental_api(rental)
            link = api.new_link(rental["server_id"], rental["place_id"] or server_place_fallback(rental["server_id"]))
        except Exception as exc:
            LOG.warning("AutoVIP: !ссылка: %s", exc)
            send_fp(rental["chat_id"], "❌ Не удалось создать новую ссылку. Попробуйте позже или напишите продавцу.",
                    rental["buyer_name"])
            return
        S.ex("UPDATE rentals SET link=? WHERE rental_id=?", (link, rental["rental_id"]))
    send_fp(rental["chat_id"], f"🔗 Новая ссылка на сервер (заказ #{rental['order_id']}):\n{link}\n"
                               "Старая ссылка больше не работает, время аренды не изменилось.", rental["buyer_name"])


# ---------------------------------------------------------------------------
# Команды покупателя в чате FunPay
# ---------------------------------------------------------------------------
COMMANDS = {"!команды": "help", "!help": "help", "!время": "time", "!ссылка": "link", "!друзья": "friends",
            "!бронь": "stock", "!отзыв": "review"}
HELP_TEXT = ("📋 Команды:\n"
             "!время — сколько осталось до конца аренды\n"
             "!ссылка — новая ссылка на сервер\n"
             "!друзья ник — принять заявку в друзья и добавить на сервер\n"
             "!бронь — сколько свободных серверов сейчас\n"
             "!отзыв — бонусное время за отзыв\n"
             "!команды — этот список")


def parse_command(text: str) -> Optional[Tuple[str, str]]:
    text = str(text or "").strip()
    if not text.startswith("!"):
        return None
    parts = text.split(maxsplit=1)
    action = COMMANDS.get(parts[0].lower())
    return (action, parts[1].strip() if len(parts) > 1 else "") if action else None


def selection_list(rentals: List[sqlite3.Row]) -> str:
    lines = []
    for index, rental in enumerate(rentals, 1):
        lines.append(f"{index}) {rental_title(rental)} · заказ #{rental['order_id']} · ⏰ осталось "
                     f"{fmt_left(rental['end_time'] - now())}")
    return "\n".join(lines)


def handle_chat_message(chat_id: Any, author_id: Any, author_name: Optional[str], text: str,
                        message_id: Any = None) -> None:
    chat_id, author_id = str(chat_id), str(author_id)
    if message_id:
        key = f"{chat_id}:{message_id}"
        with GUARD:
            for old in [k for k, at in HANDLED.items() if now() - at > 900]:
                HANDLED.pop(old, None)
            if key in HANDLED:
                return
            HANDLED[key] = now()
    if blacklisted(author_name):
        return
    with named_lock("buyer:" + author_id):
        _handle_chat(chat_id, author_id, author_name, str(text or ""))


def _handle_chat(chat_id: str, buyer_id: str, buyer_name: Optional[str], text: str) -> None:
    key = (chat_id, buyer_id)
    command = parse_command(text)
    selection = SELECTIONS.get(key)
    if selection and selection["expires"] < now():
        SELECTIONS.pop(key, None)
        selection = None
    if command is None:
        if not selection:
            return
        answer = text.strip().lower()
        if answer in CANCEL_WORDS:
            SELECTIONS.pop(key, None)
            send_fp(chat_id, "✅ Выбор отменён.", buyer_name)
            return
        if not re.fullmatch(r"\d{1,3}", answer):
            return  # обычная переписка не перехватывается
        number, rentals = int(answer), selection["rentals"]
        if not 1 <= number <= len(rentals):
            send_fp(chat_id, f"❌ Введите номер от 1 до {len(rentals)}.", buyer_name)
            return
        SELECTIONS.pop(key, None)
        rental = get_active(rentals[number - 1])
        if not rental:
            send_fp(chat_id, "❌ Эта аренда уже закончилась. Повторите команду.", buyer_name)
            return
        run_rental_action(selection["action"], rental, selection["arg"])
        return
    SELECTIONS.pop(key, None)  # новая команда отменяет незавершённый выбор
    action, arg = command
    if action == "help":
        send_fp(chat_id, HELP_TEXT, buyer_name)
        return
    if action == "stock":
        send_fp(chat_id, f"🟢 Свободных общих серверов сейчас: {free_general_count()}.", buyer_name)
        return
    rentals = active_rentals(buyer_id)
    if action == "time":
        if not rentals:
            send_fp(chat_id, "❌ Нет активной аренды.", buyer_name)
        elif len(rentals) == 1:
            send_fp(chat_id, f"⏰ До конца аренды осталось: {fmt_left(rentals[0]['end_time'] - now())}\n"
                             f"📦 Заказ #{rentals[0]['order_id']}", buyer_name)
        else:
            send_fp(chat_id, "⏰ Ваши активные аренды:\n" + selection_list(rentals), buyer_name)
        return
    if action == "friends":
        if not S.flag("friends_module_active"):
            return
        if not arg:
            send_fp(chat_id, "ℹ️ Укажите ник Roblox: !друзья ВашНик\nВместо ника можно указать ID или ссылку на профиль. "
                             "Сначала отправьте заявку в друзья владельцу сервера.", buyer_name)
            return
    if action == "review":
        if not S.flag("review_bonus_active"):
            send_fp(chat_id, "Бонусы отключены.", buyer_name)
            return
        if not rentals:
            last = S.q1("SELECT order_id FROM rentals WHERE buyer_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (buyer_id,))
            if not last:
                send_fp(chat_id, "❌ Не найдено заказов для проверки отзыва.", buyer_name)
            else:
                cmd_review(last["order_id"], chat_id, buyer_name)
            return
    if not rentals:
        send_fp(chat_id, "❌ Нет активной аренды.", buyer_name)
        return
    if len(rentals) == 1:
        run_rental_action(action, rentals[0], arg)
        return
    SELECTIONS[key] = {"action": action, "arg": arg, "rentals": [r["rental_id"] for r in rentals],
                       "expires": now() + SELECTION_TTL}
    names = {"link": "!ссылка", "friends": "!друзья", "review": "!отзыв"}
    send_fp(chat_id, f"🖥 У вас несколько активных серверов. К какому применить {names[action]}?\n"
                     + selection_list(rentals) + f"\n\nОтветьте номером (1–{len(rentals)}) или напишите «отмена».",
            buyer_name)


def run_rental_action(action: str, rental: sqlite3.Row, arg: str) -> None:
    if action == "link":
        cmd_link(rental)
    elif action == "friends":
        cmd_friends(rental, arg)
    elif action == "review":
        cmd_review(rental["order_id"], rental["chat_id"], rental["buyer_name"])


def chat_relevant(chat_id: Any, author_id: Any, text: str) -> bool:
    if parse_command(text) is not None:
        return True
    return (str(chat_id), str(author_id)) in SELECTIONS


def own_id() -> str:
    return str(getattr(getattr(CARDINAL, "account", None), "id", "") or "")


def on_new_message(cardinal: Any, event: Any) -> None:
    if getattr(cardinal, "old_mode_enabled", False):
        return
    message = getattr(event, "message", None)
    if message is None or getattr(message, "by_bot", False):
        return
    author_id = getattr(message, "author_id", None)
    text = getattr(message, "text", None) or ""
    if not author_id or str(author_id) in ("0", own_id()):
        return
    if not chat_relevant(message.chat_id, author_id, text):
        return
    EXECUTOR.submit(_safe, handle_chat_message, message.chat_id, author_id,
                    getattr(message, "author", None) or getattr(message, "chat_name", None), text,
                    getattr(message, "id", None))


def on_last_chat_message_changed(cardinal: Any, event: Any) -> None:
    """Старый режим получения сообщений Cardinal: читаем историю, чтобы узнать автора."""
    if not getattr(cardinal, "old_mode_enabled", False):
        return
    chat = getattr(event, "chat", None)
    text = str(getattr(chat, "last_message_text", "") or "")
    if chat is None or getattr(chat, "last_by_bot", False):
        return
    if parse_command(text) is None and not any(k[0] == str(chat.id) for k in SELECTIONS):
        return
    EXECUTOR.submit(_safe, _legacy_message, chat.id, getattr(chat, "name", None), text)


def _legacy_message(chat_id: Any, chat_name: Optional[str], text: str) -> None:
    history = CARDINAL.account.get_chat_history(chat_id, interlocutor_username=chat_name)
    me = own_id()
    for message in reversed(list(history or [])):
        author = getattr(message, "author_id", None)
        if not author or str(author) in ("0", me) or getattr(message, "by_bot", False):
            continue
        if str(getattr(message, "text", "") or "").strip() == text.strip():
            handle_chat_message(chat_id, author, getattr(message, "author", None) or chat_name,
                                message.text, getattr(message, "id", None))
        return


def _safe(func, *args) -> None:
    try:
        func(*args)
    except Exception:
        LOG.exception("AutoVIP: ошибка обработки сообщения")


# ---------------------------------------------------------------------------
# Синхронизация лотов и цен
# ---------------------------------------------------------------------------
def sync_lots(full: bool = False) -> None:
    if not S.flag("lot_management") or not account_ready():
        return
    with FP_LOCK:
        for lot in S.q("SELECT * FROM lots ORDER BY lot_id"):
            if STOP.is_set():
                return
            desired = free_server_for(lot)[0] is not None
            if not full and lot["last_active"] is not None and bool(lot["last_active"]) == desired:
                continue
            try:
                fields = CARDINAL.account.get_lot_fields(int(lot["lot_id"]))
                if bool(getattr(fields, "active", False)) != desired:
                    fields.active = desired
                    renew_and_save(fields)
                S.ex("UPDATE lots SET last_active=? WHERE lot_id=?", (int(desired), lot["lot_id"]))
            except Exception:
                LOG.warning("AutoVIP: не удалось обновить состояние лота %s на FunPay", lot["lot_id"], exc_info=True)
            time.sleep(0.5)


def apply_prices() -> None:
    column = "price_event" if S.flag("event_mode") else "price_normal"
    failed, done = [], 0
    with FP_LOCK:
        for lot in S.q(f"SELECT lot_id,{column} AS price FROM lots ORDER BY lot_id"):
            if not lot["price"] or lot["price"] <= 0:
                continue
            try:
                fields = CARDINAL.account.get_lot_fields(int(lot["lot_id"]))
                fields.price = round(float(lot["price"]), 2)
                renew_and_save(fields)
                done += 1
            except Exception:
                LOG.warning("AutoVIP: цена лота %s не обновлена", lot["lot_id"], exc_info=True)
                failed.append(str(lot["lot_id"]))
            time.sleep(0.5)
    mode = "🎉 Евент" if S.flag("event_mode") else "📋 Обычный"
    text = f"💰 Режим цен: {mode}. Обновлено лотов: {done}."
    if failed:
        text += "\n⚠️ Не удалось обновить: " + ", ".join(failed)
    notify_admins(text)


# ---------------------------------------------------------------------------
# Фоновый цикл: таймеры аренд, повторы, лоты доплаты, очередь сообщений
# ---------------------------------------------------------------------------
def worker_loop() -> None:
    last_full_sync = 0.0
    while not STOP.wait(5):
        if not plugin_enabled() or S is None:
            continue
        try:
            worker_tick()
            if SYNC_REQUEST.is_set() or now() - last_full_sync >= LOT_SYNC_INTERVAL:
                full = now() - last_full_sync >= LOT_SYNC_INTERVAL
                SYNC_REQUEST.clear()
                if full:
                    last_full_sync = now()
                run_once("lots", sync_lots, full)
        except Exception:
            LOG.exception("AutoVIP: ошибка фонового цикла")


def worker_tick() -> None:
    t = now()
    for rental in S.q("SELECT * FROM rentals WHERE status='active' AND warned=0 AND end_time>? AND end_time-?<=?",
                      (t, t, WARN_BEFORE)):
        S.ex("UPDATE rentals SET warned=1 WHERE rental_id=?", (rental["rental_id"],))
        if rental["hours"] * 3600 > WARN_BEFORE:
            send_fp(rental["chat_id"], f"⏰ До окончания аренды по заказу #{rental['order_id']} осталось 15 минут.",
                    rental["buyer_name"])
    for rental in S.q("SELECT rental_id FROM rentals WHERE status='active' AND end_time<=?", (t,)):
        run_once("rental:" + rental["rental_id"], end_rental, rental["rental_id"], "expired")
    for rental in S.q("SELECT rental_id FROM rentals WHERE status='ending' AND next_attempt<=?", (t,)):
        run_once("rental:" + rental["rental_id"], end_rental, rental["rental_id"], None)
    for lot in S.q("SELECT * FROM friend_lots WHERE state IN ('active','deleting') AND expires_at<=?", (t,)):
        run_once("flot:" + lot["tag"], remove_friend_lot, lot, "deleted" if lot["state"] == "active" else "paid")
    for item in S.q("SELECT * FROM outbox WHERE state='pending' AND next_at<=? ORDER BY id LIMIT 10", (t,)):
        if _send_now(item["chat_id"], item["text"], item["chat_name"]):
            S.ex("UPDATE outbox SET state='sent' WHERE id=?", (item["id"],))
        else:
            attempts = item["attempts"] + 1
            S.ex("UPDATE outbox SET attempts=?,next_at=?,state=? WHERE id=?",
                 (attempts, t + min(600, 30 * attempts), "failed" if attempts >= 10 else "pending", item["id"]))
            if attempts >= 10:
                notify_admins(f"⚠️ Не удалось доставить сообщение покупателю в чат {esc(item['chat_id'])}:\n"
                              f"{esc(short(item['text'], 300))}")


# ---------------------------------------------------------------------------
# Telegram: общие помощники
# ---------------------------------------------------------------------------
P = "avr17"
STATE = "avr17_input"
DUP_HELP = ("❌ Лот с таким названием уже добавлен!\n"
            "Бот узнаёт купленный лот по названию, поэтому названия лотов должны различаться. "
            "Измените название на FunPay, например:\n"
            "• добавьте эмодзи: 🔥 Название\n"
            "• слово или срок: Название | 3 часа\n"
            "• название игры: Название | Steal a Brainrot\n"
            "и добавьте лот снова.")


def cbd(*parts: Any) -> str:
    data = ":".join([P] + [str(p) for p in parts])
    if len(data.encode()) > 64:
        raise ValueError("callback_data слишком длинный")
    return data


def B(text: str, *parts: Any) -> Any:
    return Button(text, callback_data=cbd(*parts))


def kb_rows(*rows: Any) -> Any:
    kb = Keyboard()
    for row in rows:
        if row:
            kb.row(*row)
    return kb


def chunks(items: List[Any], size: int) -> List[List[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def bot():
    return CARDINAL.telegram.bot


def show(chat_id: Any, text: str, kb: Any = None, mid: Any = None) -> None:
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    options = {"parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": kb}
    if mid:
        try:
            bot().edit_message_text(text, chat_id, mid, **options)
            return
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return
    try:
        bot().send_message(chat_id, text, **options)
    except Exception:
        LOG.warning("AutoVIP: меню не отправилось с кнопками", exc_info=True)
        bot().send_message(chat_id, text + "\n\n⚠️ Меню слишком большое для Telegram, поэтому кнопки не показаны. "
                           "Пользуйтесь постраничной навигацией или уменьшите количество элементов.",
                           parse_mode="HTML", disable_web_page_preview=True)


def say(chat_id: Any, text: str) -> None:
    try:
        bot().send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        LOG.debug("AutoVIP: say", exc_info=True)


def ask(chat_id: Any, user_id: Any, prompt: str, action: str, args: Tuple = (), back: str = "main",
        extra: Optional[Dict[str, Any]] = None) -> None:
    message = bot().send_message(chat_id, prompt, parse_mode="HTML", disable_web_page_preview=True,
                                 reply_markup=kb_rows([B("❌ Отмена ввода", "x", back)]))
    CARDINAL.telegram.set_state(chat_id, message.message_id, user_id, STATE,
                                {"action": action, "args": [str(a) for a in args], "back": back, "extra": extra or {}})


def input_state(message: Any) -> Optional[Dict[str, Any]]:
    try:
        state = CARDINAL.telegram.get_state(message.chat.id, message.from_user.id)
    except Exception:
        return None
    return state if state and state.get("state") == STATE else None


def clear_input(chat_id: Any, user_id: Any, delete_prompt: bool = False) -> None:
    try:
        state = CARDINAL.telegram.get_state(chat_id, user_id)
        if state and state.get("state") == STATE:
            CARDINAL.telegram.clear_state(chat_id, user_id, delete_prompt)
    except Exception:
        LOG.debug("AutoVIP: clear_state", exc_info=True)


def open_route(chat_id: Any, mid: Any, user_id: Any, route: str) -> None:
    parts = route.split(":")
    ROUTES[parts[0]](chat_id, mid, user_id, *parts[1:])


def finish(message: Any, reply: str, route: str) -> None:
    clear_input(message.chat.id, message.from_user.id, True)
    bot().reply_to(message, reply, parse_mode="HTML", disable_web_page_preview=True)
    open_route(message.chat.id, None, message.from_user.id, route)


def page_nav(route: str, page: int, pages: int) -> List[Any]:
    row = []
    if page > 0:
        row.append(B("⬅️", route, page - 1))
    if page + 1 < pages:
        row.append(B("➡️", route, page + 1))
    return row


def toggle(key: str) -> bool:
    value = not S.flag(key)
    S.set(key, "1" if value else "0")
    return value


def on_off(key: str) -> str:
    return "🟢 Включено" if S.flag(key) else "🔴 Выключено"


# ---------------------------------------------------------------------------
# Главная панель
# ---------------------------------------------------------------------------
def menu_main(chat: Any, mid: Any, user: Any) -> None:
    accounts = S.val("SELECT COUNT(*) FROM accounts")
    servers = S.val("SELECT COUNT(*) FROM servers")
    categories = S.val("SELECT COUNT(*) FROM categories")
    free = S.val(f"SELECT COUNT(*) FROM servers WHERE {FREE_SQL}")
    busy = S.val(f"SELECT COUNT(DISTINCT server_id) FROM rentals WHERE status IN {LIVE}")
    ending = S.val("SELECT COUNT(*) FROM rentals WHERE status='ending'")
    text = ("🎮 <b>Auto VIP Roblox</b>\n\n"
            f"👥 Аккаунтов: <b>{accounts}</b>\n"
            f"🖥️ Серверов: <b>{servers}</b>\n"
            f"🗂 Категорий: <b>{categories}</b>\n"
            f"🌐 Глобальный прокси: {'✅ Подключен' if S.get('global_proxy') else '❌ Не задан'}\n\n"
            f"🟢 Свободно: <b>{free}</b> (из них общих: {free_general_count()})\n"
            f"🔴 Занято: <b>{busy}</b>\n"
            f"💰 Заработано (всего): <b>{fmt_money(earned_total())} {currency_symbol()}</b>\n\n"
            f"⚙️ Режим: <b>{'🎉 Евент' if S.flag('event_mode') else '📋 Обычный'}</b>")
    if ending:
        text += f"\n\n⚠️ Ожидают выключения сервера (повтор): <b>{ending}</b> — см. 🕹 Управление арендой."
    management = "🟢 Деактивировать лоты" if S.flag("lot_management") else "🔴 Активировать лоты"
    rows = [
        [B("Переключить режим", "mode")],
        [B("🌐 Настроить Прокси", "proxy")],
        [B("🖥️ Сервера", "srv", 0), B("👤 Аккаунты", "acc")],
        [B("🗂 Категории серверов", "cats")],
        [B("⚙️ Лоты", "lots", 0), B("🕹 Управление арендой", "rents", 0)],
        [B("🎁 Бонусы", "bon"), B("📊 Статистика", "stats")],
        [B("📜 История заказов", "hist"), B("⭐ Отзывы", "rev")],
        [B("🤝 Заявки в друзья", "fr")],
        [B("📨 Шаблоны сообщений", "tpl")],
        [B(management, "lotmgmt")],
    ]
    if CBT is not None and hasattr(CBT, "PLUGINS_LIST"):
        rows.append([Button("◀️ В плагины", callback_data=f"{CBT.PLUGINS_LIST}:0")])
    show(chat, text, kb_rows(*rows), mid)


def act_mode(chat: Any, mid: Any, user: Any) -> None:
    with GUARD:
        if "prices" in RUNNING:
            say(chat, "⏳ Цены ещё обновляются, подождите.")
            return
    toggle("event_mode")
    run_once("prices", apply_prices)
    say(chat, "⏳ Режим переключён, выставляю цены на FunPay. При большом количестве лотов это займёт время.")
    menu_main(chat, mid, user)


def act_lot_management(chat: Any, mid: Any, user: Any) -> None:
    if toggle("lot_management"):
        S.ex("UPDATE lots SET last_active=NULL")
        request_lot_sync()
    menu_main(chat, mid, user)


# --- прокси ---------------------------------------------------------------
def act_proxy(chat: Any, mid: Any, user: Any) -> None:
    current = S.get("global_proxy")
    ask(chat, user, "🌐 <b>Прокси</b>\n\n"
        f"Текущий прокси: {('<code>' + esc(mask_proxy(current)) + '</code>') if current else 'не задан'}\n\n"
        "Отправьте новый прокси в формате:\n<code>login:pass@ip:port</code> — с логином и паролем\n"
        "<code>ip:port</code> — без авторизации\n\nИспользуйте HTTP/HTTPS-прокси. Чтобы удалить прокси, отправьте <code>-</code>.",
        "proxy")


def in_proxy(message: Any, text: str, data: Dict[str, Any]) -> None:
    if text == "-":
        S.set("global_proxy", "")
        reply = "✅ Прокси удалён."
    else:
        try:
            S.set("global_proxy", normalize_proxy(text))
        except ValueError:
            raise ValueError("Неверный формат прокси. Попробуйте еще раз.") from None
        reply = "✅ Прокси установлен"
    with GUARD:
        API_CACHE.clear()
    try:
        bot().delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    finish(message, reply, "main")


# --- аккаунты -------------------------------------------------------------
def _check_account(account: sqlite3.Row) -> Optional[str]:
    try:
        user_id, name = api_for(account).authenticated()
    except Exception:
        return None
    S.ex("UPDATE accounts SET username=?,roblox_id=? WHERE id=?", (name, user_id, account["id"]))
    return name


def menu_accounts(chat: Any, mid: Any, user: Any) -> None:
    accounts = S.q("SELECT * FROM accounts ORDER BY id")
    futures = [EXECUTOR.submit(_check_account, account) for account in accounts]
    lines = []
    for index, (account, future) in enumerate(zip(accounts, futures), 1):
        try:
            name = future.result(timeout=40)
        except Exception:
            name = None
        lines.append(f"{index}. {esc(name) if name else 'Error'}")
    text = "👤 <b>Аккаунты Roblox</b>\n\n" + ("\n".join(lines) if lines else "Аккаунтов пока нет. Нажмите «➕ Добавить».")
    if any(line.endswith("Error") for line in lines):
        text += "\n\n<i>Error — куки устарели или не работает прокси.</i>"
    delete = [B(f"🗑️ Удалить #{i}", "acc_del", a["id"]) for i, a in enumerate(accounts, 1)]
    show(chat, text, kb_rows([B("➕ Добавить", "acc_add")], *chunks(delete, 3), [B("⬅️ Назад", "main")]), mid)


def act_account_add(chat: Any, mid: Any, user: Any) -> None:
    ask(chat, user, "🍪 Отправьте куки <code>.ROBLOSECURITY</code> аккаунта Roblox целиком одним сообщением.\n"
        "Сообщение с куки бот удалит после проверки.", "acc_add", back="acc")


def in_account_add(message: Any, text: str, data: Dict[str, Any]) -> None:
    try:
        bot().delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    try:
        api = RobloxAPI(text, S.get("global_proxy"))
        user_id, name = api.authenticated()
    except Exception:
        raise ValueError("Невалидный куки или ошибка прокси.") from None
    cookie = api.cookie
    existing = S.q1("SELECT id FROM accounts WHERE roblox_id=?", (user_id,))
    if existing:
        S.ex("UPDATE accounts SET cookie=?,username=? WHERE id=?", (cookie, name, existing["id"]))
        with GUARD:
            API_CACHE.pop(int(existing["id"]), None)
    else:
        S.ex("INSERT INTO accounts(cookie,username,roblox_id,created_at) VALUES(?,?,?,?)", (cookie, name, user_id, now()))
    finish(message, f"✅ Аккаунт {esc(name)} добавлен.", "acc")


def act_account_delete(chat: Any, mid: Any, user: Any, account_id: str) -> None:
    if S.q1(f"SELECT 1 FROM rentals WHERE account_id=? AND status IN {LIVE}", (int(account_id),)):
        say(chat, "❌ С этого аккаунта выданы активные аренды. Удалите его после их окончания.")
    else:
        S.ex("DELETE FROM accounts WHERE id=?", (int(account_id),))
        S.ex("UPDATE servers SET owner_account_id=NULL WHERE owner_account_id=?", (int(account_id),))
        with GUARD:
            API_CACHE.pop(int(account_id), None)
    menu_accounts(chat, mid, user)


# --- серверы --------------------------------------------------------------
def busy_servers() -> set:
    return {r["server_id"] for r in S.q(f"SELECT server_id FROM rentals WHERE status IN {LIVE}")}


def menu_servers(chat: Any, mid: Any, user: Any, page: Any = 0) -> None:
    total = S.val("SELECT COUNT(*) FROM servers")
    pages = max(1, math.ceil(total / 20))
    page = min(max(0, int(page)), pages - 1)
    rows = S.q("""SELECT s.*,c.name cat FROM servers s LEFT JOIN categories c ON c.id=s.category_id
                  ORDER BY s.added_at, s.server_id LIMIT 20 OFFSET ?""", (page * 20,))
    busy = busy_servers()
    bound = {str(r["server_id"]) for r in S.q("SELECT server_id FROM lots WHERE server_id IS NOT NULL")}
    lines = []
    for row in rows:
        where = f"🗂 {esc(row['cat'])}" if row["cat"] else ("📌 закреплён за лотом" if row["server_id"] in bound else "общий")
        lines.append(f"{'🔴' if row['server_id'] in busy else '🟢'} <code>{esc(row['server_id'])}</code> · {where}")
    text = (f"🖥️ <b>Сервера</b>\n\nВсего: {total}" + (f" · Страница {page + 1}/{pages}" if pages > 1 else "") + "\n\n"
            + ("\n".join(lines) if lines else "Серверов пока нет. Нажмите «➕ Добавить».")
            + "\n\n<i>Общие — без категории и без привязки к лоту; выдаются лотами в режиме «Рандом».</i>")
    delete = [B(f"🗑️ Удалить {r['server_id']}", "srv_del", r["server_id"], page) for r in rows]
    show(chat, text, kb_rows([B("➕ Добавить", "srv_add")], *chunks(delete, 2), page_nav("srv", page, pages),
                             [B("⬅️ Назад", "main")]), mid)


def act_server_add(chat: Any, mid: Any, user: Any) -> None:
    ask(chat, user, "🖥️ Введите ID VIP-сервера (цифры после <code>id=</code> на странице Configure).", "srv_add",
        back="srv:0")


def in_server_add(message: Any, text: str, data: Dict[str, Any]) -> None:
    if not re.fullmatch(r"\d{1,20}", text):
        raise ValueError("ID сервера должен состоять только из цифр.")
    if S.q1("SELECT 1 FROM servers WHERE server_id=?", (text,)):
        finish(message, "⚠️ Уже есть.", "srv:0")
        return
    S.ex("INSERT INTO servers(server_id,added_at) VALUES(?,?)", (text, now()))
    request_lot_sync()
    finish(message, f"✅ Сервер {esc(text)} добавлен.", "srv:0")


def act_server_delete(chat: Any, mid: Any, user: Any, server_id: str, page: Any = 0) -> None:
    if server_id in busy_servers():
        say(chat, f"❌ На сервере {esc(server_id)} идёт аренда. Удалите его после её окончания.")
    else:
        S.ex("DELETE FROM servers WHERE server_id=?", (server_id,))
        request_lot_sync()
    menu_servers(chat, mid, user, page)


# --- категории ------------------------------------------------------------
def category_label(cat: sqlite3.Row) -> str:
    return str(cat["name"]) + (f" • {cat['game_name'] or cat['game_id']}" if cat["game_id"] else "")


def category_counts(cat_id: int) -> Tuple[int, int]:
    total = S.val("SELECT COUNT(*) FROM servers WHERE category_id=?", (cat_id,))
    free = S.val(f"SELECT COUNT(*) FROM servers WHERE category_id=? AND {FREE_SQL}", (cat_id,))
    return int(total), int(free)


def menu_categories(chat: Any, mid: Any, user: Any) -> None:
    cats = S.q("SELECT * FROM categories ORDER BY id")
    lines = []
    for cat in cats:
        total, free = category_counts(cat["id"])
        lines.append(f"• {esc(category_label(cat))} — серверов: {total}, свободно: {free}")
    text = ("🗂 <b>Категории серверов</b>\n\nКатегория — пул серверов (например, все серверы одной игры). "
            "Привяжите категорию к лоту — бот выдаст любой свободный сервер из неё, а лот будет активен, "
            "пока в категории есть свободный сервер.\n\n" + ("\n".join(lines) if lines else "Категорий пока нет."))
    buttons = [[B("✏️ " + short(category_label(c), 40), "cat", c["id"])] for c in cats[:90]]
    show(chat, text, kb_rows(*buttons, [B("➕ Добавить категорию", "cat_add")], [B("⬅️ Назад", "main")]), mid)


def menu_category(chat: Any, mid: Any, user: Any, cat_id: Any) -> None:
    cat = S.q1("SELECT * FROM categories WHERE id=?", (int(cat_id),))
    if not cat:
        say(chat, "❌ Категория не найдена.")
        menu_categories(chat, mid, user)
        return
    servers = S.q("SELECT server_id FROM servers WHERE category_id=? ORDER BY added_at, server_id", (cat["id"],))
    busy = busy_servers()
    total, free = category_counts(cat["id"])
    game = f"{esc(cat['game_name'] or cat['game_id'])} (<code>{esc(cat['game_id'])}</code>)" if cat["game_id"] else "не привязана"
    lines = [f"{'🔴' if s['server_id'] in busy else '🟢'} <code>{esc(s['server_id'])}</code>" for s in servers[:40]]
    if len(servers) > 40:
        lines.append(f"…и ещё {len(servers) - 40}")
    text = (f"🗂 <b>{esc(cat['name'])}</b>\n\n🎮 Игра: {game}\n🖥️ Серверов: {total}, свободно: {free}\n\n"
            + ("\n".join(lines) if lines else "В категории пока нет серверов. Нажмите «➕ Добавить серверы»."))
    delete = [B(f"🗑️ {s['server_id']}", "cs_del", cat["id"], s["server_id"]) for s in servers[:20]]
    show(chat, text, kb_rows([B("➕ Добавить серверы", "cs_add", cat["id"])], *chunks(delete, 2),
                             [B("🎮 Привязать игру", "cat_game", cat["id"]), B("✏️ Переименовать", "cat_ren", cat["id"])],
                             [B("🗑️ Удалить категорию", "cat_del", cat["id"])], [B("⬅️ К категориям", "cats")]), mid)


def act_category_add(chat: Any, mid: Any, user: Any) -> None:
    ask(chat, user, "🗂 Введите название категории (например, <code>Steal a Brainrot</code>).", "cat_add", back="cats")


def in_category_add(message: Any, text: str, data: Dict[str, Any]) -> None:
    if not text:
        raise ValueError("Название не может быть пустым.")
    cat_id = S.ex("INSERT INTO categories(name) VALUES(?)", (text[:100],)).lastrowid
    finish(message, f"✅ Категория «{esc(text[:100])}» создана. Добавьте в неё серверы.", f"cat:{cat_id}")


def act_category_servers_add(chat: Any, mid: Any, user: Any, cat_id: str) -> None:
    ask(chat, user, "🖥️ Отправьте ID VIP-серверов — можно несколько через пробел, запятую или с новой строки.\n"
        "Серверы автоматически попадут и в общий список «🖥️ Сервера».", "cs_add", (cat_id,), back=f"cat:{cat_id}")


def in_category_servers_add(message: Any, text: str, data: Dict[str, Any]) -> None:
    cat_id = int(data["args"][0])
    if not S.q1("SELECT 1 FROM categories WHERE id=?", (cat_id,)):
        raise ValueError("Категория уже удалена.")
    ids = [x for x in re.split(r"[\s,;]+", text) if x]
    bad = [x for x in ids if not re.fullmatch(r"\d{1,20}", x)]
    added = already = 0
    with S.tx():
        for server_id in ids:
            if server_id in bad:
                continue
            row = S.q1("SELECT category_id FROM servers WHERE server_id=?", (server_id,))
            if row and row["category_id"] == cat_id:
                already += 1
            elif row:
                S.ex("UPDATE servers SET category_id=? WHERE server_id=?", (cat_id, server_id))
                added += 1
            else:
                S.ex("INSERT INTO servers(server_id,category_id,added_at) VALUES(?,?,?)", (server_id, cat_id, now()))
                added += 1
    if not added and not already:
        raise ValueError("Не найдено ни одного числового ID сервера.")
    reply = f"✅ Добавлено серверов: {added}. Уже были в категории: {already}."
    if bad:
        reply += "\n⚠️ Пропущены (не ID): " + esc(", ".join(bad[:10]))
    request_lot_sync()
    finish(message, reply, f"cat:{cat_id}")


def act_category_server_delete(chat: Any, mid: Any, user: Any, cat_id: str, server_id: str) -> None:
    S.ex("UPDATE servers SET category_id=NULL WHERE server_id=? AND category_id=?", (server_id, int(cat_id)))
    request_lot_sync()
    menu_category(chat, mid, user, cat_id)


def act_category_game(chat: Any, mid: Any, user: Any, cat_id: str) -> None:
    ask(chat, user, "🎮 Отправьте ID игры (число после <code>/games/</code> в ссылке) в формате <code>ID</code> или "
        "<code>ID|Название</code>, например <code>109983668079237|Steal a Brainrot</code>.\n"
        "Чтобы убрать привязку игры, отправьте <code>-</code>.", "cat_game", (cat_id,), back=f"cat:{cat_id}")


def in_category_game(message: Any, text: str, data: Dict[str, Any]) -> None:
    cat_id = int(data["args"][0])
    if text == "-":
        S.ex("UPDATE categories SET game_id=NULL,game_name=NULL WHERE id=?", (cat_id,))
        finish(message, "✅ Привязка игры снята.", f"cat:{cat_id}")
        return
    game_id, _, name = text.partition("|")
    game_id, name = game_id.strip(), name.strip()
    if not re.fullmatch(r"\d{1,20}", game_id):
        raise ValueError("ID игры должен состоять из цифр. Формат: ID или ID|Название.")
    if not name:
        name = DEFAULT_GAME_NAME if game_id == DEFAULT_PLACE_ID else game_id
    S.ex("UPDATE categories SET game_id=?,game_name=? WHERE id=?", (game_id, name[:100], cat_id))
    finish(message, f"✅ Игра «{esc(name)}» привязана к категории.", f"cat:{cat_id}")


def act_category_rename(chat: Any, mid: Any, user: Any, cat_id: str) -> None:
    ask(chat, user, "✏️ Введите новое название категории.", "cat_ren", (cat_id,), back=f"cat:{cat_id}")


def in_category_rename(message: Any, text: str, data: Dict[str, Any]) -> None:
    if not text:
        raise ValueError("Название не может быть пустым.")
    S.ex("UPDATE categories SET name=? WHERE id=?", (text[:100], int(data["args"][0])))
    finish(message, "✅ Категория переименована.", f"cat:{data['args'][0]}")


def act_category_delete(chat: Any, mid: Any, user: Any, cat_id: str) -> None:
    cat_id_int = int(cat_id)
    lots = S.val("SELECT COUNT(*) FROM lots WHERE category_id=?", (cat_id_int,))
    with S.tx():
        S.ex("UPDATE lots SET category_id=NULL WHERE category_id=?", (cat_id_int,))
        S.ex("UPDATE servers SET category_id=NULL WHERE category_id=?", (cat_id_int,))
        S.ex("DELETE FROM categories WHERE id=?", (cat_id_int,))
    request_lot_sync()
    say(chat, "🗑️ Категория удалена. Серверы остались в общем списке."
        + (f"\n⚠️ Лотов, потерявших привязку: {lots}. Они переведены в режим «Рандом» — перепривяжите их." if lots else ""))
    menu_categories(chat, mid, user)


# --- лоты -----------------------------------------------------------------
def lot_bind_text(lot: sqlite3.Row) -> str:
    mode = lot_mode(lot)
    if mode == "category":
        cat = S.q1("SELECT * FROM categories WHERE id=?", (lot["category_id"],))
        total, free = category_counts(cat["id"])
        return f"🗂 Категория «{esc(category_label(cat))}» (свободно {free}/{total})"
    if mode == "server":
        exists = S.q1("SELECT 1 FROM servers WHERE server_id=?", (str(lot["server_id"]),))
        return f"🖥️ Один сервер <code>{esc(lot['server_id'])}</code>" + ("" if exists else " ⚠️ нет в списке серверов")
    return "🎲 Рандом (общие серверы)"


def lot_limit_text(lot: sqlite3.Row) -> str:
    if lot["friend_limit"] is None:
        return "по умолчанию"
    return "без ограничений" if int(lot["friend_limit"]) == 0 else str(lot["friend_limit"])


def menu_lots(chat: Any, mid: Any, user: Any, page: Any = 0) -> None:
    total = S.val("SELECT COUNT(*) FROM lots")
    pages = max(1, math.ceil(total / 20))
    page = min(max(0, int(page)), pages - 1)
    lots = S.q("SELECT * FROM lots ORDER BY added_at, lot_id LIMIT 20 OFFSET ?", (page * 20,))
    dups = duplicate_lots()
    dup_ids = {lot["lot_id"] for group in dups.values() for lot in group}
    text = f"⚙️ <b>Лоты</b>\n\nВсего лотов: {total} · Страница {page + 1}/{pages}"
    if dups:
        text += "\n\n⚠️ <b>Одинаковые названия лотов!</b> Бот не сможет понять, какой из них купили:\n"
        text += "\n".join("• " + ", ".join(f"{esc(l['name'])} (ID {l['lot_id']})" for l in group) for group in dups.values())
        text += "\nПереименуйте лоты на FunPay и нажмите «🔄 Обновить название с FunPay» в карточке."
    if not lots:
        text += "\n\nЛотов пока нет. Нажмите «➕ Добавить лот»."
    buttons = [[B(("⚠️ " if l["lot_id"] in dup_ids else "• ") + short(l["name"], 40) + f" ({fmt_num(l['hours'])}ч)",
                  "lot", l["lot_id"])] for l in lots]
    show(chat, text, kb_rows(*buttons, page_nav("lots", page, pages), [B("➕ Добавить лот", "lot_add")],
                             [B("⬅️ Назад", "main")]), mid)


def menu_lot(chat: Any, mid: Any, user: Any, lot_id: Any) -> None:
    lot = S.q1("SELECT * FROM lots WHERE lot_id=?", (int(lot_id),))
    if not lot:
        say(chat, "❌ Лот не найден.")
        menu_lots(chat, mid, user, 0)
        return
    text = (f"📦 <b>{esc(lot['name'])}</b>\n\n"
            f"🆔 ID лота FunPay: <code>{lot['lot_id']}</code>\n"
            f"⏰ Часов за 1 шт.: {fmt_num(lot['hours'])}\n"
            f"🔗 Выдача: {lot_bind_text(lot)}\n"
            f"👥 Лимит друзей: {lot_limit_text(lot)}\n"
            f"💰 Обычная цена: {fmt_num(lot['price_normal'])}\n"
            f"🎉 Евент цена: {fmt_num(lot['price_event'])}")
    twin = name_taken(lot["name"], lot["lot_id"])
    if twin:
        text += (f"\n\n⚠️ У лота ID {twin['lot_id']} такое же название — бот может выдать не тот сервер или не то время. "
                 "Переименуйте один из лотов на FunPay и нажмите «🔄 Обновить название с FunPay».")
    lid = lot["lot_id"]
    show(chat, text, kb_rows(
        [B("⏰ Изменить время (часы)", "lh", lid)],
        [B("🗂 Привязать категорию", "lc", lid)],
        [B("🖥️ Привязать один сервер", "ls", lid)],
        [B("💰 Обычная цена", "lpn", lid), B("🎉 Евент цена", "lpe", lid)],
        [B(f"👥 Лимит друзей ({lot_limit_text(lot)})", "lf", lid)],
        [B("🔄 Обновить название с FunPay", "lr", lid)],
        [B("🗑️ Удалить лот", "ld", lid)],
        [B("⬅️ Назад", "lots", 0)]), mid)


def act_lot_add(chat: Any, mid: Any, user: Any) -> None:
    ask(chat, user, "📦 Введите ID лота FunPay (цифры в конце ссылки на лот, например "
        "<code>https://funpay.com/lots/offer?id=12345678</code> → <code>12345678</code>).", "lot_add", back="lots:0")


def in_lot_add(message: Any, text: str, data: Dict[str, Any]) -> None:
    if not re.fullmatch(r"\d{1,12}", text):
        raise ValueError("ID лота должен быть числом")
    lot_id = int(text)
    if S.q1("SELECT 1 FROM lots WHERE lot_id=?", (lot_id,)):
        raise ValueError(f"⚠️ Лот {lot_id} уже добавлен в плагин")
    if not account_ready():
        raise ValueError("Бот ещё подключается к FunPay. Повторите через минуту.")
    try:
        name = lot_title_from_funpay(lot_id)
    except Exception:
        raise ValueError(f"Не удалось прочитать название лота {lot_id} с FunPay. Проверьте ID (лот должен быть вашим) "
                         "и попробуйте ещё раз.") from None
    if name_taken(name):
        raise ValueError(DUP_HELP[2:] + f"\n\nНазвание: «{name}»")
    clear_input(message.chat.id, message.from_user.id, True)
    ask(message.chat.id, message.from_user.id,
        f"📦 <b>{esc(name)}</b>\n🆔 {lot_id}\n\nВведите количество часов аренды за 1 шт. товара "
        "(например, <code>24</code> или <code>1.5</code>).", "lot_add2", (lot_id,), back="lots:0", extra={"name": name})


def in_lot_add2(message: Any, text: str, data: Dict[str, Any]) -> None:
    try:
        hours = parse_number(text)
    except ValueError:
        hours = 0
    if hours <= 0:
        raise ValueError("Введите число больше 0, например 24 или 1.5.")
    lot_id, name = int(data["args"][0]), data["extra"]["name"]
    if S.q1("SELECT 1 FROM lots WHERE lot_id=?", (lot_id,)):
        raise ValueError(f"⚠️ Лот {lot_id} уже добавлен в плагин")
    if name_taken(name):
        raise ValueError(DUP_HELP[2:])
    S.ex("INSERT INTO lots(lot_id,name,hours,added_at) VALUES(?,?,?,?)", (lot_id, name, hours, now()))
    request_lot_sync()
    finish(message, f"✅ Лот {esc(name)} ({fmt_num(hours)}ч) добавлен.", "lots:0")


def _lot_input(action: str, prompt: str):
    def opener(chat: Any, mid: Any, user: Any, lot_id: str) -> None:
        ask(chat, user, prompt, action, (lot_id,), back=f"lot:{lot_id}")
    return opener


def in_lot_value(message: Any, text: str, data: Dict[str, Any]) -> None:
    action, lot_id = data["action"], int(data["args"][0])
    if not S.q1("SELECT 1 FROM lots WHERE lot_id=?", (lot_id,)):
        raise ValueError("Лот уже удалён.")
    if action == "lh":
        try:
            value = parse_number(text)
        except ValueError:
            value = 0
        if value <= 0:
            raise ValueError("Введите положительное число часов (точка или запятая).")
        S.ex("UPDATE lots SET hours=? WHERE lot_id=?", (value, lot_id))
        reply = f"✅ Время: {fmt_num(value)} ч. за 1 шт."
    elif action in ("lpn", "lpe"):
        try:
            value = parse_number(text)
        except ValueError:
            value = -1
        if value <= 0:
            raise ValueError("Введите цену числом больше 0.")
        S.ex(f"UPDATE lots SET {'price_normal' if action == 'lpn' else 'price_event'}=? WHERE lot_id=?", (value, lot_id))
        reply = "✅ Цена сохранена. На FunPay она применится при переключении режима."
    elif action == "lf":
        if text == "-":
            S.ex("UPDATE lots SET friend_limit=NULL WHERE lot_id=?", (lot_id,))
            reply = "✅ Используется лимит по умолчанию."
        else:
            if not re.fullmatch(r"\d{1,4}", text):
                raise ValueError("Введите целое число от 1 до 9999, 0 — без ограничений, - — по умолчанию.")
            S.ex("UPDATE lots SET friend_limit=? WHERE lot_id=?", (int(text), lot_id))
            reply = "✅ Лимит друзей: " + ("без ограничений" if int(text) == 0 else text)
    else:  # ls — один сервер
        if text == "-":
            S.ex("UPDATE lots SET server_id=NULL WHERE lot_id=?", (lot_id,))
            reply = "✅ Привязка одного сервера снята."
        else:
            if not re.fullmatch(r"\d{1,20}", text):
                raise ValueError("ID сервера должен состоять из цифр (или - чтобы снять привязку).")
            S.ex("UPDATE lots SET server_id=?,category_id=NULL WHERE lot_id=?", (text, lot_id))
            reply = f"✅ Лот выдаёт только сервер {esc(text)}."
            if not S.q1("SELECT 1 FROM servers WHERE server_id=?", (text,)):
                reply += "\n⚠️ Этого сервера нет в списке «🖥️ Сервера» — добавьте его, иначе выдача не сработает."
        request_lot_sync()
    finish(message, reply, f"lot:{lot_id}")


def menu_lot_category(chat: Any, mid: Any, user: Any, lot_id: str) -> None:
    lot = S.q1("SELECT * FROM lots WHERE lot_id=?", (int(lot_id),))
    if not lot:
        menu_lots(chat, mid, user, 0)
        return
    cats = S.q("SELECT * FROM categories ORDER BY id")
    buttons = []
    for cat in cats[:80]:
        total, free = category_counts(cat["id"])
        mark = "✅ " if lot["category_id"] == cat["id"] else ""
        buttons.append([B(f"{mark}{short(category_label(cat), 36)} ({free}/{total})", "lcb", lot["lot_id"], cat["id"])])
    rows = buttons
    if lot["category_id"]:
        rows.append([B("🎲 Снять привязку (рандом/сервер)", "lcu", lot["lot_id"])])
    rows.append([B("⬅️ Назад к лоту", "lot", lot["lot_id"])])
    text = (f"🗂 <b>Привязка категории</b> — {esc(lot['name'])}\n\nВыберите категорию: лот будет выдавать любой свободный "
            "сервер из неё. Привязка категории отменяет привязку одного сервера.\nВ скобках — свободно/всего."
            + ("" if cats else "\n\nКатегорий пока нет — создайте их в «🗂 Категории серверов»."))
    show(chat, text, kb_rows(*rows), mid)


def act_lot_bind_category(chat: Any, mid: Any, user: Any, lot_id: str, cat_id: str) -> None:
    S.ex("UPDATE lots SET category_id=?,server_id=NULL WHERE lot_id=?", (int(cat_id), int(lot_id)))
    request_lot_sync()
    menu_lot(chat, mid, user, lot_id)


def act_lot_unbind_category(chat: Any, mid: Any, user: Any, lot_id: str) -> None:
    S.ex("UPDATE lots SET category_id=NULL WHERE lot_id=?", (int(lot_id),))
    request_lot_sync()
    menu_lot(chat, mid, user, lot_id)


def act_lot_refresh(chat: Any, mid: Any, user: Any, lot_id: str) -> None:
    lot = S.q1("SELECT * FROM lots WHERE lot_id=?", (int(lot_id),))
    if not lot:
        menu_lots(chat, mid, user, 0)
        return
    try:
        name = lot_title_from_funpay(lot["lot_id"])
    except Exception:
        say(chat, f"❌ Не удалось прочитать название лота {lot['lot_id']} с FunPay. Попробуйте ещё раз.")
        return
    if name == lot["name"]:
        say(chat, "ℹ️ Название не изменилось.")
    elif name_taken(name, lot["lot_id"]):
        say(chat, esc(DUP_HELP).replace("добавьте лот снова", "нажмите «🔄 Обновить название с FunPay» ещё раз")
            + f"\n\nНовое название на FunPay: «{esc(name)}» — обновление не выполнено.")
    else:
        S.ex("UPDATE lots SET name=? WHERE lot_id=?", (name, lot["lot_id"]))
        say(chat, f"✅ Название обновлено.\nБыло: {esc(lot['name'])}\nСтало: {esc(name)}")
    menu_lot(chat, mid, user, lot_id)


def act_lot_delete(chat: Any, mid: Any, user: Any, lot_id: str) -> None:
    S.ex("DELETE FROM lots WHERE lot_id=?", (int(lot_id),))
    say(chat, f"🗑️ Лот {esc(lot_id)} удалён из плагина. На FunPay он остался как был — не забудьте "
        "деактивировать его вручную, иначе его будут покупать, а выдачи не будет.")
    menu_lots(chat, mid, user, 0)


# --- аренды ---------------------------------------------------------------
def menu_rentals(chat: Any, mid: Any, user: Any, page: Any = 0) -> None:
    rows_all = S.q(f"SELECT * FROM rentals WHERE status IN {LIVE} ORDER BY end_time, created_at, rowid")
    pages = max(1, math.ceil(len(rows_all) / 10))
    page = min(max(0, int(page)), pages - 1)
    rows = rows_all[page * 10:(page + 1) * 10]
    if not rows_all:
        show(chat, "🕹 <b>Управление арендой</b>\n\n📭 Активных аренд нет.", kb_rows([B("⬅️ Назад в меню", "main")]), mid)
        return
    lines = []
    for index, rental in enumerate(rows, page * 10 + 1):
        left = "⏳ завершается…" if rental["status"] == "ending" else "⏰ " + fmt_left(rental["end_time"] - now())
        lines.append(f"{index}. #{esc(rental['order_id'])} · 🖥️ <code>{esc(rental['server_id'])}</code> · {left}")
    buttons = [B(str(i), "rent", r["rental_id"]) for i, r in enumerate(rows, page * 10 + 1)]
    show(chat, f"🕹 <b>Управление арендой</b>\n\nАктивных аренд: {len(rows_all)}\n\n" + "\n".join(lines),
         kb_rows(*chunks(buttons, 5), page_nav("rents", page, pages), [B("⬅️ Назад в меню", "main")]), mid)


def menu_rental(chat: Any, mid: Any, user: Any, rental_id: str) -> None:
    rental = S.q1("SELECT * FROM rentals WHERE rental_id=?", (rental_id,))
    if not rental or rental["status"] not in ("active", "ending"):
        say(chat, f"ℹ️ Аренда {('#' + esc(rental['order_id'])) if rental else ''} уже не активна.")
        menu_rentals(chat, mid, user, 0)
        return
    limit = friend_limit_for(rental)
    used = friends_used(rental_id)
    paid = int(rental["paid_slots"] or 0)
    friends = f"{used}/{'∞' if not limit else limit + paid}" + (f" (+{paid} оплачено)" if paid else "")
    text = (f"🕹 <b>Аренда #{esc(rental['order_id'])}</b>\n\n"
            f"📦 Лот: {esc(rental_title(rental))}\n"
            f"🖥️ Сервер: <code>{esc(rental['server_id'])}</code>\n"
            f"👤 Покупатель FunPay: {esc(rental['buyer_name'] or '—')} (ID {esc(rental['buyer_id'])})\n"
            f"⏰ Осталось: {fmt_left(rental['end_time'] - now())} из {fmt_num(rental['hours'])} ч.\n"
            f"👥 Друзья: {friends}\n"
            f"🔗 Ссылка: {esc(rental['link'] or '—')}")
    if rental["status"] == "ending":
        text += ("\n\n⏳ <b>Завершается</b>: Roblox пока не подтвердил выключение сервера, бот повторяет попытки.\n"
                 f"Ошибка: {esc(rental['last_error'] or '—')}")
        kb = kb_rows([B("🔁 Повторить сейчас", "rr", rental_id)], [B("⬅️ Назад", "rents", 0)])
    else:
        kb = kb_rows([B("✅ Завершить", "re", rental_id), B("🔄 Продлить", "rx", rental_id)],
                     [B("🤫 Тихо завершить", "rq", rental_id), B("❌ Отменить", "rc", rental_id)],
                     [B("⬅️ Назад", "rents", 0)])
    show(chat, text, kb, mid)


def _end_from_panel(chat: Any, mid: Any, user: Any, rental_id: str, reason: str) -> None:
    rental = get_active(rental_id)
    if not rental:
        old = S.q1("SELECT order_id FROM rentals WHERE rental_id=?", (rental_id,))
        say(chat, f"ℹ️ Аренда #{esc(old['order_id'] if old else '?')} уже не активна.")
        menu_rentals(chat, mid, user, 0)
        return
    order = rental["order_id"]

    def job() -> None:
        ok, error = end_rental(rental_id, reason)
        if ok:
            message = {"manual": f"✅ Аренда #{esc(order)} завершена.",
                       "quiet": f"✅ Аренда #{esc(order)} завершена 🤫 (тихо, покупатель не уведомлён).",
                       "cancelled": f"❌ Аренда #{esc(order)} отменена."}[reason]
        elif error == "not_active":
            message = f"ℹ️ Аренда #{esc(order)} уже не активна."
        else:
            message = (f"⚠️ Аренда #{esc(order)}: Roblox не подтвердил выключение сервера ({esc(error)}). "
                       "Бот повторит попытку автоматически.")
        say(chat, message)
    if not run_once("rental:" + rental_id, job):
        say(chat, "⏳ С этой арендой уже выполняется операция — подождите.")
        return
    say(chat, f"⏳ Завершаю аренду #{esc(order)} — это займёт около 10 секунд, не нажимайте повторно.")


def act_rent_end(chat, mid, user, rid):
    _end_from_panel(chat, mid, user, rid, "manual")


def act_rent_quiet(chat, mid, user, rid):
    _end_from_panel(chat, mid, user, rid, "quiet")


def act_rent_cancel(chat, mid, user, rid):
    _end_from_panel(chat, mid, user, rid, "cancelled")


def act_rent_retry(chat: Any, mid: Any, user: Any, rental_id: str) -> None:
    S.ex("UPDATE rentals SET next_attempt=0 WHERE rental_id=? AND status='ending'", (rental_id,))
    say(chat, "🔁 Повторная попытка поставлена в очередь.")
    menu_rental(chat, mid, user, rental_id)


def act_rent_extend(chat: Any, mid: Any, user: Any, rental_id: str) -> None:
    if not get_active(rental_id):
        menu_rental(chat, mid, user, rental_id)
        return
    ask(chat, user, "🔄 На сколько часов продлить аренду? (положительное число, например <code>2</code> или <code>1.5</code>)",
        "rx", (rental_id,), back=f"rent:{rental_id}")


def in_rent_extend(message: Any, text: str, data: Dict[str, Any]) -> None:
    try:
        hours = parse_number(text)
    except ValueError:
        hours = 0
    if hours <= 0:
        raise ValueError("Введите положительное число часов.")
    rental = extend_rental(data["args"][0], hours)
    finish(message, f"✅ Аренда #{esc(rental['order_id'])} продлена на {fmt_num(hours)} ч. Покупатель уведомлён.",
           f"rent:{data['args'][0]}")


# --- бонусы ---------------------------------------------------------------
def menu_bonuses(chat: Any, mid: Any, user: Any) -> None:
    rules = S.q("SELECT * FROM bonus_rules ORDER BY id")
    text = (f"🎁 <b>Бонусы за объём: {'ВКЛ' if S.flag('bonus_active') else 'ВЫКЛ'}</b>\n\n"
            + ("\n".join(f"{r['buy_hours']:.1f}ч -> +{r['get_hours']:.1f}ч" for r in rules) if rules else "Правил пока нет.")
            + "\n\n<i>Применяется первое подходящее правило по порядку списка; бонус не умножается. "
              "Добавляйте правила от большего порога к меньшему.</i>")
    show(chat, text, kb_rows([B("🔄 Вкл/Выкл", "bon_t"), B("➕ Правило", "bon_a")],
                             [B("✏️ Ред. правила", "bon_el"), B("🗑️ Уд. правило", "bon_dl")], [B("⬅️ Назад", "main")]), mid)


def act_bonus_toggle(chat, mid, user):
    toggle("bonus_active")
    menu_bonuses(chat, mid, user)


def act_bonus_add(chat, mid, user):
    ask(chat, user, "➕ Отправьте правило в формате <code>купить:получить</code>, например <code>5:1</code> "
        "(купи 5 часов — получи 1 в подарок). Дробные числа — через точку.", "bon_a", back="bon")


def menu_bonus_pick(chat: Any, mid: Any, user: Any, mode: str) -> None:
    rules = S.q("SELECT * FROM bonus_rules ORDER BY id")
    action = "bon_e" if mode == "edit" else "bon_d"
    buttons = [[B(f"{'✏️' if mode == 'edit' else '🗑️'} {r['buy_hours']:.1f}ч -> +{r['get_hours']:.1f}ч", action, r["id"])]
               for r in rules[:90]]
    text = "✏️ Выберите правило для изменения:" if mode == "edit" else "🗑️ Нажмите на правило, чтобы удалить его:"
    show(chat, text if rules else "Правил пока нет.", kb_rows(*buttons, [B("⬅️ Назад", "bon")]), mid)


def act_bonus_edit(chat, mid, user, rule_id):
    ask(chat, user, "✏️ Отправьте новое значение в формате <code>купить:получить</code>.", "bon_e", (rule_id,), back="bon")


def act_bonus_delete(chat, mid, user, rule_id):
    S.ex("DELETE FROM bonus_rules WHERE id=?", (int(rule_id),))
    menu_bonus_pick(chat, mid, user, "delete")


def in_bonus(message: Any, text: str, data: Dict[str, Any]) -> None:
    if ":" not in text:
        raise ValueError("Неверный формат.")
    try:
        buy, get = (parse_number(x) for x in text.split(":", 1))
    except ValueError:
        raise ValueError("Ошибка. Используйте числа.") from None
    if buy <= 0 or get <= 0:
        raise ValueError("Ошибка. Используйте числа больше 0.")
    if data["action"] == "bon_a":
        S.ex("INSERT INTO bonus_rules(buy_hours,get_hours) VALUES(?,?)", (buy, get))
    else:
        S.ex("UPDATE bonus_rules SET buy_hours=?,get_hours=? WHERE id=?", (buy, get, int(data["args"][0])))
    finish(message, f"✅ Правило: {buy:.1f}ч -> +{get:.1f}ч", "bon")


# --- статистика и история -------------------------------------------------
def menu_stats(chat: Any, mid: Any, user: Any) -> None:
    rentals = S.val("SELECT COUNT(*) FROM rentals")
    active = S.val("SELECT COUNT(*) FROM rentals WHERE status='active'")
    paid = S.val("SELECT COUNT(*) FROM friend_payments WHERE state='applied'")
    text = (f"📊 <b>Статистика</b>\n\n💰 Total Earned: <b>{fmt_money(earned_total())} {currency_symbol()}</b>\n\n"
            f"Аренд всего: {rentals} (активных: {active})\nОплат доплаты за друзей: {paid}")
    show(chat, text, kb_rows([B("⬅️ Назад", "main")]), mid)


def menu_history(chat: Any, mid: Any, user: Any) -> None:
    rows = S.q("SELECT * FROM rentals ORDER BY created_at DESC, rowid DESC LIMIT 5")
    lines = [f"• #{esc(r['order_id'])} · {fmt_num(r['hours'])} ч · {fmt_money(r['price'])} {currency_symbol()} · "
             f"{fmt_dt(r['created_at'], True)}" for r in rows]
    show(chat, "📜 <b>История заказов</b> (5 последних)\n\n" + ("\n".join(lines) if lines else "Заказов пока нет."),
         kb_rows([B("⬅️ Назад", "main")]), mid)


# --- отзывы ---------------------------------------------------------------
def menu_reviews(chat: Any, mid: Any, user: Any) -> None:
    text = (f"⭐ <b>Бонус за отзыв</b>\n\nСтатус: {on_off('review_bonus_active')}\n"
            f"Бонус: {esc(S.get('review_bonus_hours'))} ч.\n"
            f"Сообщение: «{esc(short(get_template('review_bonus'), 80))}»\n\n"
            "<i>Покупатель оставляет отзыв и пишет !отзыв — бот добавит часы к активной аренде этого заказа. "
            "Бонус по заказу выдаётся один раз.</i>")
    show(chat, text, kb_rows([B(f"🔄 Переключить ({'Включено' if S.flag('review_bonus_active') else 'Выключено'})", "rev_t")],
                             [B("⏰ Изменить время бонуса", "rev_h")],
                             [B("📝 Изменить сообщение", "te", "review_bonus", "rev")], [B("⬅️ Назад", "main")]), mid)


def act_review_toggle(chat, mid, user):
    toggle("review_bonus_active")
    menu_reviews(chat, mid, user)


def act_review_hours(chat, mid, user):
    ask(chat, user, "⏰ Введите количество бонусных часов за отзыв (например, <code>1</code> или <code>0.5</code>).",
        "rev_h", back="rev")


def in_review_hours(message: Any, text: str, data: Dict[str, Any]) -> None:
    try:
        hours = parse_number(text)
    except ValueError:
        hours = 0
    if hours <= 0:
        raise ValueError("Введите положительное число.")
    S.set("review_bonus_hours", fmt_num(hours))
    finish(message, f"✅ Бонус за отзыв: {fmt_num(hours)} ч.", "rev")


# --- шаблоны --------------------------------------------------------------
def act_template_edit(chat: Any, mid: Any, user: Any, key: str, back: str = "tpl") -> None:
    info = TEMPLATES[key]
    current = get_template(key)
    shown = current if len(current) <= 3000 else current[:3000] + "…"
    variables = "\n".join(f"<code>{{{name}}}</code> — {esc(desc)}" for name, desc in info["vars"].items())
    ask(chat, user, f"📝 <b>{esc(info['title'])}</b>\n\nТекущий текст:\n<code>{esc(shown)}</code>\n\n"
        f"Доступные переменные:\n{variables}\n\nОтправьте новый текст одним сообщением. HTML не поддерживается, "
        "фигурные скобки используйте только для переменных.", "tpl", (key, back), back=back)


def in_template(message: Any, text: str, data: Dict[str, Any]) -> None:
    key, back = data["args"][0], data["args"][1]
    raw = (message.text or "").strip()
    if not raw:
        raise ValueError("Текст не может быть пустым.")
    S.set("tpl_" + key, raw)
    reply = "✅ Сообщение сохранено."
    problems = template_problems(key, raw)
    if problems:
        reply += ("\n⚠️ В тексте есть неизвестные переменные или лишние скобки: " + esc(", ".join(problems))
                  + ". Пока это не исправлено, покупатель будет получать упрощённое стандартное сообщение.")
    finish(message, reply, back)


def reset_templates(keys: Tuple[str, ...]) -> None:
    for key in keys:
        S.ex("DELETE FROM settings WHERE key=?", ("tpl_" + key,))


def menu_templates(chat: Any, mid: Any, user: Any) -> None:
    text = ("📨 <b>Шаблоны сообщений</b>\n\nСообщения, которые бот автоматически отправляет покупателю.\n\n"
            f"📝 После выдачи сервера: «{esc(short(get_template('post_payment'), 70))}»\n"
            f"📝 Об истечении аренды: «{esc(short(get_template('post_expire'), 70))}»")
    show(chat, text, kb_rows([B("📝 Сообщение после выдачи сервера", "te", "post_payment", "tpl")],
                             [B("📝 Сообщение об истечении аренды", "te", "post_expire", "tpl")],
                             [B("🔄 Сбросить шаблоны к стандартным", "tpl_reset")], [B("⬅️ Назад", "main")]), mid)


def act_templates_reset(chat, mid, user):
    reset_templates(MAIN_TEMPLATES)
    say(chat, "✅ Шаблоны сброшены к стандартным.")
    menu_templates(chat, mid, user)


# --- друзья ---------------------------------------------------------------
def default_limit_text() -> str:
    value = S.get("friends_default_limit") or "0"
    return "без ограничений" if value in ("0", "") else value


def menu_friends(chat: Any, mid: Any, user: Any) -> None:
    outdated = "!друзья" not in get_template("friend_invite")
    paid_label = f"🟢 {fmt_num(S.get('paid_price'))} {currency_symbol()}" if S.flag("paid_active") else "🔴 выкл"
    text = ("🤝 <b>Заявки в друзья</b>\n\n"
            "Пользователи Roblox младше 13 лет не могут зайти на VIP-сервер по ссылке. После выдачи сервера бот присылает "
            "покупателю приглашение со ссылкой на профиль владельца. Покупатель отправляет заявку в друзья и пишет "
            "!друзья ник — бот принимает заявку и добавляет игрока на VIP-сервер. По окончании аренды добавленные "
            "игроки удаляются.\n\n"
            f"Статус: {on_off('friends_module_active')}\n"
            f"Удаление по окончании: {'Вкл' if S.flag('friends_auto_remove') else 'Выкл'}\n"
            f"Лимит по умолчанию: {default_limit_text()}\n\n"
            f"📝 Приглашение: «{esc(short(get_template('friend_invite'), 60))}»\n"
            f"📝 После добавления: «{esc(short(get_template('friend_added'), 60))}»\n"
            f"📝 Не найдено: «{esc(short(get_template('friend_not_found'), 60))}»\n"
            f"📝 Лимит: «{esc(short(get_template('friend_limit'), 60))}»")
    rows = []
    if outdated:
        text = ("⚠️ <b>В приглашении нет команды !друзья</b> — похоже, остались старые тексты. Нажмите «Сбросить шаблоны».\n\n"
                + text)
        rows.append([B("🆕 Сбросить шаблоны (применить обновление)", "fr_reset")])
    rows += [
        [B(f"🔄 Переключить ({'Включено' if S.flag('friends_module_active') else 'Выключено'})", "fr_t")],
        [B(f"🔄 Удаление по окончании ({'Вкл' if S.flag('friends_auto_remove') else 'Выкл'})", "fr_r")],
        [B(f"🔢 Лимит по умолчанию ({default_limit_text()})", "fr_l")],
        [B(f"💳 Платное добавление ({paid_label})", "pd")],
        [B("📝 Сообщение-приглашение", "te", "friend_invite", "fr")],
        [B("📝 Сообщение о добавлении", "te", "friend_added", "fr")],
        [B("📝 Сообщение 'не найдено'", "te", "friend_not_found", "fr")],
        [B("📝 Сообщение о лимите", "te", "friend_limit", "fr")],
    ]
    if not outdated:
        rows.append([B("🔄 Сбросить шаблоны к стандартным", "fr_reset")])
    rows.append([B("⬅️ Назад", "main")])
    show(chat, text, kb_rows(*rows), mid)


def act_friends_toggle(chat, mid, user):
    toggle("friends_module_active")
    menu_friends(chat, mid, user)


def act_friends_autoremove(chat, mid, user):
    toggle("friends_auto_remove")
    menu_friends(chat, mid, user)


def act_friends_reset(chat, mid, user):
    reset_templates(FRIEND_TEMPLATES)
    say(chat, "✅ Шаблоны заявок в друзья сброшены к стандартным.")
    menu_friends(chat, mid, user)


def act_friends_limit(chat, mid, user):
    ask(chat, user, "🔢 Сколько игроков можно бесплатно добавить командой !друзья за одну аренду? "
        "Введите целое число от 1 до 9999. <code>0</code> или <code>-</code> — без ограничений.", "fr_l", back="fr")


def in_friends_limit(message: Any, text: str, data: Dict[str, Any]) -> None:
    if text in ("-", "0"):
        S.set("friends_default_limit", "0")
        finish(message, "✅ Лимит по умолчанию снят (без ограничений).", "fr")
        return
    if not re.fullmatch(r"\d{1,4}", text) or int(text) < 1:
        raise ValueError("Введите целое число от 1 до 9999, 0 или - — без ограничений.")
    S.set("friends_default_limit", str(int(text)))
    finish(message, f"✅ Лимит по умолчанию: {int(text)}.", "fr")


# --- платное добавление ---------------------------------------------------
PAID_SETTINGS = [
    ("paid_price", "💰 Цена за 1 добавление", "price"),
    ("paid_lifetime", "⏳ Время жизни лота (мин)", "minutes"),
    ("paid_node", "🗂 ID раздела FunPay (node)", "node"),
    ("paid_game", "🎮 Поле «Игра»", "text"),
    ("paid_game_name_ru", "🏷 Название игры (RU)", "text"),
    ("paid_game_name_en", "🏷 Название игры (EN)", "text"),
    ("paid_type", "📂 Поле «Тип»", "text"),
    ("paid_title_ru", "📝 Название лота (RU)", "title"),
    ("paid_title_en", "📝 Название лота (EN)", "title"),
    ("paid_desc_ru", "📄 Описание лота (RU)", "text"),
    ("paid_desc_en", "📄 Описание лота (EN)", "text"),
    ("paid_pay_msg_ru", "✉️ Сообщение после оплаты (RU)", "text"),
    ("paid_pay_msg_en", "✉️ Сообщение после оплаты (EN)", "text"),
]
PAID_KEYS = {key: (label, kind) for key, label, kind in PAID_SETTINGS}


def menu_paid(chat: Any, mid: Any, user: Any) -> None:
    active = S.val("SELECT COUNT(*) FROM friend_lots WHERE state='active'")
    text = ("💳 <b>Платное добавление в друзья</b>\n\n"
            "Когда бесплатный лимит друзей для аренды исчерпан, бот по команде !друзья ник создаёт на FunPay временный "
            "лот для доплаты, а после оплаты начисляет добавления и сразу добавляет друга. К названию и описанию лота "
            "добавляется тег #FRXXXXX — по нему бот узнаёт оплату.\n\n"
            f"Статус: {on_off('paid_active')}\n"
            f"Цена за 1 добавление: {fmt_num(S.get('paid_price'))} {currency_symbol()}\n"
            f"Время жизни лота: {esc(S.get('paid_lifetime'))} мин\n"
            f"Раздел FunPay: {esc(S.get('paid_node'))}\n"
            f"Поле «Игра»: {esc(S.get('paid_game'))} · «Тип»: {esc(S.get('paid_type'))}\n"
            f"Название RU: {esc(short(S.get('paid_title_ru'), 60))}\n"
            f"Название EN: {esc(short(S.get('paid_title_en'), 60))}\n"
            f"Активных лотов доплаты сейчас: {active}")
    if S.get("friends_default_limit") in ("0", ""):
        text += ("\n\n⚠️ Лимит друзей по умолчанию не задан (без ограничений) — платное добавление сработает только для "
                 "лотов, у которых в карточке указан свой лимит. Задайте его в 🤝 Заявки в друзья → 🔢 Лимит по умолчанию.")
    rows = [[B("🔄 Выключить" if S.flag("paid_active") else "🔄 Включить", "pd_t")]]
    settings = [B(label, "pd_s", key) for key, label, _ in PAID_SETTINGS]
    rows += [settings[0:1], settings[1:2], settings[2:3], settings[3:4], settings[4:6], settings[6:7], settings[7:9],
             settings[9:11], settings[11:13]]
    rows += [[B("💬 Лимит исчерпан (после добавления)", "te", "paid_limit_reached", "pd")],
             [B("💬 Лот создан", "te", "paid_lot_created", "pd"), B("💬 Лот уже есть", "te", "paid_lot_exists", "pd")],
             [B("💬 Оплата получена", "te", "paid_received", "pd")],
             [B("♻️ Сбросить тексты к стандартным", "pd_reset")],
             [B("🗑 Удалить все активные лоты доплаты", "pd_del")],
             [B("⬅️ Назад", "fr")]]
    show(chat, text, kb_rows(*rows), mid)


def act_paid_toggle(chat, mid, user):
    toggle("paid_active")
    menu_paid(chat, mid, user)


def act_paid_setting(chat: Any, mid: Any, user: Any, key: str) -> None:
    label, kind = PAID_KEYS[key]
    hints = {"price": "Введите цену числом больше 0 (в валюте аккаунта FunPay).",
             "minutes": "Введите число минут от 1 до 1440.",
             "node": "Введите ID раздела FunPay (число из ссылки вида https://funpay.com/lots/402/).",
             "title": "Введите название. Тег #FRXXXXX добавится автоматически; длиннее ~58 символов будет обрезано.",
             "text": "Введите текст."}
    current = S.get(key)
    ask(chat, user, f"{esc(label)}\n\nСейчас: <code>{esc(current[:1500])}</code>\n\n{hints[kind]}", "pd_s", (key,), back="pd")


def in_paid_setting(message: Any, text: str, data: Dict[str, Any]) -> None:
    key = data["args"][0]
    label, kind = PAID_KEYS[key]
    raw = (message.text or "").strip()
    reply = "✅ Сохранено."
    if kind == "price":
        try:
            value = parse_number(raw)
        except ValueError:
            value = 0
        if value <= 0:
            raise ValueError("Цена должна быть больше 0.")
        raw = fmt_num(value)
    elif kind == "minutes":
        if not re.fullmatch(r"\d{1,4}", raw) or not 1 <= int(raw) <= 1440:
            raise ValueError("Введите целое число от 1 до 1440.")
    elif kind == "node":
        if not re.fullmatch(r"\d{1,8}", raw):
            raise ValueError("ID раздела должен быть числом.")
    else:
        if not raw:
            raise ValueError("Текст не может быть пустым.")
        if kind == "title" and len(raw) > 58:
            reply += "\n⚠️ Название длиннее 58 символов — вместе с тегом оно будет обрезано до ~70 символов."
    S.set(key, raw)
    finish(message, reply, "pd")


def act_paid_reset(chat, mid, user):
    for key, value in PAID_TEXT_DEFAULTS.items():
        S.set(key, value)
    reset_templates(PAID_TEMPLATES)
    say(chat, "✅ Тексты платного добавления сброшены к стандартным.")
    menu_paid(chat, mid, user)


def act_paid_delete_all(chat: Any, mid: Any, user: Any) -> None:
    lots = S.q("SELECT * FROM friend_lots WHERE state='active'")

    def job() -> None:
        deleted = sum(1 for lot in lots if remove_friend_lot(lot))
        say(chat, f"🗑 Удалено лотов доплаты: {deleted} из {len(lots)}.")
    if not lots:
        say(chat, "ℹ️ Активных лотов доплаты нет.")
    elif not run_once("paid_delete_all", job):
        say(chat, "⏳ Удаление уже выполняется.")
    else:
        say(chat, f"⏳ Удаляю лоты доплаты: {len(lots)}…")


# ---------------------------------------------------------------------------
# Маршруты и обработчики Telegram
# ---------------------------------------------------------------------------
ROUTES = {
    "main": menu_main, "mode": act_mode, "lotmgmt": act_lot_management, "proxy": act_proxy,
    "acc": menu_accounts, "acc_add": act_account_add, "acc_del": act_account_delete,
    "srv": menu_servers, "srv_add": act_server_add, "srv_del": act_server_delete,
    "cats": menu_categories, "cat": menu_category, "cat_add": act_category_add, "cs_add": act_category_servers_add,
    "cs_del": act_category_server_delete, "cat_game": act_category_game, "cat_ren": act_category_rename,
    "cat_del": act_category_delete,
    "lots": menu_lots, "lot": menu_lot, "lot_add": act_lot_add,
    "lh": _lot_input("lh", "⏰ Введите количество часов аренды за 1 шт. (целое или дробное, точка или запятая)."),
    "ls": _lot_input("ls", "🖥️ Введите ID сервера, который нужно выдавать при покупке этого лота. "
                           "Отправьте <code>-</code>, чтобы снять привязку одного сервера."),
    "lpn": _lot_input("lpn", "💰 Введите обычную цену лота (число)."),
    "lpe": _lot_input("lpe", "🎉 Введите евент цену лота (число)."),
    "lf": _lot_input("lf", "👥 Сколько игроков покупатель может бесплатно добавить за одну аренду по этому лоту? "
                           "Число от 1 до 9999, <code>0</code> — без ограничений, <code>-</code> — по умолчанию."),
    "lc": menu_lot_category, "lcb": act_lot_bind_category, "lcu": act_lot_unbind_category,
    "lr": act_lot_refresh, "ld": act_lot_delete,
    "rents": menu_rentals, "rent": menu_rental, "re": act_rent_end, "rq": act_rent_quiet, "rc": act_rent_cancel,
    "rx": act_rent_extend, "rr": act_rent_retry,
    "bon": menu_bonuses, "bon_t": act_bonus_toggle, "bon_a": act_bonus_add,
    "bon_el": lambda c, m, u: menu_bonus_pick(c, m, u, "edit"), "bon_dl": lambda c, m, u: menu_bonus_pick(c, m, u, "delete"),
    "bon_e": act_bonus_edit, "bon_d": act_bonus_delete,
    "stats": menu_stats, "hist": menu_history,
    "rev": menu_reviews, "rev_t": act_review_toggle, "rev_h": act_review_hours,
    "tpl": menu_templates, "tpl_reset": act_templates_reset, "te": act_template_edit,
    "fr": menu_friends, "fr_t": act_friends_toggle, "fr_r": act_friends_autoremove, "fr_l": act_friends_limit,
    "fr_reset": act_friends_reset,
    "pd": menu_paid, "pd_t": act_paid_toggle, "pd_s": act_paid_setting, "pd_reset": act_paid_reset,
    "pd_del": act_paid_delete_all,
}

INPUTS = {
    "proxy": in_proxy, "acc_add": in_account_add, "srv_add": in_server_add, "cat_add": in_category_add,
    "cs_add": in_category_servers_add, "cat_game": in_category_game, "cat_ren": in_category_rename,
    "lot_add": in_lot_add, "lot_add2": in_lot_add2, "lh": in_lot_value, "ls": in_lot_value, "lpn": in_lot_value,
    "lpe": in_lot_value, "lf": in_lot_value, "rx": in_rent_extend, "bon_a": in_bonus, "bon_e": in_bonus,
    "rev_h": in_review_hours, "tpl": in_template, "fr_l": in_friends_limit, "pd_s": in_paid_setting,
}


def on_callback(call: Any) -> None:
    chat, mid, user = call.message.chat.id, call.message.message_id, call.from_user.id
    if not is_admin(user):
        bot().answer_callback_query(call.id, "Нет доступа", show_alert=True)
        return
    try:
        bot().answer_callback_query(call.id)
    except Exception:
        pass
    parts = call.data.split(":")[1:]
    try:
        if parts[0] == "x":  # отмена ввода → возврат в меню
            clear_input(chat, user, False)
            parts = parts[1:]
        else:
            clear_input(chat, user, False)
        handler = ROUTES.get(parts[0])
        if handler is None:
            raise ValueError("Неизвестное действие. Откройте /vip_config заново.")
        handler(chat, mid, user, *parts[1:])
    except ValueError as exc:
        say(chat, "❌ " + esc(exc))
    except Exception:
        LOG.exception("AutoVIP: ошибка действия в Telegram")
        say(chat, "❌ Не удалось выполнить действие. Подробности — в логе Cardinal.")


def on_input(message: Any) -> None:
    state = input_state(message)
    if not state or not is_admin(message.from_user.id):
        return
    data = state.get("data") or {}
    handler = INPUTS.get(data.get("action"))
    if handler is None:
        clear_input(message.chat.id, message.from_user.id, False)
        return
    try:
        handler(message, (message.text or "").strip(), data)
    except ValueError as exc:
        text = str(exc)
        bot().reply_to(message, esc(text) if text.startswith(("⚠️", "❌")) else "❌ " + esc(text), parse_mode="HTML")
    except Exception:
        LOG.exception("AutoVIP: ошибка обработки ввода")
        bot().reply_to(message, "❌ Не удалось выполнить действие. Подробности — в логе Cardinal.")


def on_panel_command(message: Any) -> None:
    if not is_admin(message.from_user.id):
        return
    clear_input(message.chat.id, message.from_user.id, False)
    menu_main(message.chat.id, None, message.from_user.id)


def on_plugin_settings(call: Any) -> None:
    if not is_admin(call.from_user.id):
        return
    try:
        bot().answer_callback_query(call.id)
    except Exception:
        pass
    menu_main(call.message.chat.id, call.message.message_id, call.from_user.id)


def init_telegram(cardinal: Any) -> None:
    tg = getattr(cardinal, "telegram", None)
    if not tg:
        return
    tg.msg_handler(on_panel_command, commands=["vip_config"])
    tg.cbq_handler(on_callback, lambda c: bool(c.data) and c.data.startswith(P + ":"))
    if CBT is not None and hasattr(CBT, "PLUGIN_SETTINGS"):
        tg.cbq_handler(on_plugin_settings,
                       lambda c: bool(c.data) and c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
    tg.msg_handler(on_input, func=lambda m: bool(getattr(m, "text", None)) and not m.text.startswith("/")
                   and input_state(m) is not None)
    try:
        cardinal.add_telegram_commands(UUID, [("vip_config", "панель Auto VIP Roblox", True)])
    except Exception:
        LOG.debug("AutoVIP: add_telegram_commands", exc_info=True)


# ---------------------------------------------------------------------------
# Привязка к Cardinal
# ---------------------------------------------------------------------------
def init_plugin(cardinal: Any) -> None:
    global S, CARDINAL
    CARDINAL = cardinal
    if S is None:
        S = Store(DB_PATH)
        report = migrate_old_database(S, OLD_DB_PATH)
        if report:
            LOG.info("AutoVIP: %s", report)
            S.set("migration_report", report)
    init_telegram(cardinal)
    LOG.info("Auto VIP Roblox v%s инициализирован", VERSION)


def start_plugin(cardinal: Any) -> None:
    if S is None:
        init_plugin(cardinal)
    STOP.clear()
    with GUARD:
        if any(t.is_alive() for t in THREADS):
            return
        THREADS[:] = [threading.Thread(target=worker_loop, name="AutoVIP-worker", daemon=True),
                      threading.Thread(target=order_loop, name="AutoVIP-orders", daemon=True)]
        for thread in THREADS:
            thread.start()
    interrupted = S.q("SELECT order_id FROM processed_orders WHERE kind='processing'")
    for row in interrupted:
        S.ex("UPDATE processed_orders SET kind='interrupted' WHERE order_id=?", (row["order_id"],))
        if not S.q1("SELECT 1 FROM rentals WHERE order_id=?", (row["order_id"],)):
            notify_admins(f"⚠️ Обработка заказа #{esc(row['order_id'])} была прервана перезапуском. "
                          "Проверьте заказ и при необходимости выдайте сервер вручную.")
    report = S.get("migration_report", "")
    if report:
        notify_admins("🎮 Auto VIP Roblox v" + VERSION + "\n" + esc(report)
                      + "\nОткройте /vip_config и проверьте аккаунты, серверы и лоты.")
        S.set("migration_report", "")
    request_lot_sync()


def delete_plugin(cardinal: Any, *args: Any) -> None:
    STOP.set()


BIND_TO_PRE_INIT = [init_plugin]
BIND_TO_POST_START = [start_plugin]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [on_last_chat_message_changed]
BIND_TO_DELETE = delete_plugin
