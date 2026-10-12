"""
🚨 Big Order Alert — охота на «крупную рыбу». Плагин для FunPay Cardinal от LuckyFunpay.

Выделяет дорогие заказы из общего потока: отдельное яркое уведомление с закреплением и кнопкой
перехода в чат с покупателем. Пороги настраиваются отдельно для RUB, USD и EUR.

ИНСТРУКЦИЯ (полная версия — в боте: /bigorder → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. /bigorder — меню. По умолчанию крупным считается заказ от 1000 ₽, от 15 $ и от 15 €.
3. Нажмите на кнопку валюты (например «🇷🇺 RUB > 1000 ₽») и отправьте новую сумму. 0 — не следить за валютой.
4. «🟢 Мониторинг включен» — временно выключить/включить уведомления.

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

NAME = "Big Order Alert"
VERSION = "1.0.0"
DESCRIPTION = ("🚨 Отдельное закреплённое уведомление о крупных заказах FunPay. Свои пороги для RUB, USD и EUR, "
               "кнопка чата с покупателем. Панель: /bigorder")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "7c4e1f93-2b6a-4d58-8f0e-b39a5d7c2e16"
SETTINGS_PAGE = True
SLUG = "BigOrderAlert"
PREFIX = "lfbig:"

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
        """Возвращает копию значения: изменять данные можно только через set()/change()."""
        with self.lock:
            value = self.data[key]
            return copy.deepcopy(value) if isinstance(value, (dict, list)) else value

    def get(self, key, default=None):
        with self.lock:
            value = self.data.get(key, default)
            return copy.deepcopy(value) if isinstance(value, (dict, list)) else value

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
        self._local = threading.local()

    # текущие сообщение и нажатие — свои у каждого потока telebot
    @property
    def message(self):
        return getattr(self._local, "message", None)

    @message.setter
    def message(self, value):
        self._local.message = value

    @property
    def call(self):
        return getattr(self._local, "call", None)

    @call.setter
    def call(self, value):
        self._local.call = value

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
                if len(self.tokens) > 20000:
                    newest = sorted(self.tokens.items(), key=lambda kv: kv[1][0])[-10000:]
                    self.tokens = dict(newest)
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
        self.input_owners()[chat] = UUID

    def input_owners(self) -> dict:
        """Общий для всех плагинов LuckyFunpay реестр: какой плагин сейчас ждёт ввод в чате."""
        owners = getattr(self.cardinal, "_lucky_input_owner", None)
        if owners is None:
            owners = {}
            try:
                setattr(self.cardinal, "_lucky_input_owner", owners)
            except Exception:
                pass
        return owners

    def pending_for(self, message, doc=False):
        if not self.allowed(getattr(message, "from_user", None), getattr(message, "chat", None)):
            return None
        with self.lock:
            item = self.pending.get(message.chat.id)
        if not item or time.time() - item["t"] > 900:
            return None
        if self.input_owners().get(message.chat.id, UUID) != UUID:
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


CURRENCIES = [("RUB", "🇷🇺", "₽"), ("USD", "🇺🇸", "$"), ("EUR", "🇪🇺", "€")]
DEFAULTS = {"enabled": True, "pin": True, "limits": {"RUB": 1000.0, "USD": 15.0, "EUR": 15.0}, "caught": [],
            "seen": []}


def currency_code(currency) -> str:
    name = enum_name(currency)
    if name in ("RUB", "USD", "EUR"):
        return name
    return {"₽": "RUB", "$": "USD", "€": "EUR"}.get(str(currency), "UNKNOWN")


def money(value) -> str:
    return f"{float(value):,.2f}".replace(",", " ")


class BigOrderAlert:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("settings.json", DEFAULTS)
        self.ui = None

    def is_big(self, order) -> bool:
        data = self.store.snapshot()
        code = currency_code(getattr(order, "currency", ""))
        limit = data["limits"].get(code)
        try:
            price = float(getattr(order, "price", 0) or 0)
        except (TypeError, ValueError):
            return False
        return bool(data["enabled"] and limit and limit > 0 and price >= limit)

    def alert_text(self, order) -> str:
        code = currency_code(order.currency)
        return ("🚨 <b>BIG ORDER ALERT</b> 🚨\n\n"
                f"💎 Сумма: <code>{money(order.price)} {code}</code>\n"
                f"👤 Покупатель: <code>{esc(getattr(order, 'buyer_username', ''))}</code>\n"
                f"🆔 Заказ: <code>#{esc(order.id)}</code>\n\n"
                f"📦 Лот: <b>{esc(getattr(order, 'description', '') or '—')}</b>\n"
                f"🎮 Раздел: {esc(getattr(order, 'subcategory_name', '') or '—')}")

    def on_new_order(self, order):
        if not self.ui or not self.is_big(order):
            return
        oid = str(order.id)
        if oid in self.store["seen"]:
            return
        self.store.change(lambda d: (d.__setitem__("seen", (d["seen"] + [oid])[-200:]),
                                     d.__setitem__("caught", ([{"id": oid, "price": float(order.price),
                                                                "cur": currency_code(order.currency),
                                                                "buyer": getattr(order, "buyer_username", ""),
                                                                "t": time.time()}] + d["caught"])[:20])))
        chat_id = getattr(order, "chat_id", "")
        try:
            chat = self.cardinal.account.get_chat_by_name(order.buyer_username)
            if chat is not None:
                chat_id = chat.id
        except Exception:
            pass
        rows = [[("💬 Чат с покупателем", f"https://funpay.com/chat/?node={chat_id}")],
                [("🧾 Открыть заказ", f"https://funpay.com/orders/{oid}/")]]
        self.ui.notify(self.alert_text(order), rows, pin=self.store["pin"])
        LOG.info("%s: крупный заказ #%s на %s %s", NAME, oid, order.price, currency_code(order.currency))


class BigOrderPanel(Panel):
    COMMAND = "bigorder"
    ICON = "🚨"
    GUIDE = [
        ("💎 Для чего это нужно", "Когда у вас много дешёвых лотов, уведомления превращаются в фоновый шум и легко "
         "пропустить покупку дорогого аккаунта или услуги.\n1. <b>VIP-сервис</b> — мгновенно реагируйте на крупных "
         "клиентов.\n2. <b>Фокус</b> — обычные заказы приходят как обычно, крупные — отдельным ярким сообщением.\n"
         "3. <b>Безопасность</b> — крупные сделки требуют внимания к деталям и проверки покупателя."),
        ("⚙️ Возможности", "💱 <b>Мультивалютность</b> — свои пороги для RUB, USD и EUR, например от 1000₽, "
         "от 15$ или от 15€.\n📌 <b>Закрепление</b> — уведомление закрепляется в диалоге с ботом.\n"
         "⚡ <b>Быстрый доступ</b> — кнопки чата с покупателем и заказа.\n🔔 <b>Особый формат</b> — сирены 🚨 и жирный "
         "шрифт, сообщение отправляется напрямую всем администраторам бота, поэтому не теряется в общем потоке."),
        ("📖 Как использовать", "1. Включите плагин: /menu → Система и конфиги → Плагины → <b>Big Order Alert</b>, "
         "затем /restart.\n2. Откройте меню командой <code>/bigorder</code>.\n3. Нажмите на кнопку валюты, например "
         "<code>🇷🇺 RUB &gt; 1000 ₽</code>, и отправьте новую сумму, например <code>5000</code>. "
         "<code>0</code> — не следить за этой валютой.\n4. «🟢 Мониторинг включен» — нажмите, чтобы временно "
         "выключить уведомления (🔴).\n5. «🧪 Тест» пришлёт пример уведомления."),
        ("🧠 Логика работы", "1. Плагин перехватывает событие «Новый заказ».\n2. Определяет валюту и сравнивает "
         "сумму с вашим порогом для этой валюты.\n3. Если <code>Сумма заказа ≥ Ваш лимит</code> — формируется "
         "приоритетное уведомление:\n\n🚨 <b>BIG ORDER ALERT</b> 🚨\n💎 Сумма: <code>5 000.00 RUB</code>\n"
         "👤 Покупатель: <code>RichGamer</code>\n🆔 Заказ: <code>#AbCdEfGh</code>\n📦 Лот: Прокачка 1-100 (VIP)"),
    ]

    def on_home(self, chat, mid=None, *args):
        data = self.plugin.store.snapshot()
        lines = [f"🚨 <b>{esc(NAME)}</b> v{VERSION}", "",
                 f"Мониторинг: {'🟢 включен' if data['enabled'] else '🔴 выключен'}",
                 f"Закреплять уведомления: {'да' if data['pin'] else 'нет'}", "", "Крупным считается заказ от:"]
        for code, flag, sign in CURRENCIES:
            limit = data["limits"].get(code, 0)
            lines.append(f"{flag} {code}: " + (f"<b>{limit:g} {sign}</b>" if limit else "не отслеживается"))
        if data["caught"]:
            lines += ["", "🎣 <b>Последние крупные заказы:</b>"]
            for c in data["caught"][:5]:
                lines.append(f"• #{esc(c['id'])} — {money(c['price'])} {c['cur']} · {esc(c['buyer'])} · {fmt_dt(c['t'])}")
        rows = [[(f"{flag} {code} > {data['limits'].get(code, 0):g} {sign}", ("limit", code))]
                for code, flag, sign in CURRENCIES]
        rows.append([("🟢 Мониторинг включен" if data["enabled"] else "🔴 Мониторинг выключен", ("toggle", "enabled"))])
        rows.append([("📌 Закреплять: " + ("🟢" if data["pin"] else "🔴"), ("toggle", "pin")), ("🧪 Тест", ("test",))])
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_limit(self, chat, mid, code):
        if code not in {c for c, _, _ in CURRENCIES}:
            raise UserError("Неизвестная валюта.")
        self.ask(chat, f"💱 Введите сумму в {code}, от которой заказ считается крупным (0 — не следить).",
                 ("limit", code), mid)

    def in_limit(self, chat, text, code):
        value = parse_float(text, 0, 10_000_000, "Сумма")
        self.plugin.store.change(lambda d: d["limits"].__setitem__(code, value))
        self.on_home(chat)

    def on_toggle(self, chat, mid, key):
        if key not in ("enabled", "pin"):
            raise UserError("Неизвестная настройка.")
        self.plugin.store.change(lambda d: d.update({key: not d[key]}))
        self.on_home(chat, mid)

    def on_test(self, chat, mid):
        demo = type("Demo", (), {"id": "TEST0000", "price": 5000.0, "currency": "₽", "buyer_username": "RichGamer",
                                 "description": "Прокачка уровня 1-100 (VIP)", "subcategory_name": "Тест"})()
        self.send(chat, "🧪 Пример:\n\n" + self.plugin.alert_text(demo), [[("🏠 Меню", ("home",))]])


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = BigOrderAlert(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = BigOrderPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


def new_order(cardinal, event, *args):
    if PLUGIN is not None:
        try:
            PLUGIN.on_new_order(event.order)
        except Exception:
            LOG.exception("%s: ошибка обработки заказа", NAME)


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_DELETE = None
