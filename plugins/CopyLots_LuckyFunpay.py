"""
📦 Copy Lots Manager — полный контроль над витриной. Плагин для FunPay Cardinal от LuckyFunpay.

Переносит лоты на другой аккаунт FunPay по Golden Key, сохраняет их в файл-бэкап (.json) и
создаёт заново из файла. Каждый лот подгоняется под форму нужного раздела: лишние поля
отбрасываются, значения переводятся, обязательные поля дозаполняются.

ИНСТРУКЦИЯ (полная версия — в боте: /copy_lots → 📖 Инструкция)
⚠️ Сначала включите плагин: /menu → Система и конфиги → Плагины → Copy Lots Manager → Включить,
   затем ОБЯЗАТЕЛЬНО /restart.
1. /copy_lots → «🚀 Копировать на другой аккаунт» → отправьте Golden Key целевого аккаунта (32 символа).
2. «📥 Бэкап (Экспорт)» → выберите категории → «🚀 Начать бэкап» → бот пришлёт .json.
3. «📤 Импорт из файла» → отправьте .json документом — лоты создадутся на текущем аккаунте.
Переключатели: 🔐 Секреты/Автовыдача, 🧩 Дозаполнять поля, 🇬🇧 Дублировать название в EN,
📝 Лимит строк описания, 📊 Отчёт по полям, ♻️ Сбросить кэш разделов.

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
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

NAME = "Copy Lots Manager"
VERSION = "1.0.0"
DESCRIPTION = ("📦 Перенос лотов FunPay на другой аккаунт по Golden Key, бэкап в .json и восстановление из файла "
               "с автоматической подгонкой полей под форму раздела. Команда: /copy_lots")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "e2a7c5f9-3d84-4e16-8b3a-0c9f6e1d4b75"
SETTINGS_PAGE = True
SLUG = "CopyLots"
PREFIX = "lfcopy:"

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


DEFAULTS = {"secrets": False, "fill": True, "dup_title": True, "lines": 12, "report": True, "known_subs": []}
SCHEMA_TTL = 14 * 86400
SKIP_KEYS = {"csrf_token", "offer_id", "node_id", "location", "deleted", "form_created_at"}
STANDARD = {"price", "amount", "active", "deactivate_after_sale", "fields[images]", "fields[summary][ru]",
            "fields[summary][en]", "fields[desc][ru]", "fields[desc][en]", "fields[payment_msg][ru]",
            "fields[payment_msg][en]"}
SECRET_KEYS = {"secrets", "auto_delivery"}
PAGE = 8


def make_account(golden_key, source):
    """Создаёт и авторизует второй аккаунт FunPay."""
    from FunPayAPI import Account
    return Account(golden_key, user_agent=getattr(source, "user_agent", None),
                   proxy=getattr(source, "proxy", None)).get()


def parse_form(page: str) -> dict:
    """Читает форму раздела offerEdit: значения по умолчанию, списки и подписи полей."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page, "lxml")
    form = soup.find("form", class_="form-offer-editor")
    if form is None:
        raise ValueError("форма не найдена")
    schema = {"defaults": {}, "selects": {}, "labels": {}, "t": time.time()}

    def group_label(tag):
        group = tag.find_parent(class_="form-group")
        label = group.find("label") if group else None
        hidden = bool(group and "hidden" in (group.get("class") or []))
        return (label.get_text(" ", strip=True) if label else ""), hidden

    for field in form.find_all("input"):
        name = field.get("name")
        if not name:
            continue
        if field.get("type") == "checkbox":
            schema["defaults"][name] = "on" if field.has_attr("checked") else ""
        else:
            schema["defaults"][name] = field.get("value") or ""
        schema["labels"][name] = group_label(field)[0]
    for field in form.find_all("textarea"):
        name = field.get("name")
        if name:
            schema["defaults"][name] = field.text or ""
            schema["labels"][name] = group_label(field)[0]
    for field in form.find_all("select"):
        name = field.get("name")
        if not name:
            continue
        label, hidden = group_label(field)
        options = [[o.get("value", ""), o.get_text(" ", strip=True)] for o in field.find_all("option")]
        chosen = field.find("option", selected=True)
        schema["selects"][name] = {"label": label or name, "options": options, "hidden": hidden}
        schema["defaults"][name] = chosen.get("value", "") if chosen else ""
        schema["labels"][name] = label or name
    return schema


def limit_lines(text: str, limit: int) -> str:
    if not text or not limit:
        return text
    lines = text.replace("\r", "").split("\n")
    if len(lines) <= limit:
        return text
    compact = []
    for line in lines:
        if line.strip() or (compact and compact[-1].strip()):
            compact.append(line)
    lines = compact
    if len(lines) <= limit:
        return "\n".join(lines)
    head, tail = lines[:limit - 1], [line.strip() for line in lines[limit - 1:] if line.strip()]
    return "\n".join(head + [" • ".join(tail)])


class CopyLots:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("settings.json", DEFAULTS)
        self.schemas = JsonStore("schemas.json", {"nodes": {}})
        self.ui = None
        self.job = None
        self.sync = False

    # --- формы разделов
    def schema(self, account, node):
        cached = self.schemas["nodes"].get(str(node))
        if cached and time.time() - cached.get("t", 0) < SCHEMA_TTL:
            return cached
        try:
            response = account.method("get", f"lots/offerEdit?node={node}", {"accept": "*/*"}, {},
                                      raise_not_200=True)
            content = response.content
            schema = parse_form(content.decode() if isinstance(content, bytes) else response.text)
        except Exception as error:
            LOG.warning("%s: форма раздела %s не прочитана: %s", NAME, node, error)
            return None
        self.schemas.change(lambda d: d["nodes"].__setitem__(str(node), schema))
        return schema

    def schema_count(self) -> int:
        now = time.time()
        return sum(1 for s in self.schemas["nodes"].values() if now - s.get("t", 0) < SCHEMA_TTL)

    # --- чтение лотов
    def inventory(self, account=None) -> dict:
        account = account or self.cardinal.account
        subs = my_subcategories(self.cardinal, extra_ids=self.store["known_subs"], from_sales=True)
        result = {}
        for sub in subs:
            try:
                lots = account.get_my_subcategory_lots(sub.id)
            except Exception:
                continue
            if lots:
                result[sub.id] = (sub, lots)
            if not self.sync:
                time.sleep(0.3)
        self.store.set(known_subs=sorted(result))
        return result

    def export_lot(self, account, lot_id) -> dict:
        fields = account.get_lot_fields(lot_id)
        raw = dict(lot_fields_raw(fields))
        node = lot_node(fields)
        if not self.store["secrets"]:
            for key in SECRET_KEYS:
                raw.pop(key, None)
        for key in ("csrf_token", "form_created_at"):
            raw.pop(key, None)
        item = {"id": lot_id, "node_id": node, "title": lot_title(fields), "fields": raw, "labels": {},
                "field_labels": {}}
        schema = self.schema(account, node) if node else None
        if schema:
            for key, value in raw.items():
                if key in schema["selects"]:
                    text = next((t for v, t in schema["selects"][key]["options"] if v == value), None)
                    if text:
                        item["labels"][key] = text
                if schema["labels"].get(key):
                    item["field_labels"][key] = schema["labels"][key]
        return item

    # --- подгонка под форму
    def adapt(self, item: dict, schema, report: Counter) -> dict:
        settings = self.store.snapshot()
        src = dict(item.get("fields") or {})
        node = item.get("node_id") or src.get("node_id")
        labels = item.get("labels") or {}
        field_labels = item.get("field_labels") or {}
        if schema is None:
            report["Не удалось прочитать форму раздела — поля отправлены как есть"] += 1
            result = {k: v for k, v in src.items() if k not in SKIP_KEYS}
        else:
            result = {k: v for k, v in schema["defaults"].items() if k not in SKIP_KEYS}
            for key in STANDARD:
                result.pop(key, None)
            label_to_key = {}
            for key, label in schema["labels"].items():
                if key.startswith("fields[") and label:
                    label_to_key.setdefault(label.casefold(), []).append(key)
            for key, value in src.items():
                if key in SKIP_KEYS:
                    continue
                if key in STANDARD or key in SECRET_KEYS:
                    result[key] = value
                    continue
                target = key
                if key not in schema["defaults"]:
                    label = (field_labels.get(key) or "").casefold()
                    candidates = [k for k in label_to_key.get(label, []) if k not in src]
                    if label and candidates:
                        target = candidates[0]
                        report[f"«{field_labels.get(key)}» перенесено из «{key}» в «{target}»"] += 1
                    else:
                        if value not in ("", None):
                            report[f"поле «{field_labels.get(key) or key}» отсутствует в этом разделе — пропущено"] += 1
                        continue
                if target in schema["selects"]:
                    options = schema["selects"][target]["options"]
                    values = {v for v, _ in options}
                    if value in values:
                        result[target] = value
                        continue
                    wanted = (labels.get(key) or str(value)).casefold()
                    match = next((v for v, t in options if t.casefold() == wanted and v), None)
                    if match is not None:
                        result[target] = match
                        report[f"поле «{schema['selects'][target]['label']}» восстановлено как «{labels.get(key) or value}»"] += 1
                    else:
                        result[target] = ""
                        if value not in ("", None):
                            report[f"значение «{labels.get(key) or value}» не подходит полю «{schema['selects'][target]['label']}»"] += 1
                else:
                    result[target] = value
            if settings["fill"]:
                for key, info in schema["selects"].items():
                    if info["hidden"] or result.get(key):
                        continue
                    first = next(([v, t] for v, t in info["options"] if v), None)
                    if first:
                        result[key] = first[0]
                        report[f"поле «{info['label']}» заполнено по умолчанию: «{first[1]}»"] += 1
        if not settings["secrets"]:
            for key in SECRET_KEYS:
                result.pop(key, None)
        ru, en = result.get("fields[summary][ru]", ""), result.get("fields[summary][en]", "")
        if settings["dup_title"]:
            if ru and not en:
                result["fields[summary][en]"] = ru
                report["англ. название лота было пустым — продублировано русским"] += 1
            elif en and not ru:
                result["fields[summary][ru]"] = en
                report["рус. название лота было пустым — продублировано английским"] += 1
        for key in ("fields[desc][ru]", "fields[desc][en]"):
            if result.get(key):
                result[key] = limit_lines(result[key], settings["lines"])
        if not str(result.get("price") or "").strip():
            result["price"] = "10"
        if "amount" in result and not str(result.get("amount") or "").strip() and not result.get("secrets"):
            result["amount"] = "1"
        result["offer_id"] = "0"
        result["node_id"] = str(node)
        return result

    def create(self, account, fields: dict, stop=None):
        from FunPayAPI import types as fp_types
        error_text = ""
        for attempt in range(3):
            try:
                lot = fp_types.LotFields(0, dict(fields))
                account.save_lot(lot)
                return True, ""
            except Exception as error:
                error_text = str(getattr(error, "error_message", "") or "") or type(error).__name__
                errors = getattr(error, "errors", None)
                if errors:
                    error_text += " " + "; ".join(f"{k}: {v}" for k, v in dict(errors).items())
                if "LotSavingError" in type(error).__name__:
                    return False, error_text[:250]
                if attempt == 1:
                    try:
                        account.get()
                    except Exception:
                        pass
                if wait(stop, 2 + attempt * 2):
                    break
        return False, error_text[:250]

    def create_all(self, job, account, items, progress, base=0.0, part=1.0) -> Counter:
        report = Counter()
        for i, item in enumerate(items):
            if job.stop.is_set():
                break
            node = item.get("node_id") or (item.get("fields") or {}).get("node_id")
            if not node:
                job.errors += 1
                job.last_error = f"{item.get('title', '?')}: не указан раздел (node_id)"
                continue
            schema = self.schema(account, node)
            fields = self.adapt(item, schema, report)
            ok, error = self.create(account, fields, job.stop)
            if ok:
                job.ok += 1
            else:
                job.errors += 1
                job.last_error = f"{(item.get('title') or '')[:40]}: {error}"
            job.done = i + 1
            progress(base + part * (i + 1) / max(1, len(items)))
            wait(job.stop, random.uniform(1.5, 3.0))
        return report

    def report_file(self, report: Counter):
        if not report or not self.store["report"] or not self.ui:
            return None
        lines = [f"[{count} лот(ов)] {text}" for text, count in report.most_common()]
        return ("🧩 Отчёт по полям лотов\n\n" + "\n".join(lines)).encode("utf-8")


def normalize_items(data) -> list:
    if isinstance(data, dict):
        data = data.get("lots") or data.get("items") or data.get("offers") or []
    if not isinstance(data, list):
        raise UserError("Некорректный формат JSON: нужен список лотов.")
    items = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        if isinstance(raw.get("fields"), dict):
            item = dict(raw)
        else:
            fields = {k: ("" if v is None else str(v)) for k, v in raw.items() if not isinstance(v, (dict, list))}
            item = {"fields": fields, "node_id": raw.get("node_id") or raw.get("node"),
                    "title": raw.get("fields[summary][ru]") or raw.get("title") or ""}
        item["fields"] = {k: ("" if v is None else str(v)) for k, v in item["fields"].items()}
        item.setdefault("node_id", item["fields"].get("node_id"))
        item.setdefault("title", item["fields"].get("fields[summary][ru]", ""))
        items.append(item)
    if not items:
        raise UserError("В файле нет лотов.")
    return items


class CopyPanel(Panel):
    COMMAND = "copy_lots"
    ICON = "📦"
    DOC_INPUTS = ("import",)
    GUIDE = [
        ("⚠️ Включение", "Плагин не работает, пока он не включён:\n1. <code>/menu</code> → Система и конфиги → Плагины → "
         "<b>Copy Lots Manager</b> → Включить.\n2. Обязательно <code>/restart</code>.\n3. После запуска — "
         "<code>/copy_lots</code> или кнопка настроек плагина.\n\nДоступ только у авторизованных пользователей бота."),
        ("🚀 Зачем", "1. <b>Мультиаккаунтинг</b> — скопировать все лоты на второй магазин за минуты.\n2. <b>Бэкапы</b> — "
         "аккаунт заблокирован или лоты удалены? Восстановите ассортимент на любом аккаунте.\n3. <b>Массовое "
         "редактирование</b> — выгрузить в файл, поправить цены/описания в редакторе, загрузить обратно.\n\n"
         "Плагин подгоняет каждый лот под форму раздела: лишние поля отбрасывает, значения переводит, обязательные "
         "поля дозаполняет."),
        ("🚀 Копирование", "«🚀 Копировать на другой аккаунт» → отправьте <b>Golden Key целевого аккаунта</b> (32 символа). "
         "Этап 1/2 — скачивание лотов с текущего аккаунта, этап 2/2 — создание на целевом. Видно процент, успешно, "
         "ошибки и последнюю ошибку.\n\n<b>Где взять Golden Key:</b> войдите в нужный аккаунт на funpay.com → F12 → "
         "Application (Firefox: Storage) → Cookies → funpay.com → <code>golden_key</code>.\n⚠️ Ключ даёт полный доступ к "
         "аккаунту — бот удалит ваше сообщение с ключом. Копировать на тот же аккаунт нельзя. Скорость — 3–5 сек на "
         "лот (100 лотов ≈ 5–8 минут). Не перезапускайте бота во время переноса."),
        ("📥 Бэкап", "«📥 Бэкап (Экспорт)» → экран выбора категорий: ✅/⬜ категория (число лотов), ◀️ ▶️ — листать (по 8), "
         "«✅ Выбрать все», «⬜ Снять все», «🚀 Начать бэкап (N категорий)». Придёт файл "
         "<code>funpay_lots_backup_&lt;число&gt;.json</code>: названия, описания, цены, количество, раздел, параметры, "
         "сообщение покупателю, изображения и настройки.\n🔐 Ключи автовыдачи попадают в файл, только если "
         "переключатель «Секреты/Автовыдача» включён ДО бэкапа. Храните такой файл в надёжном месте."),
        ("📤 Импорт", "«📤 Импорт из файла» → отправьте .json документом. Подходят файлы этого плагина и других "
         "клонаторов. Импорт всегда создаёт НОВЫЕ лоты (дубликаты на том же аккаунте удалите вручную). Нет цены или "
         "количества — будет 10 и 1. Ключи автовыдачи создаются только при включённом «🔐 Секреты»."),
        ("⚙️ Переключатели", "🔐 <b>Секреты/Автовыдача</b> (🔴) — переносить товары автовыдачи. ⚠️ Не держите одни ключи на "
         "двух аккаунтах.\n🧩 <b>Дозаполнять поля</b> (🟢) — пустое обязательное поле-список получит первое значение.\n"
         "🇬🇧 <b>Дублировать название в EN</b> (🟢) — пустое англ. название = русское (и наоборот).\n📝 <b>Лимит строк</b> "
         "(12, 0 = ∞) — лишние строки описания сливаются в последнюю через « • », текст не теряется.\n📊 <b>Отчёт по полям</b> "
         "(🟢) — файл «🧩 Отчёт по полям лотов».\n♻️ <b>Сбросить кэш разделов</b> — формы разделов хранятся до 14 дней."),
        ("❓ FAQ", "<b>«Подождите, выполняется другая операция!»</b> — дождитесь окончания или нажмите «🚫 Отменить».\n"
         "<b>«Неверный формат токена»</b> — Golden Key ровно 32 символа.\n<b>«Не удалось авторизоваться во втором "
         "аккаунте»</b> — ключ устарел.\n<b>Часть лотов не создалась</b> — смотрите «Последняя ошибка», включите "
         "дозаполнение и дублирование названия, сбросьте кэш разделов, проверьте лимит строк.\n<b>Только обычные "
         "лоты</b> — разделы игровой валюты пропускаются.\nПосле любого переноса проверьте лоты на FunPay."),
    ]

    def __init__(self, plugin):
        super().__init__(plugin)
        self.backup = {}

    def busy(self):
        job = self.plugin.job
        return job is not None and not getattr(job, "finished", False)

    def guard(self):
        if self.busy():
            raise UserError("Подождите, выполняется другая операция!")

    def on_home(self, chat, mid=None, *args):
        d = self.plugin.store.snapshot()
        lines = [f"📦 <b>{esc(NAME)}</b> v{VERSION}", "",
                 f"🔐 Секреты/автовыдача: {'🟢' if d['secrets'] else '🔴'}",
                 f"🧩 Дозаполнять поля раздела: {'🟢' if d['fill'] else '🔴'}",
                 f"🇬🇧 Дублировать название в EN: {'🟢' if d['dup_title'] else '🔴'}",
                 f"📝 Лимит строк описания: {d['lines'] or '∞'}",
                 f"📊 Отчёт по полям: {'🟢' if d['report'] else '🔴'}",
                 f"Схем разделов в кэше: {self.plugin.schema_count()}"]
        if self.busy():
            lines.append("\n⏳ Выполняется операция…")
        rows = [[("🚀 Копировать на другой аккаунт", ("copy",))],
                [("📥 Бэкап (Экспорт)", ("backup_open",)), ("📤 Импорт из файла", ("import",))],
                [("🔐 Секреты/Автовыдача: " + ("🟢" if d["secrets"] else "🔴"), ("toggle", "secrets"))],
                [("🧩 Дозаполнять поля раздела: " + ("🟢" if d["fill"] else "🔴"), ("toggle", "fill"))],
                [("🇬🇧 Дублировать название в EN: " + ("🟢" if d["dup_title"] else "🔴"), ("toggle", "dup_title"))],
                [("📝 Лимит строк описания: " + str(d["lines"] or "∞"), ("lines",)),
                 ("📊 Отчёт: " + ("🟢" if d["report"] else "🔴"), ("toggle", "report"))],
                [("♻️ Сбросить кэш разделов", ("reset_cache",))]]
        rows += self.footer_rows()
        rows.append([("⬅️ Назад в плагины", ("plugins",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_toggle(self, chat, mid, key):
        self.guard()
        if key not in ("secrets", "fill", "dup_title", "report"):
            raise UserError("Неизвестная настройка.")
        self.plugin.store.change(lambda d: d.update({key: not d[key]}))
        self.on_home(chat, mid)

    def on_lines(self, chat, mid):
        self.guard()
        self.ask(chat, "📝 Отправьте максимальное число строк описания (0 — без лимита).", ("lines",), mid)

    def in_lines(self, chat, text):
        if not re.fullmatch(r"\s*\d{1,4}\s*", text):
            raise UserError("Нужно целое число.")
        value = parse_int(text, 0, 1000, "Лимит")
        self.plugin.store.set(lines=value)
        self.send(chat, f"✅ Лимит строк описания: {value if value else 'без лимита'}")
        self.on_home(chat)

    def on_reset_cache(self, chat, mid):
        self.guard()
        self.plugin.schemas.change(lambda d: d["nodes"].clear())
        self.answer(self.call, "Кэш схем разделов очищен", alert=True)
        self.on_home(chat, mid)

    # --- процесс
    def start(self, chat, mid, title, worker):
        self.guard()
        job = Job(title)
        job.finished = False
        self.plugin.job = job
        msg = self.send(chat, f"⏳ <b>{esc(title)}</b>", [[("🚫 Отменить процесс", ("cancel_job",))]], mid)
        mid = getattr(msg, "message_id", mid)
        last = [0.0]

        def progress(part, stage=""):
            if time.time() - last[0] < 2 and part < 1:
                return
            last[0] = time.time()
            text = (f"⏳ <b>{esc(title)}</b>{(' — ' + stage) if stage else ''}\n{int(part * 100)}%\n"
                    f"✅ Успешно: {job.ok} · ❌ Ошибок: {job.errors}")
            if job.last_error:
                text += f"\nПоследняя ошибка: <i>{esc(job.last_error)}</i>"
            self.send(chat, text, [[("🚫 Отменить процесс", ("cancel_job",))]], mid)

        def run():
            try:
                worker(job, progress, mid)
            except UserError as error:
                self.send(chat, "❌ " + esc(error), [[("🏠 Меню", ("home",))]], mid)
            except Exception:
                LOG.exception("%s: ошибка операции", NAME)
                self.send(chat, "❌ Ошибка, подробности в логе Cardinal.", [[("🏠 Меню", ("home",))]], mid)
            finally:
                job.finished = True
                self.on_home(chat)

        if self.plugin.sync:
            run()
        else:
            threading.Thread(target=run, name="CopyLotsJob", daemon=True).start()

    def on_cancel_job(self, chat, mid):
        job = self.plugin.job
        if job is not None and not getattr(job, "finished", False):
            job.stop.set()
            self.send(chat, "Отменяю текущий процесс…")

    def finish(self, chat, job, title, report):
        head = "⛔️ Процесс отменён" if job.stop.is_set() else "✅ Готово"
        self.send(chat, f"{head}: <b>{esc(title)}</b>\nСоздано лотов: <b>{job.ok}</b> · ❌ Ошибок: {job.errors}"
                        + (f"\nПоследняя ошибка: <i>{esc(job.last_error)}</i>" if job.last_error else ""))
        data = self.plugin.report_file(report)
        if data and not job.stop.is_set():
            self.send_file(chat, "fields_report.txt", data, caption="🧩 Отчёт по полям лотов")

    # --- копирование
    def on_copy(self, chat, mid):
        self.guard()
        self.ask(chat, "🔑 Отправьте <b>Golden Key</b> аккаунта, НА КОТОРЫЙ нужно перенести лоты (32 символа).\n"
                       "Сообщение с ключом будет удалено.", ("golden",), mid)

    def in_golden(self, chat, text):
        try:
            self.bot.delete_message(chat, self.message.message_id)
        except Exception:
            pass
        key = text.strip()
        if len(key) != 32 or not re.fullmatch(r"[A-Za-z0-9]{32}", key):
            self.send(chat, "❌ Неверный формат токена. Golden Key — ровно 32 символа.", [[("🏠 Меню", ("home",))]])
            return
        source = self.cardinal.account
        if key == getattr(source, "golden_key", None):
            raise UserError("Нельзя копировать на самого себя.")
        plugin = self.plugin

        def worker(job, progress, mid):
            try:
                target = make_account(key, source)
            except Exception:
                raise UserError("Не удалось авторизоваться во втором аккаунте. Возьмите свежий Golden Key.") from None
            if getattr(target, "id", None) == getattr(source, "id", None):
                raise UserError("Нельзя копировать на самого себя.")
            progress(0.0, "Этап 1/2: скачивание лотов")
            inventory = plugin.inventory(source)
            ids = [lot.id for _, lots in inventory.values() for lot in lots]
            if not ids:
                raise UserError("Стандартных лотов для копирования не найдено.")
            items = []
            for i, lot_id in enumerate(ids):
                if job.stop.is_set():
                    break
                try:
                    items.append(plugin.export_lot(source, lot_id))
                except Exception as error:
                    job.errors += 1
                    job.last_error = f"{lot_id}: не скачан ({type(error).__name__})"
                progress(0.5 * (i + 1) / len(ids), "Этап 1/2: скачивание лотов")
                wait(job.stop, random.uniform(0.5, 1.0))
            job.total = len(items)
            report = plugin.create_all(job, target, items, lambda part: progress(part, "Этап 2/2: создание лотов"),
                                       0.5, 0.5)
            self.finish(chat, job, "Копирование на другой аккаунт", report)

        self.start(chat, None, "Копирование на другой аккаунт", worker)

    # --- бэкап
    def on_backup_open(self, chat, mid):
        self.backup.pop(chat, None)
        self.on_backup(chat, mid, 0)

    def on_backup(self, chat, mid, page=0):
        self.guard()
        state = self.backup.get(chat)
        if state is None or time.time() - state["t"] > 900:
            self.send(chat, "⏳ Загружаю категории вашего аккаунта…", mid=mid)
            inventory = self.plugin.inventory()
            if not inventory:
                self.send(chat, "ℹ️ Стандартных лотов ни в одной категории не найдено.", [[("⬅️ Назад в меню", ("home",))]], mid)
                return
            state = self.backup[chat] = {"t": time.time(), "inv": inventory, "selected": set(inventory)}
        inv, selected = state["inv"], state["selected"]
        subs = sorted((v[0] for v in inv.values()), key=lambda s: (s.category.name.lower(), s.name.lower()))
        pages = max(1, (len(subs) + PAGE - 1) // PAGE)
        page = max(0, min(int(page), pages - 1))
        rows = []
        for sub in subs[page * PAGE:(page + 1) * PAGE]:
            name = f"{sub.category.name} › {sub.name}"
            if len(name) > 38:
                name = name[:37] + "…"
            rows.append([(("✅ " if sub.id in selected else "⬜ ") + f"{name} ({len(inv[sub.id][1])})",
                          ("bk_toggle", sub.id, page))])
        if pages > 1:
            rows.append([("◀️", ("backup", max(0, page - 1))), (f"{page + 1}/{pages}", ("noop",)),
                         ("▶️", ("backup", min(pages - 1, page + 1)))])
        rows.append([("✅ Выбрать все", ("bk_all", page)), ("⬜ Снять все", ("bk_none", page))])
        rows.append([(f"🚀 Начать бэкап ({len(selected)} категорий)", ("bk_go",))])
        rows.append([("⬅️ Назад в меню", ("home",))])
        count = sum(len(inv[s][1]) for s in selected if s in inv)
        self.send(chat, f"📥 <b>Выбор категорий для бэкапа</b>\nВыбрано категорий: {len(selected)} · лотов: {count}",
                  rows, mid)

    def bk_state(self, chat):
        state = self.backup.get(chat)
        if state is None or time.time() - state["t"] > 900:
            self.backup.pop(chat, None)
            raise UserError("Список категорий устарел — откройте бэкап заново.")
        return state

    def on_bk_toggle(self, chat, mid, sub_id, page):
        self.bk_state(chat)["selected"].symmetric_difference_update({sub_id})
        self.on_backup(chat, mid, page)

    def on_bk_all(self, chat, mid, page):
        state = self.bk_state(chat)
        state["selected"] = set(state["inv"])
        self.on_backup(chat, mid, page)

    def on_bk_none(self, chat, mid, page):
        self.bk_state(chat)["selected"] = set()
        self.on_backup(chat, mid, page)

    def on_bk_go(self, chat, mid):
        state = self.bk_state(chat)
        if not state["selected"]:
            raise UserError("Выберите хотя бы одну категорию!")
        self.backup.pop(chat, None)
        plugin = self.plugin
        ids = [(sid, lot.id) for sid in state["selected"] if sid in state["inv"] for lot in state["inv"][sid][1]]
        if not ids:
            raise UserError("В выбранных категориях лотов не найдено.")
        names = [f"{state['inv'][sid][0].category.name} › {state['inv'][sid][0].name}" for sid in state["selected"]
                 if sid in state["inv"]]

        def worker(job, progress, mid):
            items = []
            job.total = len(ids)
            for i, (sid, lot_id) in enumerate(ids):
                if job.stop.is_set():
                    break
                try:
                    items.append(plugin.export_lot(self.cardinal.account, lot_id))
                    job.ok += 1
                except Exception as error:
                    job.errors += 1
                    job.last_error = f"{lot_id}: {type(error).__name__}"
                job.done = i + 1
                progress((i + 1) / len(ids), "скачивание данных")
                wait(job.stop, random.uniform(0.5, 1.0))
            if job.stop.is_set():
                self.send(chat, f"⛔️ Бэкап отменён. Скачано лотов: {len(items)}.")
                return
            data = json.dumps({"plugin": NAME, "version": VERSION, "created": int(time.time()),
                               "secrets": plugin.store["secrets"], "lots": items}, ensure_ascii=False, indent=1)
            cats = ", ".join(names) if len(names) <= 5 else f"{len(names)} категорий"
            self.send_file(chat, f"funpay_lots_backup_{int(time.time())}.json", data.encode("utf-8"),
                           caption=f"📥 Бэкап: {len(items)} лот(ов)\nКатегории: {cats}"[:1000])

        self.start(chat, mid, "Бэкап лотов", worker)

    # --- импорт
    def on_import(self, chat, mid):
        self.guard()
        self.ask(chat, "📤 Отправьте файл бэкапа <b>.json</b> документом.", ("import",), mid, doc=True)

    def in_import(self, chat, document):
        name = getattr(document, "file_name", "") or ""
        if not name.lower().endswith(".json"):
            raise UserError("Нужен файл с расширением .json")
        if (getattr(document, "file_size", 0) or 0) > 20 * 1024 * 1024:
            raise UserError("Файл слишком большой (максимум 20 МБ).")
        info = self.bot.get_file(document.file_id)
        raw = self.bot.download_file(info.file_path)
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except Exception:
            raise UserError("Некорректный формат JSON") from None
        items = normalize_items(data)
        plugin = self.plugin
        self.send(chat, f"📤 В файле лотов: <b>{len(items)}</b>. Начинаю импорт…")

        def worker(job, progress, mid):
            job.total = len(items)
            report = plugin.create_all(job, self.cardinal.account, items, lambda part: progress(part, "создание"))
            self.finish(chat, job, "Импорт из файла", report)

        self.start(chat, None, "Импорт из файла", worker)


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = CopyLots(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = CopyPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = None
