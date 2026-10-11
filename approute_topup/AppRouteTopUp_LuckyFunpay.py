# -*- coding: utf-8 -*-
"""AppRoute Top-Up for FunPay Cardinal.

Install this file through Cardinal's plugin uploader and restart Cardinal.
Persistent state: storage/plugins/approute_topup/topup.sqlite3. Never remove that
directory to troubleshoot a pending purchase: it contains the delivery journal.
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
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

NAME = "AppRoute Top-Up"
VERSION = "1.0.0"
DESCRIPTION = "Прямые пополнения AppRoute • /approute_topup"
CREDITS = "@LuckyFunPay_Support"
UUID = "24c22120-5c0d-4e3f-994c-1b758263a3b7"
SETTINGS_PAGE = True
DEVELOPER_URL = "https://t.me/LuckyFunPay"
SUPPORT_URL = "https://t.me/LuckyFunPay_Support"
PREFIX = "artop:"
LOG = logging.getLogger("FPC.AppRouteTopUp")
DEFAULTS = {
    "enabled": False, "api_key": "", "region": "RU", "proxy_mode": "bot", "proxy": "",
    "rate": "100", "markup": "15", "min_price": "0.01", "max_price": "1000000",
    "interval": 20, "auto_price": False, "sync_stock": False, "price_notify": False,
    "max_quantity": 50, "low_balance": "3", "sanity_ratio": "3", "sanity_enabled": True,
    "auto_refund": True, "notifications": True, "notify_chats": [], "templates": {},
    "precheck": True, "strict_check": True, "confirm_account": True, "discover_fields": True,
    "require_form": True, "telegram_no_at": True, "split_quantity": True,
    "amount_mode": "quantity", "multiplier": "1", "amount_currency": "",
    "timeout_min": 30, "input_attempts": 3, "poll_timeout": 15,
    "catalog_interval": 360, "catalog_at": 0, "catalog_retry_at": 0,
    "sync_at": 0, "balance_at": 0, "balance": None, "low_alert": False,
}
WAIT_STATES = {"collect", "confirm"}
ACTIVE_STATES = {"queued", "collect", "check", "confirm", "ready", "pending", "purchasing", "refund_pending"}
STATE_NAMES = {"queued":"В очереди", "collect":"Ввод данных", "check":"Проверка аккаунта",
 "confirm":"Подтверждение аккаунта", "ready":"Готов к пополнению", "purchasing":"Отправка оплаты",
 "pending":"Проверка результата", "done":"Выполнен", "partial":"Частично выполнен",
 "review":"Нужен продавец", "refunded":"Возвращён", "refund_pending":"Проверка возврата",
 "closed":"Закрыт", "ignored":"Не обслуживается"}

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
        self.args = (f"AppRoute ограничил частоту запросов (HTTP 429, код {code}). "
                     f"Пауза ещё {seconds} сек. Плагин продолжит автоматически; повторно нажимать обновление не нужно.",)


class RequestDeferred(ShopError):
    """Local scheduling, before any HTTP request; keep the worker available."""
    def __init__(self, retry_at):
        self.retry_at = retry_at
        super().__init__("Ожидание интервала запросов AppRoute. Продолжение запланировано автоматически.")


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
    """Shared across API clients; a provider cooldown also survives restart.

    Fallbacks follow GET /rate-limits, verified 2026-09-13. Live quotas may
    tighten these intervals. Catalogue/reveal waits never block the worker.
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

    def remaining(self, settings):
        state = self.store.get("rate", self.identity(settings), {})
        return max(0, state.get("blocked_until", 0) - self.clock())

    def before(self, settings, method, path, params=None):
        key, bucket = self.identity(settings), self.bucket(method, path, params)
        with self.lock:
            while True:
                if self.stop_event is not None and self.stop_event.is_set():
                    raise ShopError("Плагин остановлен. Прогресс запросов сохранён.")
                state = self.store.get("rate", key, {})
                now = self.clock()
                if state.get("pacing_version", 1) < 2:
                    # 1.0.1 could issue two calls 13 seconds apart. Empty that
                    # previous minute's window once when upgrading the journal.
                    changes = {"pacing_version": 2}
                    for old_bucket in ("catalog", "reveal"):
                        if state.get("next_" + old_bucket):
                            changes["next_" + old_bucket] = state["next_" + old_bucket] - 13 + 61
                    self.store.patch("rate", key, **changes)
                    state.update(changes)
                if state.get("blocked_until", 0) > now:
                    raise RateLimitError(state["blocked_until"], now=now)
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
    phrase = normalized(phrase)
    return bool(phrase) and f" {phrase} " in f" {normalized(text)} "


def enum_name(value):
    return str(getattr(value, "name", value)).upper().split(".")[-1]



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
        raise ShopError("Прокси: ip:порт или логин:пароль@ip:порт. Допустимы http, https, socks5h.") from None
    return value


def lot_id_from_text(value):
    value = value.strip()
    if value.isdigit() and 0 < int(value) < 10**15:
        return str(int(value))
    p = urlparse(value)
    if p.scheme == "https" and p.hostname == "funpay.com" and p.path in {"/lots/offer", "/lots/offerEdit"}:
        for key in ("id", "offer"):
            candidate = parse_qs(p.query).get(key, [""])[0]
            if candidate.isdigit():
                return lot_id_from_text(candidate)
    raise ShopError("Отправьте ID лота или ссылку https://funpay.com/lots/offer?id=12345678.")


def calculate_price(cost, settings):
    price = dec(cost) * dec(settings["rate"]) * (1 + dec(settings["markup"]) / 100)
    return money(max(dec(settings["min_price"]), min(dec(settings["max_price"]), price)))


class Store:
    """Atomic, durable journal; one transaction per mutation and no reset-on-error."""
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY(kind,id))")
        self.db.commit()
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
            os.chmod(path, 0o600)

    def get(self, kind, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, str(key))).fetchone()
            return json.loads(row[0]) if row else copy.deepcopy(default)

    def put(self, kind, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?)", (kind, str(key), json.dumps(value, ensure_ascii=False)))

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

    def enqueue(self, oid):
        if not re.fullmatch(r"[A-Za-z0-9-]{1,40}", str(oid)):
            raise ShopError("Некорректный номер заказа FunPay.")
        with self.lock, self.db:
            data = {"id": str(oid), "state": "queued", "created": time.time(), "next_at": 0, "attempts": 0}
            return self.db.execute("INSERT OR IGNORE INTO records VALUES ('order',?,?)", (str(oid), json.dumps(data))).rowcount == 1



def match_lot(order, lots, diagnostics=None):
    """No fuzzy matching for spending. Every configured condition must match."""
    sub = getattr(getattr(order, "subcategory", None), "id", None)
    text = [getattr(order, "short_description", "") or "", getattr(order, "description", "") or ""]
    fields = {}
    for key, field in (getattr(order, "fields", {}) or {}).items():
        value = getattr(field, "value", field)
        values = list(value.values()) if isinstance(value, dict) else [value]
        fields[str(key)] = [normalized(v) for v in values if v is not None]
        if str(key) not in {"desc", "payment_msg", "images"}:
            text.extend(str(v) for v in values)
    for key in ("server", "side"):
        obj = getattr(order, key, None)
        if obj is not None:
            fields[key] = [normalized(getattr(obj, "name", "")), normalized(getattr(obj, "id", ""))]
            text.append(str(getattr(obj, "name", "")))
    haystack = " \n ".join(text)
    direct = getattr(order, "lot_id", None) or getattr(order, "offer_id", None)
    candidates = []
    for lot in lots:
        if not lot.get("item_id") or not lot.get("enabled", True):
            continue
        if lot.get("subcategory") and str(sub) != str(lot["subcategory"]):
            continue
        if direct:
            if str(direct) == lot["id"]:
                candidates.append(lot)
            continue
        conditions = lot.get("conditions") or []
        if not conditions:
            if diagnostics is not None:
                diagnostics.append(f"Лот {lot['id']}: условия не заданы.")
            continue
        failed = []
        for condition in conditions:
            if "=" in condition:
                key, value = condition.split("=", 1)
                if normalized(value) not in fields.get(key.strip(), []):
                    failed.append(f"поле {key.strip()[:40]}={value[:70]}")
            else:
                if not has_phrase(haystack, condition):
                    failed.append(f"фраза «{condition[:100]}»")
        if not failed:
            candidates.append(lot)
        elif diagnostics is not None:
            diagnostics.append(f"Лот {lot['id']}: не совпали " + ", ".join(failed[:3]) + ".")
    return candidates



def selected_conditions(markup):
    """Read standardized selectors from Cardinal's authenticated offer editor."""
    from bs4 import BeautifulSoup
    form = BeautifulSoup(markup, "html.parser").select_one("form.form-offer-editor")
    if form is None:
        raise ShopError("FunPay не вернул форму редактирования лота.")
    conditions = []
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
            conditions.append(f"{match.group(1)}={label}")
        elif name in {"server_id", "side_id"} and label:
            conditions.append(f"{name.removesuffix('_id')}={label}")
    return conditions


class AppRouteAPI:
    def __init__(self, settings, bot_proxy=None, transport=None, pacer=None):
        self.settings = settings
        self.base = {"RU": "https://approute.ru", "IO": "https://approute.io"}[settings["region"]]
        self.proxies = ({"http": settings["proxy"], "https": settings["proxy"]}
                        if settings["proxy_mode"] == "own" else (bot_proxy or {}))
        if settings["proxy_mode"] == "own" and not settings["proxy"]:
            raise ShopError("Сначала задайте собственный прокси.")
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
            # Handle even HTML/empty 429 responses before trying to decode JSON.
            if response.status_code == 429:
                header = (getattr(response, "headers", None) or {}).get("Retry-After")
                deadline = (self.pacer.limited(self.settings, header) if self.pacer is not None else
                            time.time() + retry_after_seconds(header, time.time()) + 1)
                LOG.warning("AppRoute Top-Up: %s %s — HTTP 429; retry in %s s", method, path,
                            max(1, math.ceil(deadline - time.time())))
                raise RateLimitError(deadline)
            try:
                data = response.json()
            except (ValueError, TypeError):
                raise APIError(response.status_code) from None
            if not isinstance(data, dict):
                raise APIError(response.status_code)
            code = data.get("statusCode")
            if not 200 <= response.status_code < 300 or code not in (0, 1, 2, "0", "1", "2"):
                raise APIError(response.status_code, code if isinstance(code, (int, str)) and len(str(code)) < 32 else None, data.get("errors"))
            if not isinstance(data.get("data"), dict):
                raise APIError(response.status_code, code)
            return data["data"]
        except ShopError:
            raise
        except Exception:
            # requests exceptions can contain proxy passwords or request URLs.
            raise ShopError("Сетевая ошибка AppRoute. Проверьте ключ, IP и прокси. Статус закупки будет уточнён.") from None
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
            LOG.info("AppRoute Top-Up: live quotas unavailable; using saved/default intervals")
        self.pacer.store.patch("rate", key, limits_checked_at=now)

    def balance(self):
        data = self.request("GET", "/accounts")
        values = [i for i in data.get("items", []) if i.get("currency") == "USD"]
        if len(values) != 1 or "available" not in values[0]:
            raise ShopError("В ответе AppRoute отсутствует доступный баланс USD.")
        return dec(values[0]["available"])

    def catalog(self, checkpoint=None, on_page=None):
        progress = copy.deepcopy(checkpoint or {"products": [], "kind_index": 0, "offset": 0})
        products = progress["products"]
        seen = {str(p["id"]) for p in products}
        kinds, page_size = ("direct_topup",), 500
        for index in range(progress["kind_index"], len(kinds)):
            kind = kinds[index]
            offset = progress["offset"] if index == progress["kind_index"] else 0
            while True:
                data = self.request("GET", "/services", {"type": kind, "limit": page_size, "offset": offset})
                items = data.get("items")
                if not isinstance(items, list):
                    raise ShopError("Неизвестный формат каталога AppRoute.")
                fresh = 0
                for item in items:
                    if str(item.get("id")) in seen:
                        continue
                    if item.get("type") != "direct_topup":
                        continue
                    seen.add(str(item["id"]))
                    products.append(item)
                    fresh += 1
                total = integer(data["total"]) if data.get("total") is not None else None
                done = offset + len(items) >= total if total is not None else len(items) < page_size
                if items and not fresh and offset > 0:
                    raise ShopError("AppRoute повторил страницу каталога; прогресс и старый кэш сохранены.")
                if not done and (not items or not fresh or offset >= 100000):
                    raise ShopError("AppRoute повторяет страницы каталога; старый кэш сохранён.")
                progress.update(kind_index=index + 1 if done else index, offset=0 if done else offset + len(items))
                if on_page is not None:
                    on_page(copy.deepcopy(progress))
                if done:
                    break
                offset = progress["offset"]
        return products

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


    def rates(self):
        return self.request("GET", "/steam-currency/rates")

    def check(self, body):
        body = copy.deepcopy(body)
        body["checkOnly"] = True
        body.pop("referenceId", None)
        return self.request("POST", "/orders", body=body)


TEMPLATES = {
    "welcome": ("Приветствие", "💙 Спасибо за покупку! Заказ #{order_id}: {lot_name}.\nДля пополнения нужно ответить на {questions} вопрос(а). На ответ — {timeout_min} мин."),
    "question": ("Вопрос", "📝 Шаг {step} из {questions}\n{field_label}\n{options}"),
    "invalid": ("Неверный формат", "Проверьте ответ: {reason}\n{field_label}"),
    "checking": ("Начало проверки", "🔎 Данные получены. Проверяю аккаунт перед пополнением…"),
    "confirm": ("Подтверждение", "👤 Найден аккаунт: {nickname}\nЭто ваш аккаунт? Ответьте «да» для пополнения или «нет» для повторного ввода."),
    "confirm_hint": ("Ответ на подтверждение", "Ответьте «да» или «нет». Пополнение ещё не оплачено."),
    "confirmed": ("Аккаунт подтверждён", "✅ Аккаунт подтверждён. Начинаю пополнение."),
    "retry": ("Повтор ввода", "Аккаунт не прошёл проверку. Введите данные заново. Осталось попыток: {attempts_left}."),
    "rejected_account": ("Другой аккаунт", "Хорошо, укажите данные нужного аккаунта заново."),
    "processing": ("Оплата", "⏳ Выполняю пополнение. Дождитесь результата."),
    "accepted": ("Ожидание поставщика", "⏳ Поставщик принял пополнение в обработку. Обычно это занимает несколько минут."),
    "success": ("Успех", "✅ Пополнение выполнено!\n{extra}\nПроверьте аккаунт. Если всё в порядке, подтвердите заказ #{order_id} на FunPay. Спасибо! 💙"),
    "progress": ("Дробление", "✅ Выполнено {completed} из {operations} пополнений."),
    "partial": ("Частичное выполнение", "Выполнена часть заказа ({completed} из {operations}). Продавец уведомлён и проверит оставшуюся часть."),
    "review": ("Ручная проверка", "🛠 Заказ #{order_id} требует проверки продавцом. Мы уже передали ему информацию."),
    "unknown": ("Неизвестный результат", "Поставщик пока не подтвердил итог пополнения. Продавец проверит операцию. Не оформляйте повторную покупку для этого заказа."),
    "timeout": ("Нет ответа", "Время ожидания ответа истекло. Напишите продавцу, чтобы продолжить заказ #{order_id}."),
    "refund": ("Возврат выполнен", "↩️ Пополнение не выполнено. Средства за заказ #{order_id} возвращены на FunPay."),
    "failed": ("Отказ поставщика", "Пополнение не выполнено. {refund_note}"),
    "refund_pending": ("Возврат проверяется", "Пополнение не выполнено. Продавец проверяет возврат по заказу #{order_id}."),
    "queued": ("Очередь покупателя", "Заказ #{order_id} принят. Сначала завершим ваш предыдущий заказ, затем запросим данные для этого."),
    "paused": ("Выдача на паузе", "Заказ #{order_id} сохранён. Автоматическое пополнение временно на паузе; продавец уведомлён."),
    "unavailable": ("Нет товара", "Этот номинал временно недоступен у поставщика. {refund_note}"),
    "balance": ("Не хватает баланса", "Пополнение временно недоступно. {refund_note}"),
    "limit": ("Лимит количества", "Заказ превышает лимит автоматической обработки. Продавец проверит его вручную."),
    "amount_range": ("Границы суммы", "Сумма этого заказа требует проверки продавцом. Деньги поставщику ещё не отправлены."),
    "missing_form": ("Неизвестная форма", "Уточняем данные, которые требуются для этого пополнения. Продавец уже уведомлён."),
    "additional_field": ("Дополнительное поле", "Поставщик запросил ещё одно поле. Уточним его перед пополнением."),
    "optional": ("Необязательное поле", "Поле необязательное: отправьте «-», чтобы пропустить."),
    "resumed": ("Диалог продолжен", "Продолжаем заказ #{order_id}."),
    "cancel_hint": ("Слово отмена", "Для отмены свяжитесь с продавцом. Сейчас ожидаю данные для заказа #{order_id}."),
    "pre_filled": ("Данные из заказа", "Данные аккаунта уже указаны в заказе. Проверю их перед пополнением."),
}
TEMPLATE_FIELDS = {"order_id", "lot_name", "quantity", "timeout_min", "questions", "step", "field_label",
                   "options", "reason", "nickname", "attempts_left", "extra", "completed", "operations", "refund_note"}
REQUIRED_TEMPLATE_FIELDS = {"question": {"field_label"}, "confirm": {"nickname"}, "success": {"extra"}}


def validate_template(key, value):
    if not 1 <= len(value) <= 2500:
        raise ShopError("Текст: от 1 до 2500 символов.")
    try:
        fields = set()
        for _, field, spec, conversion in string.Formatter().parse(value):
            if field is not None:
                if field not in TEMPLATE_FIELDS or spec or conversion:
                    raise ValueError
                fields.add(field)
        if not REQUIRED_TEMPLATE_FIELDS.get(key, set()) <= fields:
            raise ValueError
    except ValueError:
        raise ShopError("Проверьте скобки и подстановки. Обязательные: " + ", ".join(REQUIRED_TEMPLATE_FIELDS.get(key, set()))) from None
    return value


def esc(value):
    return html.escape(str(value))


def field_key(field):
    return str(field.get("key", ""))


def form_fields(service, item):
    raw = item.get("fields") or service.get("fields")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ShopError("Неизвестный формат формы поставщика.")
    result, seen = [], set()
    for f in raw:
        if not isinstance(f, dict) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", field_key(f)):
            raise ShopError("Некорректное поле формы поставщика.")
        if f.get("type") == "summary":
            continue
        if field_key(f) in seen:
            raise ShopError("Повтор поля в форме поставщика.")
        seen.add(field_key(f))
        result.append(copy.deepcopy(f))
    return result


def is_amount(field):
    return field_key(field).casefold() in {"amount", "topup_amount"} or field.get("type") == "amount_input"


def open_amount(fields):
    return next((f for f in fields or [] if is_amount(f)), None)


def available(item):
    if item.get("available") is False or item.get("isAvailable") is False:
        return False
    stock = item.get("inStock", item.get("stock"))
    return stock is None or dec(stock) > 0


def stock_count(item):
    value = item.get("inStock", item.get("stock"))
    return None if value is None else max(0, integer(value))


def validate_field(field, value, service_name="", settings=None):
    settings = settings or DEFAULTS
    value = str(value).strip()
    if value == "-" and not field.get("required", False):
        return ""
    if not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ShopError("Ответ должен содержать от 1 до 256 символов без переноса строки.")
    options = field.get("options") or []
    if options:
        if value.isdigit() and 1 <= int(value) <= len(options):
            value = str(options[int(value)-1]["value"])
        else:
            found = [o for o in options if value.casefold() in {str(o.get("value", "")).casefold(), str(o.get("label", "")).casefold()}]
            if len(found) != 1:
                raise ShopError("Выберите номер, значение или название из списка.")
            value = str(found[0]["value"])
    key, name = field_key(field).casefold(), service_name.casefold()
    if "telegram" in name and key in {"account_reference", "username", "account", "login"}:
        if not settings["telegram_no_at"] and not value.startswith("@"):
            raise ShopError("Укажите username с @.")
        value = value.removeprefix("@")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{2,31}", value):
            raise ShopError("Telegram username: 3–32 символа, латиница, цифры и _. Отправьте username, а не ссылку.")
        value = "@" + value
    if "steam" in name and key in {"account_reference", "login", "account"}:
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", value):
            raise ShopError("Нужен логин Steam: 3–64 символа, латиница, цифры, _ и -. Не e-mail и не ссылка.")
    rules = field.get("validation") or {}
    if is_amount(field) or field.get("type") in {"number", "integer", "amount_input"}:
        number = dec(value)
        if number <= 0:
            raise ShopError("Число должно быть положительным.")
        if field.get("type") == "integer" and number != number.to_integral_value():
            raise ShopError("Требуется целое число.")
        # Amount bounds may be in native currency; they are checked separately.
        if not is_amount(field):
            if rules.get("min") is not None and number < dec(rules["min"]) or rules.get("max") is not None and number > dec(rules["max"]):
                raise ShopError("Число вне допустимого диапазона.")
    elif not options:
        if rules.get("min") is not None and len(value) < int(rules["min"]) or rules.get("max") is not None and len(value) > int(rules["max"]):
            raise ShopError("Длина ответа вне допустимого диапазона.")
    pattern = rules.get("pattern")
    if pattern:
        try:
            if len(pattern) > 256 or len(value) > 128 or re.search(r"\([^)]*[+*][^)]*\)[+*{]", pattern):
                raise ShopError("Сложный шаблон проверки — требуется проверка продавцом.")
            if re.fullmatch(pattern, value) is None:
                raise ShopError("Ответ не соответствует формату поставщика.")
        except re.error:
            raise ShopError("Некорректный шаблон формы поставщика.") from None
    return value


def make_body(item_id, data, quantity=1, currency="", reference=None, check=False):
    order = {"denominationId": item_id, "quantity": quantity,
             "fields": [{"key": k, "value": str(v)} for k, v in data.items() if v != ""]}
    if currency:
        order["amountCurrencyCode"] = currency
    body = {"ordersType": "dtu", "orders": [order]}
    if check:
        body["checkOnly"] = True
    if reference:
        body["referenceId"] = reference
    return body


def discovered_fields(error):
    # Only machine-readable MISSING errors are used, never words in a provider message.
    result = []
    for entry in error.errors if isinstance(error.errors, list) else []:
        key = entry.get("field", "")
        if entry.get("code") == "MISSING" and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", key):
            result.append({"key": key, "type": "amount_input" if key == "amount" else "text", "required": True, "label": key})
    return result


def quote_cost(value):
    if value.get("currency") != "USD" or value.get("price") is None or dec(value["price"]) <= 0:
        raise ShopError("Поставщик не вернул достоверную цену USD. Нужна ручная проверка.")
    return dec(value["price"])


def rates_map(data):
    if data.get("baseCurrencyCode") != "USD":
        raise ShopError("Неизвестная база курса поставщика.")
    result = {"USD": Decimal(1)}
    for item in data.get("items", []):
        rate = dec(item["rate"])
        if rate > 0:
            result[str(item["quoteCurrencyCode"])] = rate
    return result


def native_currency(service, field):
    return field.get("currencyCode") or service.get("amountCurrencyCode") or service.get("currencyCode") or ""


def check_amount_bounds(field, value, currency, native, rates=None, manual=None):
    number = dec(value)
    rules = manual or field.get("validation") or {}
    if currency and native and currency != native and not manual:
        if not rates or currency not in rates or native not in rates:
            raise ShopError("Не удалось пересчитать границы суммы. Обновите курсы или задайте лимиты лота.")
        number = number / rates[currency] * rates[native]
    elif currency and not native and not manual and any(rules.get(k) is not None for k in ("min", "max")):
        raise ShopError("Неизвестна валюта границ поставщика. Укажите родную валюту или ручные лимиты лота.")
    if rules.get("min") is not None and number < dec(rules["min"]) or rules.get("max") is not None and number > dec(rules["max"]):
        raise ShopError("Расчётная сумма вне границ пополнения.")


class TopUp:
    def __init__(self, cardinal, store=None, api_factory=None):
        self.cardinal = cardinal
        self.store = store or Store("storage/plugins/approute_topup/topup.sqlite3")
        self.api_factory = api_factory
        self.stop_event, self.wakeup = threading.Event(), threading.Event()
        self.pacer = RequestPacer(self.store, self.stop_event)
        self.jobs = queue.Queue(maxsize=200)
        self.thread, self.ui, self.instance_lock = None, None, None
        self.last_maintenance = 0

    def active(self):
        plugin = getattr(self.cardinal, "plugins", {}).get(UUID)
        return (plugin is None or plugin.enabled) and not self.stop_event.is_set()

    def api(self, settings=None):
        settings = settings or self.store.settings()
        return self.api_factory(settings) if self.api_factory else AppRouteAPI(settings, getattr(self.cardinal.account, "proxy", None), pacer=self.pacer)

    def notify(self, text):
        tg = getattr(self.cardinal, "telegram", None)
        if not tg or not self.store.settings()["notifications"]:
            return
        for chat in self.store.settings()["notify_chats"]:
            try:
                tg.bot.send_message(chat, text, parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                LOG.warning("AppRoute Top-Up: Telegram notification not sent")

    def save(self, record):
        record["updated"] = time.time()
        self.store.put("order", record["id"], record)

    def tell(self, record, key, **values):
        s = self.store.settings()
        context = {k: "" for k in TEMPLATE_FIELDS}
        context.update(order_id=record["id"], lot_name=record.get("lot", {}).get("name", ""),
                       quantity=record.get("quantity", 1), timeout_min=s["timeout_min"],
                       completed=record.get("completed", 0), operations=record.get("operations", 1),
                       refund_note="Возврат будет проверен продавцом." if s["auto_refund"] else "Свяжитесь с продавцом.")
        context.update(values)
        text = s["templates"].get(key, TEMPLATES[key][1])
        try:
            text = validate_template(key, text).format(**context)
        except Exception:
            text = TEMPLATES[key][1].format(**context)
        if record.get("chat"):
            # Persist the message with the state transaction before sending. A retry never repeats a payment.
            record.setdefault("outbox", []).append({"id": uuid.uuid4().hex, "text": text})
            self.save(record)

    def flush(self, record):
        while record.get("outbox"):
            msg = record["outbox"][0]
            result = self.cardinal.account.send_message(record["chat"], msg["text"], record.get("buyer"), int(record["buyer_id"]))
            if not result:
                raise ShopError("FunPay не подтвердил доставку сообщения.")
            record["outbox"].pop(0)
            self.save(record)

    def review(self, record, reason, message="review"):
        if record.get("completed", 0):
            record["state"] = "partial"
            message = "partial"
        else:
            record["state"] = "review"
        record["reason"] = reason
        self.save(record)
        self.tell(record, message)
        self.notify(f"🛠 <b>Заказ #{esc(record['id'])}</b> · {esc(record.get('buyer', ''))}\n{esc(reason)}")

    def get_order(self, oid):
        order = self.cardinal.account.get_order(oid)
        if str(getattr(order, "seller_id", "")) != str(self.cardinal.account.id):
            raise ShopError("Этот аккаунт Cardinal не является продавцом заказа.")
        return order

    def paid(self, record, order=None):
        order = order if order is not None else self.get_order(record["id"])
        if enum_name(order.status) != "PAID":
            raise ShopError("Заказ FunPay больше не оплачен. Автоматическая закупка остановлена.")
        if str(getattr(order, "buyer_id", "")) == str(self.cardinal.account.id):
            raise ShopError("Заказ от собственного аккаунта не обслуживается.")
        if enum_name(getattr(order, "currency", "")) != "RUB":
            raise ShopError("Для автоматического расчёта требуется заказ в RUB.")
        if getattr(order, "order_secrets", None):
            raise ShopError("Заказ содержит встроенную автовыдачу FunPay. Проверьте выдачу вручную.")
        if record.get("quantity") is not None and integer(order.amount) != record["quantity"]:
            raise ShopError("Количество в заказе FunPay изменилось. Проверьте вручную.")
        if record.get("revenue") is not None and dec(order.sum) != dec(record["revenue"]):
            raise ShopError("Сумма заказа FunPay изменилась. Проверьте вручную.")
        return order

    def enqueue(self, oid):
        self.store.enqueue(oid)
        self.wakeup.set()

    def submit(self, action):
        try:
            self.jobs.put_nowait(action)
        except queue.Full:
            raise ShopError("Очередь панели заполнена. Повторите позже.") from None
        self.wakeup.set()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        # Prevent two processes from using one payment journal.
        path = Path("storage/plugins/approute_topup/worker.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.instance_lock = path.open("a+b")
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
        self.thread = threading.Thread(target=self.loop, name="AppRouteTopUp", daemon=True)
        self.thread.start()

    def loop(self):
        while not self.stop_event.is_set():
            if self.active():
                try:
                    action = self.jobs.get_nowait()
                except queue.Empty:
                    action = None
                if action:
                    try:
                        action()
                    except Exception:
                        LOG.warning("AppRoute Top-Up: panel job failed")
                for record in sorted(self.store.all("order"), key=lambda r: r["created"]):
                    if self.stop_event.is_set():
                        break
                    if (record.get("outbox") or record["state"] in ACTIVE_STATES) and record.get("next_at", 0) <= time.time():
                        self.process(record["id"])
                for job in self.store.all("message"):
                    try:
                        self.handle_message(job)
                        self.store.delete("message", job["id"])
                    except Exception:
                        LOG.warning("AppRoute Top-Up: buyer message deferred")
                if time.time() - self.last_maintenance >= 30:
                    self.last_maintenance = time.time()
                    try:
                        self.maintenance()
                    except (RequestDeferred, RateLimitError):
                        pass
                    except Exception:
                        LOG.warning("AppRoute Top-Up: maintenance failed")
            self.wakeup.wait(1)
            self.wakeup.clear()

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
            if record["state"] in WAIT_STATES:
                if time.time() - record.get("last_input", record["created"]) > self.store.settings()["timeout_min"] * 60:
                    self.review(record, "Покупатель не ответил вовремя. Возврат автоматически не выполняется.", "timeout")
                return
            if record["state"] in {"pending", "purchasing"}:
                self.reconcile(record)
            elif record["state"] == "refund_pending":
                self.refund(record)
            elif record["state"] == "queued":
                self.prepare(record)
            elif not self.store.settings()["enabled"]:
                if not record.get("pause_alert"):
                    record["pause_alert"] = True
                    self.save(record)
                    self.notify(f"⏸ Заказ #{esc(oid)} сохранён: пополнения на паузе.")
            elif record["state"] == "check":
                self.check(record)
            elif record["state"] == "ready":
                self.purchase(record)
            flushing = True
            self.flush(record)
        except (RateLimitError, RequestDeferred) as exc:
            record["next_at"] = exc.retry_at
            self.save(record)
        except APIError as exc:
            if record["state"] in {"pending", "purchasing", "refund_pending"}:
                record["next_at"] = time.time() + 30
                self.save(record)
            else:
                self.review(record, str(exc))
        except Exception as exc:
            if record["state"] in {"pending", "purchasing", "refund_pending"} or flushing:
                record["next_at"] = time.time() + 30
                self.save(record)
            else:
                self.review(record, str(exc) if isinstance(exc, ShopError) else "Ошибка обработки; проверьте настройку заказа.")

    def product_form(self, lot):
        api = self.api()
        service = api.service(lot["product_id"])
        if str(service.get("id")) != str(lot["product_id"]):
            raise ShopError("Поставщик вернул другой сервис.")
        if service.get("type") != "direct_topup":
            raise ShopError("Этот товар не является прямым пополнением.")
        item = api.item(lot["product_id"], lot["item_id"])
        if str(item.get("id")) != str(lot["item_id"]):
            raise ShopError("Поставщик вернул другой номинал.")
        fields = form_fields(service, item)
        s = self.store.settings()
        if not fields and s["discover_fields"]:
            try:
                result = api.check(make_body(lot["item_id"], {}))
                fields = form_fields({"fields": result.get("fields")}, {})
            except APIError as exc:
                fields = discovered_fields(exc) or None
        if not fields:
            if s["require_form"]:
                raise ShopError("Поставщик не описал форму. Выполните диагностику; до определения полей оплат не будет.")
            fields = [{"key": "account_reference", "type": "text", "label": "Данные аккаунта", "required": True}]
        self.store.patch("lot", lot["id"], form=fields, service_name=service.get("name", ""),
                         item_name=item.get("name", ""), last_cost=str(item.get("price", "")),
                         native_currency=native_currency(service, open_amount(fields) or {}) or lot.get("native_currency", ""))
        return service, item, fields

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
        order = self.paid(record, order)
        diagnostics = []
        matches = match_lot(order, self.store.all("lot"), diagnostics)
        if not matches:
            sub = getattr(getattr(order, "subcategory", None), "id", None)
            configured = any(l.get("item_id") and l.get("enabled", True) and l.get("subcategory") and str(l["subcategory"]) == str(sub) for l in self.store.all("lot"))
            if diagnostics and configured and not (getattr(order, "lot_id", None) or getattr(order, "offer_id", None)):
                record.update(chat=str(order.chat_id), buyer=getattr(order, "buyer_username", "") or "", buyer_id=str(order.buyer_id), revenue=str(order.sum))
                self.review(record, "Заказ в настроенной категории, но условия лота не совпали. " + " ".join(diagnostics[:3]))
                return
            record["state"] = "ignored"
            self.save(record)
            return
        if len(matches) != 1:
            self.review(record, "Условия совпали с несколькими лотами. Уточните условия определения заказа.")
            return
        lot = copy.deepcopy(matches[0])
        quantity = integer(order.amount)
        if quantity <= 0:
            raise ShopError("Неверное количество в заказе.")
        chat = getattr(order, "chat_id", None)
        if not chat:
            raise ShopError("FunPay не вернул ID чата покупателя.")
        if dec(order.sum) <= 0:
            raise ShopError("Некорректная сумма заказа FunPay.")
        record.update(lot=lot, quantity=quantity, revenue=str(order.sum), buyer=getattr(order, "buyer_username", "") or "",
                      buyer_id=str(order.buyer_id), chat=str(chat), completed=0, cost="0", data={},
                      operations=quantity, last_input=time.time())
        if not self.store.settings()["enabled"]:
            if not record.get("pause_alert"):
                record["pause_alert"] = True
                self.notify(f"⏸ Заказ #{esc(record['id'])} сохранён: пополнения на паузе.")
            self.save(record)
            return
        earlier = [r for r in self.store.all("order") if r["id"] != record["id"] and r.get("chat") == record["chat"] and
                   r["state"] in ACTIVE_STATES and (r["created"], r["id"]) < (record["created"], record["id"])]
        if earlier:
            if not record.get("queue_alert"):
                record["queue_alert"] = True
                self.tell(record, "queued")
            return
        service, item, fields = self.product_form(lot)
        record.update(service_name=service.get("name", ""), fields=fields, item=item)
        amount = open_amount(fields)
        s = self.store.settings()
        if not available(item):
            self.disable_lots("Товар недоступен", [lot])
            self.fail(record, "Товар недоступен", "unavailable")
            return
        if not amount and quantity > s["max_quantity"]:
            self.review(record, "Превышен лимит отдельных пополнений.", "limit")
            return
        if not amount and quantity > 1 and not s["split_quantity"]:
            record["operations"] = 1
            record["provider_quantity"] = quantity
        else:
            record["provider_quantity"] = 1
        if amount:
            record["operations"] = 1
            multiplier = dec(lot.get("multiplier") or s["multiplier"])
            mode = lot.get("amount_mode") or s["amount_mode"]
            if mode not in {"quantity", "fixed"}:
                raise ShopError("Неподдерживаемый источник суммы. Выберите количество или фиксированную сумму.")
            value = dec(lot.get("fixed_amount", "0")) * quantity if mode == "fixed" else dec(quantity) * multiplier
            if value <= 0:
                raise ShopError("Сумма пополнения должна быть положительной.")
            record["data"][field_key(amount)] = str(value)
            # Global currency applies only to monetary amount forms, never implicitly to Stars/TON.
            currency = lot.get("amount_currency")
            if currency is None:
                currency = native_currency(service, amount)
                if "steam" in record["service_name"].casefold():
                    currency = currency or s["amount_currency"]
            record["amount_currency"] = currency or ""
            native = lot.get("native_currency") or native_currency(service, amount)
            rates = rates_map(self.api().rates()) if currency and native and currency != native else None
            check_amount_bounds(amount, value, currency, native, rates, lot.get("limits"))
        else:
            record["amount_currency"] = ""
        fixed = lot.get("fixed_fields") or {}
        for f in fields:
            key = field_key(f)
            if is_amount(f):
                continue
            if key in fixed:
                record["data"][key] = validate_field(f, fixed[key], record["service_name"], s)
        # Use explicit field aliases only; never parse amounts or arbitrary description text.
        values = {}
        for k, field in (getattr(order, "fields", {}) or {}).items():
            value = getattr(field, "value", field)
            if isinstance(value, dict):
                value = value.get(getattr(order, "locale", "ru")) or value.get("ru") or value.get("en")
            if value is not None:
                values[str(k)] = str(value)
        if getattr(order, "player", None):
            values.setdefault("player", str(order.player))
        common_aliases = {"account_reference": ("login", "username", "player", "account"),
                          "account_number": ("uid", "user_id", "player_id", "player")}
        for f in fields:
            key = field_key(f)
            if key in record["data"] or is_amount(f):
                continue
            aliases = lot.get("aliases") or {}
            candidates = [aliases[key]] if key in aliases else [key, *common_aliases.get(key, ())]
            answers = {values[k] for k in candidates if k in values and values[k]}
            if len(answers) == 1:
                try:
                    record["data"][key] = validate_field(f, answers.pop(), record["service_name"], s)
                except ShopError:
                    pass
        record["state"] = "collect"
        record["attempts"] = 0
        self.save(record)
        missing = self.missing(record)
        self.tell(record, "welcome" if missing else "pre_filled", questions=len(missing))
        self.ask_next(record)

    def missing(self, record):
        return [f for f in record["fields"] if not is_amount(f) and field_key(f) not in record["data"]]

    def ask_next(self, record):
        missing = self.missing(record)
        if not missing:
            record.update(state="check", next_at=0)
            self.save(record)
            return
        field = missing[0]
        record.update(state="collect", asking=field_key(field), last_input=time.time())
        self.save(record)
        options = "\n".join(f"{i+1}. {o.get('label', o['value'])} ({o['value']})" for i, o in enumerate(field.get("options") or []))
        if not field.get("required", False):
            options += "\nНеобязательно — можно отправить -."
        questions = len([f for f in record["fields"] if not is_amount(f) and field_key(f) not in (record["lot"].get("fixed_fields") or {})])
        self.tell(record, "question", step=max(1, questions-len(missing)+1), questions=questions,
                  field_label=field.get("label") or field_key(field), options=options)

    def handle_message(self, job):
        def same_chat(record):
            if record.get("chat") == job["chat"]:
                return True
            # Older FunPayAPI runners emit numeric node ids; full orders use users-A-B.
            pair = sorted((str(self.cardinal.account.id), job["buyer_id"]), key=int)
            return job["chat"].isdigit() and record.get("chat") == "users-" + "-".join(pair)
        candidates = sorted([r for r in self.store.all("order") if same_chat(r) and
                             r.get("buyer_id") == job["buyer_id"] and r["state"] in WAIT_STATES], key=lambda r: r["created"])
        if not candidates:
            return
        record = candidates[0]
        if job["id"] in record.get("seen_messages", []) or job["created"] < record["created"]:
            return
        record.setdefault("seen_messages", []).append(job["id"])
        record["last_input"] = time.time()
        text = job["text"].strip()
        if text.casefold() in {"отмена", "стоп", "cancel", "/cancel"}:
            self.tell(record, "cancel_hint")
        elif record["state"] == "confirm":
            if text.casefold() in {"да", "yes", "д", "+"}:
                record.update(state="ready", next_at=0)
                self.tell(record, "confirmed")
            elif text.casefold() in {"нет", "no", "н"}:
                self.reset_input(record, "rejected_account")
            else:
                self.tell(record, "confirm_hint")
        else:
            field = next(f for f in record["fields"] if field_key(f) == record["asking"])
            try:
                value = validate_field(field, text, record["service_name"], self.store.settings())
            except ShopError as exc:
                self.tell(record, "invalid", reason=str(exc), field_label=field.get("label") or field_key(field))
            else:
                record["data"][field_key(field)] = value
                self.ask_next(record)
        self.save(record)
        self.flush(record)

    def reset_input(self, record, message="retry"):
        record["attempts"] = record.get("attempts", 0) + 1
        if record["attempts"] >= self.store.settings()["input_attempts"]:
            self.fail(record, "Исчерпаны попытки подтверждения/ввода.")
            return
        fixed = record["lot"].get("fixed_fields") or {}
        record["data"] = {k: v for k, v in record["data"].items() if k in fixed or any(is_amount(f) and field_key(f) == k for f in record["fields"])}
        record.pop("quote", None)
        self.tell(record, message, attempts_left=self.store.settings()["input_attempts"] - record["attempts"])
        self.ask_next(record)

    def body(self, record, reference=None):
        return make_body(record["lot"]["item_id"], record["data"], record["provider_quantity"], record.get("amount_currency", ""), reference)

    def check(self, record):
        self.paid(record)
        s = self.store.settings()
        if not s["precheck"]:
            if s["strict_check"] or open_amount(record["fields"]):
                self.review(record, "Предпроверка выключена, но для этого заказа требуется достоверная проверка цены.")
                return
            record.update(state="ready", next_at=0)
            self.save(record)
            return
        if not record.get("checking_notice"):
            record["checking_notice"] = True
            self.tell(record, "checking")
        try:
            result = self.api().check(self.body(record))
        except APIError as exc:
            extra = [f for f in discovered_fields(exc) if field_key(f) not in {field_key(x) for x in record["fields"]}]
            if extra and s["discover_fields"]:
                if any(is_amount(f) for f in extra):
                    self.review(record, "Поставщик дополнительно запросил сумму. Перепривяжите лот с правильной формой.")
                else:
                    record["fields"].extend(extra)
                    self.tell(record, "additional_field")
                    self.ask_next(record)
                return
            if str(exc.code) in {"9", "10", "13"}:
                self.disable_lots("Номинал недоступен" if str(exc.code) == "9" else "Баланс/лимит трат AppRoute", [record["lot"]] if str(exc.code) == "9" else None)
                self.fail(record, str(exc))
                return
            if exc.status == 422 and str(exc.code) == "3":
                # Alternative Telegram spelling is attempted only in a free check.
                key = next((k for k, v in record["data"].items() if str(v).startswith("@")), None)
                if "telegram" in record["service_name"].casefold() and key and not record.get("telegram_variant"):
                    record["telegram_variant"] = True
                    record["data"][key] = record["data"][key].removeprefix("@")
                    self.save(record)
                    return
                self.reset_input(record)
                return
            raise
        if result.get("canRecharge") is not True:
            self.reset_input(record)
            return
        cost = quote_cost(result)
        record["quote"] = {"price": str(cost), "at": time.time(), "data": copy.deepcopy(record["data"])}
        self.guard(record, cost * (record["operations"] - record["completed"]))
        attrs = result.get("attributes") or {}
        nickname = next((str(attrs[k]) for k in ("nickname", "username", "name", "accountName", "account_name") if attrs.get(k)), "")
        if s["confirm_account"]:
            nickname = nickname or ", ".join(str(v) for k, v in record["data"].items() if not any(is_amount(f) and field_key(f) == k for f in record["fields"]))
            if not nickname:
                self.review(record, "Нет данных аккаунта для подтверждения.")
                return
            record.update(state="confirm", last_input=time.time(), nickname=nickname[:256])
            self.tell(record, "confirm", nickname=nickname[:256])
        else:
            record.update(state="ready", next_at=0)
        self.save(record)

    def guard(self, record, cost):
        s = self.store.settings()
        if s["sanity_enabled"] and dec(cost) * dec(s["rate"]) > dec(record["revenue"]) * dec(s["sanity_ratio"]):
            raise ShopError("Предохранитель остановил заказ: закупка существенно дороже оплаты покупателя.")

    def disable_lots(self, reason, lots=None):
        for lot in lots if lots is not None else self.store.all("lot"):
            try:
                fields = self.cardinal.account.get_lot_fields(int(lot["id"]))
                fields.active = False
                self.cardinal.account.save_lot(fields.renew_fields())
                self.store.patch("lot", lot["id"], disabled_reason=reason)
            except Exception:
                self.notify(f"⚠️ Не удалось отключить лот {esc(lot['id'])}. Отключите вручную: {esc(reason)}")

    def purchase(self, record):
        s = self.store.settings()
        self.paid(record)
        api = self.api()
        # Check again at every split operation and after account confirmation.
        if s["precheck"]:
            try:
                result = api.check(self.body(record))
            except APIError as exc:
                if str(exc.code) in {"9", "10", "13"}:
                    self.disable_lots("Баланс/лимит/наличие AppRoute", [record["lot"]] if str(exc.code) == "9" else None)
                    self.fail(record, str(exc))
                    return
                raise
            if result.get("canRecharge") is not True:
                if record["completed"]:
                    self.review(record, "Оставшаяся часть не прошла предпроверку.")
                else:
                    self.reset_input(record)
                return
            cost = quote_cost(result)
        else:
            if s["strict_check"] or open_amount(record["fields"]):
                raise ShopError("Для оплаты нужна успешная предпроверка.")
            cost = quote_cost(record["item"]) * record["provider_quantity"]
        item = api.item(record["lot"]["product_id"], record["lot"]["item_id"])
        if not available(item):
            self.disable_lots("Номинал недоступен", [record["lot"]])
            self.fail(record, "Номинал недоступен", "unavailable")
            return
        remaining = record["operations"] - record["completed"]
        self.guard(record, dec(record["cost"]) + cost * remaining)
        balance = api.balance()
        self.record_balance(balance)
        if balance < cost * remaining:
            self.disable_lots("Недостаточно баланса AppRoute")
            self.fail(record, "Недостаточно баланса AppRoute", "balance")
            return
        if not self.active() or not self.store.settings()["enabled"]:
            return
        self.paid(record)
        # Persist exact request and supplier identity BEFORE any paid HTTP request.
        reference = "lt-" + uuid.uuid4().hex
        operation = {"reference": reference, "body": self.body(record, reference), "state": "sent",
                     "expected": str(cost), "created": time.time(), "identity": RequestPacer.identity(s)}
        record.setdefault("transactions", []).append(operation)
        record.update(state="purchasing", next_at=time.time() + 10)
        self.save(record)
        self.tell(record, "processing")
        try:
            result = api.purchase(operation["body"])
        except RequestDeferred:
            # No request was sent. Keep the same body/reference for the next safe send.
            operation["state"] = "unsent"
            self.save(record)
            raise
        except RateLimitError:
            # A 429 can originate upstream: reconcile first, do not assume no charge.
            self.save(record)
            raise
        except APIError as exc:
            if exc.code in (13, "13"):
                self.disable_lots("Исчерпан лимит трат API-ключа")
            # Only explicit pre-admission refusals establish a safe refund.
            if exc.status in {400, 401, 403, 404, 409, 422} and exc.code in {3, 4, 5, 6, 9, 10, 13, "3", "4", "5", "6", "9", "10", "13"}:
                operation["state"] = "rejected"
                self.save(record)
                self.fail(record, str(exc))
            else:
                record["state"] = "pending"
                self.save(record)
            return
        except ShopError:
            record["state"] = "pending"
            self.save(record)
            return
        self.accept_result(record, result)

    def accept_result(self, record, result):
        op = record["transactions"][-1]
        status = enum_name(result.get("status", ""))
        op.update(provider_id=str(result.get("orderId", "")), result=result)
        if result.get("reference", result.get("referenceId", op["reference"])) != op["reference"]:
            self.review(record, "Поставщик вернул другой номер операции.")
            return
        returned_item = result.get("denominationId", result.get("itemId"))
        if returned_item is not None and str(returned_item) != str(record["lot"]["item_id"]):
            self.review(record, "Поставщик вернул результат другого номинала.", "unknown")
            return
        if status in {"SUCCESS", "COMPLETED"}:
            # Do not lose a physical success because accounting metadata is malformed.
            op["state"] = "success"
            record["completed"] += 1
            try:
                record["cost"] = str(dec(record["cost"]) + quote_cost(result))
            except ShopError:
                self.save(record)
                self.review(record, "Пополнение выполнено, но поставщик не вернул достоверное списание USD.")
                return
            if record["completed"] >= record["operations"]:
                record.update(state="done", finished=time.time(), next_at=0)
                self.save(record)
                attrs = (result.get("result") or {}).get("attributes") or {}
                # Allow only customer-facing result keys; never expose arbitrary response secrets.
                extra = "\n".join(f"{k}: {v}" for k, v in attrs.items() if k in {"nickname", "account_reference", "amount", "currency", "message"})
                self.tell(record, "success", extra=extra or record["lot"].get("item_name", record["lot"]["name"]))
                self.notify(f"✅ <b>#{esc(record['id'])}</b> · {esc(record['buyer'])}\n{esc(record['lot']['name'])}\nВыручка: {esc(record['revenue'])} ₽ · закупка: {esc(record['cost'])} USD\nОпераций: {record['completed']} · ref: <code>{esc(op['reference'])}</code>")
            else:
                record.update(state="ready", next_at=0)
                self.tell(record, "progress")
        elif status == "CANCELLED":
            op["state"] = "rejected"
            self.save(record)
            self.fail(record, "Поставщик отменил пополнение.")
        elif status == "PARTIALLY_COMPLETED":
            op["state"] = "partial"
            record["state"] = "partial"
            try:
                record["cost"] = str(dec(record["cost"]) + quote_cost(result))
            except ShopError:
                record["accounting_unknown"] = True
            self.save(record)
            self.review(record, "Поставщик сообщил о частичном выполнении. Возврат заблокирован.", "partial")
            record["state"] = "partial"
            self.save(record)
        elif status == "IN_PROGRESS":
            first = op.get("state") != "pending"
            op["state"] = "pending"
            record.update(state="pending", next_at=time.time() + 10)
            self.save(record)
            if first:
                self.tell(record, "accepted")
        else:
            self.review(record, "Неизвестный статус поставщика. Проверьте операцию вручную.", "unknown")

    def reconcile(self, record):
        op = record["transactions"][-1]
        if RequestPacer.identity(self.store.settings()) != op["identity"]:
            self.review(record, "Изменён ключ/регион незавершённой операции. Проверьте её в прежнем кабинете.", "unknown")
            return
        api = self.api()
        result = api.find_order(op["reference"])
        if result:
            if enum_name(result.get("status")) == "IN_PROGRESS" and time.time()-op["created"] > self.store.settings()["poll_timeout"] * 60:
                self.review(record, "Поставщик слишком долго выполняет операцию. Проверьте результат вручную.", "unknown")
                return
            self.accept_result(record, result)
            return
        if op["state"] == "unsent" and self.store.settings()["enabled"]:
            self.paid(record)
            op["state"] = "sent"
            self.save(record)
            self.accept_result(record, api.purchase(op["body"]))
            return
        if time.time() - op["created"] > self.store.settings()["poll_timeout"] * 60:
            self.review(record, "Результат операции неизвестен. Автоматический повтор и возврат остановлены.", "unknown")
        else:
            record.update(state="pending", next_at=time.time() + 20)
            self.save(record)

    def fail(self, record, reason, message="failed"):
        if record.get("completed") or any(t["state"] not in {"rejected", "unsent"} for t in record.get("transactions", [])):
            self.review(record, reason)
            return
        if self.store.settings()["auto_refund"]:
            record.update(state="refund_pending", reason=reason, next_at=0)
            self.save(record)
        else:
            self.review(record, reason, message)

    def refund(self, record):
        if record.get("completed") or any(t["state"] not in {"rejected", "unsent"} for t in record.get("transactions", [])):
            self.review(record, "Возврат запрещён: есть выполненная или неопределённая операция.")
            return
        order = self.get_order(record["id"])
        if enum_name(order.status) == "REFUNDED":
            record.update(state="refunded", finished=time.time())
            self.tell(record, "refund")
            self.notify(f"↩️ Заказ #{esc(record['id'])} возвращён. {esc(record.get('reason', ''))}")
            return
        if enum_name(order.status) != "PAID":
            self.review(record, "Возврат не выполнен: заказ FunPay больше не PAID.")
            return
        if record.get("refund_sent"):
            self.review(record, "Ответ FunPay на возврат неизвестен. Проверьте заказ вручную.", "refund_pending")
            return
        if not self.store.settings()["auto_refund"]:
            self.review(record, "Автоматический возврат выключен.")
            return
        record["refund_sent"] = True
        self.save(record)
        self.cardinal.account.refund(record["id"])
        record["next_at"] = time.time() + 5
        self.save(record)

    def record_balance(self, value):
        s = self.store.settings()
        low = value < dec(s["low_balance"])
        self.store.configure(balance=str(value), balance_at=time.time(), low_alert=low)
        if low and not s["low_alert"]:
            self.notify(f"👛 Низкий баланс AppRoute: {esc(value)} USD.")

    def refresh_catalog(self):
        self.api().refresh_limits()
        progress = self.store.get("catalog", "progress")
        def checkpoint(value):
            self.store.put("catalog", "progress", value)
        try:
            products = self.api().catalog(progress, checkpoint)
        except (RateLimitError, RequestDeferred) as exc:
            self.store.configure(catalog_retry_at=exc.retry_at)
            raise
        self.store.put("catalog", "main", {"products": products})
        self.store.delete("catalog", "progress")
        self.store.configure(catalog_at=time.time(), catalog_retry_at=0)
        return len(products)

    def maintenance(self):
        s = self.store.settings()
        if not s["api_key"]:
            return
        if time.time() - s["balance_at"] > 600:
            self.record_balance(self.api().balance())
        if time.time() - s["catalog_at"] > s["catalog_interval"] * 60 and time.time() >= s["catalog_retry_at"]:
            self.refresh_catalog()
        if (s["auto_price"] or s["sync_stock"]) and time.time() - s["sync_at"] > s["interval"] * 60:
            self.sync_lots()

    def unit_cost(self, lot, item, service, fields):
        cost = quote_cost(item)
        if not open_amount(fields):
            return cost
        nominal = dec(lot.get("price_nominal") or item.get("nominal") or 0)
        if nominal <= 0:
            raise ShopError("Для автоцены произвольной суммы нужен номинал, которому соответствует цена API. Задайте «База цены» в лоте.")
        s = self.store.settings()
        multiplier = dec(lot.get("fixed_amount") or 0) if lot.get("amount_mode") == "fixed" else dec(lot.get("multiplier") or s["multiplier"])
        currency = lot.get("amount_currency") or ""
        native = lot.get("native_currency") or native_currency(service, open_amount(fields))
        if currency and not native:
            raise ShopError("Для автоцены укажите родную валюту номинала.")
        if currency and currency != native:
            rates = rates_map(self.api().rates())
            if currency not in rates or native not in rates:
                raise ShopError("Нет курса валюты номинала.")
            multiplier = multiplier / rates[currency] * rates[native]
        return cost / nominal * multiplier

    def sync_lots(self, force=False):
        s, errors, changed = self.store.settings(), [], 0
        for lot in self.store.all("lot"):
            if not lot.get("item_id") or not self.active():
                continue
            try:
                service, item, fields = self.product_form(lot)
                fp = self.cardinal.account.get_lot_fields(int(lot["id"]))
                if enum_name(getattr(fp, "currency", "")) != "RUB" or getattr(fp, "auto_delivery", False):
                    raise ShopError("Лот должен быть в RUB с выключенной встроенной автовыдачей.")
                old = fp.price, fp.amount, fp.active
                if (force or s["auto_price"]) and lot.get("auto_price", True):
                    fp.price = float(calculate_price(self.unit_cost(lot, item, service, fields), s))
                if s["sync_stock"]:
                    if not available(item):
                        fp.active = False
                    stock = stock_count(item)
                    if stock is not None and fp.amount is not None and not open_amount(fields):
                        fp.amount = stock
                # Never automatically reactivate a manually/safely disabled offer.
                if old != (fp.price, fp.amount, fp.active):
                    self.cardinal.account.save_lot(fp.renew_fields())
                    changed += 1
                    if s["price_notify"]:
                        self.notify(f"💰 Лот {esc(lot['id'])}: {money(fp.price)} ₽")
                self.store.patch("lot", lot["id"], last_price=str(fp.price), last_cost=str(item["price"]), sync_error="")
            except (RequestDeferred, RateLimitError):
                raise
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ShopError) else "Ошибка сохранения FunPay"
                self.store.patch("lot", lot["id"], sync_error=reason)
                errors.append(f"{lot['id']}: {reason}")
        self.store.configure(sync_at=time.time())
        if errors:
            self.notify("⚠️ Обновление лотов:\n" + esc("\n".join(errors[:10])))
        return changed, len(errors)

    def read_lot(self, lid):
        fields = self.cardinal.account.get_lot_fields(int(lid))
        if getattr(fields, "auto_delivery", False):
            raise ShopError("Сначала отключите встроенную автовыдачу FunPay у лота.")
        title = getattr(fields, "title_ru", "") or getattr(fields, "title_en", "")
        conditions = [title] if title else []
        try:
            response = self.cardinal.account.method("get", f"lots/offerEdit?offer={lid}", {"accept": "*/*"}, {}, raise_not_200=True)
            conditions.extend(selected_conditions(response.content.decode("utf-8")))
        except Exception:
            pass
        lot = self.store.get("lot", lid, {"id": lid, "auto_price": True, "enabled": True, "fixed_fields": {}})
        lot.update(name=lot.get("name") or title or f"Лот {lid}", title=title,
                   subcategory=getattr(getattr(fields, "subcategory", None), "id", None), suggested_conditions=conditions)
        if not lot.get("conditions"):
            lot["conditions"] = conditions
        if "steam" in (title or "").casefold() and "amount_currency" not in lot:
            codes = [c for c in ("RUB", "USD", "KZT", "UAH") if has_phrase(" ".join(conditions), c)]
            if len(codes) == 1:
                lot["amount_currency"] = codes[0]
        self.store.put("lot", lid, lot)
        return lot


GUIDE = [
    ("Запуск", "<b>AppRoute Top-Up · быстрый запуск</b>\n\n1. Установите AppRouteTopUp_LuckyFunpay.py через загрузчик плагинов Cardinal. Включите плагин и перезапустите бота.\n2. Откройте /approute_topup → Настройки → API. Задайте ключ, регион и прокси, проверьте баланс.\n3. Витрина → Каталог → обновите каталог.\n4. Витрина → Лоты → добавьте ID/ссылку FunPay, выберите товар и номинал.\n5. Проверьте форму, сумму и валюту; отключите другие выдачи для этого лота.\n6. Финансы → Автоцены: задайте курс и наценку.\n7. Включите приём заказов на главном экране. Сделайте одну небольшую покупку с другого аккаунта."),
    ("API и прокси", "<b>Подключение AppRoute</b>\n\nСоздайте ключ в кабинете AppRoute → API Access. Нужны чтение каталога/баланса/заказов и создание заказов (product:read, orders:read, orders:write, доступ к accounts). Добавьте в IP allowlist внешний IP сервера или выбранного прокси. Выберите RU или IO по месту регистрации.\n\nПрокси от бота использует cardinal.account.proxy; без него — IP сервера. Собственный прокси: http://user:password@host:port или socks5h://host:port. Ключ и пароль прокси в панели скрыты. Сообщения с ними удаляются, если Telegram разрешает. Проверка ключа читает баланс, но не делает покупку."),
    ("Лоты и формы", "<b>Привязка лота</b>\n\nДобавьте ID или ссылку FunPay, найдите товар по названию и выберите точный номинал/регион. В API product_id — ID сервиса, item_id — ID номинала. Диагностика читает форму и делает только бесплатные checkOnly-запросы.\n\nУсловия определения сравниваются вместе, без приблизительного подбора: строки текста или key=value. Если FunPayAPI передал ID лота, он имеет приоритет. Несколько совпадений — ручная проверка.\n\nФиксированные поля задаются строками key=value. «Поля заказа» сопоставляют поле поставщика с полем FunPay. Сумма через эти поля никогда не считывается."),
    ("Сумма и валюта", "<b>Расчёт суммы</b>\n\nПроизвольная сумма = количество FunPay × множитель. Фиксированная сумма = сумма за одну позицию × количество. Покупатель вводит только аккаунт/сервер; он не может увеличить сумму в чате.\n\nSteam: укажите валюту суммы RUB/KZT/UAH/USD и родную валюту, в которой поставщик задаёт номинал/границы. Курсы приходят из /steam-currency/rates. Для Stars количество означает звёзды: множитель 1, валюту не отправлять. Для Premium выбирайте точный пакет месяцев.\n\nЕсли валюта границ неизвестна, задайте ручные min-max в валюте суммы; плагин остановит оплату до уточнения. Диапазон можно задать с одной границей: 10- или -1000."),
    ("Автоцены", "<b>Цена на FunPay</b>\n\nЦена закупки USD × курс USD/USDT → RUB × (1 + наценка/100), с нижним/верхним пределом. Кнопка обновления сохраняет цену в лоте FunPay.\n\nДля открытой суммы: цена API / номинал API × сумма одной позиции, с пересчётом валюты. Если API не указал номинал, задайте «База цены» — сумму, которой соответствует эта цена. Если цена API относится к недостоверному номиналу, выключите автоцену для лота.\n\nСинхронизация выключает недоступные лоты и обновляет известные остатки фиксированных пакетов. Отключённые лоты включайте вручную после устранения причины. Баланс API обозначен USD; USDT — способ пополнения кабинета."),
    ("Диалог", "<b>Данные покупателя</b>\n\nВопросы задаются по форме поставщика. В списках принимаются номер, название или значение. Необязательное поле можно пропустить символом -. Данные из точных полей заказа подставляются автоматически.\n\ncheckOnly проверяет аккаунт и цену без покупки. После проверки покупатель подтверждает найденный ник или введённый аккаунт. «Нет» запускает повторный ввод. Перед оплатой проверка и статус PAID выполняются ещё раз.\n\nНесколько заказов покупателя идут по очереди. Диалоги сохраняются при перезапуске. Слово «отмена» не возвращает деньги; отмену выполняет продавец. Таймаут передаёт заказ продавцу."),
    ("Ошибки и возвраты", "<b>Защита оплаты</b>\n\nКаждая операция сохраняет уникальный referenceId и точное тело запроса до отправки денег. После сетевой ошибки плагин читает историю поставщика; новая оплата автоматически не создаётся. После изменения ключа/региона незавершённые операции требуют сверки.\n\nАвтовозврат возможен только до оплаты либо после достоверного отказа/отмены поставщика. Частичный результат, таймаут ответа покупателя, неизвестный итог поставщика, предохранитель и неизвестная форма передаются продавцу без возврата.\n\nПри недостатке баланса или лимите трат ключа лоты отключаются. Ограничение HTTP 429 сохраняет паузу и повторяет безопасные проверки позже. Не удаляйте базу storage/plugins/approute_topup."),
    ("Заказы и история", "<b>Работа с заказами</b>\n\nЗаказы → Активные: состояние, покупатель и прогресс. История содержит все обслуживаемые заказы, в том числе ошибки и частичные пополнения, номер операции и списание. Финансы → Статистика: выполненные заказы и выручка за 24 часа, 7/30 дней и всё время; закупка отражает все известные списания.\n\n«Сверить операцию» выполняет только чтение AppRoute. «Продолжить диалог» доступно для ручной проверки до любых операций поставщика. «Остановить диалог» передаёт неоплаченный у поставщика заказ вам; деньги не возвращаются.\n\nШаблоны редактируются в Настройки → Сообщения. Обязательные подстановки защищены. Экспорт журнала содержит аккаунты покупателей — храните его приватно."),
]

SETTING_LABELS = {
    "rate": "Курс USD/USDT → RUB", "markup": "Наценка, %", "min_price": "Минимальная цена, ₽", "max_price": "Максимальная цена, ₽",
    "interval": "Интервал цен, мин", "max_quantity": "Лимит отдельных пополнений", "low_balance": "Порог баланса, USD",
    "sanity_ratio": "Порог предохранителя, ×", "timeout_min": "Ожидание ответа, мин", "input_attempts": "Попытки ввода",
    "multiplier": "Общий множитель суммы", "catalog_interval": "Интервал каталога, мин", "poll_timeout": "Ожидание поставщика, мин",
    "api_key": "API-ключ", "proxy": "Свой прокси", "name": "Название лота", "conditions": "Условия определения",
    "fixed_fields": "Фиксированные поля", "aliases": "Поля из заказа FunPay", "ids": "product_id и item_id",
    "amount_currency": "Валюта суммы", "native_currency": "Родная валюта номинала", "fixed_amount": "Сумма одной позиции",
    "limits": "Границы суммы", "price_nominal": "База цены API", "lot_id": "ID или ссылка на лот", "search": "Поиск в каталоге",
}


class TelegramUI:
    def __init__(self, shop):
        from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
        self.Button, self.Markup = InlineKeyboardButton, InlineKeyboardMarkup
        self.shop, self.store = shop, shop.store
        self.tg, self.bot = shop.cardinal.telegram, shop.cardinal.telegram.bot
        self.tokens, self.pending = {}, {}
        self.lock = threading.RLock()

    def allowed(self, user, chat):
        users = getattr(self.tg, "authorized_users", {})
        return (int(user.id) in users or str(user.id) in users) and chat.type == "private" and self.shop.active()

    def markup(self, rows):
        kb = self.Markup()
        for row in rows:
            buttons = []
            for label, action in row:
                if isinstance(action, str) and action.startswith("https://"):
                    buttons.append(self.Button(label, url=action))
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
        rows = rows or [[("🏠 Главная", ("home",))]]
        kwargs = {"reply_markup": self.markup(rows), "parse_mode": "HTML", "disable_web_page_preview": True}
        if mid:
            try:
                return self.bot.edit_message_text(text, chat, mid, **kwargs)
            except Exception:
                pass
        return self.bot.send_message(chat, text, **kwargs)

    def home(self, chat, mid=None):
        s = self.store.settings()
        lots, orders = self.store.all("lot"), self.store.all("order")
        products = self.store.get("catalog", "main", {}).get("products", [])
        status = "🟢 Приём заказов включён" if s["enabled"] else "⏸ Приём заказов на паузе"
        text = f"⚡ <b>{NAME}</b> <code>{VERSION}</code>\n{status}\n\n<b>👛 Счёт</b>\n{esc(s['balance'] or '—')} USD · регион {s['region']}\n\n<b>🛍 Витрина</b>\nЛотов: {sum(bool(l.get('item_id')) for l in lots)} · товаров: {len(products)}\n\n<b>📋 Заказы</b>\nВ работе: {sum(r['state'] in ACTIVE_STATES for r in orders)} · выполнено: {sum(r['state']=='done' for r in orders)}\n\nАвтор: <a href='{SUPPORT_URL}'>{CREDITS}</a>"
        rows = [[("⏸ Пауза" if s["enabled"] else "▶️ Включить", ("enable", not s["enabled"])), ("🔔 Уведомления" if s["notifications"] else "🔕 Уведомления", ("toggle", "notifications", "home"))],
                [("🛍 Витрина", ("showcase",)), ("📋 Заказы", ("orders",))],
                [("💰 Финансы", ("finance",)), ("⚙️ Настройки", ("settings",))],
                [("📖 Инструкция", ("guide", 0)), ("📣 Канал", DEVELOPER_URL)],
                [("👤 Автор", SUPPORT_URL), ("🧩 Плагины", ("plugins",))]]
        self.show(chat, text, rows, mid)

    def lot(self, chat, lid, mid=None):
        lot = self.store.get("lot", lid)
        if not lot:
            raise ShopError("Лот удалён.")
        fixed = ", ".join(f"{k}={v}" for k, v in lot.get("fixed_fields", {}).items()) or "—"
        text = f"🛍 <b>{esc(lot['name'])}</b>\nFunPay ID: <code>{lid}</code>\n\n<b>Привязка</b>\n{esc(lot.get('service_name', 'Товар не выбран'))}\n{esc(lot.get('item_name', ''))}\nproduct_id: <code>{esc(lot.get('product_id', '—'))}</code>\nitem_id: <code>{esc(lot.get('item_id', '—'))}</code>\n\n<b>Расчёт</b>\nРежим: {esc(lot.get('amount_mode') or self.store.settings()['amount_mode'])} · множитель {esc(lot.get('multiplier') or self.store.settings()['multiplier'])}\nФикс. сумма: {esc(lot.get('fixed_amount', '—'))} · валюта {esc(lot.get('amount_currency', 'как у поставщика') or 'не отправлять')}\nЦена API: {esc(lot.get('last_cost', '—'))} USD · FunPay {esc(lot.get('last_price', '—'))} ₽\nФикс. поля: {esc(fixed)}\nУсловия: {esc('; '.join(lot.get('conditions', [])))}\n{esc(lot.get('sync_error', ''))}"
        rows = [[("🔗 Выбрать товар", ("ask", "search", lid)), ("🔎 Форма и диагностика", ("diagnose", lid))],
                [("🧮 Сумма", ("amount", lid)), ("🌍 Валюта", ("currency", lid))],
                [("🧩 Фикс. поля", ("ask", "fixed_fields", lid)), ("📥 Поля заказа", ("ask", "aliases", lid))],
                [("✏️ Название", ("ask", "name", lid)), ("🎯 Условия", ("ask", "conditions", lid))],
                [("💰 Автоцена: " + ("вкл" if lot.get("auto_price", True) else "выкл"), ("lot_toggle", lid, "auto_price")), ("🔢 Ввести ID", ("ask", "ids", lid))],
                [("♻️ Перечитать", ("reread", lid)), ("🗑 Удалить", ("confirm", "delete_lot", lid))],
                [("🛍 Лоты", ("lots", 0)), ("🏠 Главная", ("home",))]]
        self.show(chat, text, rows, mid)

    def paged(self, chat, values, title, route, page, label, action, mid=None, extra=None):
        page = min(max(0, int(page)), max(0, (len(values)-1)//6))
        rows = [[(label(v)[:100], action(v))] for v in values[page*6:(page+1)*6]]
        nav = []
        if page:
            nav.append(("◀️", (*route, page-1)))
        if (page+1)*6 < len(values):
            nav.append(("▶️", (*route, page+1)))
        if nav:
            rows.append(nav)
        rows.extend(extra or [])
        rows.append([("🏠 Главная", ("home",))])
        self.show(chat, title + f"\nЗаписей: {len(values)} · страница {page+1}", rows, mid)

    def products(self):
        return self.store.get("catalog", "main", {}).get("products", [])

    def catalog(self, chat, lid="", query="", page=0, mid=None):
        values = [p for p in self.products() if not query or query.casefold() in (str(p.get("name", "")) + " " + str(p.get("categoryName", ""))).casefold()]
        self.paged(chat, values, "🗂 <b>Каталог прямого пополнения</b>\n" + ("Поиск: " + esc(query) if query else "Выберите сервис"),
                   ("catalog", lid, query), page, lambda p: str(p.get("name", p["id"])), lambda p: ("items", lid, p["id"], 0), mid,
                   [[("🔍 Поиск", ("ask", "search", lid)), ("🔄 Обновить", ("refresh_catalog",))]])

    def pricing(self, chat, mid=None):
        s = self.store.settings()
        text = f"💰 <b>Автоцены на FunPay</b>\n\nЗакупка USD × курс × (1 + наценка/100)\nКурс: {s['rate']} ₽ · наценка: {s['markup']}%\nДиапазон: {s['min_price']}–{s['max_price']} ₽\nИнтервал: {s['interval']} мин\n\n{esc('При синхронизации цены сохраняются непосредственно в FunPay. Открытой сумме нужна достоверная база цены API.')}"
        rows = [[("📈 Наценка", ("ask", "markup", "")), ("💱 Курс", ("ask", "rate", ""))],
                [("📉 Минимум", ("ask", "min_price", "")), ("📈 Максимум", ("ask", "max_price", ""))],
                [("⏱ Интервал", ("ask", "interval", "")), ("🔄 Обновить цены", ("sync",))],
                [("Автоцены: " + str(s["auto_price"]), ("toggle", "auto_price", "pricing")), ("Остатки: " + str(s["sync_stock"]), ("toggle", "sync_stock", "pricing"))],
                [("Отчёт цен: " + str(s["price_notify"]), ("toggle", "price_notify", "pricing")), ("🛡 Защита", ("safety",))],
                [("💰 Финансы", ("finance",)), ("🏠 Главная", ("home",))]]
        self.show(chat, text, rows, mid)

    def api_menu(self, chat, mid=None):
        s = self.store.settings()
        key = s["api_key"]
        masked = "••••" + key[-4:] if key else "не задан"
        text = f"🔑 <b>Подключение AppRoute</b>\n\nКлюч: <code>{esc(masked)}</code>\nРегион: {s['region']}\nАдрес: https://approute.{s['region'].lower()}/api/v1\nМаршрут: {'от бота' if s['proxy_mode']=='bot' else 'свой прокси (скрыт)'}\n\nAPI баланс и закупки обозначает USD. Пополнение кабинета возможно через USDT."
        self.show(chat, text, [[("✏️ Ключ", ("ask", "api_key", "")), ("🌍 Регион: " + s["region"], ("region",))],
                              [("🧪 Проверить ключ", ("balance",)), ("🌐 Сменить маршрут", ("proxy_mode",))],
                              [("🔐 Свой прокси", ("ask", "proxy", "")), ("⏱ Каталог", ("ask", "catalog_interval", ""))],
                              [("⚙️ Настройки", ("settings",)), ("🏠 Главная", ("home",))]], mid)

    def dialog(self, chat, mid=None):
        s = self.store.settings()
        fields = [("precheck", "🔍 Предпроверка"), ("strict_check", "🛡 Строгая проверка"), ("confirm_account", "👤 Подтверждение"),
                  ("discover_fields", "🧪 Узнавать форму"), ("require_form", "🧩 Требовать форму"), ("telegram_no_at", "✈️ Ник без @"),
                  ("split_quantity", "🔢 По одному"), ("auto_refund", "↩️ Автовозврат")]
        buttons = [(label + (" · вкл" if s[key] else " · выкл"), ("toggle", key, "dialog")) for key, label in fields]
        rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
        rows.extend([[("⏰ Ответ: " + str(s["timeout_min"]) + " мин", ("ask", "timeout_min", "")), ("🔁 Попытки: " + str(s["input_attempts"]), ("ask", "input_attempts", ""))],
                     [("🧮 Множитель", ("ask", "multiplier", "")), ("⌛ Поставщик", ("ask", "poll_timeout", ""))],
                     [("✉️ Сообщения", ("templates", 0)), ("⚙️ Настройки", ("settings",))]])
        self.show(chat, "💬 <b>Сценарий покупателя</b>\n\nДанные → бесплатная проверка → подтверждение → пополнение.\nСумма определяется оплаченным количеством, не сообщениями покупателя.\nОчередь и ответы сохраняются после перезапуска.", rows, mid)

    def order(self, chat, oid, mid=None):
        r = self.store.get("order", oid)
        if not r:
            raise ShopError("Заказ не найден.")
        transactions = r.get("transactions", [])
        refs = "\n".join(f"{t.get('provider_id', '—')} · {t['reference']} · {t['state']}" for t in transactions)
        data = ", ".join(f"{k}: {'••••' if any(x in k.lower() for x in ('pass', 'secret', 'token')) else v}" for k, v in r.get("data", {}).items())
        text = f"📋 <b>Заказ #{esc(oid)}</b>\n{esc(STATE_NAMES.get(r['state'], r['state']))}\n\n<b>Покупатель</b>\n{esc(r.get('buyer', '—'))} · ID {esc(r.get('buyer_id', '—'))}\nВыручка: {esc(r.get('revenue', '—'))} ₽\n\n<b>Пополнение</b>\n{esc(r.get('lot', {}).get('name', '—'))}\nКоличество: {r.get('quantity', '—')} · прогресс {r.get('completed', 0)}/{r.get('operations', '—')}\nЗакупка: {esc(r.get('cost', '0'))} USD\n{esc(data)}\n\n<b>Операции</b>\n<code>{esc(refs or 'не отправлялись')}</code>\n\n{esc(r.get('reason', ''))}"
        rows = [[("💙 FunPay", f"https://funpay.com/orders/{quote(oid, safe='')}/")]]
        if r.get("buyer_id", "").isdigit():
            rows[0].append(("👤 Покупатель", f"https://funpay.com/users/{r['buyer_id']}/"))
        if transactions:
            rows.append([("🔎 Сверить операцию", ("reconcile", oid))])
            if r["state"] in {"review", "partial"}:
                rows.append([("⏹ Завершить после сверки", ("confirm", "close", oid))])
        elif r["state"] == "review":
            rows.append([("▶️ Продолжить диалог", ("confirm", "resume", oid)), ("⏹ Закрыть вручную", ("confirm", "close", oid))])
        elif r["state"] in WAIT_STATES:
            rows.append([("⏹ Остановить диалог", ("confirm", "stop_dialog", oid))])
        rows.extend([[("📜 История", ("history", 0)), ("🏠 Главная", ("home",))]])
        self.show(chat, text[:3900], rows, mid)

    def stats(self, chat, mid=None):
        orders, now = self.store.all("order"), time.time()
        lines = []
        for name, days in (("24 часа", 1), ("7 дней", 7), ("30 дней", 30), ("Всё время", None)):
            values = [r for r in orders if days is None or r.get("finished", r.get("updated", r["created"])) >= now-days*86400]
            done = [r for r in values if r["state"] == "done"]
            revenue = sum((dec(r.get("revenue", "0")) for r in done), Decimal(0))
            cost = sum((dec(r.get("cost", "0")) for r in values), Decimal(0))
            lines.append(f"<b>{name}</b>\nЗаказов: {len(done)} · выручка {money(revenue)} ₽\nИзвестная закупка: {money(cost)} USD")
        self.show(chat, "📊 <b>Статистика</b>\n\n" + "\n\n".join(lines) + "\n\nЗакупка включает известные частичные списания. Выручка — только полностью выполненные заказы.",
                  [[("📜 История", ("history", 0)), ("📥 Экспорт", ("export",))], [("💰 Финансы", ("finance",)), ("🏠 Главная", ("home",))]], mid)

    def prompt(self, chat, user, key, lid=""):
        hints = {"lot_id": "Отправьте ID или ссылку https://funpay.com/lots/offer?id=12345.",
                 "fixed_fields": "Строки key=value. Список допустимых полей — в диагностике. Сумму здесь задавать нельзя. '-' — очистить.",
                 "aliases": "Строки ключ_поставщика=ключ_поля_FunPay. '-' — очистить.",
                 "ids": "product_id item_id (два ID через пробел).", "conditions": "Все условия должны совпадать. По одному на строке: фраза или key=value. '-' — вернуть прочитанные условия.",
                 "limits": "min-max, например 50-10000. '-' — границы поставщика.",
                 "amount_currency": "Три латинские буквы, например RUB. '-' — не отправлять валюту.",
                 "native_currency": "Родная валюта, в которой задан номинал и границы API (например USD). '-' — убрать.",
                 "price_nominal": "Сумма, которой соответствует цена API. Например: API price=1 USD за nominal=100 звёзд → введите 100.",
                 "proxy": "host:port, user:password@host:port или socks5h://host:port. '-' — убрать."}
        if key.startswith("template:"):
            name = key.split(":", 1)[1]
            title = TEMPLATES[name][0]
            hint = "Подстановки: " + ", ".join("{" + k + "}" for k in sorted(TEMPLATE_FIELDS)) + "\nОбязательные: " + ", ".join(REQUIRED_TEMPLATE_FIELDS.get(name, set())) + "\n'-' — стандартный текст."
        else:
            title, hint = SETTING_LABELS.get(key, key), hints.get(key, "Отправьте новое значение одним сообщением.")
        sent = self.bot.send_message(chat, "✏️ <b>" + esc(title) + "</b>\n\n" + esc(hint) + "\n\nОтветьте на это сообщение. Отмена: /topup_cancel", parse_mode="HTML")
        self.pending[(str(chat), str(user))] = (key, lid, sent.message_id, time.time())

    def has_input(self, message):
        return (str(message.chat.id), str(message.from_user.id)) in self.pending

    def message(self, message):
        if not self.allowed(message.from_user, message.chat):
            return
        key = (str(message.chat.id), str(message.from_user.id))
        pending = self.pending.get(key)
        if not pending:
            return
        name, lid, prompt_id, created = pending
        if message.text == "/topup_cancel":
            self.pending.pop(key, None)
            self.home(message.chat.id)
            return
        reply = getattr(message, "reply_to_message", None)
        if time.time()-created > 900 or reply and reply.message_id != prompt_id:
            self.show(message.chat.id, "Форма ввода устарела или ответ относится к другому сообщению. Откройте настройку снова.")
            return
        value = message.text or ""
        if name in {"api_key", "proxy"}:
            try:
                self.bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
        def action():
            try:
                self.apply_input(name, lid, value)
                self.pending.pop(key, None)
                if name == "search":
                    self.catalog(message.chat.id, lid, value)
                elif lid:
                    self.lot(message.chat.id, lid)
                else:
                    self.home(message.chat.id)
            except Exception as exc:
                self.show(message.chat.id, esc(str(exc) if isinstance(exc, ShopError) else "Ошибка сохранения. Проверьте данные и повторите."))
        self.shop.submit(action)

    def ensure_identity_change(self):
        if any(r.get("transactions") and r["state"] in ACTIVE_STATES | {"review", "partial"} for r in self.store.all("order")):
            raise ShopError("Сначала завершите сверку незавершённых операций; менять ключ/регион сейчас нельзя.")

    def apply_input(self, key, lid, value):
        value = value.strip()
        if key == "search":
            if len(value) > 100:
                raise ShopError("Поиск: не более 100 символов.")
            return
        if key == "lot_id":
            self.shop.read_lot(lot_id_from_text(value))
            return
        if key.startswith("template:"):
            name = key.split(":", 1)[1]
            templates = self.store.settings()["templates"].copy()
            if value == "-":
                templates.pop(name, None)
            else:
                templates[name] = validate_template(name, value)
            self.store.configure(templates=templates)
            return
        if key == "api_key":
            self.ensure_identity_change()
            if not 8 <= len(value) <= 512 or any(c.isspace() for c in value):
                raise ShopError("Некорректный формат ключа.")
            self.store.configure(api_key=value, catalog_at=0, balance_at=0)
            self.store.delete("catalog", "progress")
            self.store.delete("catalog", "main")
            return
        if key == "proxy":
            self.store.configure(proxy="" if value == "-" else proxy_url(value), proxy_mode="bot" if value == "-" else "own")
            return
        if key in {"fixed_fields", "aliases", "conditions", "name", "ids", "limits", "amount_currency", "native_currency", "price_nominal", "fixed_amount"} or lid and key == "multiplier":
            lot = self.store.get("lot", lid)
            if not lot:
                raise ShopError("Лот не найден.")
            if key in {"fixed_fields", "aliases"}:
                data = {}
                for line in [] if value == "-" else value.splitlines():
                    if "=" not in line:
                        raise ShopError("Требуются строки key=value.")
                    k, v = [p.strip() for p in line.split("=", 1)]
                    field = next((f for f in lot.get("form", []) if field_key(f) == k), None)
                    if not field or is_amount(field):
                        raise ShopError("Сначала выполните диагностику формы. Укажите существующее поле аккаунта/сервера, не сумму.")
                    data[k] = validate_field(field, v, lot.get("service_name", ""), self.store.settings()) if key == "fixed_fields" else v
                lot[key] = data
            elif key == "conditions":
                lot[key] = lot.get("suggested_conditions", []) if value == "-" else [x.strip() for x in value.splitlines() if x.strip()]
            elif key == "name":
                if not 1 <= len(value) <= 120:
                    raise ShopError("Название: 1–120 символов.")
                lot[key] = value
            elif key == "ids":
                ids = value.split()
                if len(ids) != 2 or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", x) for x in ids):
                    raise ShopError("Нужны два ID через пробел.")
                lot.update(product_id=ids[0], item_id=ids[1])
                service, item, fields = self.shop.product_form(lot)
                lot.update(form=fields, service_name=service.get("name", ""), item_name=item.get("name", ""),
                           last_cost=str(item.get("price", "")), native_currency=native_currency(service, open_amount(fields) or {}) or lot.get("native_currency", ""), fixed_fields={}, aliases={})
            elif key in {"amount_currency", "native_currency"}:
                if value != "-" and not re.fullmatch(r"[A-Za-z]{3}", value):
                    raise ShopError("Нужен код валюты из трёх латинских букв или '-'.")
                lot[key] = "" if value == "-" else value.upper()
            elif key == "limits":
                if value == "-":
                    lot.pop(key, None)
                else:
                    found = re.fullmatch(r"(\d+(?:[.,]\d+)?)?-(\d+(?:[.,]\d+)?)?", value)
                    if not found or not any(found.groups()):
                        raise ShopError("Формат min-max, например 50-1000.")
                    low, high = (str(dec(x)) if x is not None else None for x in found.groups())
                    if low and high and dec(low) > dec(high):
                        raise ShopError("Минимум превышает максимум.")
                    lot[key] = {"min": low, "max": high}
            else:
                if value == "-":
                    lot.pop(key, None)
                elif dec(value) <= 0:
                    raise ShopError("Число должно быть положительным.")
                else:
                    lot[key] = str(dec(value))
            self.store.put("lot", lid, lot)
            return
        limits = {"rate": ("0.000001", "1000000"), "markup": ("0", "10000"), "min_price": ("0.01", "1000000000"),
                  "max_price": ("0.01", "1000000000"), "interval": (1, 10080), "max_quantity": (1, 100000),
                  "low_balance": (0, 1000000000), "sanity_ratio": (1, 1000), "timeout_min": (1, 1440),
                  "input_attempts": (1, 20), "multiplier": ("0.000001", "1000000000"), "catalog_interval": (5, 10080), "poll_timeout": (1, 1440)}
        if key not in limits:
            raise ShopError("Неизвестная настройка.")
        number = dec(value)
        low, high = limits[key]
        if not dec(low) <= number <= dec(high):
            raise ShopError(f"Допустимый диапазон: {low}–{high}.")
        val = integer(number) if key in {"interval", "max_quantity", "timeout_min", "input_attempts", "catalog_interval", "poll_timeout"} else str(number)
        s = self.store.settings()
        s[key] = val
        if dec(s["min_price"]) > dec(s["max_price"]):
            raise ShopError("Минимальная цена не должна превышать максимальную.")
        self.store.configure(**{key: val})

    def command(self, message):
        if not self.allowed(message.from_user, message.chat):
            return
        chats = self.store.settings()["notify_chats"]
        if message.chat.id not in chats:
            self.store.configure(notify_chats=chats + [message.chat.id])
        self.home(message.chat.id)

    def callback(self, call):
        if not call.message or not self.allowed(call.from_user, call.message.chat):
            return
        try:
            self.bot.answer_callback_query(call.id)
        except Exception:
            pass
        with self.lock:
            entry = self.tokens.get(call.data.removeprefix(PREFIX))
        if not entry or time.time()-entry[0] > 86400:
            self.home(call.message.chat.id)
            return
        action = entry[1]
        chat, user, mid = call.message.chat.id, call.from_user.id, call.message.message_id
        # Keep HTTP out of the Telegram handler. All mutations run in the same worker as purchases.
        def run():
            try:
                self.dispatch(chat, user, action, mid)
            except (RequestDeferred, RateLimitError) as exc:
                self.show(chat, esc(str(exc)), mid=mid)
            except Exception as exc:
                self.show(chat, esc(str(exc) if isinstance(exc, ShopError) else "Не удалось выполнить действие. Проверьте подключение."), mid=mid)
        self.shop.submit(run)

    def dispatch(self, chat, user, action, mid=None):
        op, *args = action
        s = self.store.settings()
        if op == "home":
            self.home(chat, mid)
        elif op in {"showcase", "orders", "finance", "settings"}:
            panels = {
                "showcase": ("🛍 <b>Витрина</b>\nПривяжите ваши лоты к номиналам поставщика.", [[("📦 Лоты", ("lots", 0)), ("🗂 Каталог", ("catalog", "", "", 0))], [("➕ Добавить лот", ("ask", "lot_id", "")), ("🏠 Главная", ("home",))]]),
                "orders": ("📋 <b>Центр заказов</b>\nДиалоги, выполнение и операции поставщика.", [[("💬 Активные", ("active", 0)), ("📜 История", ("history", 0))], [("📥 Экспорт", ("export",)), ("🏠 Главная", ("home",))]]),
                "finance": ("💰 <b>Финансы</b>\nБаланс: " + esc(s["balance"] or "—") + " USD", [[("💱 Автоцены", ("pricing",)), ("📊 Статистика", ("stats",))], [("👛 Обновить баланс", ("balance",)), ("🛡 Защита", ("safety",))], [("🏠 Главная", ("home",))]]),
                "settings": ("⚙️ <b>Настройки</b>\nПодключение и общие правила работы.", [[("🔑 API", ("api",)), ("💬 Диалог", ("dialog",))], [("✉️ Сообщения", ("templates", 0)), ("📖 Инструкция", ("guide", 0))], [("👤 Автор", SUPPORT_URL), ("📣 Канал", DEVELOPER_URL)], [("🏠 Главная", ("home",))]])}
            text, rows = panels[op]
            self.show(chat, text, rows, mid)
        elif op == "enable":
            self.store.configure(enabled=bool(args[0]))
            self.home(chat, mid)
        elif op == "toggle":
            key, page = args
            allowed = {"notifications", "auto_price", "sync_stock", "price_notify", "sanity_enabled", "precheck", "strict_check", "confirm_account", "discover_fields", "require_form", "telegram_no_at", "split_quantity", "auto_refund"}
            if key not in allowed:
                raise ShopError("Неизвестный переключатель.")
            self.store.configure(**{key: not s[key]})
            self.dispatch(chat, user, (page,), mid)
        elif op == "api":
            self.api_menu(chat, mid)
        elif op == "region":
            self.ensure_identity_change()
            self.store.configure(region="IO" if s["region"] == "RU" else "RU", catalog_at=0, balance_at=0)
            self.store.delete("catalog", "progress")
            self.store.delete("catalog", "main")
            self.api_menu(chat, mid)
        elif op == "proxy_mode":
            if s["proxy_mode"] == "bot" and not s["proxy"]:
                raise ShopError("Сначала задайте свой прокси.")
            self.store.configure(proxy_mode="own" if s["proxy_mode"] == "bot" else "bot")
            self.api_menu(chat, mid)
        elif op == "balance":
            self.shop.record_balance(self.shop.api().balance())
            self.show(chat, "✅ Подключение работает. Баланс: <b>" + esc(self.store.settings()["balance"]) + " USD</b>\nПрава на создание заказов проверяются при работе; тестовая покупка не выполнялась.", [[("🔑 API", ("api",)), ("🏠 Главная", ("home",))]], mid)
        elif op == "lots":
            self.paged(chat, self.store.all("lot"), "📦 <b>Лоты FunPay</b>", ("lots",), args[0], lambda l: f"{l['id']} · {l['name']}", lambda l: ("lot", l["id"]), mid,
                       [[("➕ Добавить", ("ask", "lot_id", "")), ("🗂 Каталог", ("catalog", "", "", 0))]])
        elif op == "lot":
            self.lot(chat, args[0], mid)
        elif op == "lot_toggle":
            lid, key = args
            lot = self.store.get("lot", lid)
            self.store.patch("lot", lid, **{key: not lot.get(key, True)})
            self.lot(chat, lid, mid)
        elif op == "reread":
            self.shop.read_lot(args[0])
            self.lot(chat, args[0], mid)
        elif op == "catalog":
            self.catalog(chat, *args, mid=mid)
        elif op == "refresh_catalog":
            count = self.shop.refresh_catalog()
            self.show(chat, f"✅ Каталог обновлён: {count} сервисов прямого пополнения.", [[("🗂 Каталог", ("catalog", "", "", 0)), ("🏠 Главная", ("home",))]], mid)
        elif op == "items":
            lid, pid, page = args
            p = next((p for p in self.products() if str(p["id"]) == pid), None)
            if not p:
                raise ShopError("Сервис не найден. Обновите каталог.")
            self.paged(chat, p.get("items", []), "🗂 <b>" + esc(p.get("name", pid)) + "</b>", ("items", lid, pid), page,
                       lambda i: f"{i.get('name', i['id'])} · {i.get('price', '—')} {i.get('currency', '')}", lambda i: ("bind" if lid else "inspect_item", lid, pid, i["id"]), mid)
        elif op == "inspect_item":
            _, pid, iid = args
            self.show(chat, f"🔢 product_id: <code>{esc(pid)}</code>\nitem_id: <code>{esc(iid)}</code>\nДобавьте лот и выберите этот номинал в карточке.", [[("➕ Добавить лот", ("ask", "lot_id", "")), ("🏠 Главная", ("home",))]], mid)
        elif op == "bind":
            lid, pid, iid = args
            self.apply_input("ids", lid, f"{pid} {iid}")
            self.lot(chat, lid, mid)
        elif op == "diagnose":
            lot = self.store.get("lot", args[0])
            if not lot.get("item_id"):
                raise ShopError("Сначала выберите товар.")
            service, item, fields = self.shop.product_form(lot)
            lines = [f"{field_key(f)} · {f.get('type', 'text')} · {'обязательно' if f.get('required') else 'необязательно'}" + (" · из оплаченного заказа" if is_amount(f) else "") + ("\nВарианты: " + ", ".join(str(o['value']) for o in f.get('options', [])) if f.get('options') else "") for f in fields]
            self.show(chat, "🔎 <b>Диагностика формы</b>\n" + esc(service.get("name", "")) + "\n\n" + esc("\n".join(lines)) + "\n\nЦена API: " + esc(item.get("price", "—")) + " USD\nПокупка не выполнялась.", [[("📦 Лот", ("lot", lot["id"])), ("🧩 Фикс. поля", ("ask", "fixed_fields", lot["id"]))]], mid)
        elif op == "amount":
            lid = args[0]
            lot = self.store.get("lot", lid)
            self.show(chat, "🧮 <b>Сумма лота</b>\nРежим: " + esc(lot.get("amount_mode") or s["amount_mode"]) + "\nСумма рассчитывается за каждую оплаченную позицию. Покупатель её не задаёт.",
                      [[("Из количества", ("amount_mode", lid, "quantity")), ("Фиксированная", ("amount_mode", lid, "fixed"))],
                       [("✖️ Множитель", ("ask", "multiplier", lid)), ("📌 Фикс. сумма", ("ask", "fixed_amount", lid))],
                       [("📏 Границы", ("ask", "limits", lid)), ("💰 База цены", ("ask", "price_nominal", lid))],
                       [("🌍 Валюта", ("currency", lid)), ("📦 Лот", ("lot", lid))]], mid)
        elif op == "amount_mode":
            self.store.patch("lot", args[0], amount_mode=args[1])
            self.dispatch(chat, user, ("amount", args[0]), mid)
        elif op == "currency":
            lid = args[0]
            lot = self.store.get("lot", lid)
            self.show(chat, "🌍 <b>Валюта суммы</b>\nВыбрана: " + esc(lot.get("amount_currency", "поставщика") or "не отправлять") + "\nРодная: " + esc(lot.get("native_currency", "не задана")) + "\nДля Stars и TON валюту не отправляйте.",
                      [[("💱 Код валюты", ("ask", "amount_currency", lid)), ("🏦 Родная валюта", ("ask", "native_currency", lid))],
                       [("📊 Курсы Steam", ("rates", lid)), ("📦 Лот", ("lot", lid))]], mid)
        elif op == "rates":
            rates = rates_map(self.shop.api().rates())
            self.show(chat, "💱 <b>Курсы поставщика, база USD</b>\n\n" + esc("\n".join(f"{k}: {v}" for k, v in rates.items())), [[("🌍 Валюта лота", ("currency", args[0])), ("🏠 Главная", ("home",))]], mid)
        elif op == "pricing":
            self.pricing(chat, mid)
        elif op == "sync":
            changed, errors = self.shop.sync_lots(force=True)
            self.show(chat, f"💰 Обновлено лотов: {changed}. Ошибок: {errors}. Причины отображаются в карточках лотов.", [[("💰 Автоцены", ("pricing",)), ("📦 Лоты", ("lots", 0))]], mid)
        elif op == "dialog":
            self.dialog(chat, mid)
        elif op == "safety":
            self.show(chat, f"🛡 <b>Защита заказов</b>\n\nЛимит отдельных пополнений: {s['max_quantity']}\nНизкий баланс: {s['low_balance']} USD\nПредохранитель: {'включён' if s['sanity_enabled'] else 'выключен'}, ×{s['sanity_ratio']}\nАвтовозврат: {s['auto_refund']}\nЧастичное выполнение и неизвестный результат требуют продавца.",
                      [[("🔢 Лимит", ("ask", "max_quantity", "")), ("👛 Порог баланса", ("ask", "low_balance", ""))],
                       [("🛡 Предохранитель", ("toggle", "sanity_enabled", "safety")), ("📐 Порог ×", ("ask", "sanity_ratio", ""))],
                       [("↩️ Автовозврат", ("toggle", "auto_refund", "safety")), ("💰 Финансы", ("finance",))]], mid)
        elif op in {"active", "history"}:
            values = sorted([r for r in self.store.all("order") if r["state"] != "ignored" and (op == "history" or r["state"] in ACTIVE_STATES)], key=lambda r: r["created"], reverse=True)
            self.paged(chat, values, "📋 <b>" + ("Активные заказы" if op == "active" else "Журнал заказов") + "</b>", (op,), args[0],
                       lambda r: f"#{r['id']} · {r.get('buyer', '')} · {STATE_NAMES.get(r['state'], r['state'])}", lambda r: ("order", r["id"]), mid)
        elif op == "order":
            self.order(chat, args[0], mid)
        elif op == "reconcile":
            r = self.store.get("order", args[0])
            if not r.get("transactions"):
                raise ShopError("Операций поставщика нет.")
            # Terminal success must never be counted twice by repeated button clicks.
            if r["transactions"][-1]["state"] in {"success", "partial", "rejected"}:
                raise ShopError("У этой операции уже известен конечный результат.")
            self.shop.reconcile(r)
            self.order(chat, args[0], mid)
        elif op == "stats":
            self.stats(chat, mid)
        elif op == "export":
            import io
            content = io.BytesIO(json.dumps(self.store.all("order"), ensure_ascii=False, indent=2).encode("utf-8"))
            content.name = "AppRouteTopUp_orders.json"
            self.bot.send_document(chat, content, caption="Журнал заказов · @LuckyFunPay_Support")
        elif op == "templates":
            self.paged(chat, list(TEMPLATES), "✉️ <b>Сообщения покупателю</b>", ("templates",), args[0], lambda k: ("✏️ " if k in s["templates"] else "▫️ ") + TEMPLATES[k][0], lambda k: ("template", k), mid,
                       [[("♻️ Сбросить все", ("confirm", "reset_templates", "")), ("💬 Диалог", ("dialog",))]])
        elif op == "template":
            key = args[0]
            self.show(chat, "✉️ <b>" + esc(TEMPLATES[key][0]) + "</b>\n\n" + esc(s["templates"].get(key, TEMPLATES[key][1])), [[("✏️ Изменить", ("ask", "template:" + key, "")), ("✉️ Все шаблоны", ("templates", 0))]], mid)
        elif op == "guide":
            page = max(0, min(int(args[0]), len(GUIDE)-1))
            rows = [[("◀️", ("guide", (page-1) % len(GUIDE))), ("▶️", ("guide", (page+1) % len(GUIDE)))], [("👤 Автор", SUPPORT_URL), ("📣 Канал", DEVELOPER_URL)], [("🏠 Главная", ("home",))]]
            self.show(chat, GUIDE[page][1] + f"\n\n<i>{page+1}/{len(GUIDE)} · {esc(CREDITS)}</i>", rows, mid)
        elif op == "ask":
            self.prompt(chat, user, *args)
        elif op == "confirm":
            kind, target = args
            text = {"delete_lot": "Удалить привязку? Действующие заказы сохранят собственную копию настроек.",
                    "stop_dialog": "Передать этот диалог на ручную обработку? Деньги автоматически не возвращаются.",
                    "resume": "Заново запросить данные по оплаченному заказу? Доступно только до операций поставщика.",
                    "close": "Вы проверили фактическое зачисление и историю AppRoute и вручную рассчитались с покупателем? Заказ будет закрыт, дальнейшие закупки и возвраты по нему остановятся. Журнал операций сохранится.",
                    "reset_templates": "Вернуть стандартные сообщения покупателю?"}[kind]
            self.show(chat, esc(text), [[("✅ Подтвердить", ("execute", kind, target)), ("↩️ Назад", ("home",))]], mid)
        elif op == "execute":
            kind, target = args
            if kind == "delete_lot":
                self.store.delete("lot", target)
            elif kind == "reset_templates":
                self.store.configure(templates={})
            else:
                r = self.store.get("order", target)
                if not r or kind != "close" and (r.get("transactions") or r.get("completed")):
                    raise ShopError("Действие запрещено после операций поставщика.")
                if kind == "resume":
                    self.shop.paid(r)
                    r.update(state="queued", next_at=0)
                    self.shop.save(r)
                elif kind == "stop_dialog":
                    if r["state"] not in WAIT_STATES:
                        raise ShopError("Диалог уже перешёл в другое состояние.")
                    self.shop.review(r, "Диалог остановлен продавцом.")
                elif kind == "close":
                    if r["state"] not in {"review", "partial"}:
                        raise ShopError("Закрыть можно только заказ на ручной проверке.")
                    r.update(state="closed", finished=time.time(), manual_resolved=True)
                    self.shop.save(r)
            self.home(chat, mid)
        elif op == "plugins":
            from tg_bot import CBT
            self.show(chat, "🧩 Откройте список плагинов Cardinal через /menu.", [[("🏠 Главная", ("home",))]], mid)

    def register(self):
        from tg_bot import CBT
        self.tg.msg_handler(self.command, commands=["approute_topup"])
        self.tg.msg_handler(self.message, func=self.has_input, content_types=["text"])
        self.tg.cbq_handler(self.callback, lambda c: bool(c.data) and c.data.startswith(PREFIX))
        def settings_callback(call):
            if call.message and self.allowed(call.from_user, call.message.chat):
                try:
                    self.bot.answer_callback_query(call.id)
                except Exception:
                    pass
                self.home(call.message.chat.id, call.message.message_id)
        self.tg.cbq_handler(settings_callback, lambda c: bool(c.data) and c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
        self.shop.cardinal.add_telegram_commands(UUID, [("approute_topup", "⚡ AppRoute Top-Up", True)])


SHOP = None


def init(cardinal, *args):
    global SHOP
    if SHOP is not None:
        return
    SHOP = TopUp(cardinal)
    if cardinal.telegram:
        SHOP.ui = TelegramUI(SHOP)
        SHOP.ui.register()
    LOG.info("AppRoute Top-Up v%s initialized. Author: %s", VERSION, CREDITS)


def start(cardinal, *args):
    if SHOP is None:
        init(cardinal)
    SHOP.start()


def stop(cardinal, *args):
    if SHOP:
        SHOP.stop_event.set()
        SHOP.wakeup.set()
        if SHOP.thread:
            SHOP.thread.join(timeout=5)
        if SHOP.instance_lock and (not SHOP.thread or not SHOP.thread.is_alive()):
            SHOP.instance_lock.close()
            SHOP.instance_lock = None


def new_order(cardinal, event, *args):
    if SHOP and SHOP.active():
        order = getattr(event, "order", None)
        if order is not None and enum_name(getattr(order, "status", "")) == "PAID":
            SHOP.enqueue(str(order.id))


def new_message(cardinal, event, *args):
    if not SHOP or not SHOP.active():
        return
    msg = getattr(event, "message", None)
    if msg is None or getattr(msg, "by_bot", False):
        return
    author, chat = str(getattr(msg, "author_id", "")), str(getattr(msg, "chat_id", ""))
    mid, text = getattr(msg, "id", None), getattr(msg, "text", "") or ""
    if not author.isdigit() or int(author) <= 0 or not re.fullmatch(r"(?:\d+|users-\d+-\d+)", chat) or mid is None or not 1 <= len(text) <= 4000:
        return
    kind = getattr(msg, "type", None)
    if kind is not None and enum_name(kind) not in {"NON_SYSTEM", "USER_MESSAGE", "DEFAULT"}:
        return
    key = f"{chat}:{mid}"
    if SHOP.store.get("seen", key):
        return
    SHOP.store.put("message", key, {"id": key, "chat": chat, "buyer_id": author, "text": text, "created": time.time()})
    SHOP.store.put("seen", key, {"at": time.time()})
    SHOP.wakeup.set()


def delete(cardinal, *args):
    stop(cardinal)


BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [start]
BIND_TO_PRE_STOP = [stop]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_ORDER_STATUS_CHANGED = [new_order]
BIND_TO_NEW_MESSAGE = [new_message]
BIND_TO_DELETE = delete

