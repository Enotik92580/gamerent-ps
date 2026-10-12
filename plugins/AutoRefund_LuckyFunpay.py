"""
🛡️ Auto Refund — защита рейтинга продавца. Плагин для FunPay Cardinal от LuckyFunpay.

Автоматически возвращает деньги за заказ при плохом отзыве (выбранные звёзды) — после возврата
отзыв перестаёт влиять на рейтинг. Может добавлять покупателя в ЧС и возвращать деньги ЧСникам.

ИНСТРУКЦИЯ (полная версия — в боте: /autoref → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. /autoref → отметьте звёзды (обычно 1⭐, 2⭐, 3⭐) — они загорятся 🟢.
3. ОБЯЗАТЕЛЬНО задайте «💰 Лимит цены» — дороже этой суммы бот возврат не делает.
4. Включите «Плагин: 🟢». Доп. опции: ЧС при отзыве, ЧС при удалении отзыва, возврат ЧСникам.

Автор: @LuckyFunPay_Support · Канал: https://t.me/LuckyFunPay
"""
from __future__ import annotations

import copy
import html
import io
import json
import logging
import os
import random
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

NAME = "Auto Refund"
VERSION = "1.0.0"
DESCRIPTION = ("🛡️ Автовозврат за плохие отзывы (выбранные звёзды) в пределах лимита суммы: отзыв перестаёт "
               "влиять на рейтинг. Авто-ЧС, защита от ЧСников и удаления отзывов. Панель: /autoref")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "e91d3b70-6c48-4a2f-b8d5-0f7e2a4c9b13"
SETTINGS_PAGE = True
SLUG = "AutoRefund"
PREFIX = "lfref:"

# ═════════════════════════════ LuckyFunpay core ═════════════════════════════
LOG = logging.getLogger(f"FPC.{SLUG}")
DATA_DIR = Path("storage") / "plugins" / SLUG


class UserError(Exception):
    """Ошибка, текст которой показывается пользователю в Telegram."""


def esc(value) -> str:
    return html.escape(str(value), quote=False)


def enum_name(value) -> str:
    return str(getattr(value, "name", value)).upper().split(".")[-1]


def now_ts() -> float:
    return time.time()


def fmt_dt(ts, offset_hours=None) -> str:
    if not ts:
        return "—"
    if offset_hours is None:
        return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M")
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=offset_hours))).strftime("%d.%m.%Y %H:%M")


def ago(ts) -> str:
    if not ts:
        return "—"
    delta = max(0, int(time.time() - ts))
    days, rest = divmod(delta, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    if not days:
        parts.append(f"{minutes}м")
    return " ".join(parts)


def parse_int(raw, low=None, high=None, name="Число") -> int:
    text = str(raw).strip().replace(" ", "")
    if not re.fullmatch(r"-?\d{1,12}", text):
        raise UserError(f"{name}: нужно целое число.")
    value = int(text)
    if low is not None and value < low or high is not None and value > high:
        bounds = f"от {low}" if low is not None else ""
        bounds += f" до {high}" if high is not None else ""
        raise UserError(f"{name}: допустимо {bounds.strip()}.")
    return value


def parse_float(raw, low=None, high=None, name="Сумма") -> float:
    text = str(raw).strip().replace(" ", "").replace(",", ".")
    if not re.fullmatch(r"-?\d{1,12}(\.\d{1,4})?", text):
        raise UserError(f"{name}: нужно число, например 150 или 99.5.")
    value = float(text)
    if low is not None and value < low or high is not None and value > high:
        bounds = f"от {low:g}" if low is not None else ""
        bounds += f" до {high:g}" if high is not None else ""
        raise UserError(f"{name}: допустимо {bounds.strip()}.")
    return value


def chunks(text: str, size: int = 3800):
    """Делит длинный текст по строкам на части, которые помещаются в сообщение Telegram."""
    out, buf = [], ""
    for line in str(text).split("\n"):
        while len(line) > size:
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:size])
            line = line[size:]
        if len(buf) + len(line) + 1 > size:
            out.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        out.append(buf)
    return out or [""]


class JsonStore:
    """Настройки и данные плагина в JSON с атомарной записью."""

    def __init__(self, name: str, defaults: dict):
        self.path = DATA_DIR / name
        self.lock = threading.RLock()
        self.defaults = copy.deepcopy(defaults)
        self.data = self._load()

    def _load(self) -> dict:
        data = copy.deepcopy(self.defaults)
        if not self.path.exists():
            return data
        try:
            with open(self.path, encoding="utf-8") as file:
                raw = json.load(file)
            if not isinstance(raw, dict):
                raise ValueError("not a dict")
        except Exception:
            LOG.error("%s: файл %s повреждён, создаю резервную копию", NAME, self.path)
            try:
                os.replace(self.path, self.path.with_suffix(self.path.suffix + f".broken{int(time.time())}"))
            except OSError:
                pass
            return data
        for key, value in raw.items():
            default = data.get(key)
            if key not in data or default is None or type(value) is type(default) or (
                    isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool)):
                data[key] = value
        return data

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as file:
                json.dump(self.data, file, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)

    def __getitem__(self, key):
        with self.lock:
            return self.data[key]

    def get(self, key, default=None):
        with self.lock:
            return self.data.get(key, default)

    def snapshot(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.data)

    def set(self, **values):
        with self.lock:
            self.data.update(values)
            self.save()

    def change(self, func):
        with self.lock:
            result = func(self.data)
            self.save()
            return result


class Job:
    """Длительная операция с прогрессом в Telegram и кнопкой «Стоп»."""

    def __init__(self, title: str):
        self.title = title
        self.stop = threading.Event()
        self.done = 0
        self.total = 0
        self.ok = 0
        self.errors = 0
        self.last_error = ""
        self.started = time.time()

    def bar(self) -> str:
        part = self.done / self.total if self.total else 0
        filled = int(part * 10)
        return f"[{'█' * filled}{'░' * (10 - filled)}] {int(part * 100)}%"


class Panel:
    """Панель управления плагином в Telegram: кнопки, ввод значений, инструкция."""

    COMMAND = ""
    COMMANDS: list = []
    GUIDE: list = []
    ICON = "🧩"

    def __init__(self, plugin):
        from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup
        self.Button, self.Markup = InlineKeyboardButton, InlineKeyboardMarkup
        self.plugin = plugin
        self.cardinal = plugin.cardinal
        self.tg = self.cardinal.telegram
        self.bot = self.tg.bot
        self.tokens: dict = {}
        self.pending: dict = {}
        self.lock = threading.RLock()
        self.message = None
        self.call = None

    # --- доступ
    def admins(self) -> list[int]:
        result = []
        for uid in list(getattr(self.tg, "authorized_users", {}) or {}):
            try:
                result.append(int(uid))
            except (TypeError, ValueError):
                continue
        return result

    def allowed(self, user, chat=None) -> bool:
        if user is None or int(getattr(user, "id", 0) or 0) not in self.admins():
            return False
        return chat is None or getattr(chat, "type", "private") == "private"

    # --- клавиатуры
    def kb(self, chat, rows):
        if not rows:
            return None
        markup = self.Markup()
        with self.lock:
            if len(self.tokens) > 4000:
                border = time.time() - 6 * 3600
                self.tokens = {k: v for k, v in self.tokens.items() if v[0] > border}
            for row in rows:
                buttons = []
                for label, action in row:
                    if isinstance(action, str) and action.startswith(("https://", "http://", "tg://")):
                        buttons.append(self.Button(label, url=action))
                    else:
                        key = uuid.uuid4().hex[:20]
                        self.tokens[key] = (time.time(), chat, tuple(action))
                        buttons.append(self.Button(label, callback_data=PREFIX + key))
                if buttons:
                    markup.row(*buttons)
        return markup

    def send(self, chat, text, rows=None, mid=None, silent=False, thread=None):
        text = str(text)
        if len(text) > 4000:
            text = text[:3990] + "…"
        markup = self.kb(chat, rows)
        kwargs = {"reply_markup": markup, "parse_mode": "HTML", "disable_web_page_preview": True}
        if mid:
            try:
                return self.bot.edit_message_text(text, chat, mid, **kwargs)
            except Exception as error:
                if "message is not modified" in str(error):
                    return None
        if silent:
            kwargs["disable_notification"] = True
        if thread:
            kwargs["message_thread_id"] = thread
        return self.bot.send_message(chat, text, **kwargs)

    def notify(self, text, rows=None, pin=False, silent=False):
        sent = []
        for uid in self.admins():
            try:
                msg = self.send(uid, text, rows, silent=silent)
                sent.append(msg)
                if pin and msg is not None:
                    try:
                        self.bot.pin_chat_message(uid, msg.message_id, disable_notification=False)
                    except Exception:
                        LOG.debug("%s: pin failed", NAME)
            except Exception:
                LOG.warning("%s: не удалось отправить уведомление в Telegram %s", NAME, uid)
        return sent

    def send_long(self, chat, text, parse_mode="HTML"):
        for part in chunks(text):
            self.bot.send_message(chat, part, parse_mode=parse_mode, disable_web_page_preview=True)

    def send_file(self, chat, name, data: bytes, caption=None):
        file = io.BytesIO(data)
        file.name = name
        try:
            return self.bot.send_document(chat, file, caption=caption, visible_file_name=name)
        except TypeError:
            file.seek(0)
            return self.bot.send_document(chat, file, caption=caption)

    # --- ввод значений
    def ask(self, chat, prompt, action, mid=None, rows=None, doc=False):
        extra = rows or []
        msg = self.send(chat, prompt + "\n\n<i>Для отмены нажмите кнопку ниже.</i>",
                        extra + [[("✖️ Отмена", ("cancel",))]], mid)
        with self.lock:
            self.pending[chat] = {"t": time.time(), "action": tuple(action), "prompt": prompt, "doc": doc,
                                  "mid": getattr(msg, "message_id", mid)}

    def pending_for(self, message, doc=False):
        if not self.allowed(getattr(message, "from_user", None), getattr(message, "chat", None)):
            return None
        with self.lock:
            item = self.pending.get(message.chat.id)
        if not item or time.time() - item["t"] > 900:
            return None
        if doc != item["doc"]:
            return None
        if not doc:
            text = getattr(message, "text", None)
            if not text or (text.startswith("/") and not item.get("allow_cmd")):
                return None
        return item

    def has_input(self, message):
        return self.pending_for(message) is not None

    def has_doc_input(self, message):
        return self.pending_for(message, doc=True) is not None

    def on_message_input(self, message):
        item = self.pending_for(message, doc=bool(getattr(message, "document", None)))
        if not item:
            return
        chat = message.chat.id
        with self.lock:
            self.pending.pop(chat, None)
        op, *args = item["action"]
        self.message = message
        try:
            value = message.document if item["doc"] else message.text
            getattr(self, "in_" + op)(chat, value, *args)
        except UserError as error:
            self.send(chat, "⚠️ " + esc(error), [[("✏️ Ввести заново", ("reask", item["prompt"], *item["action"])),
                                                 ("🏠 Меню", ("home",))]])
            if item["doc"]:
                with self.lock:
                    self.pending[chat] = dict(item, t=time.time())
        except Exception:
            LOG.exception("%s: ошибка обработки ввода", NAME)
            self.send(chat, "⚠️ Не получилось обработать значение. Подробности в логе Cardinal.",
                      [[("🏠 Меню", ("home",))]])
        finally:
            self.message = None

    def on_reask(self, chat, mid, prompt, *action):
        doc = action[0] in getattr(self, "DOC_INPUTS", ())
        self.ask(chat, prompt, action, mid, doc=doc)

    # --- кнопки
    def on_callback(self, call):
        message = getattr(call, "message", None)
        if not message or not self.allowed(call.from_user, message.chat):
            return
        chat, mid = message.chat.id, message.message_id
        with self.lock:
            token = self.tokens.get(call.data[len(PREFIX):])
        if not token or token[1] != chat:
            self.answer(call, "Кнопка устарела, откройте меню заново.")
            self.send(chat, f"Кнопка устарела. Откройте /{self.COMMAND}.")
            return
        self.call = call
        try:
            op, *args = token[2]
            if op not in {"ask"}:
                with self.lock:
                    if op != "noop":
                        self.pending.pop(chat, None)
            getattr(self, "on_" + op)(chat, mid, *args)
            self.answer(call)
        except UserError as error:
            self.answer(call, str(error)[:190], alert=True)
            self.send(chat, "⚠️ " + esc(error), [[("🏠 Меню", ("home",))]])
        except Exception:
            LOG.exception("%s: ошибка кнопки", NAME)
            self.answer(call, "Ошибка, подробности в логе Cardinal.", alert=True)
        finally:
            self.call = None

    def answer(self, call, text=None, alert=False):
        if call is None or getattr(call, "_answered", False):
            return
        try:
            self.bot.answer_callback_query(call.id, text, show_alert=alert)
            call._answered = True
        except Exception:
            pass

    def on_noop(self, chat, mid, *args):
        pass

    def on_cancel(self, chat, mid, *args):
        with self.lock:
            self.pending.pop(chat, None)
        self.on_home(chat, mid)

    def on_home(self, chat, mid=None, *args):
        raise NotImplementedError

    def footer_rows(self):
        return [[("📖 Инструкция", ("guide", 0))], [("👤 Автор", AUTHOR_URL), ("📢 Канал", CHANNEL_URL)]]

    def on_guide(self, chat, mid, page=0):
        page = max(0, min(int(page), len(self.GUIDE) - 1))
        title, text = self.GUIDE[page]
        nav = []
        if page > 0:
            nav.append(("⬅️", ("guide", page - 1)))
        nav.append((f"{page + 1}/{len(self.GUIDE)}", ("noop",)))
        if page < len(self.GUIDE) - 1:
            nav.append(("➡️", ("guide", page + 1)))
        body = (f"📖 <b>{esc(NAME)} — инструкция</b>\n<b>{esc(title)}</b>\n\n{text}\n\n"
                f"<i>Автор: {esc(CREDITS)} · Канал: @LuckyFunPay</i>")
        self.send(chat, body, [nav, [("👤 Автор", AUTHOR_URL), ("📢 Канал", CHANNEL_URL)],
                               [("🏠 Меню плагина", ("home",))]], mid)

    def on_plugins(self, chat, mid=None, *args):
        try:
            from tg_bot import CBT
            rows = self.Markup()
            rows.row(self.Button("🧩 Открыть плагины", callback_data=f"{CBT.PLUGINS_LIST}:0"))
            self.bot.send_message(chat, "Список плагинов Cardinal:", reply_markup=rows)
        except Exception:
            self.send(chat, "Откройте /menu → Система и конфиги → Плагины.")

    # --- регистрация
    def command(self, message):
        if not self.allowed(message.from_user, message.chat):
            return
        with self.lock:
            self.pending.pop(message.chat.id, None)
        self.message = message
        try:
            self.on_command(message)
        except UserError as error:
            self.send(message.chat.id, "⚠️ " + esc(error))
        except Exception:
            LOG.exception("%s: ошибка команды", NAME)
            self.send(message.chat.id, "⚠️ Ошибка, подробности в логе Cardinal.")
        finally:
            self.message = None

    def on_command(self, message):
        self.on_home(message.chat.id)

    def register(self):
        tg = self.tg
        commands = [c[0] for c in self.COMMANDS] or [self.COMMAND]
        tg.msg_handler(self.command, commands=commands)
        tg.msg_handler(self.on_message_input, func=self.has_input, content_types=["text"])
        tg.msg_handler(self.on_message_input, func=self.has_doc_input, content_types=["document"])
        tg.cbq_handler(self.on_callback, lambda call: bool(getattr(call, "data", None)) and call.data.startswith(PREFIX))

        def open_settings(call):
            if call.message and self.allowed(call.from_user, call.message.chat):
                self.answer(call)
                self.on_home(call.message.chat.id, call.message.message_id)

        try:
            from tg_bot import CBT
            settings_key = f"{CBT.PLUGIN_SETTINGS}:{UUID}"
            tg.cbq_handler(open_settings, lambda call: bool(getattr(call, "data", None)) and (
                    call.data == settings_key or call.data.startswith(settings_key + ":")))
        except Exception:
            LOG.debug("%s: CBT недоступен", NAME)
        menu = [(c[0], c[1], True) for c in self.COMMANDS] or [(self.COMMAND, f"{self.ICON} {NAME}", True)]
        try:
            self.cardinal.add_telegram_commands(UUID, menu)
        except Exception:
            LOG.debug("%s: команды не добавлены в меню", NAME)


def tg_ready(cardinal) -> bool:
    return getattr(cardinal, "telegram", None) is not None


def account_ready(cardinal) -> bool:
    account = getattr(cardinal, "account", None)
    return bool(account and getattr(account, "is_initiated", True) and getattr(account, "id", None))
# ═══════════════════════════ конец LuckyFunpay core ═══════════════════════════


DEFAULT_BYE = ("Здравствуйте! Нам жаль, что заказ вас не устроил. Мы вернули деньги за заказ #$order_id. "
               "Всего доброго!")
DEFAULTS = {"enabled": False, "stars": {"1": True, "2": True, "3": True, "4": False, "5": False}, "limit": 500.0,
            "bl_review": True, "bl_delete": False, "refund_bl": True, "notify": True, "message": DEFAULT_BYE,
            "processed": [], "log": [], "rates": {"USD": 95.0, "EUR": 105.0}}
REVIEW_TYPES = {"NEW_FEEDBACK", "FEEDBACK_CHANGED"}
BUYER_RE = re.compile(r"(?:Покупатель|The buyer|Покупець)\s+(\S+)")
ORDER_RE = re.compile(r"#([A-Z0-9]{8})")


def to_rub(cardinal, amount, currency, fallback: dict):
    code = enum_name(currency)
    if code in ("RUB", "₽") or str(currency) == "₽":
        return float(amount)
    try:
        from FunPayAPI.common.enums import Currency
        if hasattr(cardinal, "get_exchange_rate"):
            return float(amount) * float(cardinal.get_exchange_rate(currency, Currency.RUB, 3600))
    except Exception:
        LOG.debug("%s: курс не получен, использую резервный", NAME)
    code = {"$": "USD", "€": "EUR"}.get(str(currency), code)
    rate = fallback.get(code)
    return float(amount) * rate if rate else None


class AutoRefund:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("settings.json", DEFAULTS)
        self.ui = None
        self.lock = threading.Lock()
        self.inflight = set()

    def log(self, text):
        self.store.change(lambda d: d.__setitem__("log", ([{"t": time.time(), "text": text}] + d["log"])[:30]))

    def blacklist(self, name) -> bool:
        bl = getattr(self.cardinal, "blacklist", None)
        if bl is None or not name or name in bl:
            return False
        bl.append(name)
        try:
            from Utils.cardinal_tools import cache_blacklist
            cache_blacklist(bl)
        except Exception:
            LOG.debug("%s: ЧС не сохранён на диск", NAME)
        return True

    def within_limit(self, order) -> tuple:
        price = getattr(order, "sum", None)
        if price is None:
            price = getattr(order, "price", 0)
        rub = to_rub(self.cardinal, price, order.currency, self.store["rates"])
        if rub is None:
            return False, price
        return rub <= self.store["limit"] + 1e-9, price

    def mark(self, oid) -> bool:
        with self.lock:
            if oid in self.inflight or oid in self.store["processed"]:
                return False
            self.inflight.add(oid)
            return True

    def finish(self, oid):
        with self.lock:
            self.inflight.discard(oid)
        self.store.change(lambda d: d.__setitem__("processed", (d["processed"] + [oid])[-500:]))

    def refund(self, order, reason) -> bool:
        account = self.cardinal.account
        try:
            account.refund(order.id)
        except Exception as error:
            LOG.warning("%s: возврат по заказу %s не выполнен: %s", NAME, order.id, error)
            if self.ui:
                self.ui.notify(f"❌ <b>Auto Refund</b>: не удалось вернуть деньги за заказ <code>#{esc(order.id)}</code> "
                               f"({esc(reason)}). Проверьте вручную.",
                               [[("🧾 Заказ", f"https://funpay.com/orders/{order.id}/")]])
            return False
        text = (self.store["message"] or "").replace("$order_id", str(order.id)).replace(
            "$username", str(getattr(order, "buyer_username", "") or ""))
        if text.strip():
            chat_id = getattr(order, "chat_id", None)
            try:
                chat = account.get_chat_by_name(order.buyer_username)
                if chat is not None:
                    chat_id = chat.id
            except Exception:
                pass
            try:
                self.cardinal.send_message(chat_id, text, order.buyer_username)
            except Exception:
                LOG.warning("%s: прощальное сообщение не отправлено", NAME)
        return True

    # --- отзывы
    def on_review(self, msg):
        data = self.store.snapshot()
        if not data["enabled"]:
            return
        if getattr(msg, "i_am_buyer", False):
            return
        found = ORDER_RE.search(str(getattr(msg, "text", "") or ""))
        if not found:
            return
        oid = found.group(1)
        if not self.mark(oid):
            return
        done = False
        try:
            done = self.process_review(msg, oid)
        finally:
            with self.lock:
                self.inflight.discard(oid)
            if done:
                self.finish(oid)

    def get_order(self, msg, oid):
        order = None
        try:
            order = self.cardinal.get_order_from_object(msg)
        except Exception:
            order = None
        if order is None or str(getattr(order, "id", "")) != oid:
            order = self.cardinal.account.get_order(oid)
        return order

    def process_review(self, msg, oid) -> bool:
        data = self.store.snapshot()
        order = self.get_order(msg, oid)
        account = self.cardinal.account
        if getattr(order, "seller_id", account.id) != account.id:
            return True
        review = getattr(order, "review", None)
        stars = getattr(review, "stars", None)
        if not stars or not data["stars"].get(str(stars)):
            return False  # отзыв могут изменить — проверим снова
        if enum_name(order.status) == "REFUNDED":
            return True
        ok, price = self.within_limit(order)
        buyer = getattr(order, "buyer_username", "")
        if not ok:
            self.log(f"⏭ #{oid}: {stars}⭐, сумма {price} {order.currency} выше лимита — без возврата")
            if self.ui and data["notify"]:
                self.ui.notify(f"⚠️ <b>Плохой отзыв {stars}⭐</b> на заказ <code>#{esc(oid)}</code> ({esc(buyer)}), "
                               f"но сумма {price:g} {esc(order.currency)} выше лимита — возврат не сделан.",
                               [[("🧾 Заказ", f"https://funpay.com/orders/{oid}/")]])
            return True
        if not self.refund(order, f"отзыв {stars}⭐"):
            return False
        banned = data["bl_review"] and self.blacklist(buyer)
        self.log(f"↩️ #{oid}: {stars}⭐ от {buyer}, возврат {price:g} {order.currency}" + (", ЧС" if banned else ""))
        if self.ui and data["notify"]:
            self.ui.notify(f"🛡️ <b>Рейтинг спасён!</b>\nПокупатель <b>{esc(buyer)}</b> поставил {stars}⭐ на заказ "
                           f"<code>#{esc(oid)}</code>.\n↩️ Деньги возвращены: {price:g} {esc(order.currency)} — отзыв "
                           f"больше не влияет на рейтинг." + ("\n🚫 Покупатель добавлен в ЧС." if banned else ""),
                           [[("🧾 Заказ", f"https://funpay.com/orders/{oid}/")]])
        return True

    def on_review_deleted(self, msg):
        data = self.store.snapshot()
        if not data["enabled"] or not data["bl_delete"]:
            return
        text = str(getattr(msg, "text", "") or "")
        buyer = getattr(msg, "initiator_username", None)
        if not buyer:
            match = BUYER_RE.search(text)
            buyer = match.group(1) if match else None
        if not buyer or buyer == getattr(self.cardinal.account, "username", None):
            return
        if self.blacklist(buyer):
            found = ORDER_RE.search(text)
            self.log(f"🚫 {buyer} удалил отзыв" + (f" к #{found.group(1)}" if found else "") + " — добавлен в ЧС")
            if self.ui and data["notify"]:
                self.ui.notify(f"🕵️ <b>{esc(buyer)}</b> удалил отзыв" + (f" к заказу <code>#{found.group(1)}</code>"
                                                                         if found else "") + " — добавлен в ЧС.")

    # --- ЧСники
    def on_new_order(self, order):
        data = self.store.snapshot()
        if not data["enabled"] or not data["refund_bl"]:
            return
        buyer = getattr(order, "buyer_username", None)
        if not buyer or buyer not in (getattr(self.cardinal, "blacklist", None) or []):
            return
        oid = str(order.id)
        if not self.mark(oid):
            return
        try:
            ok, price = self.within_limit(order)
            if not ok:
                self.log(f"⏭ #{oid}: покупка ЧСника {buyer}, сумма выше лимита — без возврата")
                if self.ui and data["notify"]:
                    self.ui.notify(f"⚠️ ЧСник <b>{esc(buyer)}</b> оплатил заказ <code>#{esc(oid)}</code> на "
                                   f"{price:g} {esc(order.currency)} — выше лимита, возврат не сделан.",
                                   [[("🧾 Заказ", f"https://funpay.com/orders/{oid}/")]])
                return
            if self.refund(order, "покупатель в ЧС"):
                self.log(f"↩️ #{oid}: ЧСник {buyer}, возврат {price:g} {order.currency}")
                if self.ui and data["notify"]:
                    self.ui.notify(f"🚫 ЧСник <b>{esc(buyer)}</b> попытался купить (заказ <code>#{esc(oid)}</code>). "
                                   f"Деньги возвращены: {price:g} {esc(order.currency)}.")
        finally:
            self.finish(oid)


class RefundPanel(Panel):
    COMMAND = "autoref"
    ICON = "🛡️"
    GUIDE = [
        ("🚀 Для чего это нужно", "Отзыв на 1–3 звезды бьёт по продажам. Главный секрет: <b>если вернуть деньги за "
         "заказ, отзыв перестаёт учитываться в рейтинге</b>.\n1. <b>Нейтрализация негатива</b> — покупатель поставил "
         "1⭐? Бот сразу вернёт деньги, оценка уйдёт из статистики.\n2. <b>Блокировка неадекватов</b> — возврат и "
         "автоматический ЧС.\n3. <b>Защита от ЧСников</b> — человек из ЧС купил снова → моментальный возврат."),
        ("⚙️ Возможности", "⭐ <b>Триггеры по звёздам</b> — вы выбираете, на какие оценки реагировать.\n"
         "💰 <b>Лимит безопасности</b> — максимальная сумма (например 500₽). Дороже бот не вернёт никогда. Заказы в $ "
         "и € пересчитываются в рубли по курсу FunPay.\n🚫 <b>Авто-ЧС</b> после возврата за плохой отзыв.\n"
         "🕵️ <b>Детектор удаления</b> — удалил отзыв (тактика шантажистов) → ЧС.\n🔔 Уведомления в Telegram."),
        ("📖 Настройка", "1. Включите плагин в списке плагинов Cardinal и сделайте /restart.\n2. <code>/autoref</code> — "
         "меню.\n3. Нажимайте <code>1⭐</code>, <code>2⭐</code>… — загорится 🟢. Рекомендуем 1, 2 и 3.\n"
         "4. <b>«💰 Лимит цены» — ВАЖНО.</b> Продаёте ключи по 100₽ и аккаунты по 5000₽ — поставьте 300₽: мелочь "
         "защищена, дорогой аккаунт бот не вернёт.\n5. «ЧС при отзыве» — рекомендуем ✅. «ЧС при удалении» — по желанию.\n"
         "6. «Плагин: 🟢» — включить работу (по умолчанию выключен, чтобы вы сначала задали лимит)."),
        ("💡 Сценарий", "1. Покупатель недоволен товаром за 50₽ и ставит 1⭐.\n2. Cardinal видит системное сообщение "
         "о новом отзыве.\n3. Бот проверяет: 1⭐ включена? Да. 50₽ ≤ лимит? Да.\n4. Полный возврат средств.\n"
         "5. Отзыв перестаёт влиять на рейтинг.\n6. Покупатель получает прощальное сообщение (меняется кнопкой "
         "«✉️ Текст»; переменные <code>$username</code>, <code>$order_id</code>; <code>-</code> — не писать) и "
         "попадает в ЧС, если это включено.\n\nКаждый заказ обрабатывается один раз."),
    ]

    def on_home(self, chat, mid=None, *args):
        d = self.plugin.store.snapshot()
        stars = [s for s in "12345" if d["stars"].get(s)]
        lines = [f"🛡️ <b>{NAME}</b> v{VERSION}", "",
                 f"Плагин: {'🟢 работает' if d['enabled'] else '🔴 выключен'}",
                 f"Реагирую на: {', '.join(s + '⭐' for s in stars) or 'ничего'}",
                 f"Лимит цены: <b>{d['limit']:g} ₽</b>",
                 f"ЧС при отзыве: {'✅' if d['bl_review'] else '❌'} · при удалении: {'✅' if d['bl_delete'] else '❌'}"
                 f" · возврат ЧСникам: {'✅' if d['refund_bl'] else '❌'}",
                 f"В ЧС Cardinal: {len(getattr(self.cardinal, 'blacklist', []) or [])}"]
        if d["log"]:
            lines += ["", "🕓 <b>Журнал:</b>"] + [f"{fmt_dt(i['t'])} {esc(i['text'])}" for i in d["log"][:6]]
        rows = [[("Плагин: " + ("🟢" if d["enabled"] else "🔴"), ("toggle", "enabled"))],
                [((("🟢 " if d["stars"].get(s) else "⚪️ ") + s + "⭐"), ("star", s)) for s in "12345"],
                [("💰 Лимит цены: " + f"{d['limit']:g} ₽", ("limit",))],
                [("ЧС при отзыве " + ("✅" if d["bl_review"] else "❌"), ("toggle", "bl_review")),
                 ("ЧС при удалении " + ("✅" if d["bl_delete"] else "❌"), ("toggle", "bl_delete"))],
                [("Возврат ЧСникам " + ("✅" if d["refund_bl"] else "❌"), ("toggle", "refund_bl")),
                 ("🔔 " + ("✅" if d["notify"] else "❌"), ("toggle", "notify"))],
                [("✉️ Текст покупателю", ("message",))]]
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_toggle(self, chat, mid, key):
        if key not in ("enabled", "bl_review", "bl_delete", "refund_bl", "notify"):
            raise UserError("Неизвестная настройка.")
        self.plugin.store.change(lambda d: d.update({key: not d[key]}))
        self.on_home(chat, mid)

    def on_star(self, chat, mid, star):
        if star not in "12345":
            raise UserError("Неверная оценка.")
        self.plugin.store.change(lambda d: d["stars"].__setitem__(star, not d["stars"].get(star)))
        self.on_home(chat, mid)

    def on_limit(self, chat, mid):
        self.ask(chat, "💰 Введите максимальную сумму заказа в рублях, за которую бот может вернуть деньги сам.",
                 ("limit",), mid)

    def in_limit(self, chat, text):
        self.plugin.store.set(limit=parse_float(text, 0, 1_000_000, "Лимит"))
        self.on_home(chat)

    def on_message(self, chat, mid):
        self.ask(chat, "✉️ Отправьте сообщение, которое бот напишет покупателю после возврата (до 1000 символов).\n"
                       "Переменные: <code>$username</code>, <code>$order_id</code>. <code>-</code> — ничего не писать.\n\n"
                       f"Сейчас: <i>{esc(self.plugin.store['message'] or '—')}</i>", ("message",), mid)

    def in_message(self, chat, text):
        if len(text) > 1000:
            raise UserError("Максимум 1000 символов.")
        self.plugin.store.set(message="" if text.strip() == "-" else text.strip())
        self.on_home(chat)


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = AutoRefund(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = RefundPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


def run_async(func, *args):
    if getattr(PLUGIN, "sync", False):
        return func(*args)
    threading.Thread(target=func, args=args, name="AutoRefundJob", daemon=True).start()


def new_message(cardinal, event, *args):
    if PLUGIN is None:
        return
    msg = getattr(event, "message", None)
    if msg is None or getattr(msg, "author_id", None) != 0:
        return
    kind = enum_name(getattr(msg, "type", ""))
    try:
        if kind in REVIEW_TYPES:
            run_async(PLUGIN.on_review, msg)
        elif kind == "FEEDBACK_DELETED":
            run_async(PLUGIN.on_review_deleted, msg)
    except Exception:
        LOG.exception("%s: ошибка обработки сообщения", NAME)


def new_order(cardinal, event, *args):
    if PLUGIN is not None:
        try:
            run_async(PLUGIN.on_new_order, event.order)
        except Exception:
            LOG.exception("%s: ошибка обработки заказа", NAME)


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_MESSAGE = [new_message]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_DELETE = None
