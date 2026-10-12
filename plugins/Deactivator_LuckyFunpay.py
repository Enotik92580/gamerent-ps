"""
🧩 Деактиватор лотов (Lot Deactivator) — массовое выключение и включение лотов. Плагин для FunPay Cardinal от LuckyFunpay.

Выключает все лоты (или лоты одной категории), когда вы уходите спать или в отпуск, запоминает,
что именно выключено, и так же массово включает обратно. Поддерживает таблицы игровых валют,
перепроверяет реальное состояние лота и не теряет ID при ошибках.

ИНСТРУКЦИЯ (полная версия — в боте: /deactivator → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. /deactivator → «🔴 Деактивировать все» или «🔴 По категории».
3. Когда готовы вернуться — «🟢 Активировать все» или «🟢 По категории».
4. «📋 Список» и «📎 Выгрузить файлом» — что сейчас «на паузе». «⛔️ Остановить» — прервать операцию.
Если в Cardinal включено «Автовосстановление лотов» — выключите его на время паузы.

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

NAME = "Деактиватор лотов"
VERSION = "1.0.0"
DESCRIPTION = ("🧩 Массово выключает лоты FunPay (все или по категории, включая таблицы валют), запоминает их и "
               "включает обратно с перепроверкой. Список, выгрузка в .txt, кнопка «Стоп». Панель: /deactivator")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "a9b4e6c1-5d27-4f80-9c3e-1b7f2d8a6e53"
SETTINGS_PAGE = True
SLUG = "Deactivator"
PREFIX = "lfdeact:"

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

# ═══════════════════════════ LuckyFunpay lot switcher ═══════════════════════════
def target_key(target: dict) -> str:
    return f"chip:{target['sub']}:{target['offer']}" if target["kind"] == "chip" else f"lot:{target['id']}"


def collect_active(cardinal, sub_ids=None, include_chips=True) -> list:
    """Активные лоты и активные строки таблиц валют с профиля продавца."""
    profile = fresh_profile(cardinal)
    targets = []
    seen_chips = set()
    for lot in profile.get_lots():
        sub = getattr(lot, "subcategory", None)
        if sub is None:
            continue
        if sub_ids is not None and sub.id not in sub_ids:
            continue
        if enum_name(sub.type) == "CURRENCY":
            if include_chips and sub.id not in seen_chips:
                seen_chips.add(sub.id)
                targets.extend(chip_targets(cardinal, sub, active=True))
            continue
        targets.append({"kind": "lot", "id": int(lot.id), "name": (lot.description or f"Лот {lot.id}")[:80],
                        "sub": sub.id, "sub_name": sub.name, "game": sub.category.name})
    return targets


def chip_targets(cardinal, sub, active=True) -> list:
    account = cardinal.account
    if not hasattr(account, "get_chip_fields"):
        return []
    try:
        fields = account.get_chip_fields(sub.id)
    except Exception:
        LOG.warning("%s: не удалось открыть таблицу валюты %s", NAME, sub.id)
        return []
    result = []
    for offer_id, offer in getattr(fields, "chip_offers", {}).items():
        if bool(offer.active) == active:
            label = " / ".join(str(x) for x in offer_id.split("-")[3:])
            result.append({"kind": "chip", "sub": sub.id, "offer": offer_id, "name": f"{sub.name} [{label}]"[:80],
                           "sub_name": sub.name, "game": sub.category.name})
    return result


def set_lot_state(cardinal, lot_id: int, active: bool, attempts: int = 3, stop=None):
    """Включает/выключает обычный лот с перепроверкой. Возвращает (ok, причина)."""
    account = cardinal.account
    reason = "неизвестная ошибка"
    for attempt in range(attempts):
        try:
            fields = account.get_lot_fields(lot_id)
        except Exception as error:
            if "LotParsingError" in type(error).__name__:
                return False, "лот не найден (удалён?)"
            reason = f"не удалось открыть лот ({type(error).__name__})"
            if wait(stop, 2):
                return False, "остановлено"
            continue
        if lot_active(fields) == active:
            return True, ""
        lot_update(fields, active=active)
        try:
            account.save_lot(fields)
        except Exception as error:
            reason = str(getattr(error, "error_message", "") or error)[:150] or type(error).__name__
            if wait(stop, 2):
                return False, "остановлено"
            continue
        if wait(stop, 1):
            return False, "остановлено"
        try:
            if lot_active(account.get_lot_fields(lot_id)) == active:
                return True, ""
            reason = "FunPay сохранил лот, но оставил его " + ("выключенным (лимит активных лотов?)" if active
                                                               else "включённым")
        except Exception:
            reason = "не удалось перепроверить лот"
    return False, reason


def set_chip_state(cardinal, sub_id: int, offer_ids: list, active: bool):
    """Включает/выключает строки таблицы валюты. Возвращает (успешные id, ошибка)."""
    account = cardinal.account
    try:
        fields = account.get_chip_fields(sub_id)
    except Exception as error:
        return [], f"таблица валюты недоступна ({type(error).__name__})"
    changed = []
    for offer_id in offer_ids:
        offer = fields.chip_offers.get(offer_id)
        if offer is None:
            continue
        offer.active = active
        changed.append(offer_id)
    if not changed:
        return [], "строки таблицы не найдены"
    try:
        if hasattr(account, "save_chip"):
            account.save_chip(fields)
        else:
            account.save_offer(fields)
    except Exception as error:
        return [], str(getattr(error, "error_message", "") or error)[:150]
    try:
        check = account.get_chip_fields(sub_id)
        ok = [i for i in changed if i in check.chip_offers and bool(check.chip_offers[i].active) == active]
        return ok, "" if len(ok) == len(changed) else "FunPay не применил часть строк"
    except Exception:
        return changed, ""


def sync_cardinal_profile(cardinal):
    """Обновляет профиль Cardinal, чтобы его автовосстановление не включало скрытые плагином лоты."""
    try:
        profile = cardinal.account.get_user(cardinal.account.id)
        cardinal.profile = profile
        cardinal.curr_profile = profile
    except Exception:
        LOG.debug("%s: профиль Cardinal не обновлён", NAME)
# ═════════════════════════ конец LuckyFunpay lot switcher ═════════════════════════

# ═══════════════════════════ LuckyFunpay mass jobs ═══════════════════════════
class JobPanelMixin:
    """Фоновая массовая операция с прогресс-баром, кнопкой «СТОП» и итогом."""

    JOB_PAGE = 8

    def job_running(self):
        job = getattr(self.plugin, "job", None)
        return job is not None and not getattr(job, "finished", False)

    def run_job(self, chat, mid, title, worker, done_rows=None):
        """worker(job) должен заполнять job.total/done/ok/errors и вернуть текст итога."""
        if self.job_running():
            raise UserError("Подождите, выполняется другая операция!")
        job = Job(title)
        job.finished = False
        self.plugin.job = job
        msg = self.send(chat, f"⏳ <b>{esc(title)}</b>\nПодготовка…", [[("⛔️ СТОП", ("job_stop",))]], mid)
        mid = getattr(msg, "message_id", mid)
        state = {"last": 0.0}

        def progress(force=False):
            if not force and time.time() - state["last"] < 2:
                return
            state["last"] = time.time()
            text = (f"⏳ <b>{esc(title)}</b>\n{job.bar()}\nОбработано: <b>{job.done}</b> из {job.total or '?'}\n"
                    f"✅ Успешно: {job.ok} · ❌ Ошибок: {job.errors}")
            if job.last_error:
                text += f"\nПоследняя ошибка: <i>{esc(job.last_error)[:200]}</i>"
            self.send(chat, text, [[("⛔️ СТОП", ("job_stop",))]], mid)

        job.progress = progress

        def run():
            summary = ""
            try:
                summary = worker(job) or ""
            except UserError as error:
                summary = "❌ " + esc(error)
            except Exception:
                LOG.exception("%s: ошибка массовой операции", NAME)
                summary = "❌ Ошибка операции, подробности в логе Cardinal."
            finally:
                job.finished = True
            head = "⛔️ <b>Остановлено</b>" if job.stop.is_set() else "✅ <b>Готово</b>"
            text = (f"{head} — {esc(title)}\nОбработано: {job.done} из {job.total} · ✅ {job.ok} · ❌ {job.errors}"
                    + (f"\nПоследняя ошибка: <i>{esc(job.last_error)[:200]}</i>" if job.last_error else "")
                    + (f"\n\n{summary}" if summary else ""))
            self.send(chat, text, done_rows or [[("🏠 Меню", ("home",))]], mid)

        if getattr(self.plugin, "sync", False):
            run()
        else:
            threading.Thread(target=run, name=f"{SLUG}Job", daemon=True).start()
        return job

    def on_job_stop(self, chat, mid):
        job = getattr(self.plugin, "job", None)
        if job is not None:
            job.stop.set()
        self.answer(self.call, "Останавливаю на ближайшем шаге…")

    def subs_rows(self, subs, selected, page, toggle_op, page_op, counts=None):
        """Кнопки выбора подкатегорий с галочками и листанием."""
        pages = max(1, (len(subs) + self.JOB_PAGE - 1) // self.JOB_PAGE)
        page = max(0, min(int(page), pages - 1))
        rows = []
        for sub in subs[page * self.JOB_PAGE:(page + 1) * self.JOB_PAGE]:
            mark = "✅" if sub.id in selected else "⬜"
            count = f" ({counts.get(sub.id, 0)})" if counts is not None else ""
            label = f"{mark} {sub.category.name} › {sub.name}"
            if len(label) > 48:
                label = label[:47] + "…"
            rows.append([(label + count, (toggle_op, sub.id, page))])
        if pages > 1:
            rows.append([("◀️", (page_op, max(0, page - 1))), (f"{page + 1}/{pages}", ("noop",)),
                         ("▶️", (page_op, min(pages - 1, page + 1)))])
        return rows, page


def save_with_retry(account, fields, stop=None, attempts=3):
    """Сохраняет лот с повторами при временных ошибках. Возвращает (ok, текст ошибки)."""
    error_text = ""
    for attempt in range(attempts):
        try:
            account.save_lot(fields)
            return True, ""
        except Exception as error:
            error_text = str(getattr(error, "error_message", "") or getattr(error, "errors", "") or error)[:200] \
                or type(error).__name__
            if "LotSavingError" in type(error).__name__:
                return False, error_text
            if wait(stop, 2 + attempt * 2):
                break
    return False, error_text
# ═════════════════════════ конец LuckyFunpay mass jobs ═════════════════════════


DEFAULTS = {"saved": {}, "chips": True}


class Deactivator:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("deactivated.json", DEFAULTS)
        self.ui = None
        self.job = None
        self.sync = False
        self.pause = (1.0, 2.5)
        self.last_failures = []

    def sleep(self, job):
        if self.pause[1]:
            wait(job.stop, random.uniform(*self.pause))

    def remember(self, target):
        self.store.change(lambda d: d["saved"].__setitem__(target_key(target), dict(target, t=time.time())))

    def forget(self, key):
        self.store.change(lambda d: d["saved"].pop(key, None))

    def deactivate(self, job, sub_ids=None) -> str:
        targets = collect_active(self.cardinal, sub_ids=sub_ids, include_chips=self.store["chips"])
        lots = [t for t in targets if t["kind"] == "lot"]
        chips = {}
        for t in targets:
            if t["kind"] == "chip":
                chips.setdefault(t["sub"], []).append(t)
        job.total = len(lots) + len(chips)
        job.progress(force=True)
        failed = []
        for t in lots:
            if job.stop.is_set():
                break
            ok, why = set_lot_state(self.cardinal, t["id"], False, stop=job.stop)
            if ok:
                self.remember(t)
                job.ok += 1
            else:
                job.errors += 1
                job.last_error = f"{t['name'][:40]}: {why}"
                failed.append((t, why))
            job.done += 1
            job.progress()
            self.sleep(job)
        for sub_id, items in chips.items():
            if job.stop.is_set():
                break
            ok_ids, why = set_chip_state(self.cardinal, sub_id, [t["offer"] for t in items], False)
            for t in items:
                if t["offer"] in ok_ids:
                    self.remember(t)
                else:
                    failed.append((t, why))
            if ok_ids:
                job.ok += 1
            else:
                job.errors += 1
                job.last_error = f"{items[0]['sub_name']}: {why}"
            job.done += 1
            job.progress()
        sync_cardinal_profile(self.cardinal)
        return self.failures_text(failed, "Не удалось выключить")

    def activate(self, job, sub_ids=None) -> str:
        saved = self.store["saved"]
        targets = [t for t in saved.values() if sub_ids is None or t["sub"] in sub_ids]
        lots = [t for t in targets if t["kind"] == "lot"]
        chips = {}
        for t in targets:
            if t["kind"] == "chip":
                chips.setdefault(t["sub"], []).append(t)
        job.total = len(lots) + len(chips)
        job.progress(force=True)
        failed, done_subs = [], set()
        for t in lots:
            if job.stop.is_set():
                break
            ok, why = set_lot_state(self.cardinal, t["id"], True, stop=job.stop)
            if ok:
                self.forget(target_key(t))
                done_subs.add(t["sub"])
                job.ok += 1
            else:
                if "не найден" in why:
                    self.forget(target_key(t))
                job.errors += 1
                job.last_error = f"{t['name'][:40]}: {why}"
                failed.append((t, why))
            job.done += 1
            job.progress()
            self.sleep(job)
        for sub_id, items in chips.items():
            if job.stop.is_set():
                break
            ok_ids, why = set_chip_state(self.cardinal, sub_id, [t["offer"] for t in items], True)
            for t in items:
                if t["offer"] in ok_ids:
                    self.forget(target_key(t))
                else:
                    failed.append((t, why or "строка таблицы не включилась"))
            job.ok += 1 if ok_ids else 0
            job.errors += 0 if ok_ids else 1
            job.done += 1
            job.progress()
        # проверка видимости в каталоге
        hidden = []
        for sub_id in done_subs:
            try:
                lots_now = {int(l.id): l for l in self.cardinal.account.get_my_subcategory_lots(sub_id)}
            except Exception:
                continue
            for t in lots:
                if t["sub"] == sub_id and t["id"] in lots_now and not getattr(lots_now[t["id"]], "active", True):
                    self.remember(t)
                    hidden.append((t, "лот сохранён, но скрыт в каталоге FunPay (лимит активных лотов?)"))
        sync_cardinal_profile(self.cardinal)
        return self.failures_text(failed + hidden, "Не удалось включить")

    def failures_text(self, failed, title) -> str:
        if not failed:
            return ""
        self.last_failures = failed
        return f"⚠️ {title}: <b>{len(failed)}</b> — список ниже."

    def list_lines(self) -> list:
        saved = sorted(self.store["saved"].values(), key=lambda t: (t["game"], t["sub_name"], t["name"]))
        lines, group = [], None
        for t in saved:
            head = f"{t['game']} › {t['sub_name']}"
            if head != group:
                group = head
                lines.append(f"\n<b>{esc(head)}</b>")
            if t["kind"] == "lot":
                lines.append(f"• <a href=\"{edit_link(t['id'])}\">{t['id']}</a> {esc(t['name'][:50])}")
            else:
                lines.append(f"• 🪙 {esc(t['name'][:50])}")
        return lines

    def export(self) -> bytes:
        rows = []
        for t in sorted(self.store["saved"].values(), key=lambda t: (t["game"], t["sub_name"])):
            when = datetime.fromtimestamp(t.get("t", 0)).strftime("%d.%m.%Y %H:%M")
            if t["kind"] == "lot":
                rows.append(f"{t['id']} | {t['name']} | {edit_link(t['id'])} | {t['game']} › {t['sub_name']} | {when}")
            else:
                rows.append(f"{t['offer']} | {t['name']} | https://funpay.com/chips/{t['sub']}/trade | "
                            f"{t['game']} › {t['sub_name']} | {when}")
        return ("ID | Название | Ссылка | Раздел | Выключен\n" + "\n".join(rows) + "\n").encode("utf-8")


class DeactPanel(Panel, JobPanelMixin):
    COMMAND = "deactivator"
    ICON = "🧩"
    GUIDE = [
        ("🚀 Возможности", "🔴 <b>Массовая деактивация</b> — все активные лоты или только выбранной категории.\n"
         "💾 <b>Умная память</b> — каждый выключенный лот заносится в базу.\n🟢 <b>Массовая активация</b> — всех или по "
         "категориям.\n🪙 <b>Игровые валюты</b> — строки таблиц (серверы, стороны) выключаются и включаются с сохранением "
         "цен и объёмов.\n🔍 <b>Верификация</b> — до 3 попыток и перепроверка реального состояния лота.\n📋 <b>Контроль "
         "ошибок</b> — невключённые лоты остаются в базе, бот пришлёт нумерованный список со ссылками.\n📎 <b>Экспорт</b> — "
         "список в .txt."),
        ("⚙️ Включение", "1. /menu → Плагины → <b>Деактиватор лотов</b> → включить, затем /restart.\n2. "
         "<code>/deactivator</code> — главный дашборд: сколько сохранено (лотов и строк валют) и какая операция идёт.\n\n"
         "⚠️ Если в Cardinal включено «Автовосстановление лотов», выключите его на время паузы — иначе Cardinal "
         "может включать лоты сам."),
        ("🔴 Деактивация", "• <b>🔴 Деактивировать все</b> — после подтверждения выключит все активные лоты аккаунта.\n"
         "• <b>🔴 По категории</b> — выберите раздел (например «Rust › Услуги»), подтвердите — выключатся только его "
         "лоты.\nБот обходит лоты с безопасной паузой. Каждый выключенный лот сразу записывается в память."),
        ("🟢 Активация", "• <b>🟢 Активировать все</b> — включит всё из памяти.\n• <b>🟢 По категории</b> — только выбранный "
         "раздел.\nПосле включения бот перепроверяет лот, а в конце — видно ли его в каталоге. Если FunPay оставил лот "
         "скрытым (лимит активных лотов), ID возвращается в память и вы получите предупреждение."),
        ("📋 Список и остановка", "📋 <b>Список</b> — деактивированные лоты по категориям, ID кликабельны (ведут на "
         "редактирование). Длинный список можно выгрузить файлом.\n📎 <b>Выгрузить файлом</b> — "
         "<code>deactivated_lots.txt</code>: ID, название, ссылка, время.\n🧹 <b>Очистить список</b> — если включили "
         "лоты вручную (с подтверждением).\n⛔️ <b>Остановить</b> — безопасно прервать операцию после текущего лота."),
        ("❓ FAQ", "<b>Почему после активации список ошибок?</b> Бот пишет причину: лимит активных лотов в категории или "
         "лот удалён на сайте.\n<b>«Скрыт в каталоге»</b> — галочка «Активен» применилась, но покупателям лот не виден; "
         "бот оставит его в списке.\n<b>Безопасно?</b> Запросы идут со случайной паузой через прокси вашего Cardinal."),
    ]

    def on_home(self, chat, mid=None, *args):
        saved = self.plugin.store["saved"]
        lots = sum(1 for t in saved.values() if t["kind"] == "lot")
        chips = len(saved) - lots
        lines = [f"🧩 <b>{esc(NAME)}</b> v{VERSION}", "",
                 f"💾 Сохранено на активацию: <b>{len(saved)}</b>",
                 f"• обычных лотов: {lots}", f"• строк игровых валют: {chips}",
                 f"🪙 Валюты: {'🟢 обрабатываются' if self.plugin.store['chips'] else '🔴 пропускаются'}"]
        if self.job_running():
            lines.append(f"\n⏳ Выполняется: {esc(self.plugin.job.title)} ({self.plugin.job.done}/{self.plugin.job.total})")
        rows = [[("🔴 Деактивировать все", ("ask_off",)), ("🔴 По категории", ("cats_off",))],
                [("🟢 Активировать все", ("ask_on",)), ("🟢 По категории", ("cats_on",))],
                [("📋 Список", ("list",)), ("📎 Выгрузить файлом", ("export",))],
                [("🧹 Очистить список", ("ask_clear",)), ("🪙 Валюты: " + ("🟢" if self.plugin.store["chips"] else "🔴"),
                                                         ("chips",))]]
        if self.job_running():
            rows.insert(0, [("⛔️ Остановить", ("job_stop",))])
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_chips(self, chat, mid):
        self.plugin.store.change(lambda d: d.update(chips=not d["chips"]))
        self.on_home(chat, mid)

    # --- выключение
    def on_ask_off(self, chat, mid):
        self.send(chat, "🔴 Выключить <b>все</b> активные лоты аккаунта? Они будут сохранены для последующего включения.",
                  [[("✅ Да, выключить", ("off", None))], [("↩️ Отмена", ("home",))]], mid)

    def on_cats_off(self, chat, mid):
        self.send(chat, "⏳ Загружаю активные разделы с FunPay…", mid=mid)
        profile = fresh_profile(self.cardinal)
        counts = {}
        subs = {}
        for lot in profile.get_lots():
            sub = getattr(lot, "subcategory", None)
            if sub is None or (enum_name(sub.type) == "CURRENCY" and not self.plugin.store["chips"]):
                continue
            subs[sub.id] = sub
            counts[sub.id] = counts.get(sub.id, 0) + 1
        if not subs:
            raise UserError("Активных лотов не найдено.")
        rows = [[(f"{'🪙 ' if enum_name(s.type) == 'CURRENCY' else ''}{s.category.name} › {s.name} ({counts[sid]})",
                  ("ask_off_sub", sid, f"{s.category.name} › {s.name}"))]
                for sid, s in sorted(subs.items(), key=lambda kv: (kv[1].category.name, kv[1].name))[:40]]
        rows.append([("◀️ Назад", ("home",))])
        self.send(chat, "🔴 <b>Выберите категорию для деактивации</b> (в скобках — активных лотов):", rows, mid)

    def on_ask_off_sub(self, chat, mid, sub_id, name):
        self.send(chat, f"🔴 Выключить все активные лоты раздела <b>{esc(name)}</b>?",
                  [[("✅ Да, выключить", ("off", sub_id))], [("↩️ Отмена", ("home",))]], mid)

    def on_off(self, chat, mid, sub_id):
        subs = None if sub_id is None else {sub_id}
        self.run_job(chat, mid, "Деактивация лотов", lambda job: self.plugin.deactivate(job, subs),
                     done_rows=[[("📋 Список", ("list",)), ("🏠 Меню", ("home",))]])
        self.send_failures(chat)

    # --- включение
    def on_ask_on(self, chat, mid):
        count = len(self.plugin.store["saved"])
        if not count:
            raise UserError("Список сохранённых лотов пуст.")
        self.send(chat, f"🟢 Включить все сохранённые позиции ({count})?",
                  [[("✅ Да, включить", ("on", None))], [("↩️ Отмена", ("home",))]], mid)

    def on_cats_on(self, chat, mid):
        saved = self.plugin.store["saved"]
        groups = {}
        for t in saved.values():
            groups.setdefault(t["sub"], [f"{t['game']} › {t['sub_name']}", 0])[1] += 1
        if not groups:
            raise UserError("Список сохранённых лотов пуст.")
        rows = [[(f"{name} ({n})", ("on", sid))] for sid, (name, n) in sorted(groups.items(), key=lambda kv: kv[1][0])][:40]
        rows.append([("◀️ Назад", ("home",))])
        self.send(chat, "🟢 <b>Выберите категорию для активации</b>:", rows, mid)

    def on_on(self, chat, mid, sub_id):
        subs = None if sub_id is None else {sub_id}
        self.plugin.last_failures = []
        self.run_job(chat, mid, "Активация лотов", lambda job: self.plugin.activate(job, subs),
                     done_rows=[[("📋 Список", ("list",)), ("🏠 Меню", ("home",))]])
        self.send_failures(chat)

    def send_failures(self, chat):
        if not self.plugin.sync:
            threading.Thread(target=self._wait_failures, args=(chat,), daemon=True).start()
        else:
            self._send_failures(chat)

    def _wait_failures(self, chat):
        job = self.plugin.job
        while job is not None and not getattr(job, "finished", False):
            time.sleep(1)
        self._send_failures(chat)

    def _send_failures(self, chat):
        failed = getattr(self.plugin, "last_failures", None) or []
        self.plugin.last_failures = []
        if not failed:
            return
        lines = [f"⚠️ <b>Проблемные позиции ({len(failed)}):</b>"]
        for i, (t, why) in enumerate(failed, 1):
            link = f"<a href=\"{edit_link(t['id'])}\">{t['id']}</a>" if t["kind"] == "lot" else "🪙"
            lines.append(f"{i}. {link} {esc(t['name'][:40])} — {esc(why)}")
        for part in chunks("\n".join(lines)):
            self.bot.send_message(chat, part, parse_mode="HTML", disable_web_page_preview=True)

    # --- список
    def on_list(self, chat, mid):
        lines = self.plugin.list_lines()
        if not lines:
            self.send(chat, "📋 Список пуст — сейчас ничего не выключено плагином.", [[("🏠 Меню", ("home",))]], mid)
            return
        text = f"📋 <b>Деактивированные позиции: {len(self.plugin.store['saved'])}</b>" + "\n".join(lines)
        if len(text) > 3800:
            self.send(chat, "📋 Список слишком большой для одного сообщения — выгрузите его файлом.",
                      [[("📎 Выгрузить файлом", ("export",)), ("🏠 Меню", ("home",))]], mid)
            return
        self.send(chat, text, [[("📎 Выгрузить файлом", ("export",)), ("🏠 Меню", ("home",))]], mid)

    def on_export(self, chat, mid):
        if not self.plugin.store["saved"]:
            raise UserError("Список пуст.")
        self.send_file(chat, "deactivated_lots.txt", self.plugin.export(),
                       caption=f"📎 Сохранено позиций: {len(self.plugin.store['saved'])}")

    def on_ask_clear(self, chat, mid):
        self.send(chat, "🧹 Очистить список сохранённых лотов? Сами лоты на FunPay не изменятся, но бот больше не "
                        "сможет включить их автоматически.",
                  [[("🧹 Да, очистить", ("clear",))], [("↩️ Отмена", ("home",))]], mid)

    def on_clear(self, chat, mid):
        if self.job_running():
            raise UserError("Подождите окончания операции.")
        self.plugin.store.set(saved={})
        self.on_home(chat, mid)


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = Deactivator(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = DeactPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = None
