"""
💬 Chat Sync — синхронизация чатов FunPay с Telegram (темы форума). Плагин для FunPay Cardinal от LuckyFunpay.

Для каждого чата FunPay создаётся отдельная тема в вашей Telegram-группе. Вы читаете покупателей и
отвечаете прямо в Telegram: тексты, фото и стикеры уходят на FunPay.

ИНСТРУКЦИЯ (полная версия — в боте: кнопка настроек плагина → 📖 Инструкция)
1. Включите плагин: /menu → Система и конфиги → Плагины → Chat Sync → Включить, затем /restart.
2. Создайте у @BotFather минимум 4 бота-помощника, юзернейм каждого должен начинаться с funpay.
3. Создайте группу, включите «Темы», сделайте администраторами основного бота и всех помощников
   (право «Управление темами» обязательно).
4. Главный экран плагина: /chat_sync → «➕ Добавить Telegram бота» — отправьте токены помощников.
5. В группе отправьте /setup_sync_chat, затем /sync_chats — появятся темы для всех чатов.
6. Пишите в теме покупателя — сообщение уйдёт на FunPay. Команды в теме: /watch, /stats, /history.
Старый режим получения сообщений Cardinal должен быть выключен.

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
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

NAME = "Chat Sync"
VERSION = "1.0.0"
DESCRIPTION = ("💬 Переписка с покупателями FunPay в Telegram: отдельная тема на каждый чат, ответы текстом, "
               "фото и стикерами, статистика покупателя в названии темы, заготовки ответов. Настройка: /chat_sync")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "0b8f3d6a-91e4-4c27-b5a8-7e2c4f1d9a63"
SETTINGS_PAGE = True
SLUG = "ChatSync"
PREFIX = "lfcs:"

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


MIN_BOTS = 4
MAX_BOTS = 10
BOT_INTERVAL = 3.1  # ≈19 сообщений в минуту на одного бота в одну группу
SWITCHES = [
    ("hide_watermark", "Скрывать вотермарку", False),
    ("ads", "Рекламные сообщения FunPay", True),
    ("image_name", "Показывать имя изображения", True),
    ("mono", "Моно шрифт", False),
    ("chat_link", "Ссылка на чат", False),
    ("topic_info", "Изменять название и иконку темы", True),
    ("watching", "Покупатель смотрит", True),
    ("templates", "Заготовки ответов", False),
    ("my_sound", "Уведомление при сообщении от меня", True),
    ("mention", "@ при сообщении собеседника", False),
]
DEFAULTS = {"bots": [], "group": None, "group_title": "", "group_username": "", "group_invite": "",
            "switches": {k: v for k, _, v in SWITCHES}, "topics": {}, "names": {}, "watch_seen": {},
            "stats": {}, "flags": {}}
ICON_PREFS = {
    "support": ["🗣", "📣", "❗️"],
    "attention": ["⁉️", "❗️", "⚡️"],
    "blacklist": ["☠️", "💀", "❌", "⛔️"],
    "paid": ["💸", "💰", "🪙"],
    "vip50": ["🏆", "💎", "👑"],
    "vip10": ["🌟", "⭐️"],
    "vip1": ["⭐️", "✅"],
    "refund": ["↩️", "🔙", "❌"],
    "new": [],
}
SIGNIFICANT = {"ORDER_PURCHASED", "ORDER_CONFIRMED", "ORDER_CONFIRMED_BY_ADMIN", "REFUND", "PARTIAL_REFUND",
               "REFUND_BY_ADMIN", "ORDER_REOPENED"}
TOKEN_RE = re.compile(r"\d{5,15}:[A-Za-z0-9_-]{30,60}")


def tg_error_text(error) -> str:
    return str(getattr(error, "description", "") or error)


def make_bot(token):
    import telebot
    return telebot.TeleBot(token, threaded=False)


class SyncCore:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("settings.json", DEFAULTS)
        self.ui = None
        self.queue = deque()
        self.queue_lock = threading.Condition()
        self.bots = {}  # token → bot
        self.next_free = {}
        self.sent_ids = deque(maxlen=500)
        self.buffers = {}
        self.buffer_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.sync_running = False
        self.icons = None
        self.sync = False  # в тестах очередь выполняется синхронно
        self.flush_delay = 1.2

    # --- состояние
    @property
    def main_bot(self):
        return self.cardinal.telegram.bot if tg_ready(self.cardinal) else None

    def switch(self, key) -> bool:
        return bool(self.store["switches"].get(key, dict((k, v) for k, _, v in SWITCHES).get(key)))

    def ready_problems(self) -> list:
        data = self.store.snapshot()
        problems = []
        if getattr(self.cardinal, "old_mode_enabled", False):
            problems.append("old_mode")
        if len(data["bots"]) < MIN_BOTS:
            problems.append("bots")
        if not data["group"]:
            problems.append("group")
        return problems

    def ready(self) -> bool:
        return not self.ready_problems()

    def helper(self, token):
        bot = self.bots.get(token)
        if bot is None:
            bot = self.bots[token] = make_bot(token)
        return bot

    def senders(self) -> list:
        items = [("main", self.main_bot)] if self.main_bot else []
        items += [(b["token"], self.helper(b["token"])) for b in self.store["bots"]]
        return items

    # --- очередь отправки
    def submit(self, item, front=False):
        self.submit_many([item], front)

    def submit_many(self, items, front=False):
        with self.queue_lock:
            if front:
                for item in reversed(items):
                    self.queue.appendleft(item)
            else:
                self.queue.extend(items)
            self.queue_lock.notify()
        if self.sync and not getattr(self, "_draining", False):
            self._draining = True
            try:
                self.drain()
            finally:
                self._draining = False

    def drain(self):
        while True:
            with self.queue_lock:
                if not self.queue:
                    return
                item = self.queue.popleft()
            self.execute(item)

    def worker(self):
        while not self.stop_event.is_set():
            with self.queue_lock:
                while not self.queue and not self.stop_event.is_set():
                    self.queue_lock.wait(5)
                if self.stop_event.is_set():
                    return
                item = self.queue.popleft()
            try:
                self.execute(item)
            except Exception:
                LOG.exception("%s: ошибка отправки в Telegram", NAME)

    def pick_bot(self):
        senders = self.senders()
        if not senders:
            return None, None
        key, bot = min(senders, key=lambda kb: self.next_free.get(kb[0], 0))
        delay = self.next_free.get(key, 0) - time.time()
        if delay > 0 and not self.sync:
            time.sleep(delay)
        self.next_free[key] = time.time() + BOT_INTERVAL
        return key, bot

    def call(self, method, **kwargs):
        """Вызывает метод Telegram API через свободного бота, учитывая лимиты."""
        for attempt in range(len(self.senders()) + 2):
            key, bot = self.pick_bot()
            if bot is None:
                raise RuntimeError("Нет ботов для отправки")
            try:
                return getattr(bot, method)(**kwargs)
            except Exception as error:
                text = tg_error_text(error)
                if "Too Many Requests" in text or "retry after" in text:
                    match = re.search(r"retry after (\d+)", text)
                    self.next_free[key] = time.time() + (int(match.group(1)) if match else 30)
                    continue
                if "thread not found" in text or "TOPIC_ID_INVALID" in text or "TOPIC_DELETED" in text:
                    raise LookupError("thread") from None
                if "Unauthorized" in text or "bot was kicked" in text or "not enough rights" in text:
                    self.next_free[key] = time.time() + 300
                    continue
                raise
        raise RuntimeError("Все боты временно недоступны")

    def execute(self, item):
        group = self.store["group"]
        if not group:
            return
        kind = item["type"]
        chat = str(item.get("chat", ""))
        try:
            if kind == "topic":
                if chat in self.store["topics"]:
                    return
                name = self.topic_title(chat, item.get("name"))
                icon = self.topic_icon(chat)
                kwargs = {"chat_id": group, "name": name}
                if icon:
                    kwargs["icon_custom_emoji_id"] = icon
                topic = self.call("create_forum_topic", **kwargs)
                thread = topic.message_thread_id
                self.store.change(lambda d: (d["topics"].__setitem__(chat, thread),
                                             d["names"].__setitem__(chat, item.get("name") or d["names"].get(chat, ""))))
                self.store.change(lambda d: d["flags"].setdefault(chat, {}).update(title=name, icon=icon or ""))
                buyer = item.get("name") or self.store["names"].get(chat) or chat
                self.call("send_message", chat_id=group, message_thread_id=thread, parse_mode="HTML",
                          disable_web_page_preview=True,
                          text=(f"💬 Чат с <a href=\"https://funpay.com/chat/?node={chat}\">{esc(buyer)}</a>\n"
                                f"🧾 <a href=\"https://funpay.com/orders/trade?buyer={esc(buyer)}\">Продажи</a> · "
                                f"🛍 <a href=\"https://funpay.com/orders/?seller={esc(buyer)}\">Покупки</a>"))
            elif kind == "msg":
                thread = self.store["topics"].get(chat)
                if thread is None:
                    self.submit_many([{"type": "topic", "chat": chat, "name": item.get("name")}, item], front=True)
                    return
                kwargs = {"chat_id": group, "message_thread_id": thread, "text": item["text"][:4096],
                          "parse_mode": "HTML", "disable_web_page_preview": not item.get("preview"),
                          "disable_notification": bool(item.get("silent"))}
                if item.get("keyboard") is not None:
                    kwargs["reply_markup"] = item["keyboard"]
                self.call("send_message", **kwargs)
            elif kind == "edit_topic":
                thread = self.store["topics"].get(chat)
                if thread is None:
                    return
                name = self.topic_title(chat)
                icon = self.topic_icon(chat) or ""
                flags = self.store["flags"].get(chat, {})
                if flags.get("title") == name and flags.get("icon") == icon:
                    return
                self.call("edit_forum_topic", chat_id=group, message_thread_id=thread, name=name,
                          icon_custom_emoji_id=icon)
                self.store.change(lambda d: d["flags"].setdefault(chat, {}).update(title=name, icon=icon))
        except LookupError:
            LOG.info("%s: тема чата %s удалена, создаю заново", NAME, chat)
            self.store.change(lambda d: d["topics"].pop(chat, None))
            if kind == "msg" and not item.get("retried"):
                self.submit_many([{"type": "topic", "chat": chat, "name": item.get("name")},
                                  dict(item, retried=True)], front=True)
        except Exception as error:
            LOG.warning("%s: Telegram не принял %s: %s", NAME, kind, tg_error_text(error)[:200])

    # --- темы
    def stats(self, name, force=False) -> dict:
        cache = self.store["stats"].get(name.casefold() if name else "")
        if cache and not force and time.time() - cache.get("t", 0) < 600:
            return cache
        result = {"paid": 0, "closed": 0, "refunded": 0, "sums": {}, "t": time.time()}
        if not name:
            return result
        account = self.cardinal.account
        start, pages = None, 0
        try:
            while pages < 10:
                res = account.get_sales(start_from=start, buyer=name)
                start, orders = res[0], res[1]
                pages += 1
                for order in orders:
                    if (getattr(order, "buyer_username", "") or "").casefold() != name.casefold():
                        continue
                    status = enum_name(order.status)
                    key = {"PAID": "paid", "CLOSED": "closed", "REFUNDED": "refunded"}.get(status, "refunded")
                    result[key] += 1
                    cur = str(getattr(order, "currency", "₽"))
                    sums = result["sums"].setdefault(cur, {"paid": 0, "closed": 0, "refunded": 0})
                    sums[key] = round(sums[key] + float(getattr(order, "price", 0) or 0), 2)
                if not start:
                    break
        except Exception:
            LOG.warning("%s: не удалось посчитать статистику %s", NAME, name)
            return cache or result
        self.store.change(lambda d: d["stats"].__setitem__(name.casefold(), result))
        return result

    def topic_title(self, chat, name=None) -> str:
        name = name or self.store["names"].get(chat) or "Покупатель"
        if not self.switch("topic_info"):
            return f"{name} ({chat})"[:128]
        st = self.store["stats"].get(name.casefold())
        if st and (st["paid"] or st["closed"] or st["refunded"]):
            return f"{st['paid']}|{st['closed']}|{st['refunded']}👤{name} ({chat})"[:128]
        return f"{name} ({chat})"[:128]

    def icon_ids(self) -> dict:
        if self.icons is None:
            try:
                self.icons = {s.emoji: s.custom_emoji_id for s in self.main_bot.get_forum_topic_icon_stickers()}
            except Exception:
                self.icons = {}
        return self.icons

    def topic_icon(self, chat):
        if not self.switch("topic_info"):
            return None
        name = self.store["names"].get(chat) or ""
        flags = self.store["flags"].get(chat, {})
        st = self.store["stats"].get(name.casefold()) or {"paid": 0, "closed": 0, "refunded": 0}
        if flags.get("support"):
            kind = "support"
        elif flags.get("attention") and st["paid"]:
            kind = "attention"
        elif name and name in getattr(self.cardinal, "blacklist", []):
            kind = "blacklist"
        elif st["paid"]:
            kind = "paid"
        elif st["closed"] >= 50:
            kind = "vip50"
        elif st["closed"] >= 10:
            kind = "vip10"
        elif st["closed"] >= 1:
            kind = "vip1"
        elif st["refunded"]:
            kind = "refund"
        else:
            kind = "new"
        icons = self.icon_ids()
        for emoji in ICON_PREFS[kind]:
            if emoji in icons:
                return icons[emoji]
        return None

    def refresh_topic(self, chat, name=None, force_stats=True):
        name = name or self.store["names"].get(str(chat))
        if name and self.switch("topic_info"):
            before = copy.deepcopy(self.store["stats"].get(name.casefold()))
            after = self.stats(name, force=force_stats)
            changed = before is None or any(before.get(k) != after.get(k) for k in ("paid", "closed", "refunded"))
            self.submit({"type": "edit_topic", "chat": str(chat)})
            return changed
        return False

    # --- FunPay → Telegram
    def author_line(self, msg, chat, name) -> tuple:
        """Возвращает (значок/подпись автора, ключ автора для склейки, тип)."""
        account = self.cardinal.account
        my_id = getattr(account, "id", None)
        author = getattr(msg, "author", None) or name or "?"
        badge = getattr(msg, "badge", None)
        if getattr(msg, "author_id", None) == 0:
            if enum_name(getattr(msg, "type", "")) == "DEAR_VENDORS":
                return "📣", "fp_ad", "ad"
            return "🔵", "fp", "system"
        if getattr(msg, "is_employee", False) or getattr(msg, "is_support", False) or \
                getattr(msg, "is_moderation", False) or getattr(msg, "is_arbitration", False):
            return f"🆘 <b>{esc(author)}</b>" + (f" ({esc(badge)})" if badge else ""), f"emp{msg.author_id}", "support"
        if msg.author_id == my_id:
            if getattr(msg, "by_bot", False):
                return "🤖 <b>FPC</b>", "me_bot", "me"
            if badge:
                return f"📦 <b>Вы</b> ({esc(badge)})", "me_badge", "me"
            return "🫵 <b>Вы</b>", "me", "me"
        link = self.switch("chat_link")
        shown = f"<a href=\"https://funpay.com/chat/?node={chat}\">{esc(author)}</a>" if link else esc(author)
        if author in getattr(self.cardinal, "blacklist", []):
            return f"🚷 <b>{shown}</b>", "buyer", "buyer"
        if badge or getattr(msg, "is_autoreply", False):
            return f"🛍️ <b>{shown}</b> ({esc(badge or 'автоответ')})", "buyer_auto", "buyer_auto"
        if getattr(msg, "by_vertex", False):
            return f"🐺 <b>{shown}</b>", "buyer", "buyer"
        if getattr(msg, "by_bot", False):
            return f"🐦 <b>{shown}</b>", "buyer", "buyer"
        return f"👤 <b>{shown}</b>", "buyer", "buyer"

    def body(self, msg) -> str:
        text = getattr(msg, "text", None)
        link = getattr(msg, "image_link", None)
        if link and not text:
            label = (getattr(msg, "image_name", None) or "Фото") if self.switch("image_name") else "Фото"
            return f"<a href=\"{esc(link)}\">{esc(label)}</a>"
        text = text or ""
        watermark = ""
        try:
            watermark = self.cardinal.MAIN_CFG["Other"].get("watermark", "") or ""
        except Exception:
            watermark = ""
        prefix = ""
        if self.switch("hide_watermark") and watermark and text.startswith(watermark):
            text = text[len(watermark):].lstrip("\n")
            prefix = "<tg-spoiler>🐦</tg-spoiler> "
        if getattr(msg, "author_id", None) == 0:
            return f"<b><i>{esc(text)}</i></b>"
        return prefix + (f"<code>{esc(text)}</code>" if self.switch("mono") else esc(text))

    def on_new_message(self, msg):
        if not self.ready():
            return
        chat = str(getattr(msg, "chat_id", ""))
        if not chat or not chat.lstrip("-").isdigit():
            return  # общий чат игры и т.п.
        if getattr(msg, "id", None) in self.sent_ids:
            return
        name = getattr(msg, "chat_name", None) or self.store["names"].get(chat)
        if name and self.store["names"].get(chat) != name:
            self.store.change(lambda d: d["names"].__setitem__(chat, name))
        with self.buffer_lock:
            buf = self.buffers.setdefault(chat, [])
            buf.append(msg)
            first = len(buf) == 1
        if first:
            if self.sync:
                self.flush(chat)
            else:
                timer = threading.Timer(self.flush_delay, self.flush, args=(chat,))
                timer.daemon = True
                timer.start()

    def flush(self, chat):
        with self.buffer_lock:
            messages = self.buffers.pop(chat, [])
        if not messages:
            return
        name = self.store["names"].get(chat) or getattr(messages[0], "chat_name", None) or chat
        if chat not in self.store["topics"]:
            self.submit({"type": "topic", "chat": chat, "name": name})
        groups = []
        mention = False
        only_me = True
        significant = False
        flags_update = {}
        for msg in messages:
            line, key, kind = self.author_line(msg, chat, name)
            if kind == "ad" and not self.switch("ads"):
                continue
            if kind == "system" and enum_name(getattr(msg, "type", "")) in SIGNIFICANT:
                significant = True
                mtype = enum_name(msg.type)
                if mtype == "ORDER_REOPENED":
                    flags_update["attention"] = True
                elif mtype in ("ORDER_CONFIRMED", "ORDER_CONFIRMED_BY_ADMIN", "REFUND", "REFUND_BY_ADMIN"):
                    flags_update["attention"] = False
            if kind == "support":
                mention = True
                significant = True
                badge = (getattr(msg, "badge", "") or "").lower()
                if getattr(msg, "is_arbitration", False) or getattr(msg, "is_moderation", False) or \
                        "арбитраж" in badge or "модерац" in badge:
                    flags_update["attention"] = True
                flags_update["support"] = True
            if kind in ("buyer",) and self.switch("mention"):
                mention = True
            if kind != "me":
                only_me = False
            body = self.body(msg)
            if groups and groups[-1][0] == key:
                groups[-1][2].append(body)
            else:
                groups.append([key, line, [body]])
        if not groups:
            return
        if flags_update:
            self.store.change(lambda d: d["flags"].setdefault(chat, {}).update(flags_update))
        watching = ""
        if self.switch("watching") and any(g[0] == "buyer" for g in groups):
            watching = self.watching_line(chat, messages)
        parts = []
        for key, line, bodies in groups:
            if key in ("fp", "fp_ad"):
                parts.append(f"{line} " + "\n".join(bodies))
            else:
                parts.append(f"{line}\n" + "\n".join(bodies))
        text = (watching + "\n\n" if watching else "") + "\n\n".join(parts)
        if mention:
            text += "\n" + "".join(f"<a href=\"tg://user?id={uid}\">​</a>" for uid in self.ui.admins()) \
                if self.ui else ""
        preview = any(getattr(m, "image_link", None) for m in messages)
        silent = only_me and not self.switch("my_sound")
        keyboard = self.templates_keyboard()
        self.submit({"type": "msg", "chat": chat, "name": name, "text": text, "silent": silent, "preview": preview,
                     "keyboard": keyboard})
        if significant:
            changed = self.refresh_topic(chat, name)
            if changed and any(g[0] == "fp" for g in groups):
                self.submit({"type": "msg", "chat": chat, "name": name, "text": self.stats_text(name),
                             "silent": True})

    def watching_line(self, chat, messages) -> str:
        buyer_id = next((getattr(m, "author_id", None) for m in messages
                         if getattr(m, "author_id", None) not in (None, 0, getattr(self.cardinal.account, "id", None))),
                        None)
        if buyer_id is None:
            return ""
        last = self.store["watch_seen"].get(str(buyer_id), 0)
        if time.time() - last < 86400:
            return ""
        viewing = next((getattr(m, "buyer_viewing", None) for m in messages if getattr(m, "buyer_viewing", None)),
                       None)
        if viewing is None and hasattr(self.cardinal.account, "get_buyer_viewing"):
            try:
                viewing = self.cardinal.account.get_buyer_viewing(buyer_id)
            except Exception:
                viewing = None
        link = getattr(viewing, "link", None)
        if not link:
            return ""
        self.store.change(lambda d: d["watch_seen"].__setitem__(str(buyer_id), time.time()))
        return f"👁 Смотрит: <a href=\"{esc(link)}\">{esc(getattr(viewing, 'text', None) or link)}</a>"

    def templates_keyboard(self):
        if not self.switch("templates"):
            return None
        templates = list(getattr(self.cardinal.telegram, "answer_templates", []) or []) if tg_ready(self.cardinal) else []
        if not templates:
            return None
        from telebot.types import KeyboardButton, ReplyKeyboardMarkup
        markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
        for i in range(0, min(len(templates), 30), 2):
            markup.row(*[KeyboardButton(self.short(t)) for t in templates[i:i + 2]])
        return markup

    @staticmethod
    def short(template) -> str:
        template = str(template).replace("\n", " ")
        return template if len(template) <= 40 else template[:39] + "…"

    def full_template(self, text):
        for template in getattr(self.cardinal.telegram, "answer_templates", []) or []:
            if text == template or text == self.short(template):
                return template
        return text

    def stats_text(self, name) -> str:
        st = self.stats(name)
        lines = [f"📊 <b>Статистика по пользователю {esc(name)}</b>",
                 f"Оплачено: <b>{st['paid']}</b> · Закрыто: <b>{st['closed']}</b> · Возвратов: <b>{st['refunded']}</b>"]
        for cur, sums in st["sums"].items():
            lines.append(f"{esc(cur)}: оплачено {sums['paid']:g} · закрыто {sums['closed']:g} · "
                         f"возвращено {sums['refunded']:g}")
        return "\n".join(lines)

    def on_new_order(self, order):
        if not self.ready():
            return
        chat = str(getattr(order, "chat_id", "") or "")
        name = getattr(order, "buyer_username", None)
        try:
            found = self.cardinal.account.get_chat_by_name(name) if name else None
            if found is not None:
                chat = str(found.id)
        except Exception:
            pass
        if not chat.isdigit():
            chat = next((c for c, n in self.store["names"].items() if name and n == name), "")
        if not chat:
            return
        if name:
            self.store.change(lambda d: d["names"].__setitem__(chat, name))
        if chat not in self.store["topics"]:
            self.submit({"type": "topic", "chat": chat, "name": name})
        self.refresh_topic(chat, name)

    def on_initial_chat(self, chat_obj):
        if not self.ready():
            return
        chat = str(getattr(chat_obj, "id", ""))
        name = getattr(chat_obj, "name", None)
        if not chat.isdigit() or not name:
            return
        self.store.change(lambda d: d["names"].__setitem__(chat, name))
        if chat not in self.store["topics"]:
            self.submit({"type": "topic", "chat": chat, "name": name})

    def sync_all(self) -> int:
        account = self.cardinal.account
        try:
            chats = account.get_chats(update=True)
        except TypeError:
            chats = account.get_chats()
        created = 0
        for chat_id, chat in (chats.items() if isinstance(chats, dict) else enumerate(chats)):
            cid = str(getattr(chat, "id", chat_id))
            name = getattr(chat, "name", None)
            if not cid.isdigit() or not name:
                continue
            self.store.change(lambda d: d["names"].__setitem__(cid, name))
            if cid not in self.store["topics"]:
                self.submit({"type": "topic", "chat": cid, "name": name})
                created += 1
        return created

    # --- Telegram → FunPay
    def chat_by_thread(self, thread):
        for chat, tid in self.store["topics"].items():
            if tid == thread:
                return chat
        return None

    def send_to_funpay(self, chat, text=None, image=None) -> bool:
        account = self.cardinal.account
        name = self.store["names"].get(chat)
        for attempt in range(3):
            try:
                if image is not None:
                    image_id = account.upload_image(io.BytesIO(image), type_="chat") if not isinstance(image, int) else image
                    msg = account.send_image(int(chat), image_id, name)
                else:
                    msg = account.send_message(int(chat), text, name)
                if msg is not None and getattr(msg, "id", None) is not None:
                    self.sent_ids.append(msg.id)
                return True
            except Exception as error:
                LOG.warning("%s: не удалось отправить в FunPay чат %s: %s", NAME, chat, error)
                time.sleep(1)
        return False

    def format_outgoing(self, chat, text) -> str:
        name = self.store["names"].get(chat) or ""
        try:
            from Utils.cardinal_tools import format_msg_text
            from FunPayAPI.types import ChatShortcut
            obj = ChatShortcut(int(chat), name, "", 0, 0, "", False)
            return format_msg_text(text, obj)
        except Exception:
            now = datetime.now()
            for key, value in (("$username", name), ("$chat_name", name), ("$chat_id", chat),
                               ("$date", now.strftime("%d.%m.%Y")), ("$time", now.strftime("%H:%M"))):
                text = text.replace(key, value)
            return text

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.worker, name="ChatSyncSender", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        with self.queue_lock:
            self.queue_lock.notify_all()


class SyncPanel(Panel):
    COMMAND = "chat_sync"
    COMMANDS = [("chat_sync", "💬 Chat Sync — настройки"), ("setup_sync_chat", "💬 Привязать группу (в группе)"),
                ("delete_sync_chat", "💬 Отвязать группу"), ("sync_chats", "💬 Создать темы для всех чатов"),
                ("watch", "👁 Что смотрит покупатель (в теме)"), ("stats", "📊 Статистика покупателя (в теме)"),
                ("history", "📜 Последние 25 сообщений (в теме)")]
    ICON = "💬"
    GUIDE = [
        ("💬 Что умеет плагин", "Chat Sync переносит переписку с покупателями FunPay в Telegram-группу с темами. "
         "Для каждого чата — своя тема.\n🗂 Своя тема для каждого покупателя.\n🤖 Мульти-бот: отправка и создание "
         "тем распределяются между ботами-помощниками (лимит Telegram — 20 сообщений в минуту на бота в группу).\n"
         "📊 Статистика покупателя (оплачено|закрыто|возвраты) в названии темы и иконка-подсказка.\n"
         "👁 «Покупатель смотрит» — какой лот он открыт.\n📝 Заготовки ответов кнопками.\n🔔 Упоминания (@).\n"
         "🖼 Фото и стикеры в обе стороны.\n📜 Команды в теме: /watch, /stats, /history."),
        ("🔌 Часть 1. Включение", "⚠️ Без этого шага настройки не работают.\n1. /menu → Система и конфиги → "
         "Плагины → <b>Chat Sync</b> → Включить.\n2. Обязательно /restart и дождитесь запуска.\n"
         "3. Главный экран плагина: кнопка настроек в карточке плагина или команда /chat_sync.\n\n"
         "⛔ Плагин не работает при включённом «старом режиме получения сообщений» Cardinal — главный экран "
         "предупредит об этом."),
        ("🤖 Часть 2. Боты и группа", "<b>Шаг 1.</b> У @BotFather создайте 4 бота-помощника (/newbot). "
         "⚠️ Юзернейм каждого должен начинаться с <code>funpay</code>, например <code>funpay_helper1_bot</code>. "
         "Сохраните токены. Основного бота добавлять не нужно — он участвует сам.\n\n<b>Шаг 2.</b> Создайте группу → "
         "Управление группой → включите <b>Темы</b>. Администраторы → добавьте основного бота и всех помощников, "
         "выдайте право «Управление темами» и отправку сообщений (проще все права).\n"
         "⚠️ Основной бот обязательно администратор, иначе не увидит ваши сообщения в темах."),
        ("⚙️ Часть 3. Главный экран", "Вверху: привязанная группа, «Готов к работе ✅/❌» и подсказка "
         "«Что сейчас делать?».\n• «➕ Добавить Telegram бота» — отправьте токен помощника. Ошибки: уже добавлен; "
         "основного добавлять не нужно; неверный токен; юзернейм не начинается с funpay; лимит 10 ботов.\n"
         "• 🗑 рядом с помощником — удалить (без подтверждения). Меньше 4 — плагин остановится.\n"
         "• Значки: ✅ всё хорошо · ➕ бота нет в группе (нажмите — добавить) · 🔒 не администратор · "
         "📛 нет права «Управление темами» · 💥 не удалось проверить · ❔ неизвестно.\n"
         "• «🔄 Обновить» — перепроверить ботов после изменения прав."),
        ("🔗 Команды группы", "<code>/setup_sync_chat</code> — отправьте <b>в группе</b>, чтобы привязать её. "
         "Если группа уже была, бот спросит подтверждение: связи «тема ↔ чат» сбросятся.\n"
         "<code>/delete_sync_chat</code> — отвязать группу (в группе или в личке), с подтверждением.\n"
         "<code>/sync_chats</code> — создать темы для всех чатов FunPay, у которых их ещё нет (можно повторять).\n"
         "Темы также создаются сами: при первом сообщении нового покупателя, при заказе и при запуске бота."),
        ("🎛 Часть 4. Переключатели", "1. Скрывать вотермарку (🔴) — подпись бота заменяется скрытым 🐦.\n"
         "2. Рекламные сообщения FunPay (🟢) — показывать рассылки с пометкой 📣.\n"
         "3. Показывать имя изображения (🟢) — иначе «Фото».\n4. Моно шрифт (🔴) — текст копируется одним нажатием.\n"
         "5. Ссылка на чат (🔴) — имя собеседника кликабельно.\n6. Изменять название и иконку темы (🟢) — "
         "<code>1|5|0👤Sasha (12345678)</code>: оплачено|закрыто|возвраты.\n7. Покупатель смотрит (🟢) — строка "
         "«Смотрит: …» не чаще раза в сутки на покупателя.\n8. Заготовки ответов (🔴) — шаблоны Cardinal кнопками.\n"
         "9. Уведомление при сообщении от меня (🟢).\n10. @ при сообщении собеседника (🔴). Поддержку FunPay бот "
         "упоминает всегда."),
        ("🎨 Иконки тем", "Иконка выбирается по приоритету:\n🗣 сотрудник поддержки FunPay → ⁉️ заказ снова открыт / "
         "модерация или арбитраж при оплаченном заказе → ☠️ покупатель в ЧС → 💸 есть оплаченный заказ → "
         "🏆/🌟/⭐️ постоянный клиент (50+/10+/1+ закрытых) → ↩️ были только возвраты → стандартная (новый клиент).\n"
         "Название и иконка обновляются при оплате, подтверждении, возврате, повторном открытии и сообщениях "
         "поддержки. При системных событиях в тему приходит карточка «Статистика по пользователю»."),
        ("💬 Часть 5. Общение", "Пишите в теме покупателя — текст уйдёт на FunPay без вотермарки и не "
         "продублируется. Можно отправлять текст, фото (до 20 МБ, подпись уходит первой) и обычные стикеры. "
         "Голосовые, видео и прочие файлы не поддерживаются.\nСообщения, начинающиеся с «/», — команды. "
         "В общей теме (General) сообщения не пересылаются.\n\nЗначки: 👤 покупатель · 🫵 Вы · 🤖 FPC · 📦 Вы (пометка) · "
         "🛍️ автоответ покупателя · 🔵 система FunPay · 📣 реклама · 🆘 поддержка · 🚷 из ЧС · 🐦 другой бот · "
         "🐺 FunPay Vertex.\nУдалили тему? Бот создаст новую при следующем сообщении, старую переписку покажет /history."),
        ("📋 Команды в теме", "<code>/watch</code> — что покупатель просматривает прямо сейчас.\n"
         "<code>/stats</code> — сколько заказов оплачено, закрыто, возвращено и на какие суммы (по валютам).\n"
         "<code>/history</code> — последние 25 сообщений переписки в оформлении темы.\nВне темы покупателя бот "
         "ответит: «❌ Команду нужно вводить в теме покупателя!»"),
        ("❓ FAQ", "<b>«@username бота должен начинаться с funpay»</b> — @BotFather → /mybots → бот → Edit Bot → "
         "Edit Username.\n<b>Нет кнопки «⚙️ Настройки переключателей»</b> — плагин ещё не готов: 4 помощника, "
         "группа привязана, старый режим выключен, после включения сделан /restart.\n<b>Мои сообщения не уходят</b> — "
         "пишите в теме покупателя, основной бот — администратор, сообщение не начинается с «/», фото до 20 МБ.\n"
         "<b>Зачем 4 бота?</b> Одиночный бот при потоке сообщений получает FloodWait на 10–15 минут. Можно до 10.\n"
         "<b>Перенос в другую группу</b> — /setup_sync_chat в новой группе → ✅ Да → /sync_chats.\n"
         "<b>Отключить</b> — выключите плагин и /restart, или только отвяжите группу /delete_sync_chat."),
    ]

    # --- главный экран
    def hint(self) -> str:
        problems = self.plugin.ready_problems()
        data = self.plugin.store.snapshot()
        if "old_mode" in problems:
            return "⛔ Плагин несовместим со старым режимом получения сообщений. Выключите его в настройках Cardinal."
        if "bots" in problems:
            need = MIN_BOTS - len(data["bots"])
            return f"Создайте ещё {need} бота(-ов) у @BotFather (юзернейм начинается с funpay) и добавьте токены."
        if "group" in problems:
            return ("Создайте группу, переведите её в режим Темы, добавьте ботов администраторами и отправьте в "
                    "группе /setup_sync_chat.")
        statuses = getattr(self, "statuses", {})
        if any(s not in ("ok",) for s in statuses.values()):
            return "⚠️ Есть проблемы с некоторыми ботами. Смотрите значки рядом с ними."
        return "✅ Всё готово! Плагин работает."

    def check_status(self, bot_id) -> str:
        group = self.plugin.store["group"]
        if not group:
            return "unknown"
        try:
            member = self.bot.get_chat_member(group, bot_id)
        except Exception:
            return "error"
        status = getattr(member, "status", "")
        if status in ("left", "kicked"):
            return "absent"
        if status == "member" or status == "restricted":
            return "not_admin"
        if status == "administrator":
            return "ok" if getattr(member, "can_manage_topics", False) else "no_topics"
        if status == "creator":
            return "ok"
        return "unknown"

    def on_home(self, chat, mid=None, *args):
        p = self.plugin
        data = p.store.snapshot()
        ready = p.ready()
        group = data["group"]
        if group:
            shown = ("@" + data["group_username"]) if data["group_username"] else (
                data["group_invite"] or f"<code>{group}</code>")
        else:
            shown = "Не установлен"
        self.statuses = {}
        try:
            me = self.bot.get_me()
            main = (me.id, me.username)
        except Exception:
            main = (None, "основной бот")
        bots = [("main", main[0], main[1])] + [(b["token"], b["id"], b["username"]) for b in data["bots"]]
        if len(data["bots"]) >= MIN_BOTS and group:
            for key, bot_id, _ in bots:
                if bot_id:
                    self.statuses[key] = self.check_status(bot_id)
        text = (f"💬 <b>{NAME}</b> v{VERSION}\n\n"
                f"Группа для FunPay чатов: {shown}\n"
                f"Готов к работе: {'✅ Да' if ready else '❌ Нет'}\n"
                f"Что сейчас делать? {esc(self.hint())}\n"
                f"Тем создано: {len(data['topics'])}\n\nОбновлено: {datetime.now():%H:%M:%S}")
        icons = {"ok": "✅", "absent": "➕", "not_admin": "🔒", "no_topics": "📛", "error": "💥", "unknown": "❔"}
        rows = []
        if ready:
            rows.append([("⚙️ Настройки переключателей", ("switches",))])
        for key, bot_id, username in bots:
            row = [(("🤖 " if key == "main" else "") + "@" + str(username), f"https://t.me/{username}")]
            status = self.statuses.get(key)
            if status == "absent":
                row.append(("➕", f"https://t.me/{username}?startgroup=funpay"))
            elif status:
                row.append((icons[status], ("status", status)))
            if key != "main":
                row.append(("🗑️", ("del_bot", str(bot_id))))
            rows.append(row)
        if len(data["bots"]) < MAX_BOTS:
            rows.append([("➕ Добавить Telegram бота", ("add_bot",))])
        rows.append([("🔄 Обновить", ("home",))])
        rows += self.footer_rows()
        rows.append([("◀️ Назад", ("plugins",))])
        self.send(chat, text, rows, mid)

    def on_status(self, chat, mid, status):
        texts = {"ok": "✅ С ботом всё хорошо.",
                 "not_admin": "🔒 Бот есть в группе, но не администратор. Назначьте его администратором.",
                 "no_topics": "📛 Бот — администратор, но без права «Управление темами». Включите это право.",
                 "error": "💥 Не удалось проверить бота. Убедитесь, что группа привязана и основной бот в ней. "
                          "Затем нажмите «Обновить».",
                 "unknown": "❔ Статус определить не удалось. Нажмите «Обновить»."}
        self.answer(self.call, texts.get(status, "❔"), alert=True)

    def on_switches(self, chat, mid):
        if not self.plugin.ready():
            raise UserError("Сначала завершите настройку — плагин ещё не готов к работе.")
        rows = [[(("🟢 " if self.plugin.switch(k) else "🔴 ") + title, ("sw", k))] for k, title, _ in SWITCHES]
        rows.append([("◀️ Назад", ("home",))])
        self.send(chat, "🎛 <b>Переключатели Chat Sync</b>\n🟢 — включено, 🔴 — выключено. Нажмите, чтобы "
                        "переключить; изменения применяются сразу.", rows, mid)

    def on_sw(self, chat, mid, key):
        if key not in {k for k, _, _ in SWITCHES}:
            raise UserError("Неизвестный переключатель.")
        self.plugin.store.change(lambda d: d["switches"].__setitem__(key, not self.plugin.switch(key)))
        self.on_switches(chat, mid)

    def on_add_bot(self, chat, mid):
        if len(self.plugin.store["bots"]) >= MAX_BOTS:
            raise UserError(f"❌ Достигнут лимит ботов ({MAX_BOTS}).")
        self.ask(chat, "🤖 Отправь токен нового Telegram бота (выдаёт @BotFather).", ("bot_token",), mid)

    def in_bot_token(self, chat, text):
        token = text.strip()
        try:
            self.bot.delete_message(chat, self.message.message_id)
        except Exception:
            pass
        data = self.plugin.store.snapshot()
        if len(data["bots"]) >= MAX_BOTS:
            raise UserError(f"❌ Достигнут лимит ботов ({MAX_BOTS}).")
        if any(b["token"] == token for b in data["bots"]):
            raise UserError("❌ Бот с таким токеном уже добавлен.")
        if token == getattr(self.bot, "token", None):
            raise UserError("❌ Основного бота добавлять не нужно.")
        if not TOKEN_RE.fullmatch(token):
            raise UserError("❌ Ошибка при получении данных бота. Скопируйте токен заново из @BotFather.")
        try:
            me = make_bot(token).get_me()
        except Exception:
            raise UserError("❌ Ошибка при получении данных бота. Скопируйте токен заново из @BotFather.") from None
        username = getattr(me, "username", "") or ""
        if not username.lower().startswith("funpay"):
            raise UserError("❌ @username бота должен начинаться с funpay. Переименуйте его в @BotFather (/setusername).")
        try:
            main_id = self.bot.get_me().id
        except Exception:
            main_id = None
        if me.id == main_id:
            raise UserError("❌ Основного бота добавлять не нужно.")
        self.plugin.store.change(lambda d: d["bots"].append({"token": token, "id": me.id, "username": username}))
        self.send(chat, f"✅ Бот @{esc(username)} добавлен!", [[("🏠 Главный экран", ("home",))]])

    def on_del_bot(self, chat, mid, bot_id):
        removed = self.plugin.store.change(
            lambda d: [d["bots"].remove(b) or b for b in list(d["bots"]) if str(b["id"]) == str(bot_id)])
        for b in removed:
            self.plugin.bots.pop(b["token"], None)
        self.on_home(chat, mid)

    # --- команды в группе
    def group_user_ok(self, message) -> bool:
        user = getattr(message, "from_user", None)
        return user is not None and int(user.id) in self.admins()

    def reply(self, message, text, rows=None):
        thread = getattr(message, "message_thread_id", None) if getattr(message, "is_topic_message", False) else None
        markup = self.kb(message.chat.id, rows) if rows else None
        kwargs = {"parse_mode": "HTML", "disable_web_page_preview": True, "reply_markup": markup}
        if thread:
            kwargs["message_thread_id"] = thread
        return self.bot.send_message(message.chat.id, text, **kwargs)

    def group_command(self, message):
        if not self.group_user_ok(message):
            return
        cmd = (message.text or "").split()[0][1:].split("@")[0]
        self.message = message
        try:
            getattr(self, "cmd_" + cmd)(message)
        except UserError as error:
            self.reply(message, esc(error))
        except Exception:
            LOG.exception("%s: ошибка команды %s", NAME, cmd)
            self.reply(message, "⚠️ Ошибка, подробности в логе Cardinal.")
        finally:
            self.message = None

    def cmd_chat_sync(self, message):
        if message.chat.type == "private":
            self.on_home(message.chat.id)

    def cmd_setup_sync_chat(self, message):
        chat = message.chat
        if chat.type not in ("group", "supergroup"):
            raise UserError("❌ Эту команду нужно вводить в созданной группе!")
        info = self.bot.get_chat(chat.id)
        if not getattr(info, "is_forum", False):
            raise UserError("❌ Чат должен быть переведён в режим тем!")
        current = self.plugin.store["group"]
        if current and current != chat.id:
            self.reply(message, "Ты уверен, что хочешь изменить группу? ⚠️ Все связи «тема Telegram ↔ чат FunPay» "
                                "будут сброшены.", [[("✅ Да", ("set_group", chat.id)), ("❌ Нет", ("del_msg",))]])
            return
        self.set_group(chat.id, info)
        self.reply(message, "✅ Группа для синхронизации FunPay чатов установлена!")

    def set_group(self, group_id, info=None):
        info = info or self.bot.get_chat(group_id)
        invite = getattr(info, "invite_link", None) or ""
        self.plugin.store.change(lambda d: d.update(group=group_id, group_title=getattr(info, "title", "") or "",
                                                    group_username=getattr(info, "username", None) or "",
                                                    group_invite=invite, topics={}, flags={}))
        self.plugin.start()

    def cmd_delete_sync_chat(self, message):
        if not self.plugin.store["group"]:
            raise UserError("❌ Группа итак не привязана!")
        self.reply(message, "Отвязать группу? Плагин перестанет работать, все связи «тема ↔ чат» сбросятся.",
                   [[("✅ Да", ("unset_group",)), ("❌ Нет", ("del_msg",))]])

    def cmd_sync_chats(self, message):
        p = self.plugin
        if len(p.store["bots"]) < MIN_BOTS:
            raise UserError("❌ Сначала добавьте Telegram-ботов в настройках плагина Chat Sync!")
        if not p.store["group"]:
            raise UserError("❌ Сначала установите группу для синхронизации командой /setup_sync_chat!")
        if p.sync_running:
            raise UserError("❌ Синхронизация уже запущена!")
        p.sync_running = True
        self.reply(message, "🔄 Синхронизация запущена... Это может занять пару минут.")

        def run():
            try:
                p.sync_all()
            except Exception:
                LOG.exception("%s: ошибка синхронизации", NAME)
            finally:
                p.sync_running = False

        if p.sync:
            run()
        else:
            threading.Thread(target=run, name="ChatSyncAll", daemon=True).start()

    def topic_chat(self, message):
        p = self.plugin
        if message.chat.id != p.store["group"] or not getattr(message, "is_topic_message", False):
            raise UserError("❌ Команду нужно вводить в теме покупателя!")
        chat = p.chat_by_thread(message.message_thread_id)
        if chat is None:
            raise UserError("❌ Команду нужно вводить в теме покупателя!")
        return chat

    def cmd_watch(self, message):
        chat = self.topic_chat(message)
        account = self.cardinal.account
        buyer_id = None
        try:
            full = account.get_chat(int(chat), with_history=True)
            link, text = getattr(full, "looking_link", None), getattr(full, "looking_text", None)
            if link:
                self.reply(message, f"👁 Смотрит: <a href=\"{esc(link)}\">{esc(text or link)}</a>")
                return
            buyer_id = next((m.author_id for m in getattr(full, "messages", []) or []
                             if m.author_id not in (0, account.id)), None)
        except Exception:
            pass
        if buyer_id and hasattr(account, "get_buyer_viewing"):
            try:
                viewing = account.get_buyer_viewing(buyer_id)
                if getattr(viewing, "link", None):
                    self.reply(message, f"👁 Смотрит: <a href=\"{esc(viewing.link)}\">{esc(viewing.text or viewing.link)}</a>")
                    return
            except Exception:
                pass
        self.reply(message, "👁 Покупатель сейчас ничего не смотрит.")

    def cmd_stats(self, message):
        chat = self.topic_chat(message)
        name = self.plugin.store["names"].get(chat)
        if not name:
            raise UserError("Не знаю ник покупателя этого чата.")
        self.plugin.stats(name, force=True)
        self.reply(message, self.plugin.stats_text(name))

    def cmd_history(self, message):
        p = self.plugin
        chat = self.topic_chat(message)
        name = p.store["names"].get(chat) or chat
        try:
            history = p.cardinal.account.get_chat_history(int(chat), interlocutor_username=name)
        except TypeError:
            history = p.cardinal.account.get_chat_history(int(chat))
        except Exception:
            raise UserError("❌ Не удалось загрузить историю переписки.") from None
        history = list(history or [])[-25:]
        if not history:
            self.reply(message, "📜 История пуста.")
            return
        parts, last_key = [], None
        for msg in history:
            line, key, kind = p.author_line(msg, chat, name)
            body = p.body(msg)
            if key == last_key:
                parts[-1] += "\n" + body
            else:
                parts.append((line + " " if key in ("fp", "fp_ad") else line + "\n") + body)
            last_key = key
        for part in chunks("\n\n".join(parts), 3800):
            self.reply(message, part)

    # --- подтверждения в группе
    def on_set_group(self, chat, mid, group_id):
        self.set_group(group_id)
        self.send(chat, "✅ Группа для синхронизации FunPay чатов установлена! Выполните /sync_chats.", mid=mid)

    def on_unset_group(self, chat, mid):
        self.plugin.store.change(lambda d: d.update(group=None, topics={}, flags={}, group_username="",
                                                    group_invite="", group_title=""))
        self.send(chat, "✅ Группа синхронизации отвязана.", mid=mid)

    def on_del_msg(self, chat, mid):
        try:
            self.bot.delete_message(chat, mid)
        except Exception:
            pass

    def allowed(self, user, chat=None) -> bool:
        if user is None or int(getattr(user, "id", 0) or 0) not in self.admins():
            return False
        if chat is None or getattr(chat, "type", "private") == "private":
            return True
        return getattr(chat, "id", None) == self.plugin.store["group"] or getattr(chat, "type", "") in (
            "group", "supergroup")

    # --- сообщения из тем
    def is_topic_input(self, message) -> bool:
        p = self.plugin
        if not p.ready() or message.chat.id != p.store["group"]:
            return False
        if not getattr(message, "is_topic_message", False) or not self.group_user_ok(message):
            return False
        if getattr(getattr(message, "from_user", None), "is_bot", False):
            return False
        text = getattr(message, "text", None) or ""
        if text.startswith("/"):
            return False
        return p.chat_by_thread(message.message_thread_id) is not None

    def on_topic_message(self, message):
        p = self.plugin
        chat = p.chat_by_thread(message.message_thread_id)
        if chat is None:
            return
        ok = True
        ctype = getattr(message, "content_type", "text")
        try:
            if ctype == "text":
                ok = p.send_to_funpay(chat, p.format_outgoing(chat, p.full_template(message.text)))
            elif ctype == "photo":
                if getattr(message, "caption", None):
                    ok = p.send_to_funpay(chat, p.format_outgoing(chat, message.caption))
                photo = message.photo[-1]
                if (getattr(photo, "file_size", 0) or 0) > 20 * 1024 * 1024:
                    raise UserError("Фото больше 20 МБ.")
                ok = p.send_to_funpay(chat, image=self.download(photo.file_id)) and ok
            elif ctype == "document":
                doc = message.document
                if not str(getattr(doc, "mime_type", "")).startswith("image/"):
                    raise UserError("Поддерживаются только изображения, текст и обычные стикеры.")
                if (getattr(doc, "file_size", 0) or 0) > 20 * 1024 * 1024:
                    raise UserError("Файл больше 20 МБ.")
                if getattr(message, "caption", None):
                    ok = p.send_to_funpay(chat, p.format_outgoing(chat, message.caption))
                ok = p.send_to_funpay(chat, image=self.download(doc.file_id)) and ok
            elif ctype == "sticker":
                sticker = message.sticker
                if getattr(sticker, "is_animated", False) or getattr(sticker, "is_video", False):
                    raise UserError("Анимированные и видеостикеры не отправляются на FunPay.")
                ok = p.send_to_funpay(chat, image=self.to_png(self.download(sticker.file_id)))
            else:
                raise UserError("Этот тип сообщений не поддерживается: только текст, фото и обычные стикеры.")
        except UserError as error:
            self.reply(message, "❌ " + esc(error))
            return
        except Exception:
            LOG.exception("%s: ошибка пересылки на FunPay", NAME)
            ok = False
        if not ok:
            self.reply(message, "❌ Не удалось отправить сообщение на FunPay. Попробуйте ещё раз или отправьте с сайта.")

    def download(self, file_id) -> bytes:
        info = self.bot.get_file(file_id)
        return self.bot.download_file(info.file_path)

    @staticmethod
    def to_png(data: bytes) -> bytes:
        try:
            from PIL import Image
            image = Image.open(io.BytesIO(data)).convert("RGBA")
            out = io.BytesIO()
            image.save(out, format="PNG")
            return out.getvalue()
        except Exception:
            return data

    def register(self):
        tg = self.tg
        tg.msg_handler(self.group_command, commands=[c for c, _ in self.COMMANDS])
        tg.msg_handler(self.on_topic_message, func=self.is_topic_input,
                       content_types=["text", "photo", "sticker", "document"])
        tg.msg_handler(self.on_message_input, func=self.has_input, content_types=["text"])
        tg.cbq_handler(self.on_callback, lambda call: bool(getattr(call, "data", None)) and call.data.startswith(PREFIX))

        def open_settings(call):
            if call.message and self.allowed(call.from_user, call.message.chat):
                self.answer(call)
                self.on_home(call.message.chat.id, call.message.message_id)

        try:
            from tg_bot import CBT
            key = f"{CBT.PLUGIN_SETTINGS}:{UUID}"
            tg.cbq_handler(open_settings, lambda call: bool(getattr(call, "data", None)) and (
                    call.data == key or call.data.startswith(key + ":")))
        except Exception:
            pass
        try:
            self.cardinal.add_telegram_commands(UUID, [(c, d, True) for c, d in self.COMMANDS])
        except Exception:
            pass


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = SyncCore(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = SyncPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


def start(cardinal, *args):
    if PLUGIN is not None and PLUGIN.ui is not None:
        PLUGIN.start()


def stop(cardinal=None, *args):
    if PLUGIN is not None:
        PLUGIN.stop()


def new_message(cardinal, event, *args):
    if PLUGIN is None or PLUGIN.ui is None:
        return
    try:
        PLUGIN.on_new_message(event.message)
    except Exception:
        LOG.exception("%s: ошибка обработки сообщения", NAME)


def new_order(cardinal, event, *args):
    if PLUGIN is None or PLUGIN.ui is None:
        return
    try:
        PLUGIN.on_new_order(event.order)
    except Exception:
        LOG.exception("%s: ошибка обработки заказа", NAME)


def initial_chat(cardinal, event, *args):
    if PLUGIN is None or PLUGIN.ui is None:
        return
    try:
        PLUGIN.on_initial_chat(event.chat)
    except Exception:
        LOG.exception("%s: ошибка обработки чата", NAME)


BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [start]
BIND_TO_PRE_STOP = [stop]
BIND_TO_NEW_MESSAGE = [new_message]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_INIT_MESSAGE = [initial_chat]
BIND_TO_DELETE = None
