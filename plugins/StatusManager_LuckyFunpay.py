"""
🚦 Status Manager — контроль вашей активности. Плагин для FunPay Cardinal от LuckyFunpay.

Ставит текущий статус магазина («Сплю», «На работе», «Обед»…) в один клик, показывает, как давно он
установлен и когда вы в последний раз писали в чатах FunPay. Быстрые команды /s1, /s2… и #status.

ИНСТРУКЦИЯ (полная версия — в боте: /status → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. /status — панель: «➕ Добавить» свои статусы, нажмите на статус, чтобы его применить.
3. /s1, /s2, … — мгновенно установить статус по номеру в списке; /status_clear — снять статус.
4. Напишите #status в любом чате FunPay от своего аккаунта — бот ответит текущим статусом.

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

NAME = "Status Manager"
VERSION = "1.0.0"
DESCRIPTION = ("🚦 Статус магазина в один клик («Сплю», «На работе»…), таймеры с момента установки и с последнего "
               "вашего сообщения на FunPay, шорткаты /s1 /s2 и проверка #status из чата. Панель: /status")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "3c7d0e95-b16a-4f82-9e4b-d1a6c8f2e570"
SETTINGS_PAGE = True
SLUG = "StatusManager"
PREFIX = "lfstat:"

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


DEFAULTS = {"presets": ["💤 Сплю", "🍔 Обед", "💼 На работе", "🎮 Рейд, отвечу позже"], "current": "", "since": 0,
            "last_activity": 0, "history": [], "chat_reply": True}
SHORTCUT_RE = re.compile(r"/s(\d{1,2})(?:@\w+)?")


class StatusManager:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("settings.json", DEFAULTS)
        self.ui = None

    def set_status(self, text):
        text = str(text or "").strip()

        def save(d):
            if d["current"]:
                d["history"] = ([{"text": d["current"], "from": d["since"], "to": time.time()}] + d["history"])[:15]
            d["current"] = text
            d["since"] = time.time() if text else 0

        self.store.change(save)

    def status_line(self) -> str:
        d = self.store.snapshot()
        if not d["current"]:
            return "🟢 Онлайн (статус не установлен)"
        return f"{d['current']} (установлен {ago(d['since'])} назад)"

    def on_my_message(self, msg):
        text = (getattr(msg, "text", "") or "").strip()
        if text.lower() == "#status":
            if self.store["chat_reply"]:
                d = self.store.snapshot()
                reply = ("🤖 Бот работает. Текущий статус: " + (f"{d['current']} (установлен {ago(d['since'])} назад)"
                                                                if d["current"] else "онлайн, статус не установлен"))
                try:
                    self.cardinal.send_message(msg.chat_id, reply, getattr(msg, "chat_name", None))
                except Exception:
                    LOG.warning("%s: не удалось ответить на #status", NAME)
            return
        self.store.set(last_activity=time.time())


class StatusPanel(Panel):
    COMMAND = "status"
    COMMANDS = [("status", "🚦 Статус магазина"), ("status_clear", "🚦 Снять статус (онлайн)")]
    ICON = "🚦"
    GUIDE = [
        ("🚀 Для чего это нужно", "Вы — живой человек и не можете быть онлайн 24/7. Модуль помогает:\n"
         "1. <b>Прозрачность</b> — видно, какой статус установлен и как давно.\n2. <b>Дисциплина</b> — таймер "
         "«Последняя активность» покажет, когда вы (или менеджер) в последний раз писали в чатах FunPay.\n"
         "3. <b>Скорость</b> — переключение режимов в один клик.\n4. <b>Команда</b> — коллеги видят, заняты вы или нет."),
        ("⚙️ Возможности", "📝 <b>Свои пресеты</b> — «Ушёл в магазин», «Рейд в WoW», «Сплю (буду в 10:00)» — что угодно.\n"
         "⚡ <b>Шорткаты</b> — /s1, /s2, /s3… ставят статус по номеру в списке без меню.\n⏱ <b>Таймеры</b> — время с "
         "установки статуса и с вашего последнего сообщения на FunPay (сообщения, отправленные ботом, не считаются).\n"
         "🕵️‍♂️ <b>#status</b> — проверка из чата FunPay."),
        ("📖 Как использовать", "1. Включите плагин в списке плагинов Cardinal и сделайте /restart.\n2. "
         "<code>/status</code> — интерактивная панель.\n3. «➕ Добавить» — новый статус; «🗑 Удаление» — убрать лишние; "
         "нажмите на статус, чтобы применить.\n4. Для профи: <code>/s1</code> — первый статус из списка, "
         "<code>/s2</code> — второй и т.д., <code>/status_clear</code> — снять статус (режим «Онлайн»).\n\n"
         "В панели видно:\nТекущий статус: 💤 Сплю\nУстановлен: 4ч 15м назад\nПоследняя активность: 8ч 20м назад"),
        ("💡 Фишки и сценарий", "Напишите <code>#status</code> в любом чате FunPay от своего аккаунта — бот ответит в "
         "тот же чат текущим статусом. Удобно проверить, работает ли бот, не заходя в Telegram (отключается "
         "переключателем «💬 #status»).\n\nСценарий: ложитесь спать → <code>/s1</code> («💤 Сплю») → бот фиксирует время "
         "→ утром <code>/status_clear</code> → обычный режим."),
    ]

    def on_command(self, message):
        cmd = (message.text or "").split()[0][1:].split("@")[0]
        chat = message.chat.id
        if cmd == "status_clear":
            self.plugin.set_status("")
            self.send(chat, "🟢 Статус снят — вы снова онлайн.", [[("🚦 Панель", ("home",))]])
            return
        self.on_home(chat)

    def shortcut(self, message):
        if not self.allowed(message.from_user, message.chat):
            return
        match = SHORTCUT_RE.fullmatch((message.text or "").strip())
        presets = self.plugin.store["presets"]
        index = int(match.group(1)) - 1
        if not 0 <= index < len(presets):
            self.send(message.chat.id, f"⚠️ Статуса №{index + 1} нет. В списке {len(presets)} статус(ов).",
                      [[("🚦 Панель", ("home",))]])
            return
        self.plugin.set_status(presets[index])
        self.send(message.chat.id, f"✅ Статус установлен: <b>{esc(presets[index])}</b>", [[("🚦 Панель", ("home",))]])

    def register(self):
        super().register()
        self.tg.msg_handler(self.shortcut, func=lambda m: bool(getattr(m, "text", None)) and
                            SHORTCUT_RE.fullmatch(m.text.strip()) is not None)

    def on_home(self, chat, mid=None, *args):
        d = self.plugin.store.snapshot()
        lines = [f"🚦 <b>{NAME}</b> v{VERSION}", "",
                 f"Текущий статус: <b>{esc(d['current']) if d['current'] else '🟢 Онлайн'}</b>",
                 f"Установлен: {ago(d['since']) + ' назад' if d['since'] else '—'}",
                 f"Последняя активность: {ago(d['last_activity']) + ' назад' if d['last_activity'] else 'нет данных'}",
                 "", "Шорткаты: " + (", ".join(f"/s{i + 1} — {esc(p)}" for i, p in enumerate(d["presets"][:10]))
                                     or "добавьте статусы")]
        rows = [[(("✅ " if p == d["current"] else "") + f"{i + 1}. {p}", ("apply", i))] for i, p in
                enumerate(d["presets"][:20])]
        rows.append([("➕ Добавить", ("add",)), ("🗑 Удаление", ("delete_menu",))])
        rows.append([("✖️ Снять статус", ("clear",)), ("💬 #status: " + ("🟢" if d["chat_reply"] else "🔴"),
                                                        ("toggle_reply",))])
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_apply(self, chat, mid, index):
        presets = self.plugin.store["presets"]
        if not 0 <= index < len(presets):
            raise UserError("Статус уже удалён.")
        self.plugin.set_status(presets[index])
        self.on_home(chat, mid)

    def on_clear(self, chat, mid):
        self.plugin.set_status("")
        self.on_home(chat, mid)

    def on_toggle_reply(self, chat, mid):
        self.plugin.store.change(lambda d: d.update(chat_reply=not d["chat_reply"]))
        self.on_home(chat, mid)

    def on_add(self, chat, mid):
        self.ask(chat, "➕ Отправьте текст нового статуса (до 60 символов), например <code>😴 Сплю (буду в 10:00)</code>.",
                 ("add",), mid)

    def in_add(self, chat, text):
        text = text.strip()
        if not 1 <= len(text) <= 60:
            raise UserError("Статус: от 1 до 60 символов.")
        if len(self.plugin.store["presets"]) >= 20:
            raise UserError("Максимум 20 статусов — удалите лишние.")
        self.plugin.store.change(lambda d: d["presets"].append(text))
        self.on_home(chat)

    def on_delete_menu(self, chat, mid):
        presets = self.plugin.store["presets"]
        rows = [[(f"🗑 {i + 1}. {p}", ("delete", i, p))] for i, p in enumerate(presets)]
        rows.append([("◀️ Готово", ("home",))])
        self.send(chat, "🗑 Нажмите на статус, чтобы удалить его. Номера шорткатов сдвинутся.", rows, mid)

    def on_delete(self, chat, mid, index, text):
        def remove(d):
            if 0 <= index < len(d["presets"]) and d["presets"][index] == text:
                d["presets"].pop(index)

        self.plugin.store.change(remove)
        self.on_delete_menu(chat, mid)


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = StatusManager(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = StatusPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


def new_message(cardinal, event, *args):
    if PLUGIN is None:
        return
    msg = getattr(event, "message", None)
    if msg is None or getattr(msg, "by_bot", False):
        return
    if getattr(msg, "author_id", None) != getattr(cardinal.account, "id", None):
        return
    try:
        PLUGIN.on_my_message(msg)
    except Exception:
        LOG.exception("%s: ошибка обработки сообщения", NAME)


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_MESSAGE = [new_message]
BIND_TO_DELETE = None
