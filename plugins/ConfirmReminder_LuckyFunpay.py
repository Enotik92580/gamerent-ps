"""
⏳ Confirm Reminder — авто-напоминания о подтверждении заказа. Плагин для FunPay Cardinal от LuckyFunpay.

Покупатель получил товар и забыл нажать «Подтвердить выполнение заказа»? Бот сам напомнит ему —
одним или двумя сообщениями. Перед каждой отправкой проверяется статус: если заказ уже подтверждён
или оформлен возврат, бот промолчит.

ИНСТРУКЦИЯ (полная версия — в боте: /confirm_reminder → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. /confirm_reminder → «⏱ Задержка 1» (по умолчанию 15 минут) и «📝 Текст 1».
3. По желанию включите второе напоминание (🔔) — его задержка отсчитывается ПОСЛЕ первого.
Переменные: $username, $order_id, $game, $order_title, $date.

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

NAME = "Confirm Reminder"
VERSION = "1.0.0"
DESCRIPTION = ("⏳ Напоминает покупателю подтвердить заказ: одно или два сообщения с паузой, проверка статуса перед "
               "каждой отправкой, очередь переживает перезапуск. Панель: /confirm_reminder")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "b25e7a83-4f1c-4d09-a6b7-9e3c5d2f1a84"
SETTINGS_PAGE = True
SLUG = "ConfirmReminder"
PREFIX = "lfcr:"

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


TEXT1 = ("$username, вижу, вы уже получили товар. Пожалуйста, не забудьте подтвердить выполнение заказа #$order_id "
         "на FunPay, если всё в порядке! 💙 Это очень помогает мне в работе, заранее спасибо!")
TEXT2 = ("$username, напоминаю про заказ #$order_id ($order_title) 🙏 Если всё хорошо — нажмите, пожалуйста, "
         "«Подтвердить выполнение заказа». Если что-то не так — напишите мне, решим!")
VARIABLES = ("$username", "$order_id", "$game", "$order_title", "$date")
DEFAULTS = {"enabled": True, "delay1": 15, "text1": TEXT1, "second": False, "delay2": 30, "text2": TEXT2,
            "sent_total": 0, "skipped_total": 0}


def order_info(order) -> dict:
    sub_name = getattr(order, "subcategory_name", "") or ""
    sub = getattr(order, "subcategory", None)
    game = getattr(getattr(sub, "category", None), "name", None) or (sub_name.rsplit(", ", 1)[0] if sub_name else "")
    date = getattr(order, "date", None)
    try:
        date_text = date.strftime("%d.%m.%Y")
    except Exception:
        date_text = datetime.now().strftime("%d.%m.%Y")
    return {"id": str(order.id), "buyer": getattr(order, "buyer_username", "") or "",
            "chat": str(getattr(order, "chat_id", "") or ""),
            "title": (getattr(order, "description", None) or getattr(order, "title", None) or "")[:150],
            "game": game, "date": date_text}


def render(template: str, info: dict) -> str:
    values = {"$username": info["buyer"], "$order_id": info["id"], "$order_title": info["title"],
              "$game": info["game"], "$date": info["date"]}
    for key in sorted(values, key=len, reverse=True):
        template = template.replace(key, values[key])
    return template


class Reminder:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("settings.json", DEFAULTS)
        self.queue = JsonStore("pending_reminders.json", {"items": {}, "done": []})
        self.ui = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = None

    def schedule(self, order):
        data = self.store.snapshot()
        if not data["enabled"] or enum_name(getattr(order, "status", "PAID")) != "PAID":
            return False
        info = order_info(order)

        def add(q):
            if info["id"] in q["items"] or info["id"] in q.get("done", []):
                return False
            q["items"][info["id"]] = dict(info, stage=1, due=time.time() + data["delay1"] * 60)
            return True

        added = self.queue.change(add)
        if added:
            self.wake.set()
        return added

    def drop(self, oid, reason=""):
        def remove(q):
            item = q["items"].pop(str(oid), None)
            if item is not None:
                q["done"] = (q.get("done", []) + [str(oid)])[-2000:]
            return item

        return self.queue.change(remove)

    def chat_id(self, item):
        try:
            chat = self.cardinal.account.get_chat_by_name(item["buyer"])
            if chat is not None:
                return chat.id
        except Exception:
            pass
        return item["chat"]

    def process_due(self):
        now = time.time()
        due = [dict(v) for v in self.queue["items"].values() if v["due"] <= now]
        for item in sorted(due, key=lambda i: i["due"]):
            if self.stop_event.is_set():
                return
            self.process(item)

    def process(self, item):
        data = self.store.snapshot()
        oid = item["id"]
        if not data["enabled"]:
            return
        try:
            order = self.cardinal.account.get_order(oid)
        except Exception:
            LOG.warning("%s: не удалось проверить заказ %s, повторю позже", NAME, oid)
            self.queue.change(lambda q: q["items"].get(oid, {}).update(due=time.time() + 300))
            return
        status = enum_name(getattr(order, "status", ""))
        if status != "PAID":
            self.drop(oid)
            self.store.change(lambda d: d.update(skipped_total=d["skipped_total"] + 1))
            return
        text = render(data["text1"] if item["stage"] == 1 else data["text2"], item)
        result = None
        try:
            result = self.cardinal.send_message(self.chat_id(item), text, item["buyer"])
        except Exception:
            result = None
        if result == [] or result is False:
            self.queue.change(lambda q: q["items"].get(oid, {}).update(due=time.time() + 600))
            return
        self.store.change(lambda d: d.update(sent_total=d["sent_total"] + 1))
        if item["stage"] == 1 and data["second"]:
            self.queue.change(lambda q: q["items"].get(oid, {}).update(stage=2, due=time.time() + data["delay2"] * 60))
        else:
            self.drop(oid)
        LOG.info("%s: напоминание %s отправлено по заказу %s", NAME, item["stage"], oid)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.loop, name="ConfirmReminder", daemon=True)
        self.thread.start()

    def loop(self):
        self.stop_event.wait(10)
        while not self.stop_event.is_set():
            try:
                if account_ready(self.cardinal):
                    self.process_due()
            except Exception:
                LOG.exception("%s: ошибка очереди", NAME)
            self.wake.wait(20)
            self.wake.clear()


class ReminderPanel(Panel):
    COMMAND = "confirm_reminder"
    ICON = "⏳"
    GUIDE = [
        ("📋 Для чего этот плагин", "Клиенты получают товар, начинают играть и забывают нажать «Подтвердить выполнение "
         "заказа». Продавцу приходится долго ждать деньги или писать каждому вручную.\nПлагин сам отследит время и "
         "вежливо напомнит клиенту о подтверждении."),
        ("🔥 Возможности", "• <b>Двухэтапная система</b> — первое напоминание вежливое, второе (по желанию) для самых "
         "забывчивых.\n• <b>Автоматическое отслеживание</b> — таймер ставится сразу при новом заказе (статус «Оплачен»).\n"
         "• <b>Умная проверка</b> — перед каждой отправкой бот проверяет статус. Заказ подтверждён или возврат — бот "
         "промолчит.\n• <b>Гибкие настройки</b> — своя задержка и свой текст для каждого напоминания."),
        ("⚙️ Настройка", "Команда <code>/confirm_reminder</code>.\n<b>1. Первое напоминание</b> — ⏱ задержка в минутах "
         "после оплаты (по умолчанию 15; рекомендуем 15–30, чтобы покупатель успел проверить товар) и 📝 текст.\n"
         "<b>2. Второе напоминание</b> — кнопка 🔔/🔕. ⏱ Задержка отсчитывается ПОСЛЕ отправки первого: первая 15 мин, "
         "вторая 30 мин → второе придёт через 45 минут после оплаты. 📝 Отдельный текст, чтобы не выглядело копипастой."),
        ("🧩 Переменные", "<code>$username</code> — ник покупателя\n<code>$order_id</code> — номер заказа без #\n"
         "<code>$game</code> — игра или категория\n<code>$order_title</code> — краткое описание лота\n<code>$date</code> — "
         "дата заказа\n\n💡 Пример:\n<i>$username, вижу вы уже получили товар. Пожалуйста, не забудьте подтвердить "
         "выполнение заказа #$order_id на FunPay, если всё в порядке! 💙</i>\n\nОчередь хранится в файле и переживает "
         "перезапуск бота."),
    ]

    def on_home(self, chat, mid=None, *args):
        d = self.plugin.store.snapshot()
        queue = self.plugin.queue["items"]
        lines = [f"⏳ <b>{NAME}</b> v{VERSION}", "",
                 f"Плагин: {'🟢 включен' if d['enabled'] else '🔴 выключен'}",
                 f"1️⃣ Первое: через <b>{d['delay1']} мин</b> после оплаты",
                 f"2️⃣ Второе: " + (f"через <b>{d['delay2']} мин</b> после первого" if d["second"] else "выключено"),
                 f"\n📬 В очереди: {len(queue)} · отправлено всего: {d['sent_total']} · отменено (подтверждён/возврат): "
                 f"{d['skipped_total']}"]
        rows = [[("Плагин: " + ("🟢" if d["enabled"] else "🔴"), ("toggle", "enabled"))],
                [("⏱ Задержка 1", ("delay", "delay1")), ("📝 Текст 1", ("text", "text1"))],
                [("🔕 Второе напоминание: вкл" if d["second"] else "🔔 Второе напоминание: выкл", ("toggle", "second"))]]
        if d["second"]:
            rows.append([("⏱ Задержка 2", ("delay", "delay2")), ("📝 Текст 2", ("text", "text2"))])
        rows.append([("📋 Очередь", ("queue",))])
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_toggle(self, chat, mid, key):
        if key not in ("enabled", "second"):
            raise UserError("Неизвестная настройка.")
        self.plugin.store.change(lambda d: d.update({key: not d[key]}))
        self.on_home(chat, mid)

    def on_delay(self, chat, mid, key):
        hint = "после оплаты заказа" if key == "delay1" else "ПОСЛЕ отправки первого напоминания"
        self.ask(chat, f"⏱ Через сколько минут {hint} отправить напоминание? (от 1 до 20160)", ("delay", key), mid)

    def in_delay(self, chat, text, key):
        if key not in ("delay1", "delay2"):
            raise UserError("Неизвестное поле.")
        self.plugin.store.set(**{key: parse_int(text, 1, 20160, "Минуты")})
        self.on_home(chat)

    def on_text(self, chat, mid, key):
        self.ask(chat, f"📝 Отправьте текст напоминания (до 1000 символов).\nПеременные: "
                       f"{', '.join('<code>' + v + '</code>' for v in VARIABLES)}\n<code>-</code> — стандартный текст.\n\n"
                       f"Сейчас:\n<i>{esc(self.plugin.store[key])}</i>", ("text", key), mid)

    def in_text(self, chat, text, key):
        if key not in ("text1", "text2"):
            raise UserError("Неизвестное поле.")
        if len(text) > 1000:
            raise UserError("Максимум 1000 символов.")
        value = (TEXT1 if key == "text1" else TEXT2) if text.strip() == "-" else text.strip()
        self.plugin.store.set(**{key: value})
        self.on_home(chat)

    def on_queue(self, chat, mid):
        items = sorted(self.plugin.queue["items"].values(), key=lambda i: i["due"])
        lines = [f"📋 <b>Очередь напоминаний: {len(items)}</b>"]
        for i in items[:25]:
            lines.append(f"• #{esc(i['id'])} · {esc(i['buyer'])} · {i['stage']}-е в {fmt_dt(i['due'])}")
        rows = [[("🗑 Очистить очередь", ("clear",))]] if items else []
        rows.append([("◀️ Назад", ("home",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_clear(self, chat, mid):
        self.plugin.queue.change(lambda q: q["items"].clear())
        self.on_queue(chat, mid)


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = Reminder(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = ReminderPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


def start(cardinal, *args):
    if PLUGIN is not None:
        PLUGIN.start()


def stop(cardinal=None, *args):
    if PLUGIN is not None:
        PLUGIN.stop_event.set()
        PLUGIN.wake.set()


def new_order(cardinal, event, *args):
    if PLUGIN is not None:
        try:
            PLUGIN.schedule(event.order)
        except Exception:
            LOG.exception("%s: ошибка обработки заказа", NAME)


def status_changed(cardinal, event, *args):
    if PLUGIN is None:
        return
    order = event.order
    if enum_name(getattr(order, "status", "")) != "PAID":
        try:
            PLUGIN.drop(order.id)
        except Exception:
            LOG.exception("%s: ошибка обработки статуса", NAME)


BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [start]
BIND_TO_PRE_STOP = [stop]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_ORDER_STATUS_CHANGED = [status_changed]
BIND_TO_DELETE = None
