"""
📝 Notes on Buyers — личная CRM для FunPay Cardinal от LuckyFunpay.

Скрытые заметки о любых пользователях FunPay и мгновенные уведомления в Telegram, когда
пользователь с заметкой пишет вам или оформляет заказ.

ИНСТРУКЦИЯ (полная версия — в боте: /notes → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. /note Ник Текст — добавить или заменить заметку.  /note Ник — посмотреть заметку.
3. /notes — список всех заметок с поиском, редактированием и удалением.
4. Когда пользователь с заметкой напишет или купит — придёт уведомление (антиспам: 5 минут).

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

NAME = "Notes on Buyers"
VERSION = "1.0.0"
DESCRIPTION = ("📝 Личная CRM: заметки о покупателях FunPay и уведомления, когда такой покупатель пишет или "
               "покупает. Команды: /note, /notes")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "f4a19c3e-7b2d-4e8f-a051-3c6d9e2b7a48"
SETTINGS_PAGE = True
SLUG = "BuyerNotes"
PREFIX = "lfnote:"

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


DEFAULTS = {"notes": {}, "on_message": True, "on_order": True, "cooldown": 300}
NICK_RE = re.compile(r"[A-Za-z0-9_\-.А-Яа-яЁё]{2,40}")


def nick_key(nick: str) -> str:
    return str(nick).strip().lstrip("@").casefold()


def check_nick(nick: str) -> str:
    nick = str(nick).strip().lstrip("@")
    if not NICK_RE.fullmatch(nick):
        raise UserError("Ник: 2–40 символов (буквы, цифры, _ - .), без пробелов.")
    return nick


class BuyerNotes:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("notes.json", DEFAULTS)
        self.ui = None
        self.last_alert = {}
        self.lock = threading.Lock()

    def get(self, nick):
        return self.store["notes"].get(nick_key(nick))

    def put(self, nick, text):
        nick = check_nick(nick)
        text = str(text).strip()
        if not text:
            raise UserError("Текст заметки пустой.")
        if len(text) > 1000:
            raise UserError("Заметка слишком длинная: максимум 1000 символов.")

        def save(d):
            old = d["notes"].get(nick_key(nick))
            d["notes"][nick_key(nick)] = {"name": nick, "text": text, "created": (old or {}).get("created", time.time()),
                                          "updated": time.time()}
            return old is not None

        return self.store.change(save)

    def delete(self, nick):
        return self.store.change(lambda d: d["notes"].pop(nick_key(nick), None))

    def should_alert(self, nick, force=False) -> bool:
        key = nick_key(nick)
        with self.lock:
            last = self.last_alert.get(key, 0)
            if not force and time.time() - last < self.store["cooldown"]:
                return False
            self.last_alert[key] = time.time()
            return True

    def on_message(self, author, author_id, text, chat_id):
        if not author or not self.store["on_message"] or not self.ui:
            return
        account = self.cardinal.account
        if author_id in (0, None) or author_id == getattr(account, "id", None) or author == getattr(account, "username", None):
            return
        note = self.get(author)
        if not note or not self.should_alert(author):
            return
        preview = (text or "").strip()[:300] or "[изображение]"
        self.ui.notify(f"🔔 <b>Сообщение от пользователя с заметкой</b>\n👤 <b>{esc(note['name'])}</b>\n"
                       f"💬 {esc(preview)}\n\n📝 Заметка: <i>{esc(note['text'])}</i>",
                       [[("💬 Открыть чат", f"https://funpay.com/chat/?node={chat_id}")],
                        [("📝 Заметка", ("note", nick_key(author)))]])

    def on_order(self, order):
        if not self.store["on_order"] or not self.ui:
            return
        buyer = getattr(order, "buyer_username", None)
        note = self.get(buyer) if buyer else None
        if not note:
            return
        self.should_alert(buyer, force=True)
        price = f"{getattr(order, 'price', '')} {getattr(order, 'currency', '')}"
        self.ui.notify(f"🔔 <b>Внимание! Заказ от пользователя с заметкой</b>\n👤 <b>{esc(note['name'])}</b>\n"
                       f"🧾 Заказ <code>#{esc(order.id)}</code> · {esc(price)}\n📦 {esc(getattr(order, 'description', ''))}\n\n"
                       f"📝 Заметка: <i>«{esc(note['text'])}»</i>",
                       [[("🧾 Заказ", f"https://funpay.com/orders/{order.id}/"),
                         ("💬 Чат", f"https://funpay.com/chat/?node={getattr(order, 'chat_id', '')}")],
                        [("📝 Заметка", ("note", nick_key(buyer)))]])


class NotesPanel(Panel):
    COMMAND = "notes"
    COMMANDS = [("notes", "📝 Заметки о покупателях"), ("note", "📝 Заметка: /note Ник Текст")]
    ICON = "📝"
    PAGE = 10
    GUIDE = [
        ("✨ Возможности", "Оставляйте скрытые комментарии к любому пользователю FunPay:\n"
         "🚀 <b>Мгновенные уведомления</b> — пользователь с заметкой написал или оформил заказ → бот сразу пишет:\n"
         "<i>«🔔 Внимание! Заказ от пользователя с заметкой: „Постоянный клиент, просил скидку 5%“»</i>\n"
         "🛡 <b>Защита от скамеров</b> — «Требует возврат по пустякам», «Пытался обмануть с оплатой».\n"
         "💎 <b>VIP</b> — «Берёт оптом, выдавать бонус».\n📱 Управление командами и кнопками."),
        ("🚀 Включение", "1. Положите <code>BuyerNotes_LuckyFunpay.py</code> в <code>plugins</code> или отправьте боту.\n"
         "2. /menu → Система и конфиги → Плагины → <b>Notes on Buyers</b> → включить, затем /restart.\n"
         "3. Меню заметок: /notes или кнопка настроек плагина в списке плагинов."),
        ("🎮 Команды", "🔹 <code>/note Ник Текст</code> — добавить (или заменить) заметку.\n"
         "Пример: <code>/note Username Любит поболтать, отвечать вежливо</code>\n"
         "🔹 <code>/note Ник</code> — проверить, есть ли заметка на человека.\n"
         "🔹 <code>/notes</code> — интерактивный список всех заметок: поиск, изменение, удаление.\n\n"
         "Ник пишется как на FunPay, регистр не важен."),
        ("🛠 Как это работает", "1. Вы добавляете заметку.\n2. Пользователь пишет вам или покупает товар.\n"
         "3. Бот находит совпадение и присылает уведомление с текстом заметки и кнопкой чата.\n\n"
         "Встроен <b>антиспам</b>: по сообщениям одного пользователя уведомление не чаще раза в 5 минут "
         "(меняется в настройках). Уведомление о заказе приходит всегда.\n"
         "Переключатели «💬 Сообщения» и «🛒 Заказы» выключают нужный тип уведомлений."),
    ]

    def on_command(self, message):
        text = (message.text or "").strip()
        cmd = text.split()[0][1:].split("@")[0] if text.startswith("/") else ""
        chat = message.chat.id
        if cmd != "note":
            self.on_home(chat)
            return
        parts = text.split(maxsplit=2)
        if len(parts) == 1:
            self.send(chat, "📝 Использование:\n<code>/note Ник Текст</code> — добавить заметку\n"
                            "<code>/note Ник</code> — посмотреть заметку\n<code>/notes</code> — все заметки",
                      [[("📋 Все заметки", ("home",))]])
            return
        nick = check_nick(parts[1])
        if len(parts) == 2:
            note = self.plugin.get(nick)
            if not note:
                self.send(chat, f"ℹ️ Заметки на <b>{esc(nick)}</b> нет.",
                          [[("➕ Добавить", ("add_text", nick)), ("📋 Все заметки", ("home",))]])
            else:
                self.on_note(chat, None, nick_key(nick))
            return
        replaced = self.plugin.put(nick, parts[2])
        self.send(chat, f"✅ Заметка {'обновлена' if replaced else 'добавлена'}: <b>{esc(nick)}</b>\n"
                        f"<i>{esc(parts[2].strip())}</i>", [[("📋 Все заметки", ("home",))]])

    def on_home(self, chat, mid=None, page=0, query=""):
        data = self.plugin.store.snapshot()
        notes = sorted(data["notes"].items(), key=lambda kv: kv[1]["name"].casefold())
        if query:
            q = query.casefold()
            notes = [kv for kv in notes if q in kv[0] or q in kv[1]["text"].casefold()]
        pages = max(1, (len(notes) + self.PAGE - 1) // self.PAGE)
        page = max(0, min(int(page), pages - 1))
        lines = [f"📝 <b>{NAME}</b> v{VERSION}", "",
                 f"Заметок: <b>{len(data['notes'])}</b>" + (f" · поиск «{esc(query)}»: {len(notes)}" if query else ""),
                 f"Уведомления: 💬 сообщения {'🟢' if data['on_message'] else '🔴'} · 🛒 заказы "
                 f"{'🟢' if data['on_order'] else '🔴'} · антиспам {data['cooldown'] // 60} мин", ""]
        if not notes:
            lines.append("Заметок пока нет. Добавьте кнопкой ниже или командой <code>/note Ник Текст</code>.")
        rows = [[(f"👤 {v['name']} — {v['text'][:30]}", ("note", k))] for k, v in
                notes[page * self.PAGE:(page + 1) * self.PAGE]]
        if pages > 1:
            rows.append([("⬅️", ("home", max(0, page - 1), query)), (f"{page + 1}/{pages}", ("noop",)),
                         ("➡️", ("home", min(pages - 1, page + 1), query))])
        rows.append([("➕ Добавить", ("add",)), ("🔍 Поиск", ("search",))])
        if query:
            rows.append([("✖️ Сбросить поиск", ("home",))])
        rows.append([("💬 Сообщения " + ("🟢" if data["on_message"] else "🔴"), ("toggle", "on_message")),
                     ("🛒 Заказы " + ("🟢" if data["on_order"] else "🔴"), ("toggle", "on_order"))])
        rows.append([("⏱ Антиспам", ("cooldown",))])
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_note(self, chat, mid, key):
        note = self.plugin.store["notes"].get(key)
        if not note:
            raise UserError("Заметка не найдена.")
        self.send(chat, f"👤 <b>{esc(note['name'])}</b>\n\n📝 {esc(note['text'])}\n\n"
                        f"<i>Создана {fmt_dt(note.get('created'))}, изменена {fmt_dt(note.get('updated'))}</i>",
                  [[("✏️ Изменить", ("add_text", note["name"])), ("🗑 Удалить", ("del", key))],
                   [("📋 Все заметки", ("home",))]], mid)

    def on_add(self, chat, mid):
        self.ask(chat, "👤 Отправьте ник пользователя FunPay.", ("nick",), mid)

    def in_nick(self, chat, text):
        nick = check_nick(text)
        self.on_add_text(chat, None, nick)

    def on_add_text(self, chat, mid, nick):
        note = self.plugin.get(nick)
        current = f"\n\nСейчас: <i>{esc(note['text'])}</i>" if note else ""
        self.ask(chat, f"📝 Отправьте текст заметки для <b>{esc(nick)}</b> (до 1000 символов).{current}",
                 ("text", nick), mid)

    def in_text(self, chat, text, nick):
        self.plugin.put(nick, text)
        self.on_note(chat, None, nick_key(nick))

    def on_del(self, chat, mid, key):
        note = self.plugin.store["notes"].get(key)
        if not note:
            raise UserError("Заметка не найдена.")
        self.send(chat, f"Удалить заметку о <b>{esc(note['name'])}</b>?",
                  [[("🗑 Да", ("del_ok", key)), ("↩️ Нет", ("note", key))]], mid)

    def on_del_ok(self, chat, mid, key):
        self.plugin.delete(key)
        self.on_home(chat, mid)

    def on_search(self, chat, mid):
        self.ask(chat, "🔍 Отправьте часть ника или текста заметки.", ("search",), mid)

    def in_search(self, chat, text):
        self.on_home(chat, None, 0, text.strip()[:40])

    def on_toggle(self, chat, mid, key):
        if key not in ("on_message", "on_order"):
            raise UserError("Неизвестная настройка.")
        self.plugin.store.change(lambda d: d.update({key: not d[key]}))
        self.on_home(chat, mid)

    def on_cooldown(self, chat, mid):
        self.ask(chat, "⏱ Антиспам: не чаще одного уведомления о сообщениях пользователя раз в N минут (0–1440).",
                 ("cooldown",), mid)

    def in_cooldown(self, chat, text):
        self.plugin.store.set(cooldown=parse_int(text, 0, 1440, "Минуты") * 60)
        self.on_home(chat)


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = BuyerNotes(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = NotesPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


def new_message(cardinal, event, *args):
    if PLUGIN is None:
        return
    msg = getattr(event, "message", None)
    if msg is None or getattr(msg, "by_bot", False):
        return
    try:
        PLUGIN.on_message(getattr(msg, "author", None), getattr(msg, "author_id", None), getattr(msg, "text", ""),
                          getattr(msg, "chat_id", ""))
    except Exception:
        LOG.exception("%s: ошибка обработки сообщения", NAME)


def last_chat_message(cardinal, event, *args):
    """Старый режим получения сообщений Cardinal."""
    if PLUGIN is None or not getattr(cardinal, "old_mode_enabled", False):
        return
    chat = getattr(event, "chat", None)
    if chat is None or getattr(chat, "unread", True) is False:
        return
    text = getattr(chat, "last_message_text", "") or ""
    if getattr(cardinal.account, "username", None) and text.startswith(cardinal.account.username + ":"):
        return
    try:
        PLUGIN.on_message(getattr(chat, "name", None), -1, text, getattr(chat, "id", ""))
    except Exception:
        LOG.exception("%s: ошибка обработки чата", NAME)


def new_order(cardinal, event, *args):
    if PLUGIN is not None:
        try:
            PLUGIN.on_order(event.order)
        except Exception:
            LOG.exception("%s: ошибка обработки заказа", NAME)


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_MESSAGE = [new_message]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [last_chat_message]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_DELETE = None
