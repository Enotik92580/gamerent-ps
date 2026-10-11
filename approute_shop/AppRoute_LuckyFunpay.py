# -*- coding: utf-8 -*-
"""AppRoute Shop (Auto) для FunPay Cardinal.

Автопродажа готовых цифровых товаров магазина AppRoute (approute.ru / approute.io):
подарочные карты и ваучеры, коды пополнения, ключи активации, туристические eSIM.
После оплаты заказа на FunPay бот сам покупает номинал у поставщика и присылает
покупателю коды или данные eSIM. Прямое пополнение (логин/UID покупателя) сюда
не входит: для него есть отдельный плагин «AppRoute Direct Top-Up».

Автор: @LuckyFunPay_Support · канал https://t.me/LuckyFunPay

УСТАНОВКА И НАСТРОЙКА (подробная инструкция — кнопка «📖 Инструкция» в панели)
 0. Положите AppRoute_LuckyFunpay.py в папку plugins (или загрузите через Cardinal).
    /menu → Система и конфиги → Плагины → AppRoute Shop (Auto) → «Включить».
    Обязательно перезапустите бота: /restart. После запуска отправьте /approute_shop.
 1. Зарегистрируйтесь на approute.ru/auth и пополните баланс кабинета (USDT).
 2. approute.ru/api-access?tab=docs → «Создать постоянный ключ»: права на каталог,
    создание заказов и баланс; в поле IP — IP прокси бота (или сервера).
 3. /approute_shop → 🔑 Доступ к API: ключ, регион (RU/IO), прокси → «🧪 Проверить ключ».
 4. 🗂 Каталог → «🔄 Обновить каталог сейчас».
 5. 💲 Ценообразование: курс, наценка, границы цен.
 6. Создайте лот на FunPay → 📦 Лоты → ➕ Добавить лот → выберите номинал.
 7. Включите «🔄 Автообновление» цен, при желании измените «✉️ Шаблон выдачи».
 8. В главном меню нажмите «Статус», чтобы индикатор стал 🟢.

Данные: storage/plugins/approute_shop_lucky/shop.sqlite3. Не удаляйте эту папку при
незавершённых заказах: там журнал покупок, защищающий от повторных списаний.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import logging
import math
import os
import queue
import re
import sqlite3
import string
import threading
import time
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

NAME = "AppRoute Shop (Auto)"
VERSION = "1.0.2"
DESCRIPTION = "Автопродажа кодов, гифт-карт и eSIM из магазина AppRoute • /approute_shop"
CREDITS = "@LuckyFunPay_Support"
UUID = "d1eb44d6-c72b-48ed-8ee3-c5ca58ea9413"
SETTINGS_PAGE = True
DEVELOPER_URL = "https://t.me/LuckyFunPay"
SUPPORT_URL = "https://t.me/LuckyFunPay_Support"
PREFIX = "arshop:"
COMMAND = "approute_shop"
CANCEL_COMMAND = "shop_cancel"
# Own folder: storage/plugins/approute_shop may hold another plugin's database with a different layout.
STORAGE = Path("storage/plugins/approute_shop_lucky")
LOG = logging.getLogger("FPC.AppRouteShop")

# AppRoute order type for prepaid goods (vouchers, keys, eSIM). Direct top-up uses "dtu".
SHOP_ORDERS_TYPE = "shop"
DIRECT_TOPUP = "direct_topup"

DEFAULT_TEMPLATE = ("✅ Спасибо за покупку!\n\n🎁 {lot_name}\nКоличество: {quantity}\n\n{codes}\n\n"
                    "Если что-то не так — напишите продавцу. Спасибо за покупку! ❤️")
DEFAULTS = {
    "enabled": False, "notifications": True, "auto_refund": True,
    "api_key": "", "region": "IO", "proxy_mode": "bot", "proxy": "",
    "rate": "100", "markup": "20", "min_price": "1", "max_price": "1000000", "interval": 30,
    "max_quantity": 50, "auto_price": False, "sync_stock": True, "auto_enable": True,
    "price_notify": False, "low_balance": "30", "sanity_enabled": True, "sanity_ratio": "3",
    "template": DEFAULT_TEMPLATE, "notify_chats": [],
    "catalog_interval": 360, "catalog_at": 0, "catalog_retry_at": 0, "catalog_requested": None,
    "items_interval": 30, "items_at": 0, "sync_at": 0,
    "balance_at": 0, "balance": None, "low_alert": False,
}
POLL_TIMEOUT = 600  # 10 minutes of waiting for the provider, as the guide promises
HISTORY_LIMIT = 2000
MAX_CONDITIONS = 8
MAX_RETRIES = 6
ACTIVE_STATES = {"queued", "ready", "purchasing", "pending", "reveal", "refund_pending"}
SUCCESS_STATES = {"done", "partial"}
STATE_NAMES = {"queued": "В очереди", "ready": "Готов к покупке", "purchasing": "Отправка покупки",
               "pending": "Ожидание поставщика", "reveal": "Получение кодов", "done": "Выдан",
               "partial": "Выдан частично", "review": "Нужен продавец", "refund_pending": "Возврат",
               "refunded": "Возвращён", "closed": "Закрыт", "ignored": "Не обслуживается"}


class ShopError(Exception):
    """Safe, user-facing error; never contains credentials or voucher payloads."""


class APIError(ShopError):
    def __init__(self, status=0, code=None, errors=None):
        self.status, self.code, self.errors = status, code, errors or []
        super().__init__(f"AppRoute: HTTP {status or '—'}, код {code if code is not None else '—'}")


class RateLimitError(APIError):
    """A scheduled pause, not a failed purchase or a reason for a refund."""
    def __init__(self, retry_at, code=8, now=None):
        super().__init__(429, code)
        self.retry_at = retry_at
        seconds = max(1, math.ceil(retry_at - (time.time() if now is None else now)))
        self.args = (f"AppRoute ограничил частоту запросов (HTTP 429). Пауза ещё {seconds} сек. "
                     f"Плагин продолжит автоматически.",)


class PacerBlocked(RateLimitError):
    """A saved provider cooldown stopped the call before any HTTP request was sent."""


class RequestDeferred(ShopError):
    """Local scheduling, before any HTTP request; keep the worker available."""
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__("Ожидание интервала запросов AppRoute. Продолжение запланировано автоматически.")


class TransientError(ShopError):
    """Network/FunPay/5xx failure: retried with backoff instead of manual review."""


def is_transient(exc):
    if isinstance(exc, TransientError):
        return True
    return isinstance(exc, APIError) and not isinstance(exc, RateLimitError) and (not exc.status or exc.status >= 500)


def code_of(exc):
    return str(getattr(exc, "code", "") if getattr(exc, "code", None) is not None else "")


def retry_after_seconds(value, now, fallback=65):
    """RFC Retry-After supports delta-seconds and an HTTP date."""
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = parsedate_to_datetime(str(value)).timestamp() - now
        except (ValueError, TypeError, OverflowError):
            return fallback
    return max(1, math.ceil(seconds)) if math.isfinite(seconds) else fallback


class RequestPacer:
    """Shared request intervals per key; a provider cooldown also survives restart.

    Catalogue and code-reveal waits never block the worker: they are rescheduled.
    """
    GAPS = {"catalog": 31.0, "orders": 3.2, "reveal": 61.0, "read": 1.1,
            "purchase": 1.1, "balance": 3.2, "unfiltered": 31.0}

    def __init__(self, store, stop_event=None, clock=None, wait=None):
        self.store, self.stop_event = store, stop_event
        self.clock = clock or time.time
        self.wait = wait or (stop_event.wait if stop_event is not None else time.sleep)
        self.lock = threading.RLock()

    @staticmethod
    def identity(settings):
        return hashlib.sha256((settings["region"] + ":" + settings["api_key"]).encode()).hexdigest()

    @staticmethod
    def bucket(method, path, params):
        if method.upper() == "GET" and path == "/services":
            return "catalog"
        if method.upper() == "GET" and path == "/orders":
            if str((params or {}).get("unhide")).lower() == "true":
                return "reveal"
            return "orders" if (params or {}).get("referenceId") else "unfiltered"
        if method.upper() == "GET" and path == "/accounts":
            return "balance"
        return "purchase" if method.upper() == "POST" else "read"

    def configure_limits(self, settings, data):
        keys = {"GET /services": "catalog", "GET /orders (filtered)": "orders",
                "GET /orders (unhide completed)": "reveal", "GET /orders (unhide admission)": "reveal",
                "GET /orders (unhide pending)": "reveal", "GET /accounts": "balance",
                "GET /orders": "unfiltered", "GET /services/{id}": "read",
                "GET /services/{id}/items/{item_id}": "read"}
        gaps = dict(self.GAPS)
        for entry in data.get("endpoints", []):
            bucket = keys.get(entry.get("key"))
            try:
                limit, window = float(entry["limit"]), float(entry["windowSeconds"])
                gap = window / limit + 1
                if bucket and limit > 0 and window > 0 and math.isfinite(gap):
                    gaps[bucket] = max(gaps[bucket], gap)
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
        self.store.patch("rate", self.identity(settings), limit_gaps=gaps)

    def before(self, settings, method, path, params=None):
        key, bucket = self.identity(settings), self.bucket(method, path, params)
        with self.lock:
            while True:
                if self.stop_event is not None and self.stop_event.is_set():
                    raise ShopError("Плагин остановлен. Прогресс сохранён.")
                state = self.store.get("rate", key, {})
                now = self.clock()
                if state.get("blocked_until", 0) > now:
                    raise PacerBlocked(state["blocked_until"], now=now)
                gap = state.get("limit_gaps", {}).get(bucket, self.GAPS[bucket])
                deadline = max(state.get("next_any", 0), state.get("next_" + bucket, 0),
                               state.get("last_" + bucket, -gap) + gap)
                if deadline <= now:
                    self.store.patch("rate", key, next_any=now + 1.1,
                                     **{"next_" + bucket: now + gap, "last_" + bucket: now})
                    return
                if bucket in {"catalog", "reveal", "unfiltered"} and deadline - now > 1.1:
                    raise RequestDeferred(deadline)
                self.wait(min(1.0, deadline - now))

    def limited(self, settings, retry_after=None):
        key, now = self.identity(settings), self.clock()
        with self.lock:
            state = self.store.get("rate", key, {})
            strikes = state.get("strikes", 0) + 1 if now - state.get("limited_at", 0) < 3600 else 1
            delay = retry_after_seconds(retry_after, now, min(900, 65 * 2 ** min(strikes - 1, 4)))
            deadline = max(state.get("blocked_until", 0), now + delay + 1)
            self.store.patch("rate", key, blocked_until=deadline, strikes=strikes, limited_at=now)
            return deadline


def dec(value):
    try:
        result = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError, TypeError):
        raise ShopError("Введите корректное число.") from None
    if not result.is_finite():
        raise ShopError("Число должно быть конечным.")
    return result


def integer(value):
    result = dec(value)
    if result != result.to_integral_value():
        raise ShopError("Требуется целое число.")
    return int(result)


def money(value):
    return str(dec(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def normalized(value):
    # FunPay/OS render regional-indicator pairs either as flags or country codes.
    value = re.sub(r"[\U0001F1E6-\U0001F1FF]{2}",
                   lambda m: " " + "".join(chr(ord(c) - 0x1F1E6 + ord("A")) for c in m[0]) + " ",
                   str(value or ""))
    return " ".join(re.findall(r"[\w]+", value.casefold().replace("ё", "е")))


def has_phrase(text, phrase):
    """Whole-word match: numbers compare exactly, so 100 never matches 1000."""
    phrase = normalized(phrase)
    return bool(phrase) and f" {phrase} " in f" {normalized(text)} "


def enum_name(value):
    return str(getattr(value, "name", value)).upper().split(".")[-1]


def esc(value):
    return html.escape(str(value))


def ago(timestamp):
    if not timestamp:
        return "никогда"
    seconds = max(0, int(time.time() - timestamp))
    if seconds < 60:
        return "только что"
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    if seconds < 86400:
        return f"{seconds // 3600} ч {seconds % 3600 // 60} мин назад"
    return f"{seconds // 86400} дн назад"


def proxy_url(value):
    value = value.strip()
    if "://" not in value:
        value = "http://" + value
    try:
        p = urlparse(value)
        valid = p.scheme in {"http", "https", "socks5", "socks5h"} and p.hostname and p.port
        if not valid or p.path not in {"", "/"} or p.query or p.fragment:
            raise ValueError
    except ValueError:
        raise ShopError("Прокси: ip:порт или логин:пароль@ip:порт.") from None
    return value


def masked_proxy(value):
    if not value:
        return "не задан"
    p = urlparse(value)
    return f"{p.scheme}://{'***@' if p.username else ''}{p.hostname}:{p.port}"


def lot_id_from_text(value):
    value = value.strip()
    if value.isdigit() and 0 < int(value) < 10**15:
        return str(int(value))
    p = urlparse(value)
    if p.scheme in {"https", "http"} and p.hostname in {"funpay.com", "www.funpay.com"} and \
            p.path.rstrip("/") in {"/lots/offer", "/lots/offerEdit"}:
        for key in ("id", "offer"):
            candidate = parse_qs(p.query).get(key, [""])[0]
            if candidate.isdigit():
                return lot_id_from_text(candidate)
    raise ShopError("Нужен ID лота или ссылка на конкретный лот: https://funpay.com/lots/offer?id=12345678. "
                    "Ссылки на категорию или профиль не подходят.")


def chat_for(seller_id, buyer_id):
    """FunPay private chat name; numeric node ids are mapped to this form."""
    try:
        pair = sorted((int(seller_id), int(buyer_id)))
    except (TypeError, ValueError):
        return ""
    return f"users-{pair[0]}-{pair[1]}"


def order_attr_values(order):
    """Order parameters across FunPayAPI versions: fields, lot_params, buyer_params."""
    result = []
    for name in ("fields", "lot_params", "buyer_params"):
        raw = getattr(order, name, None)
        if not raw:
            continue
        pairs = raw.items() if isinstance(raw, dict) else raw
        for entry in pairs:
            if isinstance(entry, (tuple, list)) and len(entry) == 2:
                key, value = entry
            else:
                continue
            value = getattr(value, "value", value)
            result.append((str(key), value))
    return result


def calculate_price(cost, settings):
    price = dec(cost) * dec(settings["rate"]) * (1 + dec(settings["markup"]) / 100)
    return money(max(dec(settings["min_price"]), min(dec(settings["max_price"]), price)))


def unit_cost(item):
    """Supplier price of one unit in USD/USDT; anything else is not trusted for spending."""
    if str(item.get("currency", "USD") or "USD").upper() not in {"USD", "USDT"} or item.get("price") is None:
        raise ShopError("Поставщик не вернул цену в USD/USDT.")
    price = dec(item["price"])
    if price <= 0:
        raise ShopError("Поставщик вернул нулевую цену.")
    return price


def stock_count(item):
    for key in ("inStock", "stock", "quantity", "available_count", "availableCount"):
        value = item.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            return max(0, integer(value))
        except ShopError:
            return None
    return None


def on_demand(item):
    """Codes generated per order: no real warehouse, so zero stock is not «out of stock»."""
    if item.get("unlimited") or item.get("onDemand") or item.get("isUnlimited"):
        return True
    explicit = item.get("available", item.get("isAvailable"))
    return stock_count(item) in (None, 0) and explicit is True


def available(item, quantity=1):
    if item.get("available") is False or item.get("isAvailable") is False:
        return False
    if on_demand(item):
        return True
    stock = stock_count(item)
    return stock is None or stock >= max(1, quantity)


def lot_amount(item, settings):
    """Quantity shown on FunPay: supplier stock within 1…limit per order."""
    limit = int(settings["max_quantity"])
    stock = None if on_demand(item) else stock_count(item)
    return limit if stock is None else max(1, min(limit, stock))


class Store:
    """Atomic, durable journal; one transaction per mutation and no reset-on-error."""
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, data TEXT NOT NULL, state TEXT,"
                        " due REAL, chat TEXT, outbox INTEGER, PRIMARY KEY(kind,id))")
        # A table created by an older layout lacks the indexed columns: add and fill them.
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(records)")}
        if not {"kind", "id", "data"} <= columns:
            raise ShopError(f"Файл {path} занят базой другого плагина. Удалите или переименуйте его и перезапустите бота.")
        missing = [(n, t) for n, t in (("state", "TEXT"), ("due", "REAL"), ("chat", "TEXT"), ("outbox", "INTEGER"))
                   if n not in columns]
        for name, kind in missing:
            self.db.execute(f"ALTER TABLE records ADD COLUMN {name} {kind}")
        if missing:
            for kind, key, data in self.db.execute("SELECT kind, id, data FROM records").fetchall():
                try:
                    value = json.loads(data)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    self.db.execute("UPDATE records SET state=?, due=?, chat=?, outbox=? WHERE kind=? AND id=?",
                                    (*self.columns(kind, value), kind, key))
        self.db.execute("CREATE INDEX IF NOT EXISTS records_state ON records(kind, state)")
        self.db.execute("CREATE INDEX IF NOT EXISTS records_chat ON records(kind, chat)")
        self.db.execute("CREATE INDEX IF NOT EXISTS records_due ON records(kind, due)")
        self.db.commit()
        if os.name != "nt":
            try:
                os.chmod(path.parent, 0o700)
                os.chmod(path, 0o600)
            except OSError:
                pass

    @staticmethod
    def columns(kind, value):
        if kind == "order":
            return value.get("state"), value.get("next_at", 0), value.get("chat"), int(bool(value.get("outbox")))
        if kind == "seen":
            return None, value.get("at", 0), None, 0
        return None, None, None, 0

    def get(self, kind, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, str(key))).fetchone()
            return json.loads(row[0]) if row else copy.deepcopy(default)

    def put(self, kind, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO records (kind, id, data, state, due, chat, outbox) VALUES (?,?,?,?,?,?,?)",
                            (kind, str(key), json.dumps(value, ensure_ascii=False), *self.columns(kind, value)))

    def query(self, where, params=()):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM records WHERE kind='order' AND " + where, params).fetchall()]

    def due_orders(self, now, states):
        marks = ",".join("?" * len(states))
        return sorted(self.query(f"(outbox=1 OR state IN ({marks})) AND COALESCE(due, 0) <= ?", (*states, now)),
                      key=lambda r: (r["created"], r["id"]))

    def orders_in(self, states, chats=None):
        marks = ",".join("?" * len(states))
        if chats is None:
            return self.query(f"state IN ({marks})", tuple(states))
        chats = [c for c in chats if c]
        if not chats:
            return []
        cmarks = ",".join("?" * len(chats))
        return self.query(f"state IN ({marks}) AND chat IN ({cmarks})", (*states, *chats))

    def count_states(self):
        with self.lock:
            return dict(self.db.execute("SELECT state, COUNT(*) FROM records WHERE kind='order' GROUP BY state").fetchall())

    def prune(self, kind, before):
        with self.lock, self.db:
            return self.db.execute("DELETE FROM records WHERE kind=? AND COALESCE(due, 0) < ?", (kind, before)).rowcount

    def patch(self, kind, key, **changes):
        with self.lock:
            value = self.get(kind, key, {})
            value.update(changes)
            self.put(kind, key, value)
            return value

    def all(self, kind):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM records WHERE kind=?", (kind,)).fetchall()]

    def delete(self, kind, key):
        with self.lock, self.db:
            self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, str(key)))

    def settings(self):
        return {**DEFAULTS, **self.get("settings", "main", {})}

    def configure(self, **changes):
        return self.patch("settings", "main", **changes)

    def enqueue(self, oid, chat="", buyer_id=""):
        """One FunPay order — one record, ever. A repeated event never creates a second purchase."""
        if not re.fullmatch(r"[A-Za-z0-9-]{1,40}", str(oid)):
            raise ShopError("Некорректный номер заказа FunPay.")
        with self.lock, self.db:
            data = {"id": str(oid), "state": "queued", "created": time.time(), "next_at": 0}
            if chat:
                data.update(chat=chat, buyer_id=str(buyer_id))
            return self.db.execute("INSERT OR IGNORE INTO records (kind, id, data, state, due, chat, outbox) VALUES ('order',?,?,?,?,?,?)",
                                   (str(oid), json.dumps(data), *self.columns("order", data))).rowcount == 1


def order_text(order):
    """Everything a buyer sees in the order: title, descriptions and selected parameters."""
    text = [getattr(order, "short_description", "") or "", getattr(order, "description", "") or "",
            getattr(order, "full_description", "") or "", getattr(order, "title", "") or ""]
    fields = {}
    for key, value in order_attr_values(order):
        values = list(value.values()) if isinstance(value, dict) else [value]
        fields.setdefault(str(key), []).extend(normalized(v) for v in values if v is not None)
        if str(key) not in {"desc", "payment_msg", "images"}:
            text.extend(str(v) for v in values if v is not None)
    for key in ("server", "side"):
        obj = getattr(order, key, None)
        if obj is not None:
            fields[key] = [normalized(getattr(obj, "name", "")), normalized(getattr(obj, "id", ""))]
            text.append(str(getattr(obj, "name", "")))
    return " \n ".join(text), fields


def lot_conditions(lot):
    """Seller conditions, or the lot name when they were reset with «-»."""
    conditions = [c for c in lot.get("conditions") or [] if str(c).strip()]
    return conditions or ([lot["name"]] if lot.get("name") else [])


def condition_ok(condition, haystack, fields):
    if "=" in condition:
        key, value = condition.split("=", 1)
        if key.strip() in fields:
            return normalized(value) in fields[key.strip()]
        # Order pages without machine keys (lot_params) carry the same value as text.
        return has_phrase(haystack, value)
    return has_phrase(haystack, condition)


def match_lot(order, lots):
    """All conditions must be found. Several matches: the most and longest matched phrases win."""
    direct = getattr(order, "lot_id", None) or getattr(order, "offer_id", None)
    sub = getattr(getattr(order, "subcategory", None), "id", None)
    haystack, fields = order_text(order)
    scored = []
    for lot in lots:
        if not lot.get("item_id"):
            continue
        if direct:
            if str(direct) == str(lot["id"]):
                return lot
            continue
        if lot.get("subcategory") and sub is not None and str(sub) != str(lot["subcategory"]):
            continue
        conditions = lot_conditions(lot)
        if conditions and all(condition_ok(c, haystack, fields) for c in conditions):
            scored.append(((len(conditions), sum(len(normalized(c)) for c in conditions)), lot))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        raise ShopError("Заказ подходит под несколько лотов с одинаковыми условиями: "
                        + ", ".join(str(l["id"]) for _, l in scored[:3]) + ". Уточните условия определения заказа.")
    return scored[0][1]


def selected_conditions(markup):
    """Standardized selectors from Cardinal's authenticated offer editor: [(key, label)]."""
    from bs4 import BeautifulSoup
    form = BeautifulSoup(markup, "html.parser").select_one("form.form-offer-editor")
    if form is None:
        raise ShopError("FunPay не вернул форму редактирования лота.")
    result = []
    for selector in form.select("select[name]"):
        name = selector.get("name", "")
        group = selector.find_parent(class_="form-group")
        if group and "hidden" in (group.get("class") or []):
            continue
        selected = selector.select_one("option[selected]")
        if not selected or not selected.get("value"):
            continue
        label = selected.get_text(" ", strip=True)
        match = re.fullmatch(r"fields\[([^\]]+)\]", name)
        if match and label:
            result.append((match.group(1), label))
        elif name in {"server_id", "side_id"} and label:
            result.append((name[:-3], label))
    return result


class AppRouteAPI:
    def __init__(self, settings, bot_proxy=None, transport=None, pacer=None):
        self.settings = settings
        self.base = {"RU": "https://approute.ru", "IO": "https://approute.io"}[settings["region"]]
        own = settings["proxy_mode"] == "own" and settings["proxy"]
        # «Own» without a saved proxy falls back to the bot's proxy, as the guide describes.
        self.proxies = {"http": settings["proxy"], "https": settings["proxy"]} if own else (bot_proxy or {})
        self.transport = transport
        self.pacer = pacer

    def request(self, method, path, params=None, body=None):
        if not self.settings["api_key"]:
            raise ShopError("API-ключ не задан. Откройте «🔑 Доступ к API».")
        if self.pacer is not None:
            self.pacer.before(self.settings, method, path, params)
        if self.transport is None:
            import requests
            # A fresh session avoids shared mutable headers and environment proxy fallback.
            session = requests.Session()
            session.trust_env = False
            request = session.request
        else:
            session, request = None, self.transport
        try:
            response = request(method, self.base + "/api/v1" + path, params=params, json=body,
                               headers={"X-API-Key": self.settings["api_key"], "Accept": "application/json",
                                        "Content-Type": "application/json"},
                               proxies=self.proxies, timeout=(10, 40), allow_redirects=False)
            if response.status_code == 429:
                header = (getattr(response, "headers", None) or {}).get("Retry-After")
                deadline = (self.pacer.limited(self.settings, header) if self.pacer is not None else
                            time.time() + retry_after_seconds(header, time.time()) + 1)
                LOG.warning("AppRoute Shop: %s %s — HTTP 429", method, path)
                raise RateLimitError(deadline)
            try:
                data = response.json()
            except (ValueError, TypeError):
                raise APIError(response.status_code) from None
            if not isinstance(data, dict):
                raise APIError(response.status_code)
            code = data.get("statusCode")
            if not 200 <= response.status_code < 300 or code not in (0, 1, 2, "0", "1", "2"):
                raise APIError(response.status_code, code if isinstance(code, (int, str)) and len(str(code)) < 32 else None,
                               data.get("errors"))
            if not isinstance(data.get("data"), dict):
                raise APIError(response.status_code, code)
            return data["data"]
        except ShopError:
            raise
        except Exception as exc:
            # requests exceptions can contain proxy passwords or request URLs.
            if "socks" in str(self.proxies.get("https", "")).lower() and "Missing dependencies for SOCKS" in str(exc):
                raise TransientError("Для socks-прокси установите пакет PySocks: pip install pysocks.") from None
            raise TransientError("Сетевая ошибка AppRoute. Проверьте ключ, IP в белом списке и прокси.") from None
        finally:
            if session is not None:
                session.close()

    def refresh_limits(self):
        if self.pacer is None:
            return
        key = self.pacer.identity(self.settings)
        state = self.pacer.store.get("rate", key, {})
        now = self.pacer.clock()
        if now - state.get("limits_checked_at", 0) < 86400:
            return
        try:
            self.pacer.configure_limits(self.settings, self.request("GET", "/rate-limits"))
        except RateLimitError:
            raise
        except ShopError:
            LOG.info("AppRoute Shop: live quotas unavailable; using default intervals")
        self.pacer.store.patch("rate", key, limits_checked_at=now)

    def balance(self):
        data = self.request("GET", "/accounts")
        items = [i for i in data.get("items", []) if isinstance(i, dict)]
        # The API labels the account USD; some cabinets report USDT. Never sum both.
        for code in ("USDT", "USD"):
            values = [i for i in items if str(i.get("currency", "")).upper() == code]
            if len(values) == 1 and "available" in values[0]:
                return dec(values[0]["available"])
        raise ShopError("В ответе AppRoute нет доступного баланса USDT.")

    def catalog(self, checkpoint=None, on_page=None):
        """Every product except direct top-up; resumable page by page."""
        progress = copy.deepcopy(checkpoint or {"products": [], "offset": 0})
        products, page_size = progress["products"], 500
        seen = {str(p["id"]) for p in products}
        offset = progress["offset"]
        while True:
            data = self.request("GET", "/services", {"limit": page_size, "offset": offset})
            items = data.get("items")
            if not isinstance(items, list):
                raise ShopError("Неизвестный формат каталога AppRoute.")
            fresh = 0
            for item in items:
                if not isinstance(item, dict) or item.get("id") is None or str(item["id"]) in seen:
                    continue
                seen.add(str(item["id"]))
                fresh += 1
                if item.get("type") != DIRECT_TOPUP:
                    products.append(item)
            total = integer(data["total"]) if data.get("total") is not None else None
            done = offset + len(items) >= total if total is not None else len(items) < page_size
            if not done and (not items or not fresh or offset >= 200000):
                raise ShopError("AppRoute повторяет страницы каталога; старый каталог сохранён.")
            progress.update(offset=offset + len(items))
            if on_page is not None:
                on_page(copy.deepcopy(progress))
            if done:
                return products
            offset = progress["offset"]

    def service(self, sid):
        return self.request("GET", "/services/" + quote(str(sid), safe=""))

    def item(self, sid, iid):
        return self.request("GET", "/services/" + quote(str(sid), safe="") + "/items/" + quote(str(iid), safe=""))

    def purchase(self, body):
        return self.request("POST", "/orders", body=body)

    def find_order(self, reference, unhide=False):
        data = self.request("GET", "/orders", {"referenceId": reference, "unhide": str(unhide).lower(), "limit": 50, "offset": 0})
        page = data.get("page", data)
        items = page.get("items")
        if not isinstance(items, list):
            raise ShopError("Неизвестный формат истории AppRoute.")
        if page.get("hasNext") or len(items) > 1:
            raise ShopError("Несколько записей AppRoute для одного referenceId; нужна ручная проверка.")
        if not items:
            return None
        record = items[0]
        ref = record.get("reference", record.get("referenceId"))
        if ref is not None and ref != reference:
            raise ShopError("AppRoute вернул другой referenceId.")
        return record


def purchase_body(item_id, quantity, reference):
    return {"ordersType": SHOP_ORDERS_TYPE, "referenceId": reference,
            "orders": [{"denominationId": item_id, "quantity": quantity}]}


def product_items(product):
    for key in ("items", "denominations", "nominals"):
        if isinstance(product.get(key), list):
            return [i for i in product[key] if isinstance(i, dict) and i.get("id") is not None]
    return []


def category_of(product):
    value = product.get("categoryName") or product.get("category")
    if isinstance(value, dict):
        value = value.get("name")
    return str(value or "Без категории")


# --- Delivered payload: voucher codes, eSIM and extra data -----------------------------------

CODE_KEYS = ("code", "pin", "pinCode", "pin_code", "voucher", "voucherCode", "voucher_code", "key", "cdKey",
             "cdkey", "licenseKey", "license_key", "redeemCode", "redeem_code", "giftCode", "gift_code",
             "cardNumber", "card_number", "claimCode", "claim_code", "serialKey")
SERIAL_KEYS = ("serial", "serialNumber", "serial_number", "sn", "cardSerial")
EXPIRY_KEYS = ("expiration", "expirationDate", "expiration_date", "expiresAt", "expires_at", "expireDate",
               "expiry", "expiryDate", "validUntil", "valid_until", "validTo")
ESIM_KEYS = {
    "qr": ("qr", "qrCode", "qr_code", "qrCodeUrl", "qr_code_url", "qrUrl", "qr_url", "qrImage"),
    "smdp": ("smdp", "smdpAddress", "smDpAddress", "smdp_address", "smdpPlus", "smdp_plus", "sm_dp"),
    "activation": ("activationCode", "activation_code", "matchingId", "matching_id"),
    "iccid": ("iccid", "ICCID"),
    "ios": ("iosUrl", "ios_url", "iosInstallUrl", "ios_install_url", "appleInstallUrl", "universalLinkIos"),
    "android": ("androidUrl", "android_url", "androidInstallUrl", "android_install_url"),
    "lpa": ("lpa", "lpaString", "lpa_string", "lpaCode"),
}
ESIM_LABELS = (("qr", "QR"), ("smdp", "SM-DP+"), ("activation", "Activation code"), ("iccid", "ICCID"),
               ("ios", "Установка на iOS"), ("android", "Установка на Android"))
SKIP_KEYS = {"errors", "fields", "body", "request", "headers", "price", "currency", "status", "statusCode"}
EXTRA_SOURCES = ("attributes", "extra", "additionalData", "additional_data", "info", "details")
EXTRA_HIDE = {"id", "orderid", "order_id", "reference", "referenceid", "status", "price", "currency", "denominationid",
              "itemid", "serviceid", "productid", "quantity", "createdat", "updatedat", "created", "updated",
              "type", "orderstype", "balance", "cost", "amount", "total", "accountid", "userid", "apikey"}


def first(entry, keys):
    for key in keys:
        value = entry.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            return str(value).strip()
    return ""


def parse_lpa(value):
    parts = str(value).split("$")
    if len(parts) >= 3 and parts[0].upper().startswith("LPA:"):
        return parts[1], parts[2]
    return "", ""


def walk(obj, depth=0):
    if depth > 8:
        return
    if isinstance(obj, dict):
        yield obj
        for key, value in obj.items():
            if key not in SKIP_KEYS and isinstance(value, (dict, list)):
                yield from walk(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj[:500]:
            yield from walk(value, depth + 1)


def extract_payload(result):
    """Codes and eSIM data from any nesting the provider uses; never raw technical fields."""
    codes, esims, seen = [], [], set()
    for entry in walk(result):
        esim = {name: first(entry, keys) for name, keys in ESIM_KEYS.items()}
        if esim["lpa"] and not (esim["smdp"] and esim["activation"]):
            smdp, activation = parse_lpa(esim["lpa"])
            esim["smdp"], esim["activation"] = esim["smdp"] or smdp, esim["activation"] or activation
        if esim["smdp"] or esim["iccid"] or esim["activation"] or esim["lpa"] and esim["qr"]:
            key = ("esim", esim["iccid"], esim["activation"], esim["smdp"])
            if key not in seen:
                seen.add(key)
                esims.append(esim)
            continue
        # Codes are strings; a numeric "code" is a status or error number.
        code = next((entry[k].strip() for k in CODE_KEYS if isinstance(entry.get(k), str) and entry[k].strip()), "")
        if len(code) >= 3:
            key = ("code", code)
            if key not in seen:
                seen.add(key)
                codes.append({"code": code, "serial": first(entry, SERIAL_KEYS), "expires": first(entry, EXPIRY_KEYS)})
    extra = {}
    for entry in walk(result):
        for source in EXTRA_SOURCES:
            block = entry.get(source)
            if not isinstance(block, dict):
                continue
            for key, value in block.items():
                if isinstance(value, bool) or not isinstance(value, (str, int, float)) or not str(value).strip():
                    continue
                low = key.casefold()
                used = key in CODE_KEYS or key in SERIAL_KEYS or key in EXPIRY_KEYS or \
                    any(key in keys for keys in ESIM_KEYS.values())
                if low in EXTRA_HIDE or used or len(extra) >= 15:
                    continue
                extra[str(key)[:60]] = str(value)[:300]
    return {"codes": codes, "esims": esims, "extra": extra}


def payload_count(payload):
    return len(payload["codes"]) + len(payload["esims"])


def format_codes(payload):
    blocks = []
    codes = payload["codes"]
    if codes:
        lines = ["🔑 Ваши коды:"]
        for index, entry in enumerate(codes, 1):
            prefix = f"{index}) " if len(codes) > 1 else ""
            lines.append(prefix + entry["code"])
            pad = "   " if len(codes) > 1 else ""
            if entry["serial"]:
                lines.append(f"{pad}S/N: {entry['serial']}")
            if entry["expires"]:
                lines.append(f"{pad}Срок действия: {entry['expires']}")
        blocks.append("\n".join(lines))
    for index, esim in enumerate(payload["esims"], 1):
        title = "📶 Данные eSIM" + (f" #{index}" if len(payload["esims"]) > 1 else "") + ":"
        lines = [title] + [f"{label}: {esim[key]}" for key, label in ESIM_LABELS if esim.get(key)]
        if esim.get("lpa") and not esim.get("qr"):
            lines.append(f"LPA: {esim['lpa']}")
        blocks.append("\n".join(lines))
    if payload["extra"]:
        blocks.append("ℹ️ Дополнительные данные:\n" + "\n".join(f"{k}: {v}" for k, v in payload["extra"].items()))
    return "\n\n".join(blocks)


TEMPLATE_FIELDS = {"lot_name", "quantity", "codes", "order_id"}


def template_fields(value):
    """Placeholders of a template; ValueError for unbalanced braces or formatting specs."""
    fields = set()
    for _, field, spec, conversion in string.Formatter().parse(value):
        if field is not None:
            if spec or conversion:
                raise ValueError
            fields.add(field)
    return fields


def validate_template(value):
    if not 1 <= len(value) <= 3000:
        raise ShopError("Текст шаблона: от 1 до 3000 символов.")
    try:
        fields = template_fields(value)
    except ValueError:
        fields = None
    if fields is None and "{codes}" not in value or fields is not None and "codes" not in fields:
        raise ShopError("В шаблоне обязательно должна быть переменная {codes} — иначе покупатель не получит товар.")
    return value


def render_template(template, **values):
    try:
        if not template_fields(template) <= TEMPLATE_FIELDS or "codes" not in template_fields(template):
            raise ValueError
        return template.format(**values)
    except (ValueError, KeyError, IndexError):
        # Unknown braces: the buyer still gets the purchased data in the standard text.
        return DEFAULT_TEMPLATE.format(**values)


def split_message(text, limit=1800):
    """FunPay rejects very long messages: split on line breaks, never inside a code."""
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        candidate = current + ("\n" if current else "") + line
        if len(candidate) > limit:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        parts.append(current)
    return parts or [text[:limit]]


BUYER_TEXTS = {
    "limit": "⚠️ Заказ #{order_id}: количество превышает лимит автоматической выдачи. "
             "Пожалуйста, свяжитесь с продавцом — заказ будет выдан вручную. {refund_note}",
    "unavailable": "😔 К сожалению, товара по заказу #{order_id} сейчас нет в наличии. {refund_note}",
    "balance": "⚠️ Автоматическая выдача по заказу #{order_id} временно недоступна. "
               "Пожалуйста, свяжитесь с продавцом. {refund_note}",
    "failed": "😔 Приносим извинения: заказ #{order_id} не удалось выполнить. {refund_note}",
    "guard": "🛠 Заказ #{order_id} требует ручной проверки продавцом. Пожалуйста, подождите.",
    "timeout": "⏳ Поставщик обрабатывает заказ #{order_id} дольше обычного. Пожалуйста, подождите; "
               "при необходимости свяжитесь с продавцом.",
    "no_codes": "✅ Заказ #{order_id} оплачен у поставщика. Продавец выдаст коды вручную в ближайшее время.",
    "review": "🛠 Заказ #{order_id} требует проверки продавцом. Мы уже передали ему информацию.",
    "refunded": "↩️ Средства за заказ #{order_id} возвращены на FunPay.",
}
REFUND_NOTES = {True: "Деньги будут возвращены автоматически.", False: "Продавец уведомлён и вернёт деньги."}


class GuardError(ShopError):
    """The amount guard stopped an order before any payment."""


REFUSAL_CODES = {"3", "4", "5", "6", "9", "10", "13"}
SUCCESS_STATUSES = {"SUCCESS", "COMPLETED", "DONE", "FULFILLED", "DELIVERED"}
PARTIAL_STATUSES = {"PARTIALLY_COMPLETED", "PARTIAL", "PARTIALLY_FULFILLED"}
WAIT_STATUSES = {"IN_PROGRESS", "PENDING", "PROCESSING", "CREATED", "NEW", "ACCEPTED", "QUEUED", "ADMISSION"}
FAILED_STATUSES = {"CANCELLED", "CANCELED", "FAILED", "REJECTED", "ERROR", "DECLINED", "REFUNDED"}
LATE_LIMIT = 86400  # after the 10-minute notice the provider is still polled for a day


class Shop:
    def __init__(self, cardinal, store=None, api_factory=None):
        self.cardinal = cardinal
        self.store = store or Store(STORAGE / "shop.sqlite3")
        self.api_factory = api_factory
        self.stop_event, self.wakeup, self.service_wakeup = threading.Event(), threading.Event(), threading.Event()
        self.pacer = RequestPacer(self.store, self.stop_event)
        # Order worker: purchases and delivery. Service worker: catalogue, prices, lots, panel.
        self.jobs, self.service_jobs = queue.Queue(maxsize=200), queue.Queue(maxsize=200)
        self.thread, self.service_thread, self.ui, self.instance_lock = None, None, None, None
        self.last_maintenance = self.last_prune = 0

    # --- infrastructure -----------------------------------------------------------------------

    def active(self):
        plugin = getattr(self.cardinal, "plugins", {}).get(UUID)
        return (plugin is None or getattr(plugin, "enabled", True)) and not self.stop_event.is_set()

    def api(self, settings=None):
        settings = settings or self.store.settings()
        if self.api_factory:
            return self.api_factory(settings)
        return AppRouteAPI(settings, getattr(self.cardinal.account, "proxy", None), pacer=self.pacer)

    def notify(self, text, buttons=None, chats=None, force=False):
        tg = getattr(self.cardinal, "telegram", None)
        s = self.store.settings()
        if not tg or not (s["notifications"] or force):
            return
        markup = None
        if buttons:
            try:
                from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
                markup = InlineKeyboardMarkup()
                markup.row(*[InlineKeyboardButton(label, url=url) for label, url in buttons])
            except Exception:
                markup = None
        for chat in chats if chats is not None else s["notify_chats"]:
            try:
                tg.bot.send_message(chat, text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)
            except Exception:
                LOG.warning("AppRoute Shop: Telegram notification not sent")

    @staticmethod
    def order_buttons(record):
        buttons = [("💙 Заказ FunPay", f"https://funpay.com/orders/{quote(str(record['id']), safe='')}/")]
        if str(record.get("buyer_id", "")).isdigit():
            buttons.append(("👤 Покупатель", f"https://funpay.com/users/{record['buyer_id']}/"))
        return buttons

    @staticmethod
    def lot_url(lid):
        return f"https://funpay.com/lots/offer?id={lid}"

    def save(self, record):
        record["updated"] = time.time()
        self.store.put("order", record["id"], record)

    def tell(self, record, key=None, text=None):
        """Persist buyer messages with the state change; a resend never repeats a purchase."""
        if text is None:
            note = REFUND_NOTES[bool(self.store.settings()["auto_refund"])]
            text = BUYER_TEXTS[key].format(order_id=record["id"], refund_note=note).strip()
        if record.get("chat"):
            for part in split_message(text):
                record.setdefault("outbox", []).append({"id": uuid.uuid4().hex, "text": part})
        self.save(record)

    def flush(self, record):
        while record.get("outbox"):
            msg = record["outbox"][0]
            buyer_id = str(record.get("buyer_id", ""))
            try:
                result = self.cardinal.account.send_message(record["chat"], msg["text"], record.get("buyer") or None,
                                                            int(buyer_id) if buyer_id.isdigit() else None)
            except Exception:
                result = None
            if not result:
                record["outbox_failures"] = record.get("outbox_failures", 0) + 1
                if record["outbox_failures"] >= 10:
                    dropped = len(record["outbox"])
                    record.update(outbox=[], outbox_failures=0, undelivered=True)
                    self.save(record)
                    self.notify(f"✉️ <b>Заказ #{esc(record['id'])}</b>: FunPay не принял {dropped} сообщ. покупателю. "
                                f"Напишите ему вручную (данные смотрите в кабинете AppRoute по номеру "
                                f"<code>{esc(record.get('reference', '—'))}</code>).", self.order_buttons(record))
                    return
                self.save(record)
                raise TransientError("FunPay не подтвердил доставку сообщения.")
            record["outbox"].pop(0)
            record["outbox_failures"] = 0
            self.save(record)

    def submit(self, action, service=True):
        try:
            (self.service_jobs if service else self.jobs).put_nowait(action)
        except queue.Full:
            raise ShopError("Очередь панели заполнена. Повторите позже.") from None
        (self.service_wakeup if service else self.wakeup).set()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        # Prevent two processes from using one purchase journal.
        STORAGE.mkdir(parents=True, exist_ok=True)
        if self.instance_lock is not None:
            self.instance_lock.close()
        self.instance_lock = (STORAGE / "worker.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.instance_lock.seek(0)
                self.instance_lock.write(b"0")
                self.instance_lock.flush()
                self.instance_lock.seek(0)
                msvcrt.locking(self.instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.instance_lock.close()
            self.instance_lock = None
            raise ShopError("Другой экземпляр плагина уже использует эту базу заказов.") from None
        self.thread = threading.Thread(target=self.loop, name="AppRouteShop", daemon=True)
        self.service_thread = threading.Thread(target=self.service_loop, name="AppRouteShopService", daemon=True)
        self.thread.start()
        self.service_thread.start()

    @staticmethod
    def run_jobs(jobs, limit=20):
        for _ in range(limit):
            try:
                action = jobs.get_nowait()
            except queue.Empty:
                return
            try:
                action()
            except Exception:
                LOG.warning("AppRoute Shop: panel job failed", exc_info=True)

    def loop(self):
        while not self.stop_event.is_set():
            if self.active():
                self.run_jobs(self.jobs)
                for record in self.store.due_orders(time.time(), ACTIVE_STATES):
                    if self.stop_event.is_set():
                        break
                    self.process(record["id"])
            self.wakeup.wait(1)
            self.wakeup.clear()

    def service_loop(self):
        while not self.stop_event.is_set():
            if self.active():
                self.run_jobs(self.service_jobs)
                if time.time() - self.last_maintenance >= 30:
                    self.last_maintenance = time.time()
                    try:
                        self.maintenance()
                    except (RequestDeferred, RateLimitError):
                        pass
                    except Exception:
                        LOG.warning("AppRoute Shop: maintenance failed", exc_info=True)
            self.service_wakeup.wait(1)
            self.service_wakeup.clear()

    # --- order pipeline -----------------------------------------------------------------------

    def enqueue(self, oid, chat="", buyer_id=""):
        if self.store.enqueue(oid, chat, buyer_id):
            self.wakeup.set()

    def get_order(self, oid):
        try:
            order = self.cardinal.account.get_order(oid)
        except ShopError:
            raise
        except Exception:
            raise TransientError("FunPay временно не вернул данные заказа. Повтор запланирован.") from None
        if str(getattr(order, "seller_id", self.cardinal.account.id)) != str(self.cardinal.account.id):
            raise ShopError("Этот аккаунт Cardinal не является продавцом заказа.")
        return order

    def paid(self, record, order=None):
        order = order if order is not None else self.get_order(record["id"])
        if enum_name(order.status) != "PAID":
            raise ShopError("Заказ FunPay больше не в статусе «Оплачен». Автовыдача остановлена.")
        if getattr(order, "order_secrets", None):
            raise ShopError("У лота включена встроенная автовыдача FunPay. Отключите её, чтобы не выдать товар дважды.")
        if record.get("quantity") is not None and integer(order.amount) != record["quantity"]:
            raise ShopError("Количество в заказе FunPay изменилось. Проверьте вручную.")
        if record.get("revenue") is not None and dec(order.sum) != dec(record["revenue"]):
            raise ShopError("Сумма заказа FunPay изменилась. Проверьте вручную.")
        return order

    def retry_later(self, record, exc):
        record["retries"] = record.get("retries", 0) + 1
        record["next_at"] = time.time() + min(600, 15 * 2 ** (record["retries"] - 1))
        record["last_error"] = str(exc)[:300]
        self.save(record)

    def process(self, oid):
        record = self.store.get("order", oid)
        if not record:
            return
        flushing = True
        try:
            self.flush(record)
            flushing = False
            if record["state"] not in ACTIVE_STATES:
                return
            state = record["state"]
            if state in {"queued", "ready"} and not self.store.settings()["enabled"]:
                self.skip_disabled(record)
            elif state == "queued":
                self.prepare(record)
            elif state == "ready":
                self.purchase(record)
            elif state in {"purchasing", "pending"}:
                self.reconcile(record)
            elif state == "reveal":
                self.reveal(record)
            elif state == "refund_pending":
                self.refund(record)
            flushing = True
            self.flush(record)
            if record.get("retries"):
                record["retries"] = 0
                self.save(record)
        except (RateLimitError, RequestDeferred) as exc:
            record["next_at"] = exc.retry_at
            self.save(record)
        except GuardError as exc:
            self.review(record, str(exc), "guard")
        except Exception as exc:
            if not isinstance(exc, ShopError):
                LOG.warning("AppRoute Shop: order %s failed", oid, exc_info=True)
            if record["state"] in {"purchasing", "pending", "reveal", "refund_pending"}:
                # Money may be in flight: keep reconciling; a definite non-network answer goes to the seller.
                if flushing or is_transient(exc) or not isinstance(exc, ShopError):
                    record.update(next_at=time.time() + 30, last_error=str(exc)[:300])
                    self.save(record)
                else:
                    self.review(record, str(exc))
            elif flushing or is_transient(exc):
                if record.get("retries", 0) >= MAX_RETRIES and not flushing:
                    self.fail(record, f"{exc} Повторы исчерпаны.")
                else:
                    self.retry_later(record, exc)
            else:
                self.review(record, str(exc) if isinstance(exc, ShopError) else "Ошибка обработки заказа. Проверьте настройки лота.")
        if record.get("outbox"):
            # Messages queued by an error path go out now; a failure is retried by the worker.
            try:
                self.flush(record)
            except Exception:
                pass

    def skip_disabled(self, record):
        record.update(state="ignored", reason="Автовыдача выключена (Статус 🔴) — заказ не обработан.", next_at=0)
        self.save(record)
        if record.get("lot"):
            self.notify(f"⏸ <b>Заказ #{esc(record['id'])}</b> по лоту «{esc(record['lot'].get('name', ''))}» не выдан: "
                        f"автовыдача выключена. Выдайте его вручную.", self.order_buttons(record))

    def prepare(self, record):
        order = self.get_order(record["id"])
        if str(order.buyer_id) == str(self.cardinal.account.id):
            record["state"] = "ignored"
            self.save(record)
            return
        if enum_name(order.status) != "PAID":
            record["state"] = "closed"
            self.save(record)
            return
        chat = str(getattr(order, "chat_id", "") or record.get("chat") or "")
        if chat and not chat.startswith("users-") and str(order.buyer_id).isdigit():
            chat = chat_for(self.cardinal.account.id, order.buyer_id) or chat
        record.update(chat=chat, buyer=getattr(order, "buyer_username", "") or "", buyer_id=str(order.buyer_id),
                      revenue=str(order.sum), currency=enum_name(getattr(order, "currency", None) or "RUB"))
        lot = match_lot(order, self.store.all("lot"))
        if lot is None:
            record["state"] = "ignored"
            self.save(record)
            return
        record["lot"] = copy.deepcopy(lot)
        quantity = integer(order.amount)
        if quantity <= 0 or dec(order.sum) <= 0:
            raise ShopError("Некорректное количество или сумма заказа FunPay.")
        record["quantity"] = quantity
        self.paid(record, order)
        if not record.get("chat"):
            raise ShopError("FunPay не вернул чат покупателя.")
        earlier = [r for r in self.store.orders_in(ACTIVE_STATES, [record["chat"]])
                   if r["id"] != record["id"] and (r["created"], r["id"]) < (record["created"], record["id"])]
        if earlier:
            # Several orders of one buyer go one by one; re-check later, not every second.
            record["next_at"] = time.time() + 15
            self.save(record)
            return
        s = self.store.settings()
        if quantity > int(s["max_quantity"]):
            self.fail(record, f"Количество {quantity} больше лимита на заказ ({s['max_quantity']}).", "limit")
            return
        api = self.api(s)
        try:
            item = api.item(lot["product_id"], lot["item_id"])
        except APIError as exc:
            if is_transient(exc) or isinstance(exc, RateLimitError):
                raise
            self.remember_item(lot, None)
            self.disable_lots("stock", "товар пропал у поставщика", self.lots_for_item(lot))
            self.fail(record, "Товар пропал у поставщика.", "unavailable")
            return
        self.remember_item(lot, item)
        if not available(item, quantity):
            stock = stock_count(item)
            self.disable_lots("stock", "нет в наличии у поставщика", self.lots_for_item(lot))
            self.fail(record, "Нет в наличии у поставщика" + (f" (остаток {stock}, нужно {quantity})." if stock else "."), "unavailable")
            return
        unit = unit_cost(item)
        cost = unit * quantity
        record.update(unit_cost=str(unit), expected=str(cost), item_name=str(item.get("name", lot.get("item_name", ""))))
        self.guard(record, cost)
        balance = api.balance()
        self.record_balance(balance)
        if balance < cost:
            self.disable_lots("balance", "недостаточно средств на балансе AppRoute")
            self.fail(record, f"Недостаточно средств AppRoute: нужно {money(cost)}, доступно {money(balance)} USDT.", "balance")
            return
        record.update(state="ready", next_at=0)
        self.save(record)
        self.purchase(record, api)

    def guard(self, record, cost):
        s = self.store.settings()
        if not s["sanity_enabled"]:
            return
        currency = record.get("currency") or "RUB"
        if currency == "RUB":
            paid = dec(record["revenue"]) / dec(s["rate"])
        elif currency in {"USD", "USDT"}:
            paid = dec(record["revenue"])
        else:
            raise GuardError(f"Предохранитель: заказ в валюте {currency}, сравнить суммы нельзя. Проверьте заказ вручную.")
        allowed = paid * dec(s["sanity_ratio"]) + Decimal("0.5")
        if dec(cost) > allowed:
            raise GuardError(f"Предохранитель сумм: закупка {money(cost)} USDT, а покупатель заплатил "
                             f"{money(record['revenue'])} {currency} ≈ {money(paid)} USDT. Допустимо до {money(allowed)} USDT "
                             f"(×{s['sanity_ratio']}). Проверьте привязку лота и курс.")

    def purchase(self, record, api=None):
        s = self.store.settings()
        if not s["enabled"]:
            self.skip_disabled(record)
            return
        self.paid(record)
        api = api or self.api(s)
        reference = record.get("reference") or "fpc-" + re.sub(r"[^A-Za-z0-9-]", "", record["id"])
        body = purchase_body(record["lot"]["item_id"], record["quantity"], reference)
        # Persist the exact request and key identity BEFORE the paid HTTP request.
        record.update(state="purchasing", reference=reference, body=body, sent_at=time.time(),
                      identity=RequestPacer.identity(s), uncertain=False, lookups=0, next_at=time.time() + 10)
        self.save(record)
        try:
            result = api.purchase(body)
        except (PacerBlocked, RequestDeferred) as exc:
            # Stopped locally before any HTTP request: nothing was sent.
            record.pop("sent_at", None)
            record.update(state="ready", next_at=exc.retry_at)
            self.save(record)
            return
        except RateLimitError:
            # HTTP 429 normally means «not accepted»; look the order up first, then resend the same referenceId.
            record.update(state="pending", uncertain=True, rate_limited=True)
            self.save(record)
            return
        except APIError as exc:
            code = code_of(exc)
            if exc.status in {400, 401, 403, 404, 422} and code in REFUSAL_CODES:
                record["rejected"] = True
                self.save(record)
                if code == "9":
                    self.disable_lots("stock", "нет в наличии у поставщика", self.lots_for_item(record["lot"]))
                    self.fail(record, "Поставщик: товара нет в наличии.", "unavailable")
                elif code == "10":
                    self.disable_lots("balance", "недостаточно средств на балансе AppRoute")
                    self.fail(record, "Поставщик: недостаточно средств на балансе.", "balance")
                elif code == "13":
                    self.disable_lots("limit", "исчерпан лимит трат API-ключа")
                    self.fail(record, "Исчерпан лимит трат API-ключа AppRoute.")
                else:
                    self.fail(record, f"Поставщик отклонил заказ ({exc}).")
            else:
                # 409 (same referenceId) or an unclear answer: look the order up before anything else.
                record.update(state="pending", uncertain=True, last_error=str(exc)[:300])
                self.save(record)
            return
        except ShopError as exc:
            record.update(state="pending", uncertain=True, last_error=str(exc)[:300])
            self.save(record)
            return
        self.accept(record, result)

    def accept(self, record, result):
        if not isinstance(result, dict):
            self.review(record, "Поставщик вернул ответ неизвестного формата. Проверьте заказ в кабинете AppRoute.")
            return
        ref = result.get("reference", result.get("referenceId"))
        if ref is not None and ref != record["reference"]:
            self.review(record, "Поставщик вернул другой номер покупки. Проверьте заказ вручную.")
            return
        status = enum_name(result.get("status", ""))
        record["provider_id"] = str(result.get("orderId") or result.get("transactionId") or result.get("id") or record.get("provider_id", ""))
        if status in SUCCESS_STATUSES | PARTIAL_STATUSES:
            record["partial"] = status in PARTIAL_STATUSES
            payload = extract_payload(result)
            if payload_count(payload):
                self.deliver(record, payload, result)
            else:
                # Codes are often hidden in the purchase answer; they are revealed separately.
                record.update(state="reveal", reveal_tries=0, next_at=0)
                self.save(record)
        elif status in WAIT_STATUSES:
            record.update(state="pending", uncertain=False, next_at=time.time() + 10)
            self.save(record)
        elif status in FAILED_STATUSES:
            record["rejected"] = True
            self.save(record)
            self.fail(record, f"Поставщик отклонил заказ (статус {status}).")
        else:
            self.review(record, f"Неизвестный статус поставщика «{status[:40]}». Проверьте заказ вручную.")

    def reconcile(self, record):
        if RequestPacer.identity(self.store.settings()) != record.get("identity"):
            self.review(record, "Ключ или регион изменён во время покупки. Проверьте заказ в прежнем кабинете AppRoute.")
            return
        elapsed = time.time() - record.get("sent_at", record["created"])
        result = self.api().find_order(record["reference"])
        if result is not None:
            if enum_name(result.get("status", "")) in WAIT_STATUSES:
                self.waiting(record, elapsed)
                return
            self.accept(record, result)
            return
        record["lookups"] = record.get("lookups", 0) + 1
        if record.get("rate_limited") and record.get("resends", 0) < 3 and record["lookups"] >= 2:
            record.update(state="ready", rate_limited=False, resends=record.get("resends", 0) + 1, next_at=time.time())
            self.save(record)
            return
        if record.get("uncertain") and record["lookups"] >= 3 and elapsed >= 60:
            record["rejected"] = True
            self.save(record)
            self.fail(record, "Сетевая ошибка при создании заказа; у поставщика заказ не найден.")
        else:
            self.waiting(record, elapsed)

    def waiting(self, record, elapsed):
        if elapsed > LATE_LIMIT:
            self.review(record, "Поставщик не завершил заказ за сутки. Проверьте его в кабинете AppRoute.", None)
            return
        if elapsed > POLL_TIMEOUT and not record.get("timeout_told"):
            # No refund: the order may still complete; polling continues and delivery is automatic.
            record["timeout_told"] = True
            self.tell(record, "timeout")
            self.notify(f"⏳ <b>Заказ #{esc(record['id'])}</b>: поставщик обрабатывает заказ дольше 10 минут. "
                        f"Возврат не выполнялся. Проверьте заказ в кабинете AppRoute по номеру "
                        f"<code>{esc(record['reference'])}</code>. Бот продолжит проверять и выдаст коды сам.",
                        self.order_buttons(record))
        record.update(state="pending", next_at=time.time() + (20 if elapsed < POLL_TIMEOUT else 60))
        self.save(record)

    def reveal(self, record):
        result = self.api().find_order(record["reference"], unhide=True)
        payload = extract_payload(result) if result else {"codes": [], "esims": [], "extra": {}}
        if payload_count(payload):
            self.deliver(record, payload, result)
            return
        record["reveal_tries"] = record.get("reveal_tries", 0) + 1
        if record["reveal_tries"] < 3:
            record["next_at"] = time.time() + 65
            self.save(record)
            return
        record["cost"] = record.get("expected", "0")
        self.review(record, "Покупка прошла, но поставщик не вернул коды. Выдайте товар вручную "
                            f"(номер покупки {record['reference']}).", "no_codes")

    def deliver(self, record, payload, result=None):
        s = self.store.settings()
        count = payload_count(payload)
        cost = dec(record.get("expected", "0"))
        if record.get("partial") and record.get("quantity"):
            cost = dec(record.get("unit_cost", "0")) * min(count, record["quantity"])
        lot_name = record["lot"].get("name", "")
        text = render_template(s["template"], lot_name=lot_name, quantity=record["quantity"],
                               codes=format_codes(payload), order_id=record["id"])
        revenue_rub = dec(record["revenue"]) * (dec(s["rate"]) if record.get("currency") in {"USD", "USDT"} else 1)
        record.update(state="partial" if record.get("partial") else "done", finished=time.time(), next_at=0,
                      cost=str(cost), codes_count=count, revenue_rub=money(revenue_rub))
        record.pop("body", None)
        self.tell(record, text=text)
        # All-time totals survive the 2000-order history limit.
        totals = self.store.settings()
        self.store.configure(total_sales=totals.get("total_sales", 0) + 1,
                             total_revenue=str(dec(totals.get("total_revenue", "0")) + revenue_rub),
                             total_cost=str(dec(totals.get("total_cost", "0")) + cost))
        balance = None
        try:
            balance = self.api(s).balance()
            self.record_balance(balance)
        except Exception:
            pass
        self.notify(f"✅ <b>Выдан заказ #{esc(record['id'])}</b>\n👤 {esc(record.get('buyer', ''))}\n"
                    f"💰 Цена на FunPay: {esc(record['revenue'])} {esc(record.get('currency', 'RUB'))}\n"
                    f"🎁 {esc(lot_name)} · {esc(record.get('item_name', ''))}\nКоличество: {record['quantity']} · выдано: {count}\n"
                    f"💸 Списано: {money(cost)} USDT" + (f" · остаток баланса: {money(balance)} USDT" if balance is not None else "")
                    + ("\n⚠️ Частичное выполнение: проверьте заказ вручную." if record.get("partial") else ""),
                    self.order_buttons(record))
        self.trim_history()

    def fail(self, record, reason, message="failed"):
        """Refund only when nothing was bought: an accepted or unknown purchase goes to the seller."""
        if record.get("sent_at") and not record.get("rejected"):
            self.review(record, reason + " Покупка у поставщика могла пройти — возврат не выполнялся.")
            return
        s = self.store.settings()
        record.update(reason=reason, next_at=0)
        if s["auto_refund"]:
            record["state"] = "refund_pending"
            self.tell(record, message)
            self.notify(f"❌ <b>Заказ #{esc(record['id'])}</b>: {esc(reason)}\nВыполняю возврат покупателю.",
                        self.order_buttons(record))
        else:
            self.review(record, reason + " Авто-возврат выключен — верните деньги вручную.", message)

    def review(self, record, reason, message="review"):
        record.update(state="review", reason=reason, next_at=0)
        self.save(record)
        if message:
            self.tell(record, message)
        self.notify(f"🛠 <b>Заказ #{esc(record['id'])}</b> · {esc(record.get('buyer', ''))}\n"
                    f"{esc(record.get('lot', {}).get('name', ''))}\n{esc(reason)}", self.order_buttons(record))

    def refund(self, record):
        if record.get("sent_at") and not record.get("rejected"):
            self.review(record, "Возврат остановлен: покупка у поставщика могла пройти.")
            return
        order = self.get_order(record["id"])
        status = enum_name(order.status)
        if status == "REFUNDED":
            record.update(state="refunded", finished=time.time())
            self.tell(record, "refunded")
            self.notify(f"↩️ <b>Возврат</b> по заказу #{esc(record['id'])} выполнен. {esc(record.get('reason', ''))}",
                        self.order_buttons(record))
            return
        if status != "PAID":
            self.review(record, "Возврат не выполнен: заказ FunPay больше не в статусе «Оплачен».", None)
            return
        attempts = record.get("refund_attempts", 0)
        if attempts >= 3:
            self.review(record, "FunPay не подтвердил возврат после нескольких попыток. Верните деньги вручную.", None)
            return
        # The order is still PAID, so a repeated request cannot refund twice.
        record.update(refund_attempts=attempts + 1, next_at=time.time() + 30)
        self.save(record)
        try:
            self.cardinal.account.refund(record["id"])
        except Exception:
            LOG.warning("AppRoute Shop: FunPay refund request failed; status will be re-checked")

    def trim_history(self):
        done = sorted(self.store.orders_in(SUCCESS_STATES), key=lambda r: r.get("finished", r["created"]), reverse=True)
        for record in done[HISTORY_LIMIT:]:
            # Keep the order id so a repeated FunPay event never buys again; drop the details.
            self.store.put("order", record["id"], {"id": record["id"], "state": "closed", "created": record["created"],
                                                   "archived": True, "next_at": 0})

    # --- balance, lots, catalogue ---------------------------------------------------------------

    def record_balance(self, value):
        s = self.store.settings()
        low = value < dec(s["low_balance"])
        self.store.configure(balance=str(value), balance_at=time.time(), low_alert=low)
        if low and not s["low_alert"]:
            # Once per drop: the alert re-arms after the balance rises above the threshold.
            self.notify(f"👛 <b>Низкий баланс AppRoute:</b> {money(value)} USDT (порог {esc(s['low_balance'])} USDT). "
                        f"Пополните баланс, чтобы заказы выдавались без перебоев.")

    def lots_for_item(self, lot):
        return [l for l in self.store.all("lot") if str(l.get("item_id")) == str(lot.get("item_id"))
                and str(l.get("product_id")) == str(lot.get("product_id"))]

    def set_lot_active(self, lot, active):
        fields = self.cardinal.account.get_lot_fields(int(lot["id"]))
        if bool(getattr(fields, "active", True)) == active:
            return False
        fields.active = active
        self.cardinal.account.save_lot(fields.renew_fields() if hasattr(fields, "renew_fields") else fields)
        return True

    def disable_lots(self, kind, reason, lots=None):
        for lot in lots if lots is not None else self.store.all("lot"):
            try:
                if self.set_lot_active(lot, False):
                    self.store.patch("lot", lot["id"], disabled_kind=kind, disabled_reason=reason, disabled_at=time.time())
                    self.notify(f"⚠️ <b>Лот деактивирован</b>: {esc(lot.get('name', lot['id']))} (ID {esc(lot['id'])})\n"
                                f"Причина: {esc(reason)}." + ("\nВключится сам, когда товар вернётся (при автовключении)." if kind == "stock" else
                                                             "\nПосле устранения причины включите лот на FunPay вручную."),
                                [("🔗 Лот на FunPay", self.lot_url(lot["id"]))])
            except Exception:
                self.notify(f"⚠️ Не удалось отключить лот {esc(lot['id'])} ({esc(reason)}). Отключите его вручную.",
                            [("🔗 Лот на FunPay", self.lot_url(lot["id"]))])

    def remember_item(self, lot, item):
        key = f"{lot['product_id']}:{lot['item_id']}"
        self.store.put("item", key, {"data": item, "missing": item is None, "at": time.time()})

    def cached_item(self, lot):
        return self.store.get("item", f"{lot.get('product_id')}:{lot.get('item_id')}")

    def refresh_items(self):
        """Fresh price and stock of every bound position, once per pass and per unique item."""
        api, done, errors = self.api(), set(), 0
        for lot in self.store.all("lot"):
            key = (str(lot.get("product_id")), str(lot.get("item_id")))
            if not lot.get("item_id") or key in done or not self.active():
                continue
            done.add(key)
            try:
                self.remember_item(lot, api.item(*key))
            except (RateLimitError, RequestDeferred):
                raise
            except APIError as exc:
                if is_transient(exc):
                    errors += 1
                else:
                    self.remember_item(lot, None)
            except ShopError:
                errors += 1
        self.store.configure(items_at=time.time())
        return len(done), errors

    def sync_lots(self, force=False):
        s, errors, changed = self.store.settings(), [], 0
        for lot in self.store.all("lot"):
            if not lot.get("item_id") or not lot.get("auto_price", True) or not self.active():
                continue
            try:
                cached = self.cached_item(lot)
                if not cached:
                    raise ShopError("нет данных поставщика — нажмите «⚡ Обновить цены сейчас»")
                item = cached.get("data")
                fp = self.cardinal.account.get_lot_fields(int(lot["id"]))
                old = (getattr(fp, "price", None), getattr(fp, "amount", None), bool(getattr(fp, "active", True)))
                gone = cached.get("missing") or not item or not available(item)
                if item:
                    new_price = dec(calculate_price(unit_cost(item), s))
                    current = dec(fp.price or 0)
                    # Skip noise: at least 0.5 ₽, or 5 % for very cheap lots.
                    if current <= 0 or abs(new_price - current) >= min(Decimal("0.5"), current * Decimal("0.05")):
                        fp.price = float(new_price)
                deactivated = reactivated = False
                if s["sync_stock"]:
                    if gone:
                        if fp.active:
                            fp.active, deactivated = False, True
                    else:
                        if fp.amount is not None:
                            fp.amount = lot_amount(item, s)
                        if s["auto_enable"] and not fp.active and lot.get("disabled_kind") == "stock":
                            fp.active, reactivated = True, True
                if old != (getattr(fp, "price", None), getattr(fp, "amount", None), bool(getattr(fp, "active", True))):
                    self.cardinal.account.save_lot(fp.renew_fields() if hasattr(fp, "renew_fields") else fp)
                    changed += 1
                    if deactivated:
                        reason = "пропал у поставщика" if cached.get("missing") else "нет в наличии у поставщика"
                        self.store.patch("lot", lot["id"], disabled_kind="stock", disabled_reason=reason, disabled_at=time.time())
                        self.notify(f"⚠️ <b>Лот деактивирован</b>: {esc(lot.get('name', ''))} (ID {esc(lot['id'])})\nПричина: {reason}.",
                                    [("🔗 Лот на FunPay", self.lot_url(lot["id"]))])
                    if reactivated:
                        self.store.patch("lot", lot["id"], disabled_kind="", disabled_reason="")
                        self.notify(f"✅ <b>Лот снова активен</b>: {esc(lot.get('name', ''))} (ID {esc(lot['id'])}) — товар вернулся у поставщика.",
                                    [("🔗 Лот на FunPay", self.lot_url(lot["id"]))])
                    if s["price_notify"] and old[0] != fp.price:
                        self.notify(f"💲 <b>Цена изменена</b>: {esc(lot.get('name', ''))} (ID {esc(lot['id'])})\n"
                                    f"{money(old[0] or 0)} ₽ → {money(fp.price)} ₽", [("🔗 Открыть лот", self.lot_url(lot["id"]))])
                self.store.patch("lot", lot["id"], last_price=str(fp.price), sync_error="")
            except (RequestDeferred, RateLimitError):
                raise
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ShopError) else "ошибка сохранения лота на FunPay"
                self.store.patch("lot", lot["id"], sync_error=reason)
                errors.append(f"{lot['id']}: {reason}")
        self.store.configure(sync_at=time.time())
        digest = hashlib.sha256("\n".join(sorted(errors)).encode()).hexdigest() if errors else ""
        if errors and (force or digest != s.get("sync_errors_digest")):
            self.notify("⚠️ Обновление лотов:\n" + esc("\n".join(errors[:10])))
        self.store.configure(sync_errors_digest=digest)
        return changed, len(errors)

    def products(self):
        return self.store.get("catalog", "main", {}).get("products", [])

    def refresh_catalog(self, chat=None):
        if chat is not None:
            self.store.configure(catalog_requested=chat)
        self.api().refresh_limits()
        progress = self.store.get("catalog", "progress")
        try:
            products = self.api().catalog(progress, lambda value: self.store.put("catalog", "progress", value))
        except (RateLimitError, RequestDeferred) as exc:
            self.store.configure(catalog_retry_at=exc.retry_at)
            raise
        self.store.put("catalog", "main", {"products": products, "at": time.time()})
        self.store.delete("catalog", "progress")
        self.store.configure(catalog_at=time.time(), catalog_retry_at=0, catalog_requested=None)
        return len(products), sum(len(product_items(p)) for p in products)

    def maintenance(self):
        s = self.store.settings()
        now = time.time()
        if now - self.last_prune > 3600:
            self.last_prune = now
            self.store.prune("seen", now - 7 * 86400)
        if not s["api_key"]:
            return
        if now - s["balance_at"] > 600:
            self.record_balance(self.api().balance())
        resume = self.store.get("catalog", "progress") is not None or s.get("catalog_requested") is not None
        if (resume or now - s["catalog_at"] > s["catalog_interval"] * 60) and now >= s["catalog_retry_at"]:
            requested = s.get("catalog_requested")
            products, items = self.refresh_catalog()
            if requested is not None:
                self.notify(f"✅ Каталог загружен: {products} товаров, {items} номиналов.", chats=[requested], force=True)
        lots = [l for l in self.store.all("lot") if l.get("item_id")]
        if lots and now - s["items_at"] > s["items_interval"] * 60:
            self.refresh_items()
        if lots and s["auto_price"] and now - s["sync_at"] > max(20, int(s["interval"])) * 60:
            self.sync_lots()

    def read_lot(self, lid):
        """Title, standardized parameters and type of a FunPay lot."""
        try:
            fields = self.cardinal.account.get_lot_fields(int(lid))
        except Exception:
            raise ShopError(f"Не удалось прочитать лот {lid} с FunPay. Проверьте, что это ваш лот.") from None
        if getattr(fields, "auto_delivery", False):
            raise ShopError("У лота включена встроенная автовыдача FunPay. Отключите её, чтобы не выдать товар дважды.")
        title = (getattr(fields, "title_ru", "") or getattr(fields, "title_en", "") or "").strip()
        params = []
        try:
            response = self.cardinal.account.method("get", f"lots/offerEdit?offer={lid}", {"accept": "*/*"}, {}, raise_not_200=True)
            params = selected_conditions(response.content.decode("utf-8"))
        except Exception:
            pass
        standardized = not title and bool(params)
        if standardized:
            conditions = [f"{key}={label}" for key, label in params][:MAX_CONDITIONS]
            name = ", ".join(label for _, label in params)[:120]
        else:
            conditions = [title] if title else []
            name = title[:120] or f"Лот {lid}"
        return {"id": str(lid), "name": name, "title": title, "standardized": standardized, "conditions": conditions,
                "params": [label for _, label in params], "subcategory": getattr(getattr(fields, "subcategory", None), "id", None)}

    def find_item(self, pid, iid):
        product = next((p for p in self.products() if str(p.get("id")) == str(pid)), None)
        item = next((i for i in product_items(product or {}) if str(i["id"]) == str(iid)), None)
        return product, item

    def bind(self, lid, pid, iid):
        """Bind a lot to an AppRoute item; an existing lot keeps its name, conditions and auto-price."""
        source = self.store.get("lot", lid) or self.store.get("draft", lid)
        if not source:
            raise ShopError("Лот не найден. Начните добавление заново.")
        product, cached = self.find_item(pid, iid)
        try:
            item = self.api().item(pid, iid)
        except APIError as exc:
            if is_transient(exc) or isinstance(exc, RateLimitError) or cached is None:
                raise ShopError("Поставщик не подтвердил этот номинал: " + str(exc)) from None
            item = cached
        if str(item.get("id", iid)) != str(iid):
            raise ShopError("Поставщик вернул другой номинал.")
        existing = self.store.get("lot", lid)
        lot = existing or {"id": str(lid), "auto_price": True}
        if not existing:
            for key in ("name", "title", "standardized", "conditions", "params", "subcategory"):
                lot[key] = source.get(key)
        lot.update(product_id=str(pid), item_id=str(iid), product_name=str((product or {}).get("name", item.get("productName", ""))),
                   item_name=str(item.get("name", "")), category=category_of(product or {}), last_cost=str(item.get("price", "")),
                   disabled_kind="", disabled_reason="", sync_error="")
        self.store.put("lot", lid, lot)
        self.store.delete("draft", lid)
        self.remember_item(lot, item)
        return lot, item

    def reread(self, lid):
        lot = self.store.get("lot", lid)
        if not lot:
            raise ShopError("Лот не найден.")
        info = self.read_lot(lid)
        lot.update({k: info[k] for k in ("name", "title", "standardized", "conditions", "params", "subcategory")})
        self.store.put("lot", lid, lot)
        return lot


# --- catalogue search ---------------------------------------------------------------------------

SYNONYMS = {
    "стим": "steam", "плейстейшн": "playstation", "плейстейшен": "playstation", "псн": "psn", "пс": "playstation",
    "иксбокс": "xbox", "хбокс": "xbox", "ксбокс": "xbox", "эпл": "apple", "эппл": "apple", "апл": "apple",
    "айтюнс": "itunes", "гугл": "google", "плей": "play", "роблокс": "roblox", "робуксы": "robux", "робукс": "robux",
    "фортнайт": "fortnite", "вбаксы": "v-bucks", "нинтендо": "nintendo", "свитч": "switch", "близзард": "blizzard",
    "батлнет": "battle", "амазон": "amazon", "нетфликс": "netflix", "спотифай": "spotify", "твич": "twitch",
    "валорант": "valorant", "лига": "league", "пабг": "pubg", "фрифаер": "free", "разер": "razer", "голд": "gold",
    "бинанс": "binance", "озон": "ozon", "тикток": "tiktok", "тиндер": "tinder", "есим": "esim",
    "иесим": "esim", "офис": "office", "виндовс": "windows", "адоб": "adobe", "геймпасс": "game", "гейм": "game",
    "пасс": "pass", "подарочная": "gift", "гифт": "gift", "карта": "card", "ключ": "key", "аккаунт": "account",
    "турция": "turkey", "турецкий": "turkey", "сша": "usa", "америка": "usa", "европа": "europe", "евро": "eur",
    "россия": "russia", "рф": "russia", "казахстан": "kazakhstan", "индия": "india", "польша": "poland",
    "германия": "germany", "великобритания": "uk", "англия": "uk", "бразилия": "brazil", "аргентина": "argentina",
    "оаэ": "uae", "япония": "japan", "китай": "china", "украина": "ukraine", "азия": "asia", "мир": "global",
    "глобал": "global", "рубли": "rub", "рублей": "rub", "лир": "try", "лиры": "try", "долларов": "usd",
    "доллар": "usd", "гб": "gb", "дней": "days", "дня": "days",
}
SEARCH_STOP = {"для", "без", "и", "в", "на", "с", "по", "the", "and", "for", "of", "моментально", "быстро",
               "дешево", "авто", "автовыдача", "мгновенно", "моментальная", "выдача", "лот", "шт", "код", "коды",
               "купить", "лучшая", "цена", "гарантия", "24", "7", "онлайн", "online", "fast", "instant"}


def search_tokens(query):
    tokens = []
    for word in normalized(query).split():
        if word in SEARCH_STOP or len(word) < 2 and not word.isdigit():
            continue
        tokens.append(word)
        if word in SYNONYMS:
            tokens.append(SYNONYMS[word])
    return list(dict.fromkeys(tokens))


def search_items(products, query, limit=60):
    """Ranked (product, item) pairs by product name, denomination and category."""
    tokens = search_tokens(query)
    if not tokens:
        return []
    scored = []
    for product in products:
        base = normalized(str(product.get("name", "")) + " " + category_of(product))
        for item in product_items(product):
            words = set((base + " " + normalized(item.get("name", ""))).split())
            score = 0.0
            for token in tokens:
                if token in words:
                    score += 2 if token.isdigit() else 1.5 if len(token) > 3 else 1
                elif len(token) >= 4 and not token.isdigit() and any(w.startswith(token) or token.startswith(w) and len(w) >= 4 for w in words):
                    score += 0.5
            if score:
                try:
                    price = float(item.get("price") or 0)
                except (TypeError, ValueError):
                    price = 0
                scored.append((-score, not available(item), str(product.get("name", "")), price, product, item))
    scored.sort(key=lambda x: x[:4])
    return [(p, i) for *_, p, i in scored[:limit]]


GUIDE = [
    ("Запуск", "<b>🛒 AppRoute Shop · быстрый старт</b>\n\n"
     "Плагин продаёт готовые коды, гифт-карты, ключи и eSIM из магазина AppRoute: после оплаты бот сам покупает номинал "
     "и присылает данные покупателю в чат FunPay. Прямое пополнение по логину/UID — в плагине «AppRoute Direct Top-Up».\n\n"
     "<b>Шаг 0.</b> /menu → Система и конфиги → Плагины → AppRoute Shop (Auto) → «Включить». Обязательно /restart. "
     "После запуска — /approute_shop.\n\n"
     "1. Зарегистрируйтесь на approute.ru/auth и пополните баланс (USDT).\n"
     "2. Создайте API-ключ (страница 2 инструкции).\n"
     "3. 🔑 Доступ к API → ключ, регион, прокси → «🧪 Проверить ключ».\n"
     "4. 🗂 Каталог → «🔄 Обновить каталог сейчас».\n"
     "5. 💲 Ценообразование → курс, наценка, границы цен.\n"
     "6. Создайте лот на FunPay → 📦 Лоты → ➕ Добавить лот.\n"
     "7. Включите «🔄 Автообновление» цен.\n"
     "8. При желании измените «✉️ Шаблон выдачи».\n"
     "9. В главном меню нажмите «Статус», чтобы стало 🟢.\n\n"
     "⚠️ Включение плагина в списке плагинов и «Статус» автовыдачи — разные вещи. По умолчанию «Статус» 🔴: заказы не обрабатываются."),
    ("API-ключ", "<b>🔑 Ключ AppRoute</b>\n\n"
     "1. approute.ru/api-access?tab=docs → «Создать постоянный ключ».\n"
     "2. Права: каталог, создание заказов, просмотр баланса.\n"
     "3. <b>Поле IP</b>: IP, с которого бот выходит в интернет, — IP прокси бота (или отдельного прокси плагина). "
     "Без прокси — IP сервера. Запросы с других IP поставщик отклонит.\n"
     "4. Скопируйте ключ.\n\n"
     "В панели: 🔑 Доступ к API → «✏️ Изменить ключ» и отправьте ключ сообщением — бот удалит его из чата.\n"
     "«🌍 Регион»: RU — approute.ru, IO — approute.io (по умолчанию). Выберите тот, где создан ключ.\n"
     "«🌐 Прокси: от бота» — прокси аккаунта FunPay; «свой» — отдельный прокси только для AppRoute "
     "(ip:порт или логин:пароль@ip:порт, «-» — удалить). Если «свой» не задан, используется прокси бота.\n\n"
     "Ключ не проходит проверку? Проверьте регион, IP в белом списке, права ключа и лишние пробелы."),
    ("Каталог и лоты", "<b>🗂 Каталог и 📦 привязка лотов</b>\n\n"
     "Каталог — все товары AppRoute, кроме прямого пополнения. Обновляется сам раз в несколько часов; цены и остатки "
     "привязанных товаров сверяются примерно раз в 30 минут и перед каждой покупкой.\n\n"
     "Привязка: 📦 Лоты → ➕ Добавить лот → ID лота или ссылка https://funpay.com/lots/offer?id=… Бот прочитает название "
     "(или параметры стандартизованного лота) и откроет 🎯 Автоподбор. Кнопка: «Название — номинал | $ ≈ ₽», "
     "🚫 — нет в наличии. Нажмите нужную — лот привязан.\n"
     "Не нашлось? «🔍 Новый поиск» (steam 500, roblox, playstation турция) или «🗂 Обзор по категориям»: "
     "Категория → Товар → Номинал.\n\n"
     "Стандартизованный лот (параметры из выпадающих списков) опознаётся по всем выбранным параметрам.\n"
     "Повторное добавление того же ID заменяет привязку, настройки лота сохраняются."),
    ("Карточка лота", "<b>⚙️ Карточка лота</b>\n\n"
     "🔁 Перепривязать — новый поиск товара (название, условия и автоцена сохраняются).\n"
     "🆔 product_id / item_id — ручная привязка в формате product_id:item_id.\n"
     "🎁 Изменить название — название в панели, уведомлениях и {lot_name}.\n"
     "♻️ Перечитать лот с FunPay — заново прочитать название и параметры (ручные название и условия заменятся).\n"
     "🔎 Условия определения заказа — фразы через запятую или с новой строки (до 8). Заказ засчитывается, если найдены "
     "все фразы; числа сравниваются точно (100 ≠ 1000). При нескольких подходящих лотах выигрывает лот с большим числом "
     "и длиной совпавших фраз. «-» — определять по названию лота.\n"
     "🔄 Автоцена — участие лота в обновлении цен и остатков (⏸ в списке — выключена).\n"
     "🗑️ Удалить лот — сразу, без подтверждения. Лот на FunPay остаётся: отключите его вручную при необходимости."),
    ("Цены", "<b>💲 Ценообразование</b>\n\n"
     "Цена лота = цена в USDT × курс USDT/RUB × (1 + наценка %), затем ограничивается мин. и макс. ценой.\n"
     "Пример: 10 USDT × 100 × 1,2 = 1200 ₽.\n\n"
     "⏱️ Интервал — как часто обновлять лоты (не меньше 20 мин).\n"
     "🔢 Лимит на заказ — максимум единиц в заказе для автовыдачи и максимум остатка на лоте (по умолчанию 50).\n"
     "🔄 Автообновление — фоновое обновление цен и остатков (по умолчанию выключено).\n"
     "📦 Синхр. остатков — количество на лоте = остаток поставщика; нет товара — лот отключается.\n"
     "♻️ Автовключение — вернувшийся товар снова включает лот, отключённый ботом из-за отсутствия.\n"
     "💬 Уведомления о смене цены, 👛 порог низкого баланса (USDT), 🛡 предохранитель сумм и его порог ×N.\n"
     "⚡ Обновить цены сейчас — сверить с поставщиком и пересчитать всё немедленно.\n\n"
     "Цена меняется, только если разница не меньше 0,5 ₽ (для очень дешёвых лотов — 5 %). Товары, которые поставщик "
     "генерирует под заказ, не отключаются из-за нулевого остатка."),
    ("Защита", "<b>🛡 Защитные механизмы</b>\n\n"
     "🔢 Лимит на заказ: больше единиц — выдача вручную, уведомление, авто-возврат (если включён).\n"
     "👛 Баланс проверяется раз в 10 минут; при нехватке на заказ бот отключает все лоты плагина и возвращает деньги. "
     "После пополнения включите лоты вручную.\n"
     "🛡 Предохранитель сумм: если закупка дороже оплаты покупателя (в USDT по вашему курсу) более чем в N раз "
     "(+≈0,5 USDT запаса), выдача останавливается и ждёт вас. Возврат не делается.\n"
     "↩️ Авто-возврат — только если товар физически не куплен (нет товара, нет денег, отказ, сбой). При долгом ожидании "
     "поставщика и срабатывании предохранителя деньги автоматически не возвращаются.\n"
     "🔒 Один заказ FunPay — одна покупка (номер fpc-номерЗаказа): повторное событие, сбой сети или перезапуск не спишут деньги дважды."),
    ("Выдача", "<b>✉️ Шаблон и выдача</b>\n\n"
     "Переменные шаблона: {lot_name}, {quantity}, {codes} (обязательна), {order_id}. Другие фигурные скобки не используйте — "
     "иначе бот отправит стандартный текст.\n"
     "{codes}: «Ваши коды» (нумерация, S/N, срок действия), «Данные eSIM» (QR, SM-DP+, Activation code, ICCID, ссылки "
     "для iOS/Android) и дополнительные данные поставщика.\n\n"
     "Порядок: оплата → сопоставление с лотом → лимит, наличие, баланс → предохранитель → покупка → ожидание кодов "
     "(до 10 минут) → выдача в чат. Несколько заказов одного покупателя идут по очереди.\n"
     "Поставщик дольше 10 минут — покупатель получает просьбу подождать, вы — уведомление; бот продолжает проверять и выдаст коды сам.\n"
     "Частичное выполнение — выдаётся полученное, вам приходит пометка для ручной проверки."),
    ("Вопросы", "<b>❓ Частые вопросы</b>\n\n"
     "<b>Бот не реагирует на заказы.</b> Был ли /restart после включения? «Статус» 🟢? Проходит ли «🧪 Проверить ключ»? "
     "Привязан ли лот и совпадают ли условия определения заказа?\n"
     "<b>Каталог пуст.</b> Задайте ключ и нажмите «🔄 Обновить каталог сейчас».\n"
     "<b>Цены не меняются.</b> Включено ли «🔄 Автообновление», нет ли ⏸ у лота, не велик ли интервал? «⚡ Обновить цены сейчас».\n"
     "<b>Лот отключён.</b> Нет товара — включится сам (автообновление + автовключение). Нет денег / лимит ключа — "
     "пополните и включите лоты на FunPay вручную.\n"
     "<b>Покупатель не получил коды.</b> Найдите заказ в «📜 История»: если он там — коды отправлены. Иначе смотрите "
     "уведомление об ошибке; номер покупки fpc-номерЗаказа можно назвать поддержке AppRoute.\n"
     "<b>Несколько лотов на один номинал</b> — можно.\n\n"
     "Уведомления приходят в чаты, где отправлена /approute_shop."),
]


class TelegramUI:
    def __init__(self, shop):
        from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
        self.Button, self.Markup = InlineKeyboardButton, InlineKeyboardMarkup
        self.shop, self.store = shop, shop.store
        self.tg, self.bot = shop.cardinal.telegram, shop.cardinal.telegram.bot
        self.tokens, self.pending = {}, {}
        self.lock = threading.RLock()

    # --- rendering ------------------------------------------------------------------------------

    def allowed(self, user):
        users = getattr(self.tg, "authorized_users", {}) or {}
        plugin = getattr(self.shop.cardinal, "plugins", {}).get(UUID)
        return (int(user.id) in users or str(user.id) in users) and getattr(plugin, "enabled", True)

    def markup(self, rows):
        kb = self.Markup()
        for row in rows:
            buttons = []
            for label, action in row:
                if isinstance(action, str) and action.startswith("https://"):
                    buttons.append(self.Button(label, url=action))
                elif isinstance(action, dict):
                    buttons.append(self.Button(label, callback_data=action["cb"]))
                else:
                    token = uuid.uuid4().hex[:16]
                    with self.lock:
                        if len(self.tokens) > 4000:
                            self.tokens = dict(list(self.tokens.items())[-2000:])
                        self.tokens[token] = (time.time(), action)
                    buttons.append(self.Button(label, callback_data=PREFIX + token))
            kb.row(*buttons)
        return kb

    def show(self, chat, text, rows=None, mid=None):
        rows = rows or [[("🔙 В меню", ("home",))]]
        kwargs = {"reply_markup": self.markup(rows), "parse_mode": "HTML", "disable_web_page_preview": True}
        if mid:
            try:
                return self.bot.edit_message_text(text, chat, mid, **kwargs)
            except Exception as exc:
                # Same text and buttons: Telegram refuses the edit; do not send a duplicate menu.
                if "message is not modified" in str(exc):
                    return None
        return self.bot.send_message(chat, text, **kwargs)

    @staticmethod
    def cbt():
        try:
            from tg_bot import CBT
            return CBT
        except Exception:
            return None

    @classmethod
    def settings_prefix(cls):
        """Callback of Cardinal's «⚙️ Настройки» button on the plugin card: 47:UUID:offset."""
        return f"{getattr(cls.cbt(), 'PLUGIN_SETTINGS', None) or '47'}:{UUID}"

    def plugins_button(self):
        cbt = self.cbt()
        if cbt is not None and getattr(cbt, "PLUGINS_LIST", None):
            return ("◀️ В плагины", {"cb": f"{cbt.PLUGINS_LIST}:0"})
        return ("◀️ В плагины", ("plugins",))

    def nav(self, route, page, total, size):
        row = []
        if page > 0:
            row.append(("◀️", (*route, page - 1)))
        if (page + 1) * size < total:
            row.append(("▶️", (*route, page + 1)))
        return [row] if row else []

    @staticmethod
    def page_of(page, total, size):
        return min(max(0, int(page)), max(0, (total - 1) // size))

    def rub(self, item):
        try:
            return calculate_price(unit_cost(item), self.store.settings())
        except ShopError:
            return "—"

    def item_label(self, product, item, with_product=True):
        mark = "" if available(item) else "🚫 "
        name = (str(product.get("name", "")) + " — " if with_product else "") + str(item.get("name", item["id"]))
        price = item.get("price", "—")
        if with_product:
            return f"{mark}{name} | {price} $ ≈ {self.rub(item)} ₽"
        stock = "∞" if on_demand(item) else stock_count(item)
        return f"{mark}{name} | {price} $ ≈ {self.rub(item)} ₽ (ост. {stock if stock is not None else '—'})"

    def lot_or_draft(self, lid):
        lot = self.store.get("lot", lid) or self.store.get("draft", lid)
        if not lot:
            raise ShopError("Лот не найден. Начните добавление заново: 📦 Лоты → ➕ Добавить лот.")
        return lot

    # --- screens --------------------------------------------------------------------------------

    def home(self, chat, mid=None):
        s = self.store.settings()
        products = self.shop.products()
        items = sum(len(product_items(p)) for p in products)
        lots = [l for l in self.store.all("lot") if l.get("item_id")]
        balance = f"{money(s['balance'])} USDT" if s["balance"] is not None else "неизвестно"
        text = (f"🛒 <b>{NAME}</b> <code>v{VERSION}</code>\n\n"
                f"Статус: {'🟢 автовыдача включена' if s['enabled'] else '🔴 автовыдача выключена'}\n"
                f"API-ключ: {'✅ задан' if s['api_key'] else '❌ не задан'} ({s['region']})\n"
                f"Баланс: {balance}\n"
                f"Каталог: {len(products)} товаров · {items} номиналов\n"
                f"Лотов привязано: {len(lots)}\n"
                f"Успешных продаж: {s.get('total_sales', 0)}\n\n"
                f"Автор: <a href='{SUPPORT_URL}'>{CREDITS}</a>")
        on = lambda key: "🟢" if s[key] else "🔴"
        rows = [[(f"Статус {on('enabled')}", ("toggle", "enabled", "home")),
                 (f"Уведомления {'🔔' if s['notifications'] else '🔕'}", ("toggle", "notifications", "home"))],
                [(f"Авто-возврат {on('auto_refund')}", ("toggle", "auto_refund", "home"))],
                [("🔑 Доступ к API", ("api",)), ("🔄 Обновить баланс", ("balance", "home"))],
                [("📦 Лоты", ("lots", 0)), ("💲 Ценообразование", ("pricing",))],
                [("🗂 Каталог", ("catalog",)), ("✉️ Шаблон выдачи", ("template",))],
                [("📊 Статистика", ("stats",)), ("📜 История", ("history", 0))],
                [("📖 Инструкция", ("guide", 0)), ("👤 Автор", SUPPORT_URL)],
                [self.plugins_button()]]
        self.show(chat, text, rows, mid)

    def api_menu(self, chat, mid=None):
        s = self.store.settings()
        key = s["api_key"]
        masked = (key[:4] + "••••" + key[-4:]) if len(key) > 12 else ("••••" if key else "не задан")
        text = (f"🔑 <b>Доступ к API</b>\n\nКлюч: <code>{esc(masked)}</code>\nРегион: {s['region']}\n"
                f"Адрес API: https://approute.{s['region'].lower()}/api/v1\n"
                f"Режим прокси: {'от бота' if s['proxy_mode'] == 'bot' else 'свой'}"
                + (" (не задан — используется прокси бота)" if s["proxy_mode"] == "own" and not s["proxy"] else "")
                + f"\nСвой прокси: {esc(masked_proxy(s['proxy']))}\n\n"
                "⚠️ IP прокси (или сервера без прокси) должен быть в белом списке ключа в кабинете AppRoute.")
        rows = [[("✏️ Изменить ключ", ("ask", "api_key", "")), (f"🌍 Регион: {s['region']}", ("region",))],
                [("🧪 Проверить ключ", ("balance", "api"))],
                [(f"🌐 Прокси: {'от бота' if s['proxy_mode'] == 'bot' else 'свой'}", ("proxy_mode",)),
                 ("✏️ Установить свой прокси", ("ask", "proxy", ""))],
                [("◀️ Назад", ("home",))]]
        self.show(chat, text, rows, mid)

    def catalog_menu(self, chat, mid=None, note=""):
        s = self.store.settings()
        products = self.shop.products()
        categories = {}
        for p in products:
            categories[category_of(p)] = categories.get(category_of(p), 0) + 1
        top = sorted(categories.items(), key=lambda x: (-x[1], x[0]))[:12]
        hours = s["catalog_interval"] / 60
        text = (f"🗂 <b>Каталог AppRoute</b>\n\nТоваров: {len(products)}\nНоминалов: {sum(len(product_items(p)) for p in products)}\n"
                f"Категорий: {len(categories)}\n" + "".join(f"• {esc(name)} — {count}\n" for name, count in top)
                + f"\nОбновлён: {ago(s['catalog_at'])}\nАвтообновление: каждые {hours:g} ч"
                + ("\n⏳ Загрузка продолжается…" if self.store.get("catalog", "progress") is not None else "")
                + (f"\n\n{note}" if note else ""))
        self.show(chat, text, [[("🔄 Обновить каталог сейчас", ("refresh_catalog",))], [("🔙 Назад", ("home",))]], mid)

    def lots(self, chat, page=0, mid=None):
        lots = sorted([l for l in self.store.all("lot") if l.get("item_id")], key=lambda l: str(l.get("name", "")).casefold())
        page = self.page_of(page, len(lots), 6)
        rows = [[((("⏸ " if not l.get("auto_price", True) else "") + f"🎁 {l.get('name', '')}")[:50] + f" (ID: {l['id']})", ("lot", l["id"]))]
                for l in lots[page * 6:(page + 1) * 6]]
        rows += self.nav(("lots",), page, len(lots), 6)
        rows += [[("➕ Добавить лот", ("ask", "lot_id", ""))], [("🔙 Назад", ("home",))]]
        text = (f"📦 <b>Привязанные лоты</b>: {len(lots)}" + (f" · стр. {page + 1}/{(len(lots) - 1) // 6 + 1}" if lots else "")
                + ("\n\nПока нет привязанных лотов. Создайте лот на FunPay и нажмите «➕ Добавить лот»." if not lots else
                   "\n\n⏸ — автоцена выключена."))
        self.show(chat, text, rows, mid)

    def require_catalog(self, chat, mid):
        if self.shop.products():
            return True
        self.show(chat, "🗂 Каталог пуст — привязать лот пока нельзя.\nПроверьте, что задан API-ключ, и загрузите каталог.",
                  [[("🔄 Загрузить каталог", ("refresh_catalog",))], [("🔑 Доступ к API", ("api",)), ("📦 Лоты", ("lots", 0))]], mid)
        return False

    def results(self, chat, lid, query, page=0, mid=None, mode="suggest"):
        if not self.require_catalog(chat, mid):
            return
        lot = self.lot_or_draft(lid)
        s = self.store.settings()
        if mode == "suggest":
            query = lot.get("title") or " ".join(lot.get("params") or []) or lot.get("name", "")
        found = search_items(self.shop.products(), query)
        page = self.page_of(page, len(found), 8)
        title = "🎯 <b>Автоподбор</b>" if mode == "suggest" else "🔍 <b>Поиск</b>"
        note = ""
        if lot.get("standardized"):
            note = ("\nℹ️ Это стандартизованный лот (без собственного названия). Заказы буду опознавать по параметрам: "
                    + esc(", ".join(lot.get("params") or [])) + ". Заказ засчитывается, только если совпали все параметры.\n")
        text = (f"{title}\nЛот: {esc(lot.get('name', ''))} (ID {esc(lid)})\nЗапрос: {esc(query or '—')}\n"
                f"Найдено: {len(found)}\nКурс: {esc(s['rate'])} ₽ · наценка {esc(s['markup'])}%\n{note}\n"
                + ("Нажмите на номинал, чтобы привязать лот. 🚫 — нет в наличии." if found else
                   "Ничего не найдено. Попробуйте запрос покороче (только название игры или сервиса), на английском, "
                   "или выберите товар по категориям."))
        rows = [[(self.item_label(p, i)[:64], ("bind", lid, str(p["id"]), str(i["id"])))] for p, i in found[page * 8:(page + 1) * 8]]
        rows += self.nav(("results", lid, query, mode), page, len(found), 8) if mode != "suggest" else self.nav(("suggest", lid), page, len(found), 8)
        rows += [[("🔍 Новый поиск", ("ask", "search", lid)), ("🗂 Обзор по категориям", ("categories", lid, 0))],
                 [("❌ Отмена", ("cancel", lid))]]
        self.show(chat, text, rows, mid)

    def methods(self, chat, lid, mid=None):
        lot = self.lot_or_draft(lid)
        self.show(chat, f"🔗 <b>Привязка лота</b>\n{esc(lot.get('name', ''))} (ID {esc(lid)})\n\nКак найти товар AppRoute?",
                  [[("🎯 Автоподбор по названию лота", ("suggest", lid, 0))], [("🔍 Поиск по строке", ("ask", "search", lid))],
                   [("🗂 Обзор по категориям", ("categories", lid, 0))], [("❌ Отмена", ("cancel", lid))]], mid)

    def categories(self, chat, lid, page=0, mid=None):
        if not self.require_catalog(chat, mid):
            return
        counts = {}
        for p in self.shop.products():
            counts[category_of(p)] = counts.get(category_of(p), 0) + 1
        names = sorted(counts, key=str.casefold)
        page = self.page_of(page, len(names), 10)
        rows = [[(f"{name} ({counts[name]})"[:64], ("category", lid, name, 0))] for name in names[page * 10:(page + 1) * 10]]
        rows += self.nav(("categories", lid), page, len(names), 10)
        rows += [[("⬅️ Назад", ("methods", lid)), ("❌ Отмена", ("cancel", lid))]]
        self.show(chat, f"🗂 <b>Категории</b>: {len(names)} · стр. {page + 1}\nВ скобках — число товаров.", rows, mid)

    def category(self, chat, lid, name, page=0, mid=None):
        products = sorted([p for p in self.shop.products() if category_of(p) == name], key=lambda p: str(p.get("name", "")).casefold())
        page = self.page_of(page, len(products), 8)
        rows = [[(f"{p.get('name', p['id'])} ({len(product_items(p))})"[:64], ("product", lid, name, str(p["id"]), 0))]
                for p in products[page * 8:(page + 1) * 8]]
        rows += self.nav(("category", lid, name), page, len(products), 8)
        rows += [[("⬅️ К категориям", ("categories", lid, 0)), ("❌ Отмена", ("cancel", lid))]]
        self.show(chat, f"🗂 <b>{esc(name)}</b>: {len(products)} товаров · стр. {page + 1}\nВ скобках — число номиналов.", rows, mid)

    def product(self, chat, lid, name, pid, page=0, mid=None):
        product = next((p for p in self.shop.products() if str(p.get("id")) == pid), None)
        if product is None:
            raise ShopError("Товар не найден. Обновите каталог.")
        items = product_items(product)
        if not items:
            # Some catalogue pages list products without denominations; read the product itself.
            items = product_items(self.shop.api().service(pid))
        page = self.page_of(page, len(items), 8)
        rows = [[(self.item_label(product, i, False)[:64], ("bind", lid, pid, str(i["id"])))] for i in items[page * 8:(page + 1) * 8]]
        rows += self.nav(("product", lid, name, pid), page, len(items), 8)
        rows += [[("⬅️ К товарам", ("category", lid, name, 0)), ("❌ Отмена", ("cancel", lid))]]
        self.show(chat, f"🎁 <b>{esc(product.get('name', pid))}</b>\nНоминалов: {len(items)}\n"
                        f"Цена ₽ — по вашему курсу и наценке. 🚫 — нет в наличии.", rows, mid)

    def lot(self, chat, lid, mid=None):
        lot = self.store.get("lot", lid)
        if not lot:
            raise ShopError("Лот не найден.")
        s = self.store.settings()
        cached = self.shop.cached_item(lot) or {}
        item = cached.get("data") or {}
        try:
            cost = unit_cost(item) if item else dec(lot.get("last_cost") or "0")
            price = f"{money(cost)} USDT ≈ {calculate_price(cost, s)} ₽" if cost > 0 else "—"
        except ShopError:
            price = "—"
        if cached.get("missing"):
            stock = "❌ пропал у поставщика"
        elif item:
            count = "под заказ" if on_demand(item) else stock_count(item)
            stock = ("✅ в наличии" if available(item) else "🚫 нет в наличии") + (f" · остаток: {count}" if count is not None else "")
        else:
            stock = "нет данных"
        conditions = lot.get("conditions") or []
        text = (f"⚙️ <b>{esc(lot.get('name', ''))}</b>\nID лота: <code>{esc(lid)}</code> · "
                f"{'стандартизованный' if lot.get('standardized') else 'обычный'}\n\n"
                f"<b>Определение заказа:</b> " + (esc("; ".join(conditions)) if conditions else "по названию лота") + "\n\n"
                f"<b>Товар AppRoute:</b> {esc(lot.get('product_name', ''))} — {esc(lot.get('item_name', ''))}\n"
                f"product_id: <code>{esc(lot.get('product_id', '—'))}</code>\nitem_id: <code>{esc(lot.get('item_id', '—'))}</code>\n\n"
                f"<b>Цена:</b> {esc(price)}\n<b>Наличие:</b> {esc(stock)}\n"
                f"<b>Автоцена:</b> {'вкл' if lot.get('auto_price', True) else 'выкл'}"
                + (f"\nНа FunPay: {esc(lot['last_price'])} ₽" if lot.get("last_price") else "")
                + (f"\n⚠️ Отключён ботом: {esc(lot['disabled_reason'])}" if lot.get("disabled_reason") else "")
                + (f"\n⚠️ {esc(lot['sync_error'])}" if lot.get("sync_error") else ""))
        rows = [[("🔁 Перепривязать (поиск)", ("suggest", lid, 0))],
                [("🆔 Ввести product_id / item_id", ("ask", "ids", lid))],
                [("🎁 Изменить название", ("ask", "name", lid)), ("♻️ Перечитать лот с FunPay", ("reread", lid))],
                [("🔎 Условия определения заказа", ("ask", "conditions", lid))],
                [(f"🔄 Автоцена: {'вкл' if lot.get('auto_price', True) else 'выкл'}", ("lot_auto_price", lid))],
                [("🗑️ Удалить лот", ("delete_lot", lid)), ("🔗 На FunPay", self.shop.lot_url(lid))],
                [("⬅️ Назад", ("lots", 0))]]
        self.show(chat, text, rows, mid)

    def bound(self, chat, lid, lot, item, replaced, mid=None):
        s = self.store.settings()
        try:
            cost = unit_cost(item)
            price = f"{money(cost)} USDT ≈ {calculate_price(cost, s)} ₽"
        except ShopError as exc:
            price = str(exc)
        count = "под заказ" if on_demand(item) else stock_count(item)
        text = (f"✅ <b>Лот привязан!</b>\n{esc(lot.get('name', ''))} (ID {esc(lid)})\n\n"
                f"Товар: {esc(lot.get('product_name', ''))} — {esc(lot.get('item_name', ''))}\n"
                f"Себестоимость и цена лота: {esc(price)}\n"
                f"Остаток: {esc(count if count is not None else '—')}" + ("" if available(item) else " · 🚫 нет в наличии") + "\n"
                f"product_id: <code>{esc(lot['product_id'])}</code> · item_id: <code>{esc(lot['item_id'])}</code>"
                + ("\n\nПривязка заменена, настройки лота сохранены." if replaced else "")
                + ("" if s["enabled"] else "\n\n🔴 Автовыдача выключена — включите «Статус» в главном меню.")
                + ("" if s["auto_price"] else "\n💡 Включите «🔄 Автообновление» в «💲 Ценообразование», чтобы цена на FunPay ставилась сама."))
        self.show(chat, text, [[("⚙️ Карточка лота", ("lot", lid)), ("📦 Лоты", ("lots", 0))], [("🔙 В меню", ("home",))]], mid)

    def pricing(self, chat, mid=None):
        s = self.store.settings()
        flag = lambda key: "🟢" if s[key] else "🔴"
        example = calculate_price("10", s)
        text = (f"💲 <b>Ценообразование</b>\n\n<b>Цена лота = цена в USDT × курс × (1 + наценка)</b>\n"
                f"Пример: 10 USDT → {example} ₽\n\n"
                f"Наценка: {esc(s['markup'])}%\nКурс USDT/RUB: {esc(s['rate'])}\n"
                f"Мин. / макс. цена: {esc(s['min_price'])} / {esc(s['max_price'])} ₽\n"
                f"Интервал: {s['interval']} мин · лимит на заказ: {s['max_quantity']}\n"
                f"Автообновление: {flag('auto_price')} · синхр. остатков: {flag('sync_stock')} · автовключение: {flag('auto_enable')}\n"
                f"Уведомления о смене цены: {flag('price_notify')}\n"
                f"Порог низкого баланса: {esc(s['low_balance'])} USDT\n"
                f"Предохранитель сумм: {flag('sanity_enabled')}" + (f" ×{esc(s['sanity_ratio'])}" if s["sanity_enabled"] else "") + "\n\n"
                f"Данные поставщика по лотам: {ago(s['items_at'])}\nЦены на FunPay: {ago(s['sync_at'])}")
        rows = [[("💹 Наценка", ("ask", "markup", "")), (f"💵 Курс: {s['rate']}", ("ask", "rate", ""))],
                [("📉 Мин. цена", ("ask", "min_price", "")), ("📈 Макс. цена", ("ask", "max_price", ""))],
                [("⏱️ Интервал", ("ask", "interval", "")), ("🔢 Лимит на заказ", ("ask", "max_quantity", ""))],
                [(f"🔄 Автообновление {flag('auto_price')}", ("toggle", "auto_price", "pricing")),
                 (f"📦 Синхр. остатков {flag('sync_stock')}", ("toggle", "sync_stock", "pricing"))],
                [(f"♻️ Автовключение {flag('auto_enable')}", ("toggle", "auto_enable", "pricing")),
                 (f"💬 Уведомл. о смене цены {flag('price_notify')}", ("toggle", "price_notify", "pricing"))],
                [("👛 Порог низкого баланса", ("ask", "low_balance", "")),
                 (f"🛡 Предохранитель сумм {flag('sanity_enabled')}", ("toggle", "sanity_enabled", "pricing"))]]
        if s["sanity_enabled"]:
            rows.append([(f"📐 Порог предохранителя: x{s['sanity_ratio']}", ("ask", "sanity_ratio", ""))])
        rows += [[("⚡ Обновить цены сейчас", ("sync_now",))], [("🔙 Назад", ("home",))]]
        self.show(chat, text, rows, mid)

    def template(self, chat, mid=None):
        s = self.store.settings()
        text = ("✉️ <b>Шаблон выдачи</b>\n\nСообщение покупателю после успешной покупки:\n\n"
                f"<code>{esc(s['template'])}</code>\n\n"
                "Переменные: {lot_name} — товар, {quantity} — количество, {codes} — выданные данные (обязательна), "
                "{order_id} — номер заказа. Другие фигурные скобки не используйте.")
        rows = [[("✏️ Изменить шаблон", ("ask", "template", ""))]]
        if s["template"] != DEFAULT_TEMPLATE:
            rows.append([("♻️ Вернуть стандартный", ("reset_template",))])
        rows.append([("🔙 Назад", ("home",))])
        self.show(chat, text, rows, mid)

    def successful(self):
        return sorted(self.store.orders_in(SUCCESS_STATES), key=lambda r: r.get("finished", r["created"]), reverse=True)

    def stats(self, chat, mid=None):
        s, now, orders = self.store.settings(), time.time(), self.successful()
        lines = ["📊 <b>Статистика</b> (только успешные выдачи)\n", "<b>Выручка FunPay</b>"]
        for name, days in (("24 часа", 1), ("7 дней", 7), ("30 дней", 30)):
            values = [r for r in orders if r.get("finished", 0) >= now - days * 86400]
            lines.append(f"{name}: {len(values)} продаж · {money(sum((dec(r.get('revenue_rub', r.get('revenue', '0'))) for r in values), Decimal(0)))} ₽")
        lines.append("\n<b>Закупка</b>")
        for name, days in (("24 часа", 1), ("30 дней", 30)):
            values = [r for r in orders if r.get("finished", 0) >= now - days * 86400]
            lines.append(f"{name}: {money(sum((dec(r.get('cost', '0')) for r in values), Decimal(0)))} USDT")
        lines.append(f"\n<b>Итого</b>\nУспешных продаж: {s.get('total_sales', 0)}\n"
                     f"Выручка: {money(s.get('total_revenue', '0'))} ₽\nЗакупка: {money(s.get('total_cost', '0'))} USDT")
        self.show(chat, "\n".join(lines), [[("📜 История", ("history", 0)), ("🔙 Назад", ("home",))]], mid)

    def history(self, chat, page=0, mid=None):
        orders = self.successful()
        page = self.page_of(page, len(orders), 10)
        rows = [[(f"🎁 #{r['id']} | {money(r.get('revenue', '0'))} ₽ | x{r.get('quantity', 1)}" + (" ⚠️" if r["state"] == "partial" else ""),
                  ("order", r["id"], page))] for r in orders[page * 10:(page + 1) * 10]]
        rows += self.nav(("history",), page, len(orders), 10)
        rows.append([("🔙 Назад", ("home",))])
        text = (f"📜 <b>История</b>: {len(orders)} заказов" + (f" · стр. {page + 1}" if orders else "")
                + ("\n\nПока нет выполненных заказов." if not orders else "\nНовые сверху. ⚠️ — частичное выполнение."))
        self.show(chat, text, rows, mid)

    def order(self, chat, oid, page=0, mid=None):
        r = self.store.get("order", oid)
        if not r:
            raise ShopError("Заказ не найден.")
        finished = datetime.fromtimestamp(r.get("finished", r["created"])).strftime("%d.%m.%Y %H:%M:%S")
        text = (f"🎁 <b>Заказ #{esc(oid)}</b> · {esc(STATE_NAMES.get(r['state'], r['state']))}\n\n"
                f"Покупатель: {esc(r.get('buyer', '—'))}\nСумма на FunPay: {esc(r.get('revenue', '—'))} {esc(r.get('currency', 'RUB'))}\n"
                f"Товар: {esc(r.get('lot', {}).get('name', '—'))}" + (f" · {esc(r['item_name'])}" if r.get("item_name") else "") + "\n"
                f"Количество: {r.get('quantity', '—')}\nСписано: {esc(r.get('cost', '—'))} USDT\nВыдано кодов: {r.get('codes_count', 0)}\n"
                f"Номер покупки (reference): <code>{esc(r.get('reference', '—'))}</code>\n"
                f"Транзакция AppRoute: <code>{esc(r.get('provider_id') or '—')}</code>\n"
                + ("⚠️ Выполнен частично — проверьте вручную.\n" if r.get("partial") else "")
                + ("⚠️ Сообщение покупателю не доставлено.\n" if r.get("undelivered") else "")
                + f"Дата: {finished}")
        rows = [[("💙 FunPay", f"https://funpay.com/orders/{quote(oid, safe='')}/")]]
        if str(r.get("buyer_id", "")).isdigit():
            rows[0].append(("👤 Покупатель", f"https://funpay.com/users/{r['buyer_id']}/"))
        rows.append([("⬅️ Назад", ("history", page))])
        self.show(chat, text, rows, mid)

    def guide(self, chat, page=0, mid=None):
        page = int(page) % len(GUIDE)
        rows = [[(("• " if i == page else "") + title, ("guide", i)) for i, (title, _) in enumerate(GUIDE[j:j + 4], j)]
                for j in range(0, len(GUIDE), 4)]
        rows += [[("◀️", ("guide", (page - 1) % len(GUIDE))), ("▶️", ("guide", (page + 1) % len(GUIDE)))],
                 [("👤 Автор", SUPPORT_URL), ("📣 Канал", DEVELOPER_URL)], [("🔙 В меню", ("home",))]]
        self.show(chat, GUIDE[page][1] + f"\n\n<i>{page + 1}/{len(GUIDE)} · {NAME} v{VERSION} · {CREDITS}</i>", rows, mid)

    def plugin_card(self, chat, mid=None):
        cbt = self.cbt()
        back = ("◀️ Назад", {"cb": f"{cbt.EDIT_PLUGIN}:{UUID}:0"}) if cbt is not None and getattr(cbt, "EDIT_PLUGIN", None) else self.plugins_button()
        self.show(chat, f"🛒 <b>{NAME}</b> v{VERSION}\n{esc(DESCRIPTION)}\n\nАвтор: <a href='{SUPPORT_URL}'>{CREDITS}</a>\n"
                        f"Панель также открывается командой /{COMMAND}.",
                  [[("🛒 Открыть панель AppRoute Shop", ("home",))], [("📖 Инструкция", ("guide", 0))], [back]], mid)

    # --- input ----------------------------------------------------------------------------------

    PROMPTS = {
        "lot_id": ("➕ Добавить лот", "Отправьте ID лота FunPay или прямую ссылку на лот.\nНапример: 75790150 или "
                   "https://funpay.com/lots/offer?id=75790150\nСсылки на категорию или профиль не подходят."),
        "search": ("🔍 Поиск по строке", "Название товара, номинал, страна. Например: steam 500, roblox, playstation турция."),
        "api_key": ("✏️ API-ключ AppRoute", "Отправьте ключ одним сообщением — бот постарается удалить его из чата."),
        "proxy": ("✏️ Свой прокси", "ip:порт или логин:пароль@ip:порт. Отправьте «-», чтобы удалить свой прокси "
                  "и вернуться на прокси бота."),
        "markup": ("💹 Наценка, %", "Ваша прибыль поверх себестоимости, например 20."),
        "rate": ("💵 Курс USDT/RUB", "Внутренний курс для пересчёта цен и предохранителя, например 102.5."),
        "min_price": ("📉 Мин. цена, ₽", "Цена лота на FunPay никогда не станет ниже."),
        "max_price": ("📈 Макс. цена, ₽", "Цена лота на FunPay никогда не станет выше."),
        "interval": ("⏱️ Интервал, мин", "Как часто обновлять цены и остатки на FunPay. Минимум 20 минут."),
        "max_quantity": ("🔢 Лимит на заказ", "Максимум единиц в одном заказе для автовыдачи и максимум остатка на лоте."),
        "low_balance": ("👛 Порог низкого баланса, USDT", "Ниже этой суммы бот пришлёт экстренное уведомление."),
        "sanity_ratio": ("📐 Порог предохранителя", "Во сколько раз закупка может превышать оплату покупателя. Минимум 1."),
        "template": ("✉️ Шаблон выдачи", "Отправьте новый текст. Переменные: {lot_name}, {quantity}, {codes} (обязательна), "
                     "{order_id}. Другие фигурные скобки не используйте."),
        "name": ("🎁 Название товара", "Название в панели, уведомлениях и {lot_name} (1–120 символов)."),
        "ids": ("🆔 product_id / item_id", "Отправьте в формате product_id:item_id. Бот проверит позицию у поставщика."),
        "conditions": ("🔎 Условия определения заказа", "Фразы через запятую или с новой строки (до 8). Заказ засчитывается, "
                       "только если найдены все фразы; числа сравниваются точно. «-» — определять по названию лота."),
    }

    def prompt(self, chat, user, key, lid=""):
        title, hint = self.PROMPTS[key]
        current = ""
        if key == "conditions":
            lot = self.store.get("lot", lid) or {}
            current = "\n\nСейчас: " + ("; ".join(lot.get("conditions") or []) or "по названию лота")
        elif key in {"markup", "rate", "min_price", "max_price", "interval", "max_quantity", "low_balance", "sanity_ratio"}:
            current = f"\n\nСейчас: {self.store.settings()[key]}"
        sent = self.bot.send_message(chat, f"<b>{esc(title)}</b>\n\n{esc(hint + current)}\n\n"
                                           f"Отмена: /{CANCEL_COMMAND}", parse_mode="HTML")
        self.pending[(str(chat), str(user))] = (key, lid, getattr(sent, "message_id", None), time.time())

    def has_input(self, message):
        key = (str(message.chat.id), str(message.from_user.id))
        pending = self.pending.get(key)
        if not pending:
            return False
        if time.time() - pending[3] > 900:
            # An abandoned form must not capture the admin's later messages.
            self.pending.pop(key, None)
            return False
        text = message.text or ""
        # Other bot commands pass through to their own handlers.
        return not (text.startswith("/") and text.split("@")[0] != "/" + CANCEL_COMMAND)

    def message(self, message):
        if not self.allowed(message.from_user):
            return
        key = (str(message.chat.id), str(message.from_user.id))
        pending = self.pending.pop(key, None)
        if not pending:
            return
        name, lid, _, _ = pending
        chat = message.chat.id
        if (message.text or "").split("@")[0] == "/" + CANCEL_COMMAND:
            self.home(chat)
            return
        value = message.text or ""
        if name in {"api_key", "proxy"}:
            try:
                self.bot.delete_message(chat, message.message_id)
            except Exception:
                pass

        def action():
            try:
                result = self.apply_input(name, lid, value)
                if name == "lot_id":
                    if self.require_catalog(chat, None):
                        self.results(chat, result, "", 0)
                elif name == "search":
                    self.results(chat, lid, result, 0, mode="search")
                elif name == "ids":
                    lot, item = result
                    self.bound(chat, lid, lot, item, True)
                elif name in {"name", "conditions"}:
                    self.lot(chat, lid)
                elif name in {"api_key", "proxy"}:
                    self.api_menu(chat)
                elif name == "template":
                    self.template(chat)
                else:
                    self.pricing(chat)
            except Exception as exc:
                self.show(chat, "⚠️ " + esc(str(exc) if isinstance(exc, ShopError) else "Не удалось сохранить. Проверьте данные и повторите."),
                          [[("🔁 Ещё раз", ("ask", name, lid)), ("🔙 В меню", ("home",))]])
        self.shop.submit(action)

    def ensure_no_purchase_in_flight(self):
        if self.store.orders_in({"purchasing", "pending", "reveal"}):
            raise ShopError("Есть незавершённые покупки у поставщика. Дождитесь их завершения, затем меняйте ключ или регион.")

    def apply_input(self, key, lid, value):
        value = value.strip()
        s = self.store.settings()
        if key == "lot_id":
            info = self.shop.read_lot(lot_id_from_text(value))
            self.store.put("draft", info["id"], info)
            return info["id"]
        if key == "search":
            if not 1 <= len(value) <= 100:
                raise ShopError("Запрос: от 1 до 100 символов.")
            return value
        if key == "api_key":
            self.ensure_no_purchase_in_flight()
            if not 8 <= len(value) <= 512 or any(c.isspace() for c in value):
                raise ShopError("Некорректный формат ключа: без пробелов, от 8 символов.")
            self.store.configure(api_key=value, balance=None, balance_at=0, low_alert=False)
            return None
        if key == "proxy":
            self.store.configure(proxy="" if value == "-" else proxy_url(value), proxy_mode="bot" if value == "-" else "own")
            return None
        if key == "template":
            self.store.configure(template=validate_template(value))
            return None
        if key in {"name", "conditions", "ids"}:
            lot = self.store.get("lot", lid)
            if not lot:
                raise ShopError("Лот не найден.")
            if key == "ids":
                ids = value.replace(" ", ":").split(":")
                ids = [x for x in ids if x]
                if len(ids) != 2 or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", x) for x in ids):
                    raise ShopError("Нужно product_id:item_id.")
                return self.shop.bind(lid, ids[0], ids[1])
            if key == "name":
                if not 1 <= len(value) <= 120:
                    raise ShopError("Название: от 1 до 120 символов.")
                lot["name"] = value
            else:
                if value == "-":
                    lot["conditions"] = []
                else:
                    phrases = [x.strip() for x in re.split(r"[\n,]", value) if x.strip()]
                    if not phrases:
                        raise ShopError("Перечислите хотя бы одну фразу или отправьте «-».")
                    if len(phrases) > MAX_CONDITIONS:
                        raise ShopError(f"Не больше {MAX_CONDITIONS} фраз.")
                    lot["conditions"] = [p[:120] for p in phrases]
            self.store.put("lot", lid, lot)
            return None
        limits = {"markup": ("0", "10000"), "rate": ("0.000001", "1000000"), "min_price": ("0", "100000000"),
                  "max_price": ("0.01", "100000000"), "interval": (1, 10080), "max_quantity": (1, 100000),
                  "low_balance": ("0", "100000000"), "sanity_ratio": ("1", "1000")}
        if key not in limits:
            raise ShopError("Неизвестная настройка.")
        number = dec(value.replace("%", "").replace("₽", "").replace("x", "").replace("×", ""))
        low, high = limits[key]
        if key == "rate" and number <= 0:
            raise ShopError("Курс должен быть больше нуля.")
        if not dec(low) <= number <= dec(high):
            raise ShopError(f"Допустимый диапазон: {low}–{high}.")
        result = integer(number) if key in {"interval", "max_quantity"} else str(number.normalize() if number == number.to_integral_value() else number)
        if key == "interval":
            result = max(20, result)
        merged = {**s, key: result}
        if dec(merged["min_price"]) > dec(merged["max_price"]):
            raise ShopError("Минимальная цена не может быть больше максимальной.")
        self.store.configure(**{key: result})
        return None

    # --- telegram handlers ------------------------------------------------------------------------

    def command(self, message):
        if not self.allowed(message.from_user):
            return
        chats = self.store.settings()["notify_chats"]
        if message.chat.id not in chats:
            # The chat where the panel is opened receives the plugin notifications.
            self.store.configure(notify_chats=chats + [message.chat.id])
        self.pending.pop((str(message.chat.id), str(message.from_user.id)), None)
        self.home(message.chat.id)

    def callback(self, call):
        if not call.message or not self.allowed(call.from_user):
            return
        try:
            self.bot.answer_callback_query(call.id)
        except Exception:
            pass
        with self.lock:
            entry = self.tokens.get(call.data[len(PREFIX):])
        chat, user, mid = call.message.chat.id, call.from_user.id, call.message.message_id
        if not entry or time.time() - entry[0] > 86400:
            self.home(chat, mid)
            return
        action = entry[1]

        def run():
            try:
                self.dispatch(chat, user, action, mid)
            except (RequestDeferred, RateLimitError) as exc:
                self.show(chat, "⏳ " + esc(str(exc)), [[("🔙 В меню", ("home",))]], mid)
            except Exception as exc:
                if not isinstance(exc, ShopError):
                    LOG.warning("AppRoute Shop: panel action failed", exc_info=True)
                self.show(chat, "⚠️ " + esc(str(exc) if isinstance(exc, ShopError) else "Не удалось выполнить действие. Проверьте подключение."),
                          [[("🔙 В меню", ("home",))]], mid)
        # Keep HTTP out of the Telegram handler thread.
        self.shop.submit(run)

    def dispatch(self, chat, user, action, mid=None):
        op, *args = action
        s = self.store.settings()
        if op == "home":
            self.home(chat, mid)
        elif op == "toggle":
            key, screen = args
            if key not in {"enabled", "notifications", "auto_refund", "auto_price", "sync_stock", "auto_enable",
                           "price_notify", "sanity_enabled"}:
                raise ShopError("Неизвестный переключатель.")
            self.store.configure(**{key: not s[key]})
            if key == "enabled" and not s["enabled"] and not s["api_key"]:
                self.show(chat, "🟢 Автовыдача включена, но API-ключ не задан — заказы не будут выдаваться. "
                                "Откройте «🔑 Доступ к API».", [[("🔑 Доступ к API", ("api",)), ("🔙 В меню", ("home",))]], mid)
                return
            self.pricing(chat, mid) if screen == "pricing" else self.home(chat, mid)
        elif op == "api":
            self.api_menu(chat, mid)
        elif op == "region":
            self.ensure_no_purchase_in_flight()
            self.store.configure(region="IO" if s["region"] == "RU" else "RU", balance=None, balance_at=0,
                                 catalog_at=0, low_alert=False)
            self.store.delete("catalog", "progress")
            self.api_menu(chat, mid)
        elif op == "proxy_mode":
            self.store.configure(proxy_mode="own" if s["proxy_mode"] == "bot" else "bot")
            self.api_menu(chat, mid)
        elif op == "balance":
            balance = self.shop.api().balance()
            self.shop.record_balance(balance)
            if args and args[0] == "api":
                self.show(chat, f"✅ <b>Подключение к AppRoute успешно</b>\nРегион: {s['region']}\nБаланс: <b>{money(balance)} USDT</b>",
                          [[("◀️ Назад", ("api",)), ("🔙 В меню", ("home",))]], mid)
            else:
                self.home(chat, mid)
        elif op == "catalog":
            self.catalog_menu(chat, mid)
        elif op == "refresh_catalog":
            try:
                products, items = self.shop.refresh_catalog(chat)
            except (RequestDeferred, RateLimitError):
                self.catalog_menu(chat, mid, "⏳ Загрузка продолжается в фоне (AppRoute ограничивает частоту запросов). "
                                             "Когда закончу — пришлю сообщение.")
                return
            self.catalog_menu(chat, mid, f"✅ Каталог обновлён: загружено {products} товаров и {items} номиналов.")
        elif op == "lots":
            self.lots(chat, args[0], mid)
        elif op == "lot":
            self.lot(chat, args[0], mid)
        elif op == "suggest":
            self.results(chat, args[0], "", args[1], mid)
        elif op == "results":
            lid, query, mode, page = args
            self.results(chat, lid, query, page, mid, mode)
        elif op == "methods":
            self.methods(chat, args[0], mid)
        elif op == "categories":
            self.categories(chat, args[0], args[1], mid)
        elif op == "category":
            self.category(chat, *args, mid=mid)
        elif op == "product":
            self.product(chat, *args, mid=mid)
        elif op == "bind":
            lid, pid, iid = args
            replaced = self.store.get("lot", lid) is not None
            lot, item = self.shop.bind(lid, pid, iid)
            self.bound(chat, lid, lot, item, replaced, mid)
        elif op == "cancel":
            self.store.delete("draft", args[0])
            self.lots(chat, 0, mid)
        elif op == "reread":
            self.shop.reread(args[0])
            self.lot(chat, args[0], mid)
        elif op == "lot_auto_price":
            lot = self.store.get("lot", args[0])
            if not lot:
                raise ShopError("Лот не найден.")
            self.store.patch("lot", args[0], auto_price=not lot.get("auto_price", True))
            self.lot(chat, args[0], mid)
        elif op == "delete_lot":
            self.store.delete("lot", args[0])
            self.lots(chat, 0, mid)
        elif op == "pricing":
            self.pricing(chat, mid)
        elif op == "sync_now":
            checked, failed = self.shop.refresh_items()
            changed, errors = self.shop.sync_lots(force=True)
            self.show(chat, f"⚡ <b>Готово</b>\nПозиций сверено с поставщиком: {checked}" + (f" (ошибок: {failed})" if failed else "")
                            + f"\nЛотов обновлено на FunPay: {changed}\nОшибок: {errors}"
                            + ("\nПричины — в карточках лотов." if errors else ""),
                      [[("💲 Ценообразование", ("pricing",)), ("📦 Лоты", ("lots", 0))]], mid)
        elif op == "template":
            self.template(chat, mid)
        elif op == "reset_template":
            self.store.configure(template=DEFAULT_TEMPLATE)
            self.template(chat, mid)
        elif op == "stats":
            self.stats(chat, mid)
        elif op == "history":
            self.history(chat, args[0], mid)
        elif op == "order":
            self.order(chat, args[0], args[1] if len(args) > 1 else 0, mid)
        elif op == "guide":
            self.guide(chat, args[0], mid)
        elif op == "ask":
            self.prompt(chat, user, *args)
        elif op == "plugin_card":
            self.plugin_card(chat, mid)
        elif op == "plugins":
            self.show(chat, "Список плагинов: /menu → Система и конфиги → Плагины.", [[("🔙 В меню", ("home",))]], mid)

    def register(self):
        self.tg.msg_handler(self.command, commands=[COMMAND])
        self.tg.msg_handler(self.message, func=self.has_input, content_types=["text"])
        self.tg.cbq_handler(self.callback, lambda c: bool(c.data) and c.data.startswith(PREFIX))
        prefix = self.settings_prefix()

        def settings_callback(call):
            if call.message and self.allowed(call.from_user):
                try:
                    self.bot.answer_callback_query(call.id)
                except Exception:
                    pass
                self.plugin_card(call.message.chat.id, call.message.message_id)
        self.tg.cbq_handler(settings_callback, lambda c: bool(c.data) and c.data.startswith(prefix))
        try:
            self.shop.cardinal.add_telegram_commands(UUID, [(COMMAND, "🛒 AppRoute Shop — панель", True)])
        except Exception:
            LOG.warning("AppRoute Shop: command was not added to the Telegram menu")


SHOP = None
STARTUP_ERROR = ""
UI_REGISTERED = False


def register_fallback(cardinal):
    """The storage did not open: the command and the plugin card still explain why."""
    tg = cardinal.telegram

    def reply(chat):
        text = (f"⛔ <b>{NAME}</b> не запустился.\n\n{esc(STARTUP_ERROR)}\n\n"
                f"Исправьте причину и перезапустите бота: /restart. Подробности — в консоли и logs/log.log.")
        tg.bot.send_message(chat, text, parse_mode="HTML")

    tg.msg_handler(lambda m: reply(m.chat.id), commands=[COMMAND])
    prefix = TelegramUI.settings_prefix()

    def settings(call):
        try:
            tg.bot.answer_callback_query(call.id)
        except Exception:
            pass
        reply(call.message.chat.id)
    tg.cbq_handler(settings, lambda c: bool(c.data) and c.data.startswith(prefix))


def register_ui(cardinal):
    """Idempotent: tried at PRE_INIT, POST_INIT and POST_START, whichever first has Telegram."""
    global UI_REGISTERED
    if UI_REGISTERED or not getattr(cardinal, "telegram", None):
        return
    UI_REGISTERED = True
    try:
        if SHOP is None:
            register_fallback(cardinal)
            return
        SHOP.ui = TelegramUI(SHOP)
        SHOP.ui.register()
        LOG.info("%s: панель зарегистрирована, команда /%s", NAME, COMMAND)
    except Exception:
        LOG.error("%s: не удалось зарегистрировать панель Telegram", NAME, exc_info=True)


def init(cardinal, *args):
    global SHOP, STARTUP_ERROR
    if SHOP is None and not STARTUP_ERROR:
        try:
            SHOP = Shop(cardinal)
            LOG.info("%s v%s initialized. Author: %s", NAME, VERSION, CREDITS)
        except Exception as exc:
            STARTUP_ERROR = (str(exc) or exc.__class__.__name__)[:500]
            LOG.error("%s: ошибка запуска: %s", NAME, STARTUP_ERROR, exc_info=True)
    register_ui(cardinal)


def start(cardinal, *args):
    init(cardinal)
    if SHOP is None:
        return
    SHOP.stop_event.clear()
    try:
        SHOP.start()
    except Exception as exc:
        LOG.error("%s: фоновая обработка не запущена: %s", NAME, exc, exc_info=not isinstance(exc, ShopError))
        if SHOP.ui is not None:
            SHOP.notify(f"⛔ <b>{NAME}</b>: фоновая обработка заказов не запущена.\n{esc(exc)}", force=True)


def stop(cardinal, *args):
    if SHOP:
        SHOP.stop_event.set()
        SHOP.wakeup.set()
        SHOP.service_wakeup.set()
        for thread in (SHOP.thread, SHOP.service_thread):
            if thread:
                thread.join(timeout=5)
        alive = any(t and t.is_alive() for t in (SHOP.thread, SHOP.service_thread))
        if SHOP.instance_lock and not alive:
            SHOP.instance_lock.close()
            SHOP.instance_lock = None


def new_order(cardinal, event, *args):
    """Only with «Статус» 🟢: while it is 🔴, new orders are ignored, as the guide says."""
    if not SHOP or not SHOP.active() or not SHOP.store.settings()["enabled"]:
        return
    order = getattr(event, "order", None)
    if order is None or enum_name(getattr(order, "status", "PAID")) != "PAID":
        return
    buyer = str(getattr(order, "buyer_id", "") or "")
    chat = str(getattr(order, "chat_id", "") or "")
    if not chat.startswith("users-"):
        chat = chat_for(cardinal.account.id, buyer) if buyer.isdigit() else ""
    SHOP.enqueue(str(order.id), chat, buyer if chat else "")


def order_status_changed(cardinal, event, *args):
    order = getattr(event, "order", None)
    if order is not None and enum_name(getattr(order, "status", "")) == "PAID":
        new_order(cardinal, event, *args)


def delete(cardinal, *args):
    stop(cardinal)


BIND_TO_PRE_INIT = [init]
BIND_TO_POST_INIT = [init]
BIND_TO_POST_START = [start]
BIND_TO_PRE_STOP = [stop]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_ORDER_STATUS_CHANGED = [order_status_changed]
BIND_TO_DELETE = delete
