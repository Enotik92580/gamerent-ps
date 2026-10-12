"""
📦 Auto Stock & Cloner — автопополнение наличия и бесконечная автовыдача. Плагин для FunPay Cardinal от LuckyFunpay.

Модуль 1 «Auto Stock»: в выбранных разделах держит «Наличие» лотов без автовыдачи — если оно упало
ниже минимума, ставит случайное число от «Мин» до «Макс».
Модуль 2 «Cloner»: в выбранных лотах с автовыдачей FunPay размножает оставшиеся строки до нужного
количества (гайды, ссылки, многоразовые ключи) и следит, чтобы автовыдача была включена.

ИНСТРУКЦИЯ (полная версия — в боте: /stock_cloner → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. /stock_cloner → «🔻 Мин» и «🔺 Макс» → «🎮 Выбрать категории» (игра → разделы ✅).
3. «🧬 Клонатор автовыдачи» → «🎯 Поддерживать X строк» → «➕ Добавить лот в клонатор» (ID лота).
   В лоте уже должна быть хотя бы 1 строка автовыдачи!
Проверка — каждые 5 минут.

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

NAME = "Auto Stock & Cloner"
VERSION = "1.0.0"
DESCRIPTION = ("📦 Автопополнение «Наличия» лотов без автовыдачи случайным числом и клонатор строк автовыдачи FunPay "
               "до заданного количества. Проверка каждые 5 минут. Панель: /stock_cloner")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "7a5d9c2e-4b18-4f63-a0e7-3f8b6d1c9e25"
SETTINGS_PAGE = True
SLUG = "AutoStockCloner"
PREFIX = "lfasc:"

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


DEFAULTS = {"stock_on": True, "min": 100, "max": 50000, "subs": [], "sub_names": {}, "cloner_on": True,
            "target": 50, "clone_lots": {}, "interval": 300, "log": [], "last_run": 0, "empty_warned": []}
PAGE = 8


def parse_lot_id(raw: str) -> int:
    text = str(raw).strip()
    match = re.search(r"(?:offer(?:Edit)?\?(?:id|offer)=)(\d{1,12})", text) or re.fullmatch(r"#?(\d{1,12})", text)
    if not match:
        raise UserError("Нужен ID лота (цифры) или ссылка на лот.")
    return int(match.group(1))


def clone_to(secrets: list, target: int) -> list:
    if not secrets or len(secrets) >= target:
        return list(secrets)
    result = list(secrets)
    i = 0
    while len(result) < target:
        result.append(secrets[i % len(secrets)])
        i += 1
    return result


class StockCloner:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("settings.json", DEFAULTS)
        self.ui = None
        self.busy = threading.Lock()
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.pause = (2.0, 3.0)

    def log(self, text):
        self.store.change(lambda d: d.__setitem__("log", ([{"t": time.time(), "text": text}] + d["log"])[:20]))

    def sleep(self):
        if self.pause[1]:
            self.stop_event.wait(random.uniform(*self.pause))

    # --- модуль 1
    def restock(self) -> int:
        d = self.store.snapshot()
        if not d["stock_on"] or not d["subs"]:
            return 0
        account = self.cardinal.account
        low, high = sorted((d["min"], d["max"]))
        changed = 0
        for sid in d["subs"]:
            try:
                lots = account.get_my_subcategory_lots(int(sid))
            except Exception:
                LOG.warning("%s: лоты раздела %s не получены", NAME, sid)
                continue
            for lot in lots:
                if self.stop_event.is_set():
                    return changed
                if getattr(lot, "auto", False) or lot.amount is None or lot.amount >= low:
                    continue
                try:
                    fields = account.get_lot_fields(lot.id)
                    if lot_auto_delivery(fields) or lot_amount(fields) is None:
                        continue
                    if lot_amount(fields) >= low:
                        continue
                    value = random.randint(low, max(low, high))
                    lot_update(fields, amount=value)
                    account.save_lot(fields)
                    changed += 1
                    self.log(f"📈 {(lot.description or str(lot.id))[:40]}: {lot.amount} → {value}")
                except Exception as error:
                    LOG.warning("%s: не удалось пополнить лот %s: %s", NAME, lot.id, error)
                self.sleep()
        return changed

    # --- модуль 2
    def clone_all(self) -> int:
        d = self.store.snapshot()
        if not d["cloner_on"] or not d["clone_lots"]:
            return 0
        account = self.cardinal.account
        changed = 0
        for key in list(d["clone_lots"]):
            if self.stop_event.is_set():
                break
            try:
                fields = account.get_lot_fields(int(key))
            except Exception as error:
                LOG.warning("%s: лот %s клонатора не открылся: %s", NAME, key, error)
                continue
            secrets = lot_secrets(fields)
            self.store.change(lambda s: s["clone_lots"].get(key, {}).update(count=len(secrets), name=lot_title(fields)))
            if not secrets:
                if key not in d["empty_warned"]:
                    self.store.change(lambda s: s["empty_warned"].append(key))
                    if self.ui:
                        self.ui.notify(f"⚠️ <b>Клонатор</b>: в лоте <b>{esc(lot_title(fields))}</b> не осталось строк "
                                       f"автовыдачи — копировать нечего. Добавьте хотя бы одну строку.",
                                       [[("✏️ Открыть лот", edit_link(key))]])
                continue
            if key in d["empty_warned"]:
                self.store.change(lambda s: s["empty_warned"].remove(key) if key in s["empty_warned"] else None)
            need = len(secrets) < d["target"]
            if not need and lot_auto_delivery(fields):
                continue
            lot_update(fields, secrets=clone_to(secrets, d["target"]), auto_delivery=True)
            try:
                account.save_lot(fields)
                changed += 1
                self.store.change(lambda s: s["clone_lots"].get(key, {}).update(count=max(len(secrets), d["target"])))
                self.log(f"🧬 {lot_title(fields)[:40]}: {len(secrets)} → {max(len(secrets), d['target'])} строк")
            except Exception as error:
                LOG.warning("%s: не удалось сохранить лот %s: %s", NAME, key, error)
            self.sleep()
        return changed

    def run_once(self) -> tuple:
        if not self.busy.acquire(blocking=False):
            return None
        try:
            a = self.restock()
            b = self.clone_all()
            self.store.set(last_run=time.time())
            return a, b
        finally:
            self.busy.release()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.loop, name="AutoStockCloner", daemon=True)
        self.thread.start()

    def loop(self):
        self.stop_event.wait(30)
        while not self.stop_event.is_set():
            try:
                if account_ready(self.cardinal):
                    self.run_once()
            except Exception:
                LOG.exception("%s: ошибка цикла", NAME)
            self.wake.wait(max(60, self.store["interval"]))
            self.wake.clear()


class StockPanel(Panel):
    COMMAND = "stock_cloner"
    ICON = "📦"
    GUIDE = [
        ("🟢 Модуль 1. Auto Stock", "Для товаров, которые вы выдаёте вручную (валюта, услуги, аккаунты), но FunPay "
         "требует указать «Наличие». Когда количество падает почти до нуля, лот пропадает из выдачи. Бот следит за этим "
         "и пополняет наличие случайным числом в диапазоне — выглядит естественно.\n🎯 Лоты со встроенной автовыдачей "
         "FunPay игнорируются (чтобы не сломать продажу реальных ключей).\n🗂 Работает только в выбранных вами разделах."),
        ("⚙️ Настройка Auto Stock", "1. <code>/stock_cloner</code>.\n2. «🔻 Мин» — минимальный порог (например "
         "<code>100</code>): если наличие ниже — бот пополнит.\n3. «🔺 Макс» — верхний предел (например <code>50000</code>).\n"
         "4. «🎮 Выбрать категории» → игра → нажимайте разделы, чтобы появилась ✅.\n\nПример: золото WoW, Мин 500, "
         "Макс 10000. Купили — осталось 300. Через ≤5 минут бот увидит 300 &lt; 500, выберет случайное число (например "
         "7431) и обновит лот."),
        ("🧬 Модуль 2. Cloner", "Для товара, который продаётся много раз через встроенную автовыдачу (гайды, мануалы, "
         "ссылки, многоразовые ключи, общие аккаунты). Когда строки заканчиваются, FunPay выключает лот. Клонатор "
         "берёт оставшиеся строки и размножает их до нужного количества, а галочка «Автовыдача» остаётся включённой.\n\n"
         "1. «🧬 Клонатор автовыдачи (Секретки)».\n2. «🎯 Поддерживать X строк» — например <code>50</code>.\n"
         "3. «➕ Добавить лот в клонатор» — ID лота из ссылки.\n4. ⚠️ В лоте уже должна быть хотя бы 1 строка — клонировать "
         "пустоту нельзя.\n\nПример: 5 строк гайда, цель 50 → бот добьёт до 50; раскупили до 15 → снова до 50."),
        ("🛠 FAQ", "⏱ <b>Как часто?</b> Каждые 5 минут в фоне, между изменениями лотов пауза 2–3 сек (защита от теневого "
         "бана). «🔄 Проверить сейчас» — внеочередная проверка.\n🚫 <b>Клонатор не восстановил строки?</b> Если раскупили "
         "всё до 0 — копировать нечего (бот пришлёт предупреждение). Ставьте цель с запасом (50–100).\n❌ <b>Пополняет не "
         "там?</b> Проверьте «🎮 Выбрать категории» — включайте только разделы с валютой/услугами без автовыдачи."),
    ]

    def __init__(self, plugin):
        super().__init__(plugin)
        self.games = {}

    def on_home(self, chat, mid=None, *args):
        d = self.plugin.store.snapshot()
        names = [d["sub_names"].get(str(s), str(s)) for s in d["subs"]]
        lines = [f"📦 <b>{esc(NAME)}</b> v{VERSION}", "",
                 f"🟢 <b>Auto Stock</b>: {'включено' if d['stock_on'] else 'выключено'}",
                 f"🔻 Мин: <b>{d['min']}</b> · 🔺 Макс: <b>{d['max']}</b>",
                 f"🎮 Разделов: {len(names)}" + (" — " + esc(", ".join(names[:6])) + ("…" if len(names) > 6 else "")
                                               if names else ""),
                 "", f"🧬 <b>Клонатор</b>: {'включён' if d['cloner_on'] else 'выключен'} · лотов: {len(d['clone_lots'])} · "
                     f"цель {d['target']} строк",
                 f"\n🕒 Последняя проверка: {fmt_dt(d['last_run'])}"]
        if d["log"]:
            lines += ["", "🕓 <b>Журнал:</b>"] + [f"{fmt_dt(i['t'])} {esc(i['text'])}" for i in d["log"][:5]]
        rows = [[("Auto Stock: " + ("🟢" if d["stock_on"] else "🔴"), ("toggle", "stock_on"))],
                [("🔻 Мин", ("ask_num", "min")), ("🔺 Макс", ("ask_num", "max"))],
                [("🎮 Выбрать категории", ("games",))],
                [("🧬 Клонатор автовыдачи (Секретки)", ("cloner",))],
                [("🔄 Проверить сейчас", ("run",))]]
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_toggle(self, chat, mid, key, back="home"):
        if key not in ("stock_on", "cloner_on"):
            raise UserError("Неизвестная настройка.")
        self.plugin.store.change(lambda d: d.update({key: not d[key]}))
        getattr(self, "on_" + back)(chat, mid)

    def on_ask_num(self, chat, mid, key):
        prompts = {"min": "🔻 Минимальный порог наличия (если меньше — пополнить):",
                   "max": "🔺 Верхний предел наличия:", "target": "🎯 Сколько строк автовыдачи поддерживать? (1–1000)"}
        if key not in prompts:
            raise UserError("Неизвестное поле.")
        self.ask(chat, prompts[key], ("num", key), mid)

    def in_num(self, chat, text, key):
        if key == "target":
            value = parse_int(text, 1, 1000, "Строк")
        else:
            value = parse_int(text, 1, 1_000_000, "Число")
        d = self.plugin.store.snapshot()
        if key == "min" and value > d["max"] or key == "max" and value < d["min"]:
            raise UserError("Мин не может быть больше Макс.")
        self.plugin.store.set(**{key: value})
        if key == "target":
            self.on_cloner(chat, None)
        else:
            self.on_home(chat)

    def on_games(self, chat, mid):
        self.send(chat, "⏳ Загружаю ваши разделы…", mid=mid)
        subs = my_subcategories(self.cardinal, extra_ids=self.plugin.store["subs"], from_sales=True)
        games = {}
        for sub in subs:
            games.setdefault(sub.category.id, [sub.category.name, []])[1].append(sub)
        self.games[chat] = games
        selected = set(self.plugin.store["subs"])
        rows = [[(f"🎮 {name} ({sum(1 for s in items if s.id in selected)}/{len(items)})", ("game", gid))]
                for gid, (name, items) in sorted(games.items(), key=lambda kv: kv[1][0].lower())[:40]]
        rows.append([("◀️ Назад", ("home",))])
        self.send(chat, "🎮 <b>Выберите игру</b> (выбрано/всего разделов):", rows, mid)

    def on_game(self, chat, mid, gid):
        games = self.games.get(chat)
        if not games or gid not in games:
            raise UserError("Список устарел — откройте выбор заново.")
        name, subs = games[gid]
        selected = set(self.plugin.store["subs"])
        rows = [[(("✅ " if s.id in selected else "⬜ ") + s.name, ("sub", gid, s.id))] for s in subs]
        rows.append([("◀️ К играм", ("games",)), ("🏠 Меню", ("home",))])
        self.send(chat, f"🎮 <b>{esc(name)}</b>: нажмите на раздел, чтобы включить/выключить автопополнение.", rows, mid)

    def on_sub(self, chat, mid, gid, sub_id):
        games = self.games.get(chat) or {}
        sub = next((s for s in games.get(gid, ["", []])[1] if s.id == sub_id), None)

        def toggle(d):
            if sub_id in d["subs"]:
                d["subs"].remove(sub_id)
            else:
                d["subs"].append(sub_id)
                if sub is not None:
                    d["sub_names"][str(sub_id)] = f"{sub.category.name} › {sub.name}"

        self.plugin.store.change(toggle)
        self.on_game(chat, mid, gid)

    def on_cloner(self, chat, mid=None):
        d = self.plugin.store.snapshot()
        lines = [f"🧬 <b>Клонатор автовыдачи</b>: {'🟢 включён' if d['cloner_on'] else '🔴 выключен'}",
                 f"🎯 Поддерживать: <b>{d['target']}</b> строк", ""]
        rows = [[("Клонатор: " + ("🟢" if d["cloner_on"] else "🔴"), ("toggle", "cloner_on", "cloner")),
                 (f"🎯 Поддерживать {d['target']} строк", ("ask_num", "target"))]]
        for key, info in d["clone_lots"].items():
            lines.append(f"• {esc(info.get('name', key))} — сейчас {info.get('count', '?')} строк")
            rows.append([(f"❌ {info.get('name', key)[:30]}", ("clone_del", key))])
        if not d["clone_lots"]:
            lines.append("Лотов пока нет.")
        rows.append([("➕ Добавить лот в клонатор", ("clone_add",))])
        rows.append([("◀️ Назад", ("home",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_clone_add(self, chat, mid):
        self.ask(chat, "➕ Отправьте ID лота (или ссылку). В его автовыдаче уже должна быть хотя бы 1 строка.",
                 ("clone",), mid)

    def in_clone(self, chat, text):
        lot_id = parse_lot_id(text)
        try:
            fields = self.cardinal.account.get_lot_fields(lot_id)
        except Exception:
            raise UserError("Лот не найден на FunPay или он не ваш.") from None
        secrets = lot_secrets(fields)
        if not secrets:
            raise UserError("В лоте нет ни одной строки автовыдачи — бот не может клонировать пустоту. Добавьте строку "
                            "на FunPay и попробуйте снова.")
        self.plugin.store.change(lambda d: d["clone_lots"].__setitem__(str(lot_id), {"name": lot_title(fields),
                                                                                      "count": len(secrets)}))
        self.plugin.wake.set()
        self.on_cloner(chat)

    def on_clone_del(self, chat, mid, key):
        self.plugin.store.change(lambda d: d["clone_lots"].pop(key, None))
        self.on_cloner(chat, mid)

    def on_run(self, chat, mid):
        self.send(chat, "⏳ Проверяю лоты…", mid=mid)

        def run():
            result = self.plugin.run_once()
            if result is None:
                self.send(chat, "⏳ Проверка уже идёт.", [[("🏠 Меню", ("home",))]], mid)
                return
            self.send(chat, f"✅ Готово. Пополнено лотов: {result[0]} · клонировано: {result[1]}",
                      [[("🏠 Меню", ("home",))]], mid)

        if getattr(self.plugin, "sync", False):
            run()
        else:
            threading.Thread(target=run, name="AutoStockClonerRun", daemon=True).start()


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = StockCloner(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = StockPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


def start(cardinal, *args):
    if PLUGIN is not None:
        PLUGIN.start()


def stop(cardinal=None, *args):
    if PLUGIN is not None:
        PLUGIN.stop_event.set()
        PLUGIN.wake.set()


BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [start]
BIND_TO_PRE_STOP = [stop]
BIND_TO_DELETE = None
