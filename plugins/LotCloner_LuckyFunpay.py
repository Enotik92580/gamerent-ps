"""
📦 Lot Cloner — мгновенное дублирование лотов FunPay. Плагин для FunPay Cardinal от LuckyFunpay.

Создаёт точную копию вашего лота: русское и английское описание, краткое описание, цену,
параметры раздела и изображения. Копия создаётся ВЫКЛЮЧЕННОЙ и с пометкой «(Копия)».

ИНСТРУКЦИЯ (полная версия — в боте: /clone_lot → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. Найдите ID лота — цифры в ссылке funpay.com/lots/offer?id=1234567.
3. /clone_lot → «🧬 Клонировать лот» → отправьте ID. Или сразу: /clone_lot 1234567
4. Зайдите в «Мои предложения» на FunPay, поправьте копию и активируйте её.

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

NAME = "Lot Cloner"
VERSION = "1.0.0"
DESCRIPTION = ("📦 Клонирование лотов FunPay за пару секунд: описания, цена, параметры и картинки. "
               "Копия создаётся выключенной с пометкой «(Копия)». Команда: /clone_lot")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "2d7e9b41-c8a3-4f05-9e6d-1b4a7c3f8e20"
SETTINGS_PAGE = True
SLUG = "LotCloner"
PREFIX = "lfclone:"

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


DEFAULTS = {"suffix": " (Копия)", "suffix_en": " (Copy)", "copy_secrets": False, "history": []}


def parse_lot_id(raw: str) -> int:
    text = str(raw).strip()
    match = re.search(r"(?:offer(?:Edit)?\?(?:id|offer)=)(\d{1,12})", text) or re.fullmatch(r"#?(\d{1,12})", text)
    if not match:
        raise UserError("Нужен ID лота (цифры) или ссылка вида funpay.com/lots/offer?id=1234567.")
    return int(match.group(1))


def clone_fields(fields):
    """Делает независимую копию объекта LotFields с отдельным словарём полей."""
    clone = copy.copy(fields)
    raw = dict(lot_fields_raw(fields))
    if hasattr(clone, "set_fields"):
        clone.set_fields(raw)
    else:
        clone._LotFields__fields = raw
    for attr in ("images", "secrets"):
        if isinstance(getattr(fields, attr, None), list):
            setattr(clone, attr, list(getattr(fields, attr)))
    return clone


class LotCloner:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("settings.json", DEFAULTS)
        self.ui = None
        self.lock = threading.Lock()

    def clone(self, lot_id: int) -> dict:
        if not self.lock.acquire(blocking=False):
            raise UserError("Уже клонирую другой лот, подождите пару секунд.")
        try:
            return self._clone(lot_id)
        finally:
            self.lock.release()

    def _clone(self, lot_id: int) -> dict:
        account = self.cardinal.account
        try:
            source = account.get_lot_fields(lot_id)
        except Exception:
            raise UserError("Лот не найден на FunPay или он принадлежит не вам. Проверьте ID.") from None
        settings = self.store.snapshot()
        node = lot_node(source)
        title_ru = lot_title(source)
        raw = lot_fields_raw(source)
        new_ru = (title_ru + settings["suffix"]).strip()[:150]
        title_en = raw.get("fields[summary][en]") or ""
        before = set()
        if node:
            try:
                before = {str(l.id) for l in account.get_my_subcategory_lots(node)}
            except Exception:
                before = set()
        clone = clone_fields(source)
        clone.lot_id = 0
        lot_fields_raw(clone).pop("deleted", None)
        changes = {"offer_id": "0", "title_ru": new_ru, "active": False}
        if title_en:
            changes["title_en"] = (title_en + settings["suffix_en"]).strip()[:150]
        secrets = lot_secrets(source)
        if not settings["copy_secrets"]:
            if "secrets" in raw or hasattr(clone, "secrets"):
                changes["secrets"] = []
            if lot_auto_delivery(source):
                changes["auto_delivery"] = False
        lot_update(clone, **changes)
        try:
            account.save_lot(clone)
        except Exception as error:
            message = getattr(error, "error_message", None) or getattr(error, "errors", None) or str(error)
            LOG.warning("%s: FunPay отклонил копию лота %s: %s", NAME, lot_id, message)
            raise UserError(f"FunPay не сохранил копию: {str(message)[:300]}") from None
        new_id = None
        if node:
            try:
                time.sleep(1)
                for lot in account.get_my_subcategory_lots(node):
                    if str(lot.id) not in before and (getattr(lot, "description", "") or "").strip() == new_ru:
                        new_id = lot.id
                        break
                if new_id is None:
                    fresh = [l for l in account.get_my_subcategory_lots(node) if str(l.id) not in before]
                    new_id = fresh[0].id if len(fresh) == 1 else None
            except Exception:
                new_id = None
        result = {"source": lot_id, "new": new_id, "title": new_ru, "t": time.time(),
                  "secrets": len(secrets) if settings["copy_secrets"] else 0}
        self.store.change(lambda d: d.__setitem__("history", ([result] + d["history"])[:15]))
        LOG.info("%s: лот %s склонирован → %s", NAME, lot_id, new_id or "?")
        return result


class ClonerPanel(Panel):
    COMMAND = "clone_lot"
    ICON = "📦"
    GUIDE = [
        ("🚀 Для чего это нужно", "Создание лота вручную — это долго. Lot Cloner делает точную копию за секунды:\n"
         "1. <b>Шаблоны</b> — продаёте похожие товары? Склонируйте основной лот и поменяйте заголовок.\n"
         "2. <b>A/B тест</b> — копия с другой ценой или названием, посмотрите, что продаётся лучше.\n"
         "3. <b>Безопасные эксперименты</b> — переделывайте описание на клоне, не трогая рабочий лот."),
        ("⚙️ Возможности", "📸 <b>Полное копирование</b>: русское и английское описание, краткое описание, цена, "
         "параметры раздела и изображения.\n🛡 <b>Безопасный режим</b>: клон всегда создаётся выключенным — "
         "покупатели не увидят дубликат раньше времени.\n🏷 <b>Умный нейминг</b>: к названию добавляется "
         "«(Копия)», к английскому — «(Copy)».\n⚡ <b>Скорость</b>: 1–3 секунды, картинки не загружаются заново.\n"
         "🔐 Товары автовыдачи по умолчанию <b>не</b> копируются (чтобы не продать один ключ дважды) — "
         "включается переключателем."),
        ("📖 Как использовать", "1. Включите плагин: /menu → Система и конфиги → Плагины → <b>Lot Cloner</b>, "
         "затем /restart.\n2. Найдите ID оригинала: в ссылке <code>funpay.com/lots/offer?id=1234567</code> "
         "ID — <code>1234567</code>.\n3. Отправьте <code>/clone_lot</code> и нажмите «🧬 Клонировать лот», "
         "затем пришлите ID (или ссылку).\n4. Готово! В «Мои предложения» на FunPay найдите новый (неактивный) "
         "лот, внесите правки и активируйте его."),
        ("💡 Лайфхак", "Быстрая команда без меню:\n<code>/clone_lot 1234567</code>\n\n"
         "Клонировать можно только свои лоты. Лоты игровой валюты (таблицы chips) не клонируются — "
         "у них нет отдельной страницы лота."),
    ]

    def on_command(self, message):
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) == 2:
            self.run_clone(message.chat.id, parts[1])
        else:
            self.on_home(message.chat.id)

    def on_home(self, chat, mid=None, *args):
        data = self.plugin.store.snapshot()
        lines = [f"📦 <b>{esc(NAME)}</b> v{VERSION}", "",
                 "Создаёт копию любого вашего лота. Копия появляется <b>выключенной</b>.",
                 f"Копирование автовыдачи: {'🟢 да' if data['copy_secrets'] else '🔴 нет'}"]
        if data["history"]:
            lines += ["", "🕓 <b>Последние клоны:</b>"]
            for item in data["history"][:8]:
                new = f"<a href=\"{edit_link(item['new'])}\">{item['new']}</a>" if item.get("new") else "новый лот"
                lines.append(f"• {item['source']} → {new} · {esc(item['title'][:40])} · {fmt_dt(item['t'])}")
        rows = [[("🧬 Клонировать лот", ("ask",))],
                [("🔐 Копировать автовыдачу: " + ("🟢" if data["copy_secrets"] else "🔴"), ("secrets",))],
                [("🏷 Пометка: " + data["suffix"].strip(), ("suffix",))]]
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_ask(self, chat, mid):
        self.ask(chat, "🧬 Отправьте ID лота, который нужно размножить (или ссылку на него).", ("lot",), mid)

    def in_lot(self, chat, text):
        self.run_clone(chat, text)

    def run_clone(self, chat, raw):
        lot_id = parse_lot_id(raw)
        msg = self.send(chat, f"⏳ Клонирую лот <code>{lot_id}</code>…")
        mid = getattr(msg, "message_id", None)
        try:
            result = self.plugin.clone(lot_id)
        except UserError as error:
            self.send(chat, "❌ " + esc(error), [[("🔁 Попробовать снова", ("ask",)), ("🏠 Меню", ("home",))]], mid)
            return
        rows = []
        if result["new"]:
            rows.append([("✏️ Редактировать копию", edit_link(result["new"]))])
        rows.append([("🧬 Ещё один", ("ask",)), ("🏠 Меню", ("home",))])
        extra = f"\n🔐 Скопировано товаров автовыдачи: {result['secrets']}" if result["secrets"] else ""
        self.send(chat, f"✅ <b>Лот склонирован!</b>\n\n📄 {esc(result['title'])}\n"
                        f"🆔 Новый ID: <code>{result['new'] or 'см. «Мои предложения»'}</code>\n"
                        f"🔴 Копия выключена — проверьте её и активируйте на FunPay.{extra}", rows, mid)

    def on_secrets(self, chat, mid):
        self.plugin.store.change(lambda d: d.update(copy_secrets=not d["copy_secrets"]))
        self.on_home(chat, mid)

    def on_suffix(self, chat, mid):
        self.ask(chat, "🏷 Отправьте пометку для названия копии (до 30 символов), например <code>(Копия)</code>.\n"
                       "Отправьте <code>-</code>, чтобы не добавлять пометку.", ("suffix",), mid)

    def in_suffix(self, chat, text):
        value = "" if text.strip() == "-" else " " + text.strip()
        if len(value) > 31:
            raise UserError("Максимум 30 символов.")
        self.plugin.store.set(suffix=value)
        self.on_home(chat)


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = LotCloner(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = ClonerPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = None
