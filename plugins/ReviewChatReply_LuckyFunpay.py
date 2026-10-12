"""
💬 Review Chat Reply — автоответ и подарки за отзывы в чат FunPay. Плагин для FunPay Cardinal от LuckyFunpay.

Сразу после того, как покупатель оставит, изменит или удалит отзыв, бот пишет ему в личный чат FunPay
текст для этой оценки и (по желанию) выдаёт подарок — строку из .txt файла, как автовыдача.

ИНСТРУКЦИЯ (полная версия — в боте: /review_reply → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. /review_reply → «Плагин: 🔴» переключите в 🟢.
3. Выберите оценку (⭐…⭐⭐⭐⭐⭐ или 🗑 Удалённый) → «✏️ Изменить текст» → «Статус: 🟢».
4. Подарки: «🎁 Подарки за отзывы» → «Подарки: 🟢» → «📂 Файл» (из storage/products), текст с $gift.
Переменные: $gift, $username, $order_id, $order_title, $order_params, $game, $category, $date,
$full_date_text, $time, $full_time.

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

NAME = "Review Chat Reply"
VERSION = "1.0.0"
DESCRIPTION = ("💬 Сообщение покупателю в чат FunPay после отзыва: свой текст для каждой оценки и для удалённого "
               "отзыва, подарки за отзывы из .txt файлов (как автовыдача), файловый менеджер. Панель: /review_reply")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "6f2c9e14-a837-4d5b-b0f6-8c3d1e7a4b92"
SETTINGS_PAGE = True
SLUG = "ReviewChatReply"
PREFIX = "lfrcr:"

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


PRODUCTS = Path("storage") / "products"
KINDS = ["1", "2", "3", "4", "5", "deleted"]
KIND_TITLES = {"1": "⭐", "2": "⭐⭐", "3": "⭐⭐⭐", "4": "⭐⭐⭐⭐", "5": "⭐⭐⭐⭐⭐", "deleted": "🗑 (Удалённый)"}
DEFAULT_TEXTS = {
    "1": "Здравствуйте, $username. Увидел вашу оценку и очень расстроился. Напишите, пожалуйста, что пошло не так? "
         "Я готов решить проблему!",
    "2": "Здравствуйте, $username. Вижу, вы поставили низкую оценку. Подскажите, что пошло не так? Я готов всё "
         "исправить или заменить товар!",
    "3": "Здравствуйте, $username! Спасибо за отзыв. Подскажите, что можно улучшить? Буду рад исправиться!",
    "4": "Спасибо за отзыв, $username! Рад, что всё получилось. Буду рад видеть вас снова!",
    "5": "Спасибо за покупку, $username! Рад был помочь. Буду жутко рад видеть вас снова!",
    "deleted": "Здравствуйте, $username! Вижу, вы удалили отзыв к заказу #$order_id. Подскажите, у вас возникли "
               "какие-то трудности?",
}
VARIABLES = ("$gift", "$username", "$order_id", "$order_title", "$order_params", "$game", "$category", "$date",
             "$full_date_text", "$time", "$full_time")
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]
DEFAULTS = {
    "enabled": False, "watermark": True,
    "stars": {k: {"enabled": False, "text": DEFAULT_TEXTS[k], "file": "", "gift": k in ("4", "5")} for k in KINDS},
    "gifts": {"enabled": False, "file": "", "text": "🎁 Спасибо за отзыв! Ваш подарок: $gift", "separate": True,
              "cycle": False, "threshold": 5},
    "processed": {}, "low_warned": {}, "log": [],
}
FILE_RE = re.compile(r"[\w\-. ()а-яА-ЯёЁ]{1,60}\.txt")
ORDER_RE = re.compile(r"#([A-Z0-9]{8})")


def products_dir() -> Path:
    PRODUCTS.mkdir(parents=True, exist_ok=True)
    return PRODUCTS


def safe_file(name: str) -> Path:
    name = str(name).strip()
    if not name.lower().endswith(".txt"):
        name += ".txt"
    if not FILE_RE.fullmatch(name) or name.startswith("."):
        raise UserError("Имя файла: буквы, цифры, пробел, _ - . ( ), до 60 символов, расширение .txt.")
    return products_dir() / name


def read_lines(path: Path) -> list:
    if not path.exists():
        return []
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251", errors="replace")
    return [line.rstrip("\r") for line in text.split("\n") if line.strip()]


def write_lines(path: Path, lines: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    os.replace(tmp, path)


def order_vars(order) -> dict:
    now = datetime.now()
    sub = getattr(order, "subcategory", None)
    game = getattr(getattr(sub, "category", None), "name", "") or ""
    category = getattr(sub, "name", "") or ""
    title = getattr(order, "short_description", None) or getattr(order, "title", None) or \
        getattr(order, "description", None) or ""
    params = ""
    try:
        params = order.lot_params_text or ""
    except Exception:
        params = ""
    return {"$username": str(getattr(order, "buyer_username", "") or ""), "$order_id": str(order.id),
            "$order_title": str(title), "$order_params": str(params), "$game": game, "$category": category,
            "$date": now.strftime("%d.%m.%Y"), "$full_date_text": f"{now.day} {MONTHS[now.month - 1]} {now.year} года",
            "$time": now.strftime("%H:%M"), "$full_time": now.strftime("%H:%M:%S")}


def render(template, values: dict) -> str:
    for key in sorted(values, key=len, reverse=True):
        template = template.replace(key, values[key])
    return template


class ReviewReply:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("settings.json", DEFAULTS)
        self.ui = None
        self.file_lock = threading.Lock()
        self.lock = threading.Lock()
        self.inflight = set()
        self.sync = False

    def log(self, text):
        self.store.change(lambda d: d.__setitem__("log", ([{"t": time.time(), "text": text}] + d["log"])[:30]))

    # --- подарки
    def gift_file(self, kind) -> str:
        d = self.store.snapshot()
        return d["stars"].get(kind, {}).get("file") or d["gifts"]["file"]

    def take_gift(self, file_name):
        """Берёт первую строку из файла. Возвращает (подарок или None, остаток)."""
        if not file_name:
            return None, 0
        path = safe_file(file_name)
        with self.file_lock:
            lines = read_lines(path)
            if not lines:
                return None, 0
            gift = lines.pop(0)
            if self.store["gifts"]["cycle"]:
                lines.append(gift)
            write_lines(path, lines)
            return gift, len(lines)

    def stock_check(self, file_name, left):
        threshold = self.store["gifts"]["threshold"]
        warned = self.store["low_warned"].get(file_name)
        if left >= threshold:
            if warned:
                self.store.change(lambda d: d["low_warned"].pop(file_name, None))
            return
        if not warned and self.ui:
            self.store.change(lambda d: d["low_warned"].__setitem__(file_name, True))
            self.ui.notify(f"📉 <b>Подарки заканчиваются</b>: в файле <code>{esc(file_name)}</code> осталось "
                           f"<b>{left}</b> (порог {threshold}).", [[("🎁 Подарки", ("gifts",))]])

    # --- обработка отзыва
    def on_event(self, msg, kind):
        d = self.store.snapshot()
        if not d["enabled"] or getattr(msg, "i_am_buyer", False):
            return
        found = ORDER_RE.search(str(getattr(msg, "text", "") or ""))
        if not found:
            return
        oid = found.group(1)
        key = (oid, kind)
        with self.lock:
            if key in self.inflight:
                return
            self.inflight.add(key)
        try:
            self.process(msg, oid, kind)
        except Exception:
            LOG.exception("%s: ошибка обработки отзыва %s", NAME, oid)
        finally:
            with self.lock:
                self.inflight.discard(key)

    def process(self, msg, oid, kind):
        account = self.cardinal.account
        order = None
        try:
            order = self.cardinal.get_order_from_object(msg)
        except Exception:
            order = None
        if order is None or str(getattr(order, "id", "")) != oid:
            order = account.get_order(oid)
        if getattr(order, "seller_id", account.id) != account.id:
            return
        if kind == "review":
            stars = getattr(getattr(order, "review", None), "stars", None)
            if not stars:
                return
            kind = str(stars)
        d = self.store.snapshot()
        star = d["stars"][kind]
        done = d["processed"].get(oid, {})
        if not star["enabled"] or kind in done.get("replied", []):
            return
        values = order_vars(order)
        reply = render(star["text"], values).strip()
        gift_text = None
        gifts = d["gifts"]
        if kind != "deleted" and gifts["enabled"] and star.get("gift") and not done.get("gift"):
            file_name = star.get("file") or gifts["file"]
            gift, left = self.take_gift(file_name)
            self.store.change(lambda s: s["processed"].setdefault(oid, {}).update(gift=True))
            if gift is None:
                self.log(f"🎁 #{oid}: подарков нет в {file_name or '(файл не выбран)'}")
                if self.ui:
                    self.ui.notify(f"⚠️ <b>Подарки закончились</b> (файл <code>{esc(file_name or 'не выбран')}</code>). "
                                   f"Покупатель {esc(values['$username'])} по заказу <code>#{oid}</code> получил только "
                                   f"текст ответа.", [[("🎁 Подарки", ("gifts",))]])
            else:
                template = gifts["text"] or "$gift"
                if "$gift" not in template:
                    template = template.rstrip() + "\n$gift"
                gift_text = render(template, dict(values, **{"$gift": gift})).strip()
                self.log(f"🎁 #{oid}: выдан подарок из {file_name}, осталось {left}")
                if self.ui:
                    self.ui.notify(f"🎁 Выдан подарок за отзыв {KIND_TITLES[kind]} по заказу <code>#{oid}</code> "
                                   f"({esc(values['$username'])}). Осталось в файле: {left}.")
                self.stock_check(file_name, left)
        messages = [reply] if reply else []
        if gift_text:
            if gifts["separate"] or not messages:
                messages.append(gift_text)
            else:
                messages[0] = messages[0] + "\n\n" + gift_text
        chat_id = getattr(order, "chat_id", None)
        try:
            chat = account.get_chat_by_name(values["$username"])
            if chat is not None:
                chat_id = chat.id
        except Exception:
            pass
        for text in messages:
            self.send(chat_id, text, values["$username"], d["watermark"])
        self.store.change(lambda s: s["processed"].setdefault(oid, {}).setdefault("replied", []).append(kind))
        self.trim()
        self.log(f"💬 #{oid}: ответ на {KIND_TITLES[kind]} ({values['$username']})")

    def send(self, chat_id, text, name, watermark):
        try:
            self.cardinal.send_message(chat_id, text, name, watermark=watermark)
        except TypeError:
            self.cardinal.send_message(chat_id, text, name)

    def trim(self):
        def cut(d):
            if len(d["processed"]) > 3000:
                for oid in list(d["processed"])[:1000]:
                    d["processed"].pop(oid, None)
        self.store.change(cut)


class ReplyPanel(Panel):
    COMMAND = "review_reply"
    ICON = "💬"
    DOC_INPUTS = ("upload",)
    GUIDE = [
        ("🌟 Зачем это нужно", "Публичный ответ под отзывом видят все, а личное сообщение в чате превращает случайного "
         "покупателя в постоянного.\n❤️ <b>Благодарность и подарок за 5⭐</b> — «Спасибо за отзыв! Держите подарок!».\n"
         "💔 <b>Перехват негатива (1–3⭐)</b> — «Что пошло не так? Готов всё исправить!» — часто отзыв меняют на 5⭐.\n"
         "🗑 <b>Удаление отзыва</b> — бот вежливо спросит, возникли ли трудности."),
        ("🛠 Шаг 1–2. Запуск и тексты", "1. Включите плагин в списке плагинов Cardinal и сделайте /restart.\n"
         "2. <code>/review_reply</code> → «Плагин: 🔴» переключите в 🟢. «Вотермарка» — добавлять ли подпись бота.\n"
         "3. Выберите оценку (⭐ … ⭐⭐⭐⭐⭐ или 🗑 Удалённый) → «✏️ Изменить текст» → отправьте текст → «Статус: 🟢».\n"
         "Пример для 5⭐: <i>Спасибо за покупку, $username! Рад был помочь.</i>\nЕсли статус оценки 🔴 — бот её "
         "игнорирует.\n\nОтвет отправляется один раз на каждую оценку заказа. Если покупатель изменит отзыв на другую "
         "оценку — придёт текст новой оценки."),
        ("🎁 Шаг 3. Подарки", "Подарки — обычные <code>.txt</code> из <code>storage/products</code> (те же файлы, что и "
         "в автовыдаче). 1 отзыв = 1 строка.\n1. «🎁 Подарки за отзывы» → «Подарки: 🟢».\n2. «📂 Файл» — выберите "
         "файл, создайте новый или загрузите .txt. В меню оценки «📂 Свой файл» — отдельный файл для этой оценки.\n"
         "3. «🎁 Подарок» в меню оценки — выдавать ли подарок за эту оценку (по умолчанию за 4⭐ и 5⭐).\n"
         "4. «✏️ Текст сообщения» — с <code>$gift</code> на месте подарка. Забыли $gift — подарок допишется в конец.\n"
         "5. «📨 Отдельно» — подарок отдельным сообщением или вместе с ответом. «♾ По кругу» — строка уходит в конец "
         "файла и выдаётся снова. «📉 Порог» — предупреждение, когда подарков меньше N.\n⚠️ За удалённые отзывы "
         "подарки не выдаются."),
        ("📁 Шаг 4. Файлы в Telegram", "• «⬆️ Загрузить .txt» — отправьте документ.\n• «➕ Добавить подарки» — "
         "список сообщением, каждый подарок с новой строки.\n• «📥 Скачать файл» — получить текущий файл.\n"
         "• «➕ Создать новый файл» — пустой файл с вашим именем."),
        ("📋 Переменные", "<code>$gift</code> — подарок\n<code>$username</code> — ник покупателя\n<code>$order_id</code> — "
         "ID заказа без #\n<code>$order_title</code> — название товара\n<code>$order_params</code> — параметры заказа\n"
         "<code>$game</code> — игра\n<code>$category</code> — категория\n<code>$date</code> / "
         "<code>$full_date_text</code> — дата\n<code>$time</code> / <code>$full_time</code> — время"),
        ("❓ FAQ", "<b>Подарки закончились?</b> Бот пришлёт уведомление, покупатель получит только текст. Повторной "
         "выдачи по этому заказу не будет (чтобы не было дублей).\n<b>Разные подарки за 4 и 5⭐?</b> Откройте ⭐⭐⭐⭐ → "
         "«📂 Свой файл» → <code>4_stars_gifts.txt</code>; для 5⭐ — общий или свой файл.\n<b>Защита от повторов</b> — "
         "каждый заказ помечается в базе, двух ответов или двух подарков за один отзыв не будет."),
    ]

    def on_home(self, chat, mid=None, *args):
        d = self.plugin.store.snapshot()
        lines = [f"💬 <b>{esc(NAME)}</b> v{VERSION}", "",
                 f"Плагин: {'🟢 работает' if d['enabled'] else '🔴 выключен'} · вотермарка "
                 f"{'🟢' if d['watermark'] else '🔴'}",
                 f"Подарки: {'🟢' if d['gifts']['enabled'] else '🔴'}", "", "Оценки (🟢 — бот отвечает):"]
        for k in KINDS:
            s = d["stars"][k]
            gift = " 🎁" if k != "deleted" and s.get("gift") and d["gifts"]["enabled"] else ""
            lines.append(f"{'🟢' if s['enabled'] else '🔴'} {KIND_TITLES[k]}{gift}")
        if d["log"]:
            lines += ["", "🕓 <b>Журнал:</b>"] + [f"{fmt_dt(i['t'])} {esc(i['text'])}" for i in d["log"][:5]]
        rows = [[("Плагин: " + ("🟢" if d["enabled"] else "🔴"), ("toggle", "enabled")),
                 ("Вотермарка: " + ("🟢" if d["watermark"] else "🔴"), ("toggle", "watermark"))]]
        rows += [[(KIND_TITLES[k], ("star", k)) for k in KINDS[:3]], [(KIND_TITLES[k], ("star", k)) for k in KINDS[3:]]]
        rows.append([("🎁 Подарки за отзывы", ("gifts",))])
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_toggle(self, chat, mid, key):
        if key not in ("enabled", "watermark"):
            raise UserError("Неизвестная настройка.")
        self.plugin.store.change(lambda d: d.update({key: not d[key]}))
        self.on_home(chat, mid)

    # --- оценка
    def on_star(self, chat, mid, kind):
        if kind not in KINDS:
            raise UserError("Неизвестная оценка.")
        d = self.plugin.store.snapshot()
        s = d["stars"][kind]
        lines = [f"{KIND_TITLES[kind]} <b>Ответ на {'удалённый отзыв' if kind == 'deleted' else 'отзыв ' + KIND_TITLES[kind]}</b>",
                 f"Статус: {'🟢 включено' if s['enabled'] else '🔴 выключено'}", "", f"<i>{esc(s['text'])}</i>"]
        rows = [[("Статус: " + ("🟢" if s["enabled"] else "🔴"), ("star_toggle", kind, "enabled")),
                 ("✏️ Изменить текст", ("star_text", kind))]]
        if kind != "deleted":
            file_name = s.get("file") or ""
            lines += ["", f"🎁 Подарок за эту оценку: {'🟢' if s.get('gift') else '🔴'}",
                      f"📂 Файл: {esc(file_name) + ' (свой)' if file_name else esc(d['gifts']['file'] or 'общий, не выбран')}"]
            rows.append([("🎁 Подарок: " + ("🟢" if s.get("gift") else "🔴"), ("star_toggle", kind, "gift")),
                         ("📂 Свой файл", ("files", "star", kind))])
            if file_name:
                rows.append([("↩️ Использовать общий файл", ("star_file_clear", kind))])
        else:
            lines.append("\n⚠️ За удалённые отзывы подарки не выдаются.")
        rows.append([("◀️ Назад", ("home",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_star_toggle(self, chat, mid, kind, key):
        if kind not in KINDS or key not in ("enabled", "gift"):
            raise UserError("Неизвестная настройка.")
        self.plugin.store.change(lambda d: d["stars"][kind].update({key: not d["stars"][kind].get(key)}))
        self.on_star(chat, mid, kind)

    def on_star_text(self, chat, mid, kind):
        self.ask(chat, f"✏️ Отправьте текст ответа для {KIND_TITLES[kind]} (до 1500 символов).\nПеременные: "
                       f"{', '.join('<code>' + v + '</code>' for v in VARIABLES[1:])}", ("star_text", kind), mid)

    def in_star_text(self, chat, text, kind):
        if kind not in KINDS:
            raise UserError("Неизвестная оценка.")
        if len(text) > 1500:
            raise UserError("Максимум 1500 символов.")
        self.plugin.store.change(lambda d: d["stars"][kind].update(text=text.strip()))
        self.on_star(chat, None, kind)

    def on_star_file_clear(self, chat, mid, kind):
        self.plugin.store.change(lambda d: d["stars"][kind].update(file=""))
        self.on_star(chat, mid, kind)

    # --- подарки
    def on_gifts(self, chat, mid=None):
        g = self.plugin.store["gifts"]
        file_name = g["file"]
        count = len(read_lines(safe_file(file_name))) if file_name else 0
        lines = ["🎁 <b>Подарки за отзывы</b>", "",
                 f"Подарки: {'🟢 включены' if g['enabled'] else '🔴 выключены'}",
                 f"📂 Общий файл: {esc(file_name) if file_name else 'не выбран'}" + (f" — осталось <b>{count}</b>" if
                                                                                     file_name else ""),
                 f"✏️ Текст: <i>{esc(g['text'])}</i>",
                 f"📨 Отдельным сообщением: {'🟢' if g['separate'] else '🔴'} · ♾ По кругу: {'🟢' if g['cycle'] else '🔴'}"
                 f" · 📉 Порог: {g['threshold']}"]
        rows = [[("Подарки: " + ("🟢 Вкл" if g["enabled"] else "🔴 Выкл"), ("gift_toggle", "enabled"))],
                [("📂 Файл", ("files", "main", "")), ("✏️ Текст сообщения", ("gift_text",))],
                [("📨 Отдельно: " + ("🟢" if g["separate"] else "🔴"), ("gift_toggle", "separate")),
                 ("♾ По кругу: " + ("🟢" if g["cycle"] else "🔴"), ("gift_toggle", "cycle"))],
                [("📉 Порог: " + str(g["threshold"]), ("gift_threshold",))]]
        if file_name:
            rows.append([("➕ Добавить подарки", ("add_lines", file_name)), ("📥 Скачать файл", ("download", file_name))])
        rows.append([("⬆️ Загрузить .txt", ("upload_ask", "main", ""))])
        rows.append([("◀️ Назад", ("home",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_gift_toggle(self, chat, mid, key):
        if key not in ("enabled", "separate", "cycle"):
            raise UserError("Неизвестная настройка.")
        self.plugin.store.change(lambda d: d["gifts"].update({key: not d["gifts"][key]}))
        self.on_gifts(chat, mid)

    def on_gift_text(self, chat, mid):
        self.ask(chat, "✏️ Отправьте текст сообщения с подарком (до 1000 символов). Укажите <code>$gift</code> там, "
                       "где должен быть подарок.", ("gift_text",), mid)

    def in_gift_text(self, chat, text):
        if len(text) > 1000:
            raise UserError("Максимум 1000 символов.")
        self.plugin.store.change(lambda d: d["gifts"].update(text=text.strip()))
        self.on_gifts(chat)

    def on_gift_threshold(self, chat, mid):
        self.ask(chat, "📉 При каком остатке подарков присылать предупреждение? (0–100000)", ("gift_threshold",), mid)

    def in_gift_threshold(self, chat, text):
        value = parse_int(text, 0, 100000, "Порог")
        self.plugin.store.change(lambda d: (d["gifts"].update(threshold=value), d.update(low_warned={})))
        self.on_gifts(chat)

    # --- файлы
    def on_files(self, chat, mid, target, kind=""):
        files = sorted(p.name for p in products_dir().glob("*.txt"))[:40]
        rows = [[(f"📄 {name} ({len(read_lines(products_dir() / name))})", ("pick", target, kind, name))]
                for name in files]
        rows.append([("➕ Создать новый файл", ("new_file", target, kind)),
                     ("⬆️ Загрузить .txt", ("upload_ask", target, kind))])
        rows.append([("◀️ Назад", ("star", kind) if target == "star" else ("gifts",))])
        title = f"для оценки {KIND_TITLES.get(kind, '')}" if target == "star" else "(общий)"
        self.send(chat, f"📂 <b>Выберите файл с подарками {title}</b>\nПапка: <code>storage/products</code>\n"
                        f"Файлов: {len(files)}", rows, mid)

    def on_pick(self, chat, mid, target, kind, name):
        safe_file(name)
        self.set_file(target, kind, name)
        self.after_file(chat, mid, target, kind)

    def set_file(self, target, kind, name):
        if target == "star":
            if kind not in KINDS or kind == "deleted":
                raise UserError("Неизвестная оценка.")
            self.plugin.store.change(lambda d: d["stars"][kind].update(file=name))
        else:
            self.plugin.store.change(lambda d: d["gifts"].update(file=name))

    def after_file(self, chat, mid, target, kind):
        if target == "star":
            self.on_star(chat, mid, kind)
        else:
            self.on_gifts(chat, mid)

    def on_new_file(self, chat, mid, target, kind):
        self.ask(chat, "➕ Отправьте имя нового файла, например <code>5_stars_gifts.txt</code>.",
                 ("new_file", target, kind), mid)

    def in_new_file(self, chat, text, target, kind):
        path = safe_file(text)
        if path.exists():
            raise UserError("Такой файл уже есть — выберите его в списке.")
        write_lines(path, [])
        self.set_file(target, kind, path.name)
        self.after_file(chat, None, target, kind)

    def on_upload_ask(self, chat, mid, target, kind):
        self.ask(chat, "⬆️ Отправьте .txt файл документом (до 2 МБ, каждый подарок с новой строки). Файл с таким же "
                       "именем будет заменён.", ("upload", target, kind), mid, doc=True)

    def in_upload(self, chat, document, target, kind):
        name = getattr(document, "file_name", "") or ""
        if not name.lower().endswith(".txt"):
            raise UserError("Нужен файл с расширением .txt.")
        if (getattr(document, "file_size", 0) or 0) > 2 * 1024 * 1024:
            raise UserError("Файл больше 2 МБ.")
        path = safe_file(os.path.basename(name))
        info = self.bot.get_file(document.file_id)
        data = self.bot.download_file(info.file_path)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1251", errors="replace")
        lines = [line.rstrip("\r") for line in text.split("\n") if line.strip()]
        with self.plugin.file_lock:
            write_lines(path, lines)
        self.set_file(target, kind, path.name)
        self.send(chat, f"✅ Файл <code>{esc(path.name)}</code> загружен: {len(lines)} подарков.")
        self.after_file(chat, None, target, kind)

    def on_add_lines(self, chat, mid, name):
        self.ask(chat, f"➕ Отправьте подарки для <code>{esc(name)}</code> — каждый с новой строки.",
                 ("add_lines", name), mid)

    def in_add_lines(self, chat, text, name):
        path = safe_file(name)
        new = [line.strip() for line in text.split("\n") if line.strip()]
        if not new:
            raise UserError("Список пустой.")
        with self.plugin.file_lock:
            lines = read_lines(path) + new
            write_lines(path, lines)
        self.plugin.store.change(lambda d: d["low_warned"].pop(name, None))
        self.send(chat, f"✅ Добавлено {len(new)}. Всего в файле: {len(lines)}.")
        self.on_gifts(chat)

    def on_download(self, chat, mid, name):
        path = safe_file(name)
        if not path.exists():
            raise UserError("Файл не найден.")
        self.send_file(chat, path.name, path.read_bytes(), caption=f"📥 {path.name}: {len(read_lines(path))} шт.")


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = ReviewReply(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = ReplyPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


def new_message(cardinal, event, *args):
    if PLUGIN is None:
        return
    msg = getattr(event, "message", None)
    if msg is None or getattr(msg, "author_id", None) != 0:
        return
    kind = enum_name(getattr(msg, "type", ""))
    if kind in ("NEW_FEEDBACK", "FEEDBACK_CHANGED"):
        target = "review"
    elif kind == "FEEDBACK_DELETED":
        target = "deleted"
    else:
        return
    if PLUGIN.sync:
        PLUGIN.on_event(msg, target)
    else:
        threading.Thread(target=PLUGIN.on_event, args=(msg, target), name="ReviewChatReply", daemon=True).start()


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_MESSAGE = [new_message]
BIND_TO_DELETE = None
