"""
📉 Stale Lot Detector — умная оптимизация витрины. Плагин для FunPay Cardinal от LuckyFunpay.

Сверяет ваши активные лоты с историей продаж (до 50 страниц) и находит «залежавшийся» товар:
лоты без продаж дольше заданного срока или вообще без продаж. Отключает их по одному или пачкой.

ИНСТРУКЦИЯ (полная версия — в боте: /stale_lots → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. /stale_lots → «⏳ Порог» (дней без продаж, по умолчанию 60), «👻 Пустышки», «🛡️ Исключения».
3. «🔎 Начать поиск» — бот проанализирует профиль и историю сделок.
4. Нажмите на лот, чтобы выключить его, или «🗑️ Выключить ВСЕ» для всей страницы.
Цвета: 💀 никогда не продавался · 🔴 >180 дней · 🟠 >90 дней · 🟡 больше вашего порога.

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

NAME = "Stale Lot Detector"
VERSION = "1.0.0"
DESCRIPTION = ("📉 Находит лоты без продаж дольше N дней (и лоты, которые никогда не продавались) по истории продаж, "
               "с цветовой индикацией, исключениями и массовым отключением. Команда: /stale_lots")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "5e8f2a17-c4d9-4b3e-a6f0-7d1c9b4e2f68"
SETTINGS_PAGE = True
SLUG = "StaleLots"
PREFIX = "lfstale:"

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


def my_subcategories(cardinal, profile=None, extra_ids=(), include_currency=False) -> list:
    """Подкатегории с лотами продавца: из профиля (активные лоты) + известные плагину ID."""
    profile = profile or fresh_profile(cardinal)
    found = {}
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


DEFAULTS = {"days": 60, "ghosts": True, "exclude": [], "pages": 50, "result": [], "scan_t": 0, "history_pages": 0,
            "known_subs": []}
PAGE = 8


def badge(item, days) -> str:
    if item["last"] is None:
        return "💀"
    age = item["age"]
    if age > 180:
        return "🔴"
    if age > 90:
        return "🟠"
    return "🟡"


class StaleLots:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("settings.json", DEFAULTS)
        self.ui = None
        self.busy = threading.Lock()
        self.sync = False

    def scan(self, progress=None) -> list:
        if not self.busy.acquire(blocking=False):
            raise UserError("Поиск уже идёт.")
        try:
            return self._scan(progress)
        finally:
            self.busy.release()

    def _scan(self, progress) -> list:
        data = self.store.snapshot()
        account = self.cardinal.account
        subs = my_subcategories(self.cardinal, extra_ids=data["known_subs"])
        self.store.set(known_subs=sorted({s.id for s in subs}))
        lots = []
        for sub in subs:
            try:
                for lot in my_lots(self.cardinal, sub):
                    if getattr(lot, "active", True):
                        lots.append({"id": int(lot.id), "name": (lot.description or "").strip(), "sub": sub.id,
                                     "sub_name": f"{sub.category.name} › {sub.name}"})
            except Exception:
                LOG.warning("%s: не удалось получить лоты раздела %s", NAME, sub.id)
        if progress:
            progress(f"Найдено активных лотов: {len(lots)}. Читаю историю продаж…")
        last = {}
        start, pages = None, 0
        while pages < data["pages"]:
            result = None
            for attempt in range(3):
                try:
                    result = account.get_sales(start_from=start, include_refunded=False)
                    break
                except Exception:
                    time.sleep(2)
            if result is None:
                break
            start, orders = result[0], result[1]
            pages += 1
            for order in orders:
                try:
                    ts = order.date.timestamp()
                except Exception:
                    continue
                desc = (getattr(order, "description", "") or "").casefold()
                sub = getattr(order, "subcategory", None)
                for lot in lots:
                    if not lot["name"] or lot["name"].casefold() not in desc:
                        continue
                    if sub is not None and sub.id != lot["sub"]:
                        continue
                    last[lot["id"]] = max(last.get(lot["id"], 0), ts)
            if progress and pages % 5 == 0:
                progress(f"Прочитано страниц продаж: {pages}…")
            if not start:
                break
            time.sleep(random.uniform(0.4, 0.9))
        border = data["days"] * 86400
        now = time.time()
        result = []
        exclude = {int(i) for i in data["exclude"]}
        for lot in lots:
            if lot["id"] in exclude:
                continue
            ts = last.get(lot["id"])
            if ts is None:
                if not data["ghosts"]:
                    continue
                result.append(dict(lot, last=None, age=None, off=False))
            elif now - ts > border:
                result.append(dict(lot, last=ts, age=int((now - ts) // 86400), off=False))
        result.sort(key=lambda i: (i["last"] is not None, -(i["age"] or 0)))
        self.store.set(result=result, scan_t=time.time(), history_pages=pages)
        return result

    def deactivate(self, lot_id) -> tuple:
        ok, why = set_lot_state(self.cardinal, int(lot_id), False)
        if ok or "не найден" in why:
            self.store.change(lambda d: [i.update(off=True) for i in d["result"] if i["id"] == int(lot_id)])
        return ok, why


class StalePanel(Panel):
    COMMAND = "stale_lots"
    ICON = "📉"
    GUIDE = [
        ("🎯 Для чего это нужно", "С сотнями лотов следить за каждым невозможно, а «мёртвые» лоты вредят:\n"
         "1. <b>Случайная продажа</b> — товара давно нет, а лот активен → возврат и минус рейтинг.\n"
         "2. <b>Неактуальные цены</b> — рынок изменился, а лот висит по старой цене.\n3. <b>Хаос в профиле</b> — "
         "покупателям сложнее найти актуальное."),
        ("⚙️ Возможности", "🕵️‍♂️ <b>Глубокий анализ</b> — история продаж до 50 страниц, поиск лотов без продаж дольше "
         "срока.\n🛡️ <b>Исключения</b> — важные лоты (редкие услуги) никогда не попадут в отчёт.\n🎨 <b>Цвета</b>:\n"
         "💀 — никогда не продавался (в обозримой истории)\n🔴 — критически старый (&gt;180 дней)\n🟠 — старый "
         "(&gt;90 дней)\n🟡 — залежавшийся (больше вашего порога)\n⚡ <b>Массовые действия</b> — по одному или пачкой."),
        ("📖 Как использовать", "1. Включите плагин в списке плагинов Cardinal и /restart.\n2. <code>/stale_lots</code>.\n"
         "3. Фильтры: <b>⏳ Порог</b> — дней тишины (по умолчанию 60); <b>👻 Пустышки</b> — показывать лоты вообще без "
         "продаж; <b>🛡️ Исключения</b> — ID лотов, которые игнорировать.\n4. «🔎 Начать поиск» — до минуты.\n"
         "5. Нажмите на лот — он <b>выключится</b> (не удалится). «🗑️ Выключить ВСЕ» — вся страница результатов."),
        ("💡 Как сопоставляются продажи", "Продажа относится к лоту, если название лота входит в описание заказа и "
         "раздел совпадает. Анализируются активные лоты из разделов профиля. Если вы недавно переименовали лот, "
         "старые продажи с прежним названием не засчитаются — добавьте такой лот в исключения.\n\n💡 Регулярно "
         "делайте «гигиену» аккаунта — это лучший способ избежать возвратов."),
    ]

    def on_home(self, chat, mid=None, page=0, *args):
        d = self.plugin.store.snapshot()
        items = [i for i in d["result"] if not i.get("off")]
        lines = [f"📉 <b>{NAME}</b> v{VERSION}", "",
                 f"⏳ Порог: <b>{d['days']} дн.</b> · 👻 Пустышки: {'🟢' if d['ghosts'] else '🔴'} · "
                 f"🛡️ Исключений: {len(d['exclude'])}"]
        if self.plugin.busy.locked():
            lines.append("\n⏳ Идёт поиск…")
        elif d["scan_t"]:
            lines.append(f"\n🕒 Поиск: {fmt_dt(d['scan_t'])} · страниц истории: {d['history_pages']}")
            lines.append(f"Залежавшихся активных лотов: <b>{len(items)}</b>"
                         + (f" (выключено: {len(d['result']) - len(items)})" if len(d['result']) != len(items) else ""))
            if items:
                lines.append("💀 нет продаж · 🔴 &gt;180 дн · 🟠 &gt;90 дн · 🟡 &gt; порога\nНажмите на лот, чтобы выключить его.")
        rows = []
        pages = max(1, (len(items) + PAGE - 1) // PAGE)
        page = max(0, min(int(page), pages - 1))
        for item in items[page * PAGE:(page + 1) * PAGE]:
            age = "нет продаж" if item["last"] is None else f"{item['age']} дн"
            label = f"{badge(item, d['days'])} {item['name'][:30] or item['id']} · {age}"
            rows.append([(label, ("off", item["id"], page))])
        if pages > 1:
            rows.append([("◀️", ("home", max(0, page - 1))), (f"{page + 1}/{pages}", ("noop",)),
                         ("▶️", ("home", min(pages - 1, page + 1)))])
        if items:
            rows.append([("🗑️ Выключить ВСЕ на странице", ("off_all", page))])
        rows.append([("🔎 Начать поиск", ("scan",))])
        rows.append([("⏳ Порог", ("days",)), ("👻 Пустышки: " + ("🟢" if d["ghosts"] else "🔴"), ("ghosts",)),
                     ("🛡️ Исключения", ("excl",))])
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_scan(self, chat, mid):
        if self.plugin.busy.locked():
            raise UserError("Поиск уже идёт.")
        msg = self.send(chat, "🔎 Анализирую профиль и историю сделок…", mid=mid)
        mid = getattr(msg, "message_id", mid)

        def run():
            try:
                found = self.plugin.scan(lambda text: self.send(chat, "🔎 " + esc(text), mid=mid))
                self.on_home(chat, mid)
                if not found:
                    self.send(chat, "✅ Залежавшихся лотов не найдено — витрина в порядке!")
            except UserError as error:
                self.send(chat, "⚠️ " + esc(error), [[("🏠 Меню", ("home",))]], mid)
            except Exception:
                LOG.exception("%s: ошибка поиска", NAME)
                self.send(chat, "❌ Ошибка поиска, подробности в логе Cardinal.", [[("🏠 Меню", ("home",))]], mid)

        if self.plugin.sync:
            run()
        else:
            threading.Thread(target=run, name="StaleLotsScan", daemon=True).start()

    def on_off(self, chat, mid, lot_id, page):
        ok, why = self.plugin.deactivate(lot_id)
        if not ok:
            self.answer(self.call, f"Не удалось выключить: {why}"[:190], alert=True)
        self.on_home(chat, mid, page)

    def on_off_all(self, chat, mid, page):
        d = self.plugin.store.snapshot()
        items = [i for i in d["result"] if not i.get("off")][page * PAGE:(page + 1) * PAGE]
        self.send(chat, f"⏳ Выключаю {len(items)} лот(ов)…", mid=mid)
        done, failed = 0, []
        for item in items:
            ok, why = self.plugin.deactivate(item["id"])
            if ok:
                done += 1
            else:
                failed.append(f"{item['name'][:30]}: {why}")
            time.sleep(0 if self.plugin.sync else random.uniform(1.0, 2.0))
        self.on_home(chat, mid, page)
        self.send(chat, f"🗑️ Выключено: <b>{done}</b>" + ("\n⚠️ Ошибки:\n" + "\n".join(esc(f) for f in failed[:10])
                                                         if failed else ""), [[("📉 К списку", ("home", 0))]])

    def on_days(self, chat, mid):
        self.ask(chat, "⏳ Сколько дней без продаж считать «залежавшимся» лотом? (от 7 до 3650)", ("days",), mid)

    def in_days(self, chat, text):
        self.plugin.store.set(days=parse_int(text, 7, 3650, "Дни"))
        self.on_home(chat)

    def on_ghosts(self, chat, mid):
        self.plugin.store.change(lambda d: d.update(ghosts=not d["ghosts"]))
        self.on_home(chat, mid)

    def on_excl(self, chat, mid):
        ex = self.plugin.store["exclude"]
        rows = [[(f"❌ {i}", ("excl_del", i))] for i in ex[:30]]
        rows.append([("➕ Добавить ID", ("excl_add",)), ("◀️ Назад", ("home",))])
        self.send(chat, f"🛡️ <b>Исключения</b> ({len(ex)})\nЭти лоты никогда не попадут в отчёт. Нажмите ❌, чтобы "
                        "убрать из списка.", rows, mid)

    def on_excl_add(self, chat, mid):
        self.ask(chat, "➕ Отправьте ID лотов через пробел, запятую или с новой строки (или ссылки на лоты).",
                 ("excl",), mid)

    def in_excl(self, chat, text):
        ids = {int(x) for x in re.findall(r"(?<![\d])(\d{4,12})(?![\d])", text)}
        if not ids:
            raise UserError("Не нашёл ни одного ID лота.")
        self.plugin.store.change(lambda d: d.update(exclude=sorted(set(d["exclude"]) | ids),
                                                    result=[i for i in d["result"] if i["id"] not in ids]))
        self.on_excl(chat, None)

    def on_excl_del(self, chat, mid, lot_id):
        self.plugin.store.change(lambda d: d.update(exclude=[i for i in d["exclude"] if i != lot_id]))
        self.on_excl(chat, mid)


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = StaleLots(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = StalePanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = None
