"""
📦 Stock Notifier (Остатки автовыдачи) — плагин для FunPay Cardinal от LuckyFunpay.

Следит за количеством товаров во встроенной автовыдаче FunPay и присылает уведомление
в Telegram, когда товар подходит к концу или полностью закончился.

ИНСТРУКЦИЯ (полная версия — в боте: /stock_notifier → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. Откройте /stock_notifier → «➕ Добавить лот для отслеживания» и отправьте ID лота
   (цифры из ссылки funpay.com/lots/offer?id=12345678) или саму ссылку.
3. Нажмите на лот и настройте порог, тексты «Мало товара» и «Товар закончился».
4. Переменные в текстах: $lot_name, $lot_id, $stock, $threshold.
Бот проверяет остаток только в момент покупки отслеживаемого лота.

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

NAME = "Stock Notifier"
VERSION = "1.0.0"
DESCRIPTION = ("📦 Уведомления в Telegram, когда в автовыдаче FunPay заканчиваются товары. "
               "Свой порог и тексты для каждого лота, кнопка быстрого пополнения. Панель: /stock_notifier")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "5b0d7a52-3c1e-4c55-9a3f-6f2b1c8e4d11"
SETTINGS_PAGE = True
SLUG = "StockNotifier"
PREFIX = "lfsn:"

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

# ═══════════════════════════ LuckyFunpay lots helpers ═══════════════════════════
def lot_link(lot_id) -> str:
    return f"https://funpay.com/lots/offer?id={lot_id}"


def edit_link(lot_id) -> str:
    return f"https://funpay.com/lots/offerEdit?offer={lot_id}"


def lot_fields_raw(fields) -> dict:
    return fields.fields if hasattr(fields, "fields") else {}


def lot_title(fields) -> str:
    raw = lot_fields_raw(fields)
    return (raw.get("fields[summary][ru]") or getattr(fields, "title_ru", "") or raw.get("fields[summary][en]")
            or getattr(fields, "title_en", "") or f"Лот {getattr(fields, 'lot_id', '')}").strip()


def lot_secrets(fields) -> list:
    raw = lot_fields_raw(fields).get("secrets")
    if raw is None:
        return [s for s in (getattr(fields, "secrets", None) or []) if str(s).strip()]
    return [s.rstrip("\r") for s in str(raw).split("\n") if s.strip()]


def lot_auto_delivery(fields) -> bool:
    raw = lot_fields_raw(fields)
    if "auto_delivery" in raw:
        return raw.get("auto_delivery") in ("on", True, "1")
    return bool(getattr(fields, "auto_delivery", False))


def lot_active(fields) -> bool:
    return bool(getattr(fields, "active", lot_fields_raw(fields).get("active") == "on"))


def lot_amount(fields):
    raw = lot_fields_raw(fields).get("amount")
    if raw in (None, ""):
        return None
    try:
        return int(str(raw).replace(" ", ""))
    except ValueError:
        return None


def lot_node(fields):
    sub = getattr(fields, "subcategory", None)
    if sub is not None:
        return sub.id
    try:
        return int(lot_fields_raw(fields).get("node_id") or 0) or None
    except ValueError:
        return None


def lot_update(fields, **changes):
    """Меняет поля лота так, чтобы изменения сохранились во всех версиях FunPayAPI."""
    raw = {}
    for key, value in changes.items():
        if key == "active":
            fields.active = bool(value)
            raw["active"] = "on" if value else ""
        elif key == "secrets":
            value = [str(s) for s in value]
            if hasattr(fields, "secrets"):
                fields.secrets = list(value)
            raw["secrets"] = "\n".join(value)
        elif key == "auto_delivery":
            if hasattr(fields, "auto_delivery"):
                fields.auto_delivery = bool(value)
            raw["auto_delivery"] = "on" if value else ""
        elif key == "amount":
            fields.amount = value
            raw["amount"] = "" if value is None else str(value)
        elif key == "price":
            value = round(float(value), 2)
            fields.price = value
            raw["price"] = f"{value:g}" if value != int(value) else str(int(value))
        elif key in ("title_ru", "title_en", "description_ru", "description_en", "payment_msg_ru", "payment_msg_en"):
            setattr(fields, key, value)
            name = {"title": "summary", "description": "desc", "payment_msg": "payment_msg"}[key.rsplit("_", 1)[0]]
            raw[f"fields[{name}][{key.rsplit('_', 1)[1]}]"] = value
        else:
            raw[key] = value
    if hasattr(fields, "edit_fields"):
        fields.edit_fields(raw)
    else:
        lot_fields_raw(fields).update(raw)
    return fields


def wait(stop_event, seconds) -> bool:
    """Пауза, прерываемая кнопкой «Стоп». Возвращает True, если нажали стоп."""
    if stop_event is None:
        time.sleep(seconds)
        return False
    return stop_event.wait(seconds)


def fresh_profile(cardinal):
    profile = cardinal.account.get_user(cardinal.account.id)
    try:
        cardinal.curr_profile = profile
    except Exception:
        pass
    return profile


def my_subcategories(cardinal, profile=None, extra_ids=(), include_currency=False, from_sales=False) -> list:
    """Подкатегории с лотами продавца: из профиля (активные лоты) + известные плагину ID
    (+ разделы из фильтра страницы продаж, если from_sales=True)."""
    profile = profile or fresh_profile(cardinal)
    found = {}
    if from_sales:
        try:
            result = cardinal.account.get_sales()
            sales_subs = result[3] if len(result) > 3 and isinstance(result[3], dict) else {}
            for sub in sales_subs.values():
                if sub is not None and enum_name(sub.type) == "COMMON":
                    found[("COMMON", sub.id)] = sub
        except Exception:
            LOG.debug("%s: разделы со страницы продаж не получены", NAME)
    try:
        for sub in profile.get_sorted_lots(2).keys():
            if include_currency or not sub.type or enum_name(sub.type) == "COMMON":
                found[(enum_name(sub.type), sub.id)] = sub
    except Exception:
        LOG.debug("%s: не удалось разобрать профиль", NAME)
    account = cardinal.account
    for sid in extra_ids:
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            continue
        if ("COMMON", sid) in found:
            continue
        sub = None
        try:
            from FunPayAPI.common.enums import SubCategoryTypes
            sub = account.get_subcategory(SubCategoryTypes.COMMON, sid)
        except Exception:
            sub = None
        if sub is not None:
            found[("COMMON", sid)] = sub
    return sorted(found.values(), key=lambda s: (s.category.name.lower(), s.name.lower()))


def my_lots(cardinal, subcategory) -> list:
    """Все лоты продавца в подкатегории (включая выключенные)."""
    return cardinal.account.get_my_subcategory_lots(subcategory.id)
# ═════════════════════════ конец LuckyFunpay lots helpers ═════════════════════════


DEFAULT_LOW = ("⚠️ Внимание! Заканчивается товар!\n"
               "🎮 Лот: $lot_name\n"
               "📦 В наличии осталось: $stock шт. (Порог: $threshold)\n\n"
               "Пожалуйста, пополните запасы!")
DEFAULT_EMPTY = ("🚨 Товар закончился!\n"
                 "🎮 Лот: $lot_name\n"
                 "📦 В автовыдаче осталось: $stock шт. — FunPay отключает лот автоматически.\n\n"
                 "Срочно пополните запасы!")
VARIABLES = ("$lot_name", "$lot_id", "$stock", "$threshold")
DEFAULTS = {"enabled": True, "lots": {}, "delay": 3}


def render(template: str, lot_id, info: dict, stock) -> str:
    values = {"$lot_name": info.get("name", ""), "$lot_id": str(lot_id),
              "$stock": "?" if stock is None else str(stock), "$threshold": str(info.get("threshold", 0))}
    text = esc(template)
    for key in sorted(values, key=len, reverse=True):
        text = text.replace(esc(key), "<b>" + esc(values[key]) + "</b>")
    return text


def parse_lot_id(raw: str) -> int:
    text = str(raw).strip()
    match = re.search(r"(?:offer(?:Edit)?\?(?:id|offer)=)(\d{1,12})", text) or re.fullmatch(r"#?(\d{1,12})", text)
    if not match:
        raise UserError("Отправьте ID лота (только цифры) или ссылку вида funpay.com/lots/offer?id=12345678.")
    return int(match.group(1))


class StockNotifier:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("settings.json", DEFAULTS)
        self.ui = None
        self.lock = threading.Lock()
        self.checking = set()

    # --- проверка остатка
    def stock_of(self, lot_id: int):
        fields = self.cardinal.account.get_lot_fields(lot_id)
        name = lot_title(fields)
        node = lot_node(fields)
        if not lot_auto_delivery(fields):
            return name, node, None, lot_active(fields)
        return name, node, len(lot_secrets(fields)), lot_active(fields)

    def check(self, lot_id: int, manual=False):
        key = str(lot_id)
        info = self.store["lots"].get(key)
        if info is None:
            return None
        with self.lock:
            if key in self.checking:
                return None
            self.checking.add(key)
        try:
            try:
                name, node, stock, active = self.stock_of(lot_id)
            except Exception as error:
                LOG.warning("%s: не удалось проверить лот %s: %s", NAME, lot_id, error)
                if manual:
                    raise UserError("Не удалось получить лот с FunPay. Проверьте ID и попробуйте позже.") from None
                return None

            def save(data):
                item = data["lots"].get(key)
                if item is not None:
                    item.update({"name": name or item.get("name", ""), "node": node, "stock": stock,
                                 "checked": time.time(), "auto": stock is not None})
                return copy.deepcopy(item)

            info = self.store.change(save)
            if info is None:
                return None
            state = "off" if stock is None else "empty" if stock == 0 else "low" if stock <= info["threshold"] else "ok"
            if not manual and state in ("low", "empty") and self.store["enabled"]:
                self.alert(lot_id, info, stock, state)
            return state, stock, info
        finally:
            with self.lock:
                self.checking.discard(key)

    def alert(self, lot_id, info, stock, state):
        if not self.ui:
            return
        template = (info.get("empty_text") or DEFAULT_EMPTY) if state == "empty" else (info.get("low_text") or DEFAULT_LOW)
        text = render(template, lot_id, info, stock)
        self.ui.notify(text, [[("🔗 Пополнить автовыдачу", edit_link(lot_id))],
                              [("📦 Открыть в плагине", ("lot", str(lot_id)))]])
        LOG.info("%s: уведомление по лоту %s (%s, остаток %s)", NAME, lot_id, state, stock)

    # --- новый заказ
    def matching_lots(self, event) -> list:
        lots = self.store["lots"]
        if not lots:
            return []
        lot_id = getattr(event, "lot_id", None)
        if lot_id is not None and str(lot_id) in lots:
            return [str(lot_id)]
        order = event.order
        description = (getattr(order, "description", "") or "").casefold()
        found = [k for k, v in lots.items() if v.get("name") and v["name"].casefold() in description]
        if found:
            return found
        sub = getattr(order, "subcategory", None)
        if sub is not None:
            return [k for k, v in lots.items() if v.get("node") == sub.id]
        return []

    def on_new_order(self, event):
        if not self.store["enabled"]:
            return
        targets = self.matching_lots(event)
        if not targets:
            return

        def run():
            time.sleep(max(0, self.store["delay"]))
            for key in targets:
                self.check(int(key))
                time.sleep(1)

        threading.Thread(target=run, name="StockNotifierCheck", daemon=True).start()


class StockPanel(Panel):
    COMMAND = "stock_notifier"
    ICON = "📦"
    GUIDE = [
        ("🚀 Как включить", "1. Положите <code>StockNotifier_LuckyFunpay.py</code> в папку <code>plugins</code> "
         "или отправьте файл боту.\n2. /menu → Система и конфиги → Плагины → <b>Stock Notifier</b> → включить.\n"
         "3. Перезапустите бота командой /restart.\n4. Откройте панель командой /stock_notifier.\n\n"
         "Плагин не следит за всеми лотами сразу — вы сами добавляете лоты, за которыми нужен контроль."),
        ("➕ Добавление лота", "1. Нажмите «➕ Добавить лот для отслеживания».\n2. Отправьте ID лота.\n"
         "<i>Где взять ID?</i> Откройте ваш лот на FunPay — в ссылке будут цифры: "
         "<code>funpay.com/lots/offer?id=12345678</code> → ID = <code>12345678</code>. Можно прислать и всю ссылку.\n"
         "3. 🪄 Бот сам зайдёт на FunPay, скачает название лота и текущий остаток и добавит лот в мониторинг."),
        ("⚙️ Порог и тексты", "Нажмите на лот в меню, чтобы открыть его настройки.\n\n"
         "🔢 <b>Порог уведомления</b> — количество, при котором бот начнёт бить тревогу. Порог 5: как только "
         "после покупки в автовыдаче останется 5 или меньше ключей, придёт «Мало товара».\n"
         "📝 <b>Текст: Мало товара</b> — сообщение, когда остаток не больше порога, но ещё не ноль.\n"
         "📝 <b>Текст: Товар закончился</b> — когда автовыдача опустела (0). FunPay в этот момент сам отключает лот.\n"
         "Отправьте «-», чтобы вернуть стандартный текст."),
        ("✉️ Переменные", "В текстах можно использовать:\n<code>$lot_name</code> — название лота с FunPay\n"
         "<code>$lot_id</code> — ID лота\n<code>$stock</code> — сколько штук осталось сейчас\n"
         "<code>$threshold</code> — ваш порог\n\nПример:\n<code>⚠️ Заканчивается товар! 🎮 Игра: $lot_name "
         "📦 Осталось: $stock шт. (Порог: $threshold)</code>\n\nПод каждым уведомлением есть кнопка "
         "«🔗 Пополнить автовыдачу» — она открывает страницу редактирования лота."),
        ("❓ FAQ", "<b>Как часто бот проверяет остатки?</b> Только в момент покупки отслеживаемого лота: пришёл заказ — "
         "бот открывает лот, считает строки автовыдачи (поле secrets) и при необходимости пишет вам. "
         "Это экономит запросы и не привлекает внимание защиты FunPay. Кнопка «🔄 Проверить» проверяет вручную.\n\n"
         "<b>«Мало товара», хотя ключей много?</b> Слишком большой порог — уменьшите его кнопкой 🔢.\n\n"
         "<b>Работает без встроенной автовыдачи FunPay?</b> Нет: плагин считает именно товары, загруженные "
         "в автовыдачу FunPay. Такие лоты помечаются ⚪️."),
    ]

    def stock_badge(self, info):
        stock = info.get("stock")
        if not info.get("auto", True):
            return "⚪️"
        if stock is None:
            return "❔"
        return "🔴" if stock == 0 else "🟡" if stock <= info.get("threshold", 0) else "🟢"

    def on_home(self, chat, mid=None, *args):
        data = self.plugin.store.snapshot()
        lots = data["lots"]
        lines = [f"📦 <b>{esc(NAME)}</b> v{VERSION}", "",
                 f"Уведомления: {'🟢 включены' if data['enabled'] else '🔴 выключены'}",
                 f"Отслеживается лотов: <b>{len(lots)}</b>", ""]
        if not lots:
            lines.append("Пока нет лотов. Нажмите «➕ Добавить лот для отслеживания».")
        else:
            lines.append("🟢 запас есть · 🟡 мало · 🔴 закончился · ⚪️ нет автовыдачи")
        rows = []
        for key, info in sorted(lots.items(), key=lambda kv: kv[1].get("name", "").lower())[:40]:
            stock = info.get("stock")
            label = f"{self.stock_badge(info)} {info.get('name', key)[:34]} · {'?' if stock is None else stock}/{info['threshold']}"
            rows.append([(label, ("lot", key))])
        rows.append([("➕ Добавить лот для отслеживания", ("add",))])
        rows.append([("🔔 Уведомления: " + ("🟢" if data["enabled"] else "🔴"), ("toggle",)),
                     ("🔄 Проверить все", ("check_all",))])
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_toggle(self, chat, mid):
        self.plugin.store.change(lambda d: d.update(enabled=not d["enabled"]))
        self.on_home(chat, mid)

    def on_add(self, chat, mid):
        self.ask(chat, "➕ Отправьте <b>ID лота</b> (цифры из ссылки) или ссылку на лот FunPay.", ("lot_id",), mid)

    def in_lot_id(self, chat, text):
        lot_id = parse_lot_id(text)
        if str(lot_id) in self.plugin.store["lots"]:
            raise UserError("Этот лот уже отслеживается.")
        msg = self.send(chat, "⏳ Получаю лот с FunPay…")
        try:
            name, node, stock, active = self.plugin.stock_of(lot_id)
        except Exception:
            raise UserError("Лот не найден на FunPay или он не ваш. Проверьте ID.") from None
        info = {"name": name, "node": node, "threshold": 5, "low_text": "", "empty_text": "", "stock": stock,
                "checked": time.time(), "auto": stock is not None, "added": time.time()}
        self.plugin.store.change(lambda d: d["lots"].__setitem__(str(lot_id), info))
        note = ("" if stock is not None else
                "\n\n⚠️ У лота выключена встроенная автовыдача FunPay — плагину нечего считать. "
                "Включите автовыдачу в настройках лота.")
        self.send(chat, f"✅ Лот добавлен: <b>{esc(name)}</b>\nОстаток: <b>{'—' if stock is None else stock}</b>{note}",
                  [[("⚙️ Настроить", ("lot", str(lot_id))), ("🏠 Меню", ("home",))]], getattr(msg, "message_id", None))

    def lot_info(self, key):
        info = self.plugin.store["lots"].get(key)
        if info is None:
            raise UserError("Лот уже удалён из отслеживания.")
        return info

    def on_lot(self, chat, mid, key):
        info = self.lot_info(key)
        stock = info.get("stock")
        text = (f"{self.stock_badge(info)} <b>{esc(info.get('name', key))}</b>\n\n"
                f"🆔 ID: <code>{key}</code>\n"
                f"📦 Остаток: <b>{'нет автовыдачи' if not info.get('auto', True) else '?' if stock is None else stock}</b>"
                f" (проверено {fmt_dt(info.get('checked'))})\n"
                f"🔢 Порог: <b>{info['threshold']}</b>\n\n"
                f"📝 <b>Мало товара:</b>\n<i>{esc((info.get('low_text') or DEFAULT_LOW)[:600])}</i>\n\n"
                f"📝 <b>Товар закончился:</b>\n<i>{esc((info.get('empty_text') or DEFAULT_EMPTY)[:600])}</i>")
        self.send(chat, text, [
            [("🔢 Изменить порог", ("ask_threshold", key)), ("🔄 Проверить сейчас", ("check", key))],
            [("📝 Текст: Мало товара", ("ask_text", key, "low_text"))],
            [("📝 Текст: Товар закончился", ("ask_text", key, "empty_text"))],
            [("🔗 Пополнить автовыдачу", edit_link(key)), ("🛒 Лот", lot_link(key))],
            [("🗑 Удалить из отслеживания", ("del", key))],
            [("◀️ Назад", ("home",))]], mid)

    def on_ask_threshold(self, chat, mid, key):
        self.lot_info(key)
        self.ask(chat, "🔢 Введите порог уведомления (число от 0 до 100000).\n"
                       "Уведомление придёт, когда остаток станет меньше или равен порогу.", ("threshold", key), mid)

    def in_threshold(self, chat, text, key):
        value = parse_int(text, 0, 100000, "Порог")
        self.lot_info(key)
        self.plugin.store.change(lambda d: d["lots"][key].update(threshold=value))
        self.on_lot(chat, None, key)

    def on_ask_text(self, chat, mid, key, field):
        info = self.lot_info(key)
        current = info.get(field) or (DEFAULT_LOW if field == "low_text" else DEFAULT_EMPTY)
        title = "Мало товара" if field == "low_text" else "Товар закончился"
        self.ask(chat, f"📝 Отправьте новый текст «{title}» (до 1500 символов).\n"
                       f"Переменные: {', '.join('<code>' + v + '</code>' for v in VARIABLES)}\n"
                       f"Отправьте <code>-</code>, чтобы вернуть стандартный текст.\n\nСейчас:\n<i>{esc(current)}</i>",
                 ("text", key, field), mid)

    def in_text(self, chat, text, key, field):
        if field not in ("low_text", "empty_text"):
            raise UserError("Неизвестное поле.")
        if len(text) > 1500:
            raise UserError("Текст слишком длинный: максимум 1500 символов.")
        value = "" if text.strip() == "-" else text.strip()
        self.lot_info(key)
        self.plugin.store.change(lambda d: d["lots"][key].update({field: value}))
        self.on_lot(chat, None, key)

    def on_check(self, chat, mid, key):
        result = self.plugin.check(int(key), manual=True)
        if result is None:
            raise UserError("Проверка уже идёт, подождите пару секунд.")
        self.on_lot(chat, mid, key)

    def on_check_all(self, chat, mid):
        keys = list(self.plugin.store["lots"])
        if not keys:
            raise UserError("Нет лотов для проверки.")
        self.send(chat, f"⏳ Проверяю {len(keys)} лот(ов)…", mid=mid)

        def run():
            problems = 0
            for key in keys:
                try:
                    result = self.plugin.check(int(key), manual=True)
                    if result and result[0] in ("low", "empty"):
                        problems += 1
                except UserError:
                    problems += 1
                time.sleep(0.5)
            self.on_home(chat, mid)
            if problems:
                self.send(chat, f"⚠️ Требуют внимания: <b>{problems}</b> лот(ов) — они отмечены 🟡/🔴.")

        threading.Thread(target=run, name="StockNotifierCheckAll", daemon=True).start()

    def on_del(self, chat, mid, key):
        info = self.lot_info(key)
        self.send(chat, f"Удалить «{esc(info.get('name', key))}» из отслеживания? Сам лот на FunPay не изменится.",
                  [[("🗑 Да, удалить", ("del_ok", key)), ("↩️ Нет", ("lot", key))]], mid)

    def on_del_ok(self, chat, mid, key):
        self.plugin.store.change(lambda d: d["lots"].pop(key, None))
        self.on_home(chat, mid)


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = StockNotifier(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = StockPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


def new_order(cardinal, event, *args):
    if PLUGIN is not None:
        try:
            PLUGIN.on_new_order(event)
        except Exception:
            LOG.exception("%s: ошибка обработки заказа", NAME)


BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_DELETE = None
