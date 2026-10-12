"""
🗑️ Lot Manager (Delete) — глобальная чистка профиля FunPay. Плагин для FunPay Cardinal от LuckyFunpay.

Удаляет сотни лотов за несколько кликов: по выбранным играм/разделам, только активные или все
(включая скрытые). Прогресс-бар, кнопка «СТОП», двухэтапное подтверждение.

ИНСТРУКЦИЯ (полная версия — в боте: /dellots → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. /dellots → «📂 Выбрать категории» → отметьте ✅ разделы → «✅ Готово».
3. «⚙️ Режим» — «Только активные» (по умолчанию) или «Все (вкл. скрытые)».
4. «🔥 Удалить из выбранных» → «🚀 ДА, УДАЛИТЬ».
☢️ «Удалить ВСЕ лоты аккаунта» игнорирует выбор категорий — используйте только если уверены!
Удалённые лоты пропадают навсегда (в отличие от деактивации).

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

NAME = "Lot Manager (Delete)"
VERSION = "1.0.0"
DESCRIPTION = ("🗑️ Массовое удаление лотов FunPay по выбранным разделам: только активные или все, прогресс-бар, "
               "«СТОП», двойное подтверждение и ☢️ удаление всех лотов аккаунта. Команда: /dellots")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "c8e4a2d6-1f73-4b59-9d0a-e7b3f5c1a294"
SETTINGS_PAGE = True
SLUG = "LotDeleter"
PREFIX = "lfdel:"

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


DEFAULTS = {"mode": "active", "selected": [], "known_subs": [], "history": []}


class LotDeleter:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("settings.json", DEFAULTS)
        self.ui = None
        self.job = None
        self.sync = False
        self.cache = None  # (время, {sub_id: (sub, [лоты])})

    def inventory(self, force=False) -> dict:
        if self.cache and not force and time.time() - self.cache[0] < 600:
            return self.cache[1]
        subs = my_subcategories(self.cardinal, extra_ids=self.store["known_subs"], from_sales=True)
        result = {}
        for sub in subs:
            try:
                lots = my_lots(self.cardinal, sub)
            except Exception:
                LOG.warning("%s: лоты раздела %s не получены", NAME, sub.id)
                continue
            if lots:
                result[sub.id] = (sub, lots)
            if not self.sync:
                time.sleep(0.3)
        self.store.set(known_subs=sorted(result))
        self.cache = (time.time(), result)
        return result

    def delete(self, job, sub_ids, mode) -> str:
        account = self.cardinal.account
        inventory = self.inventory(force=True)
        plan = []
        for sid in sub_ids:
            if sid in inventory:
                sub, lots = inventory[sid]
                plan += [lot for lot in lots if mode == "all" or getattr(lot, "active", True)]
        job.total = len(plan)
        job.progress(force=True)
        deleted = []
        for lot in plan:
            if job.stop.is_set():
                break
            for attempt in range(3):
                try:
                    account.delete_lot(int(lot.id))
                    deleted.append(lot.id)
                    job.ok += 1
                    break
                except Exception as error:
                    job.last_error = f"{lot.id}: " + (str(getattr(error, "error_message", "") or "")[:150]
                                                      or type(error).__name__)
                    if "LotSavingError" in type(error).__name__ or wait(job.stop, 2 + attempt * 2):
                        job.errors += 1
                        break
            else:
                job.errors += 1
            job.done += 1
            job.progress()
            wait(job.stop, random.uniform(1.5, 3.0))
        self.cache = None
        self.store.change(lambda d: d.__setitem__("history", ([{"t": time.time(), "n": len(deleted), "mode": mode}]
                                                              + d["history"])[:10]))
        return f"Удалено лотов: <b>{len(deleted)}</b>."


class DeleterPanel(Panel, JobPanelMixin):
    COMMAND = "dellots"
    ICON = "🗑️"
    GUIDE = [
        ("🧹 Зачем это нужно", "Ручное удаление лотов на FunPay — долго. Модуль незаменим, если вы:\n1. <b>Меняете "
         "нишу</b> — перестали продавать в одной игре.\n2. <b>Проводите ревизию</b> — удалить устаревшее.\n"
         "3. <b>Закрываете направление</b> — убрать все товары категории.\n4. <b>Начинаете с чистого листа</b> — "
         "☢️ удалить все лоты аккаунта."),
        ("🛠 Возможности", "📂 <b>Выбор категорий</b> — отметьте нужные игры/разделы (например только Dota 2 и WoW).\n"
         "⚙️ <b>Фильтры</b>: «Только активные» — удалит включённые лоты; «Все (вкл. скрытые)» — полная зачистка.\n"
         "📊 <b>Прогресс-бар</b> в реальном времени.\n🛡️ <b>Защита</b> — двухэтапное подтверждение."),
        ("📖 Инструкция", "1. Включите плагин в списке плагинов Cardinal и /restart.\n2. <code>/dellots</code>.\n"
         "3. «📂 Выбрать категории» — список разделов с вашими лотами (в скобках: всего / активных). Отметьте ✅ и "
         "нажмите «✅ Готово».\n4. «⚙️ Режим» — переключить «Только активные» ↔ «Все».\n5. «🔥 Удалить из выбранных» → "
         "бот покажет сколько лотов будет удалено → «🚀 ДА, УДАЛИТЬ».\nРазделы ищутся по профилю, истории продаж и "
         "прошлым сканированиям — так находятся и разделы, где все лоты выключены."),
        ("☢️ Опасная зона и детали", "«☢️ Удалить ВСЕ лоты аккаунта» игнорирует выбор категорий и удаляет все "
         "найденные лоты (активные и скрытые) — два подтверждения.\n\n• Пауза 1,5–3 сек между удалениями (anti-flood).\n"
         "• «⛔️ СТОП» — экстренная остановка в любой момент.\n• Удалённые лоты пропадают из поиска и «Моих "
         "предложений» навсегда.\n• Таблицы игровой валюты не удаляются (их можно только выключить)."),
    ]

    def on_home(self, chat, mid=None, *args):
        d = self.plugin.store.snapshot()
        lines = [f"🗑️ <b>{esc(NAME)}</b> v{VERSION}", "",
                 f"📂 Выбрано разделов: <b>{len(d['selected'])}</b>",
                 f"⚙️ Режим: <b>{'Только активные' if d['mode'] == 'active' else 'Все (вкл. скрытые)'}</b>"]
        if self.job_running():
            lines.append("\n⏳ Идёт удаление…")
        if d["history"]:
            lines += ["", "🕓 <b>История:</b>"] + [f"• {fmt_dt(h['t'])} — удалено {h['n']}" for h in d["history"][:5]]
        rows = [[("📂 Выбрать категории", ("cats", 0))],
                [("⚙️ Режим: " + ("Активные" if d["mode"] == "active" else "Все"), ("mode",))],
                [("🔥 Удалить из выбранных", ("ask_delete",))],
                [("☢️ Удалить ВСЕ лоты аккаунта", ("nuke1",))]]
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_mode(self, chat, mid):
        self.plugin.store.change(lambda d: d.update(mode="all" if d["mode"] == "active" else "active"))
        self.on_home(chat, mid)

    def on_cats(self, chat, mid, page=0, refresh=False):
        if self.job_running():
            raise UserError("Подождите, выполняется другая операция!")
        if not self.plugin.cache or refresh:
            self.send(chat, "⏳ Загружаю ваши разделы и лоты…", mid=mid)
        inv = self.plugin.inventory(force=bool(refresh))
        subs = sorted((v[0] for v in inv.values()), key=lambda s: (s.category.name.lower(), s.name.lower()))
        selected = set(self.plugin.store["selected"]) & set(inv)
        counts = {sid: f"{len(lots)}/{sum(1 for l in lots if getattr(l, 'active', True))}"
                  for sid, (_, lots) in inv.items()}
        rows, page = self.subs_rows(subs, selected, page, "cat", "cats")
        for row in rows:
            for i, (label, action) in enumerate(row):
                if action[0] == "cat":
                    row[i] = (f"{label} ({counts[action[1]]})", action)
        rows.append([("☑️ Выбрать все", ("all", page)), ("⬜ Снять все", ("none", page))])
        rows.append([("🔄 Обновить список", ("cats", page, True)), ("✅ Готово", ("home",))])
        self.send(chat, f"📂 <b>Разделы с вашими лотами</b> (всего/активных)\nВыбрано: {len(selected)} из {len(subs)}",
                  rows, mid)

    def on_cat(self, chat, mid, sub_id, page):
        self.plugin.store.change(lambda d: d.update(selected=sorted(set(d["selected"]) ^ {sub_id})))
        self.on_cats(chat, mid, page)

    def on_all(self, chat, mid, page):
        inv = self.plugin.inventory()
        self.plugin.store.set(selected=sorted(inv))
        self.on_cats(chat, mid, page)

    def on_none(self, chat, mid, page):
        self.plugin.store.set(selected=[])
        self.on_cats(chat, mid, page)

    def plan_count(self, sub_ids, mode) -> tuple:
        inv = self.plugin.inventory()
        lots = [lot for sid in sub_ids if sid in inv for lot in inv[sid][1]
                if mode == "all" or getattr(lot, "active", True)]
        return len(lots), [inv[sid][0] for sid in sub_ids if sid in inv]

    def on_ask_delete(self, chat, mid):
        d = self.plugin.store.snapshot()
        if not d["selected"]:
            raise UserError("Сначала выберите категории.")
        count, subs = self.plan_count(d["selected"], d["mode"])
        if not count:
            raise UserError("В выбранных разделах нет лотов для удаления в этом режиме.")
        names = "".join(f"• {esc(s.category.name)} › {esc(s.name)}\n" for s in subs[:15])
        self.send(chat, f"⚠️ <b>Подтвердите удаление</b>\n\n{names}\nРежим: "
                        f"{'только активные' if d['mode'] == 'active' else 'все, включая скрытые'}\n"
                        f"Будет удалено лотов: <b>{count}</b>\n\nУдалённые лоты нельзя восстановить!",
                  [[("🚀 ДА, УДАЛИТЬ", ("go",))], [("↩️ Отмена", ("home",))]], mid)

    def on_go(self, chat, mid):
        d = self.plugin.store.snapshot()
        self.run_job(chat, mid, "Удаление лотов", lambda job: self.plugin.delete(job, d["selected"], d["mode"]))

    def on_nuke1(self, chat, mid):
        inv = self.plugin.inventory()
        total = sum(len(lots) for _, lots in inv.values())
        if not total:
            raise UserError("Лотов не найдено.")
        self.send(chat, f"☢️ <b>ОПАСНАЯ ЗОНА</b>\n\nБудут удалены <b>ВСЕ {total}</b> лот(ов) аккаунта во всех "
                        f"{len(inv)} разделах — активные и скрытые. Выбор категорий игнорируется.\n\nВы уверены?",
                  [[("☢️ Да, я понимаю", ("nuke2", total))], [("↩️ Отмена", ("home",))]], mid)

    def on_nuke2(self, chat, mid, total):
        self.send(chat, f"☢️ <b>Последнее подтверждение.</b>\nУдалить {total} лот(ов) без возможности восстановления?",
                  [[("↩️ Нет, отмена", ("home",))], [("🔥 УДАЛИТЬ ВСЁ", ("nuke_go",))]], mid)

    def on_nuke_go(self, chat, mid):
        inv = self.plugin.inventory()
        self.run_job(chat, mid, "☢️ Удаление ВСЕХ лотов", lambda job: self.plugin.delete(job, list(inv), "all"))


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = LotDeleter(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = DeleterPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = None
