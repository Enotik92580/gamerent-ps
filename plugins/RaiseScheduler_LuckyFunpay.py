"""
⏫ Планировщик поднятия лотов (Raise Scheduler) — плагин для FunPay Cardinal от LuckyFunpay.

Поднимает лоты строго в заданные часы и минуты (по Москве), а не сразу после кулдауна FunPay.
Общее расписание для всех игр или своё для каждой игры, пресеты ×2/×3/×4/×6 в сутки, «умный» перенос
при КД, журнал событий и уведомление о задержке больше 90 минут.

ИНСТРУКЦИЯ (полная версия — в боте: /raise_schedule → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. ⚠️ Глобальное автоподнятие Cardinal должно быть включено (🟢) — иначе расписание не работает.
3. /raise_schedule → «🕒 Время по умолчанию» (пресет или вручную, например 10:00, 14:30, 19:15).
4. «📂 Категории» → «☑️ Выбрать все» (общее расписание) или настройте игру отдельно.
Время — московское (МСК, UTC+3). Остальные игры поднимаются как обычно.

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

NAME = "Планировщик поднятия лотов"
VERSION = "1.0.0"
DESCRIPTION = ("⏫ Поднятие лотов строго по расписанию (МСК): общее или своё для каждой игры, пресеты ×2/×3/×4/×6, "
               "умный перенос при КД FunPay, журнал и уведомления о задержках. Панель: /raise_schedule")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "4f0c8e2b-6a93-4d1f-b7c5-9e2d3a8f1c64"
SETTINGS_PAGE = True
SLUG = "RaiseScheduler"
PREFIX = "lfraise:"

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


MSK = timezone(timedelta(hours=3))
PRESETS = {"2": ["09:00", "21:00"], "3": ["09:00", "17:00", "01:00"], "4": ["09:00", "15:00", "21:00", "03:00"],
           "6": ["01:00", "05:00", "09:00", "13:00", "17:00", "21:00"]}
MAX_SLOTS = 12
MISS_AFTER = 90 * 60
BLOCK_AHEAD = 600
DEFAULTS = {"enabled": True, "all": False, "default": [], "cats": {}, "state": {}, "log": []}
TIME_RE = re.compile(r"([01]?\d|2[0-3])[:.]([0-5]\d)")


def parse_times(text: str) -> list:
    parts = [p for p in re.split(r"[,\s;]+", str(text).strip()) if p]
    if not parts:
        raise UserError("Отправьте время в формате ЧЧ:ММ, например 10:00, 14:30.")
    result = []
    for part in parts:
        match = TIME_RE.fullmatch(part)
        if not match:
            raise UserError(f"«{part}» — не время. Формат ЧЧ:ММ, например 09:00.")
        value = f"{int(match.group(1)):02d}:{match.group(2)}"
        if value not in result:
            result.append(value)
    return sorted(result)


def slot_times(times: list, now: float) -> tuple:
    """Возвращает (последний наступивший слот, следующий слот) в unix-времени."""
    current = datetime.fromtimestamp(now, MSK)
    candidates = []
    for day in (-1, 0, 1):
        base = (current + timedelta(days=day)).replace(second=0, microsecond=0)
        for value in times:
            h, m = map(int, value.split(":"))
            candidates.append(base.replace(hour=h, minute=m).timestamp())
    past = [t for t in candidates if t <= now]
    future = [t for t in candidates if t > now]
    return (max(past) if past else None), (min(future) if future else None)


def min_gap(times: list) -> float:
    if len(times) < 2:
        return 24 * 3600
    minutes = sorted(int(t[:2]) * 60 + int(t[3:]) for t in times)
    gaps = [b - a for a, b in zip(minutes, minutes[1:])] + [minutes[0] + 1440 - minutes[-1]]
    return min(gaps) * 60


class RaiseScheduler:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("settings.json", DEFAULTS)
        self.ui = None
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = threading.Lock()

    def autoraise_on(self) -> bool:
        try:
            return self.cardinal.MAIN_CFG["FunPay"].getboolean("autoRaise")
        except Exception:
            return bool(getattr(self.cardinal, "autoraise_enabled", True))

    def categories(self) -> dict:
        profile = getattr(self.cardinal, "curr_profile", None) or getattr(self.cardinal, "profile", None)
        result = {}
        if profile is None:
            return result
        try:
            for sub in profile.get_sorted_lots(2).keys():
                if enum_name(sub.type) == "COMMON":
                    result[sub.category.id] = sub.category
        except Exception:
            pass
        return result

    def times_for(self, cat_id, data=None):
        data = data or self.store.snapshot()
        cfg = data["cats"].get(str(cat_id), {})
        if cfg.get("enabled"):
            return cfg.get("times") or data["default"]
        if data["all"] and not cfg.get("excluded"):
            return data["default"]
        return None

    def status(self, cat_id, data=None) -> str:
        data = data or self.store.snapshot()
        cfg = data["cats"].get(str(cat_id), {})
        if cfg.get("enabled") or (data["all"] and not cfg.get("excluded")):
            return "scheduled"
        if data["all"] and cfg.get("excluded"):
            return "excluded"
        return "normal"

    def log(self, cat_name, kind, text=""):
        entry = {"t": time.time(), "cat": cat_name, "kind": kind, "text": text}
        self.store.change(lambda d: d.__setitem__("log", ([entry] + d["log"])[:60]))

    def tick(self, now=None):
        now = now or time.time()
        data = self.store.snapshot()
        if not data["enabled"] or not self.autoraise_on() or not account_ready(self.cardinal):
            return
        raise_time = getattr(self.cardinal, "raise_time", None)
        for cat_id, category in self.categories().items():
            times = self.times_for(cat_id, data)
            if not times:
                continue
            key = str(cat_id)
            last_slot, next_slot = slot_times(times, now)
            st = data["state"].get(key)
            if st is None:
                st = {"done": last_slot if last_slot and now - last_slot > 1800 else 0, "pending": 0, "retry": 0,
                      "missed": False}
            if isinstance(raise_time, dict):
                raise_time[cat_id] = int(now) + BLOCK_AHEAD
            if last_slot and last_slot > st["done"]:
                if not st["pending"]:
                    st["pending"] = last_slot
                    st["missed"] = False
                    self.log(category.name, "pending", f"слот {datetime.fromtimestamp(last_slot, MSK):%H:%M}")
                if now >= st["retry"]:
                    self.try_raise(category, st, last_slot, now)
                if st["pending"] and now - last_slot > MISS_AFTER and not st["missed"]:
                    st["missed"] = True
                    self.log(category.name, "miss", f"задержка слота {datetime.fromtimestamp(last_slot, MSK):%H:%M}")
                    if self.ui:
                        self.ui.notify(f"⚠️ <b>Планировщик поднятия</b>: категория <b>{esc(category.name)}</b> не поднимается "
                                       f"по расписанию уже более 90 минут (слот "
                                       f"{datetime.fromtimestamp(last_slot, MSK):%H:%M} МСК). Скорее всего не прошёл КД "
                                       f"FunPay — бот продолжит попытки.", [[("📋 Журнал", ("journal",))]])
            self.store.change(lambda d: d["state"].__setitem__(key, st))

    def try_raise(self, category, st, slot, now):
        account = self.cardinal.account
        try:
            wait_time = account.raise_lots(category.id)
        except Exception as error:
            wait_time = getattr(error, "wait_time", None)
            st["retry"] = now + (int(wait_time) + 5 if wait_time else 60)
            LOG.info("%s: %s ещё на КД (%s)", NAME, category.name, wait_time)
            return False
        st.update(done=slot, pending=0, retry=0, missed=False)
        try:
            self.cardinal.raised_time[category.id] = int(now)
        except Exception:
            pass
        late = int((now - slot) // 60)
        self.log(category.name, "raised", f"слот {datetime.fromtimestamp(slot, MSK):%H:%M}" +
                 (f", с задержкой {late} мин" if late > 2 else ""))
        try:
            handlers = getattr(self.cardinal, "post_lots_raise_handlers", None)
            if handlers:
                self.cardinal.run_handlers(handlers, (self.cardinal, category, f"По расписанию {NAME}."))
        except Exception:
            pass
        return True

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self.loop, name="RaiseScheduler", daemon=True)
        self.thread.start()

    def loop(self):
        self.stop_event.wait(5)
        while not self.stop_event.is_set():
            with self.lock:
                try:
                    self.tick()
                except Exception:
                    LOG.exception("%s: ошибка планировщика", NAME)
            self.stop_event.wait(20)


class RaisePanel(Panel):
    COMMAND = "raise_schedule"
    ICON = "⏫"
    GUIDE = [
        ("🚀 Возможности", "Вместо хаотичного поднятия сразу после КД бот поднимает лоты строго в ваши часы — например в "
         "пиковые вечерние часы.\n⏱ Точность до минут.\n🌍 Два режима: <b>общее расписание</b> для всех игр (любую игру "
         "можно исключить) и <b>индивидуальное</b> для каждой игры.\n📅 Пресеты ×2/×3/×4/×6 в сутки.\n🧠 Умный перенос "
         "при КД.\n📋 Журнал событий.\n🔔 Уведомление, если слот задерживается более 90 минут.\n🇷🇺 Всё по МСК (UTC+3)."),
        ("🛠 Включение", "1. /menu → Плагины → <b>Планировщик поднятия лотов</b> → включить, затем /restart.\n2. Команда "
         "<code>/raise_schedule</code>.\n\n⚠️ <b>КРИТИЧЕСКИ ВАЖНО:</b> глобальное автоподнятие в Cardinal должно быть "
         "включено (🟢). Если оно выключено, проверки не запускаются.\n\nКатегории без расписания Cardinal поднимает как "
         "обычно; категории под расписанием плагин поднимает сам и не даёт Cardinal поднимать их раньше времени."),
        ("👤 Часть 1. Время по умолчанию", "Используется в общем режиме и как шаблон для сброса категорий.\n"
         "<code>×2/день</code> — 09:00 и 21:00\n<code>×3/день</code> — 09:00, 17:00, 01:00\n<code>×4/день</code> — 09:00, "
         "15:00, 21:00, 03:00\n<code>×6/день</code> — каждые 4 часа\n✏️ Вручную: <code>10:00, 14:30, 19:15, 23:00</code> "
         "(через запятую или с новой строки)\n🗑 Очистить — сбросить все часы."),
        ("👥 Часть 2. Категории", "<b>☑️ Выбрать все</b> — общий режим: все игры по времени по умолчанию.\n<b>❌ Снять со "
         "всех</b> — планировщик выключен для всех игр.\nСтатусы: 🟢 под расписанием · ⚪️ обычный режим · 🚫 исключена.\n"
         "В карточке игры: включить/выключить расписание или исключить из общего; «➕ Добавить время» (до 12 слотов, "
         "МСК); 🗑 ЧЧ:ММ — удалить слот; «↩️ Сбросить на время по умолчанию»."),
        ("📋 Журнал и КД", "⏫ raised — лоты подняты; ⏳ pending — слот наступил, бот ждёт возможности; ⚠️ miss — слот "
         "задерживается больше 90 минут.\n\nFunPay разрешает поднимать категорию примерно раз в 4 часа. При плотном "
         "графике (12:00 и 14:00) бот не пропустит второй слот: он будет ждать и поднимет лоты, как только FunPay снимет "
         "КД (~16:00), а затем вернётся к расписанию. Если зазор между слотами меньше 4 часов, меню покажет "
         "предупреждение — это не ошибка."),
        ("❓ FAQ", "<b>«Глобальное автоподнятие выключено»</b> — /menu → включите «Автоподнятие лотов» (🟢).\n"
         "<b>Часовой пояс?</b> Строго МСК, независимо от сервера.\n<b>«Не поднимается более 90 минут»</b> — обычно не "
         "прошёл КД FunPay (поднимали вручную?) или проблемы с прокси. Бот продолжит попытки.\n<b>Сколько слотов?</b> "
         "До 12 в сутки на категорию."),
    ]

    def warnings(self, data) -> list:
        notes = []
        if not self.plugin.autoraise_on():
            notes.append("⛔️ Глобальное автоподнятие выключено в настройках Cardinal — расписание не работает!")
        if data["default"] and min_gap(data["default"]) < 4 * 3600:
            notes.append("⚠️ Зазор между слотами по умолчанию меньше 4 часов — часть поднятий сдвинется из-за КД FunPay.")
        return notes

    def on_home(self, chat, mid=None, *args):
        d = self.plugin.store.snapshot()
        cats = self.plugin.categories()
        scheduled = sum(1 for cid in cats if self.plugin.status(cid, d) == "scheduled")
        lines = [f"⏫ <b>{esc(NAME)}</b> v{VERSION}", "",
                 f"Планировщик: {'🟢 включён' if d['enabled'] else '🔴 выключен'}",
                 f"Общий режим: {'🟢 все игры по умолчанию' if d['all'] else '⚪️ выключен'}",
                 f"🕒 Время по умолчанию (МСК): {', '.join(d['default']) or 'не задано'}",
                 f"📂 Под расписанием: {scheduled} из {len(cats)} игр",
                 f"Сейчас по МСК: {datetime.now(MSK):%H:%M}"]
        lines += [""] + self.warnings(d) if self.warnings(d) else []
        rows = [[("Планировщик: " + ("🟢" if d["enabled"] else "🔴"), ("power",))],
                [("🕒 Время по умолчанию", ("default",)), ("📂 Категории", ("cats",))],
                [("📋 Журнал", ("journal",))]]
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_power(self, chat, mid):
        self.plugin.store.change(lambda d: d.update(enabled=not d["enabled"]))
        self.on_home(chat, mid)

    # --- время по умолчанию
    def on_default(self, chat, mid=None):
        d = self.plugin.store.snapshot()
        text = (f"🕒 <b>Время по умолчанию</b> (МСК)\n{', '.join(d['default']) or 'не задано'}\n\n"
                "Выберите пресет или введите время вручную.")
        if d["default"] and min_gap(d["default"]) < 4 * 3600:
            text += "\n\n⚠️ Зазор меньше 4 часов — часть поднятий сдвинется из-за КД FunPay."
        rows = [[(f"×{k}/день", ("preset", k)) for k in PRESETS],
                [("✏️ Ввести вручную", ("ask_default",)), ("🗑 Очистить", ("clear_default",))],
                [("◀️ Назад", ("home",))]]
        self.send(chat, text, rows, mid)

    def on_preset(self, chat, mid, key):
        self.plugin.store.set(default=sorted(PRESETS[key]))
        self.on_default(chat, mid)

    def on_ask_default(self, chat, mid):
        self.ask(chat, "✏️ Отправьте время по МСК через запятую или с новой строки, например "
                       "<code>10:00, 14:30, 19:15, 23:00</code> (до 12 слотов).", ("default",), mid)

    def in_default(self, chat, text):
        times = parse_times(text)
        if len(times) > MAX_SLOTS:
            raise UserError(f"Максимум {MAX_SLOTS} слотов.")
        self.plugin.store.set(default=times)
        self.on_default(chat)

    def on_clear_default(self, chat, mid):
        self.plugin.store.set(default=[])
        self.on_default(chat, mid)

    # --- категории
    def on_cats(self, chat, mid=None):
        d = self.plugin.store.snapshot()
        cats = self.plugin.categories()
        icons = {"scheduled": "🟢", "normal": "⚪️", "excluded": "🚫"}
        rows = [[("☑️ Выбрать все", ("all_on",)), ("❌ Снять со всех", ("all_off",))]]
        for cid, cat in sorted(cats.items(), key=lambda kv: kv[1].name.lower())[:40]:
            cfg = d["cats"].get(str(cid), {})
            times = self.plugin.times_for(cid, d)
            extra = f" · {len(times)} слот." if times else ""
            own = " (своё)" if cfg.get("enabled") and cfg.get("times") else ""
            rows.append([(f"{icons[self.plugin.status(cid, d)]} {cat.name}{own}{extra}", ("cat", cid))])
        rows.append([("◀️ Назад", ("home",))])
        text = ("📂 <b>Категории с активными лотами</b>\n🟢 под расписанием · ⚪️ обычный режим · 🚫 исключена из "
                "общего расписания")
        if not cats:
            text += "\n\nАктивных лотов не найдено — категории появятся после обновления профиля Cardinal."
        self.send(chat, text, rows, mid)

    def on_all_on(self, chat, mid):
        self.plugin.store.change(lambda d: (d.update(all=True), [c.update(excluded=False) for c in d["cats"].values()]))
        if not self.plugin.store["default"]:
            self.send(chat, "⚠️ Время по умолчанию не задано — задайте его, иначе общий режим ничего не поднимет.")
        self.on_cats(chat, mid)

    def on_all_off(self, chat, mid):
        self.plugin.store.change(lambda d: (d.update(all=False), [c.update(enabled=False, excluded=False)
                                                                  for c in d["cats"].values()]))
        self.on_cats(chat, mid)

    def cat_cfg(self, cid):
        cat = self.plugin.categories().get(cid)
        name = cat.name if cat else str(cid)
        self.plugin.store.change(lambda d: d["cats"].setdefault(str(cid), {"enabled": False, "excluded": False,
                                                                            "times": [], "name": name}))
        return self.plugin.store["cats"][str(cid)], name

    def on_cat(self, chat, mid, cid):
        cfg, name = self.cat_cfg(cid)
        d = self.plugin.store.snapshot()
        status = self.plugin.status(cid, d)
        times = self.plugin.times_for(cid, d) or []
        own = cfg.get("times") or []
        state_text = {"scheduled": "🟢 Под расписанием", "normal": "⚪️ Обычный режим (сразу после КД)",
                      "excluded": "🚫 Исключена из общего расписания"}[status]
        lines = [f"🎮 <b>{esc(name)}</b>", state_text,
                 f"Время (МСК): {', '.join(times) or '—'}" + (" (своё)" if cfg.get("enabled") and own else
                                                              " (по умолчанию)" if times else "")]
        if times and min_gap(times) < 4 * 3600:
            lines.append("⚠️ Зазор меньше 4 часов — часть поднятий сдвинется из-за КД FunPay.")
        st = d["state"].get(str(cid))
        if st and st.get("pending"):
            lines.append(f"⏳ Ожидает поднятия (слот {datetime.fromtimestamp(st['pending'], MSK):%H:%M})")
        if cfg.get("enabled"):
            toggle = ("⏸ Выключить своё расписание", ("cat_toggle", cid, "enabled"))
        elif d["all"] and not cfg.get("excluded"):
            toggle = ("🚫 Исключить из общего расписания", ("cat_toggle", cid, "excluded"))
        elif d["all"] and cfg.get("excluded"):
            toggle = ("🟢 Вернуть в общее расписание", ("cat_toggle", cid, "excluded"))
        else:
            toggle = ("⚪️ Включить расписание", ("cat_toggle", cid, "enabled"))
        rows = [[toggle], [("➕ Добавить время", ("cat_add", cid))]]
        rows += [[(f"🗑 {t}", ("cat_del", cid, t)) for t in own[i:i + 4]] for i in range(0, len(own), 4)]
        rows.append([("↩️ Сбросить на время по умолчанию", ("cat_reset", cid))])
        rows.append([("◀️ К категориям", ("cats",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_cat_toggle(self, chat, mid, cid, key):
        self.cat_cfg(cid)
        self.plugin.store.change(lambda d: d["cats"][str(cid)].update({key: not d["cats"][str(cid)].get(key)}))
        self.on_cat(chat, mid, cid)

    def on_cat_add(self, chat, mid, cid):
        self.ask(chat, "➕ Отправьте время по МСК (одно или несколько через запятую), например <code>18:00, 22:30</code>.",
                 ("cat_add", cid), mid)

    def in_cat_add(self, chat, text, cid):
        cfg, name = self.cat_cfg(cid)
        times = sorted(set(cfg.get("times", [])) | set(parse_times(text)))
        if len(times) > MAX_SLOTS:
            raise UserError(f"Максимум {MAX_SLOTS} слотов на категорию.")
        self.plugin.store.change(lambda d: d["cats"][str(cid)].update(times=times, enabled=True))
        self.on_cat(chat, None, cid)

    def on_cat_del(self, chat, mid, cid, value):
        self.cat_cfg(cid)
        self.plugin.store.change(lambda d: d["cats"][str(cid)].update(
            times=[t for t in d["cats"][str(cid)].get("times", []) if t != value]))
        self.on_cat(chat, mid, cid)

    def on_cat_reset(self, chat, mid, cid):
        self.cat_cfg(cid)
        self.plugin.store.change(lambda d: d["cats"][str(cid)].update(times=[]))
        self.on_cat(chat, mid, cid)

    # --- журнал
    def on_journal(self, chat, mid=None):
        log = self.plugin.store["log"]
        icons = {"raised": "⏫", "pending": "⏳", "miss": "⚠️"}
        lines = ["📋 <b>Журнал</b> (МСК)"] + [
            f"{datetime.fromtimestamp(i['t'], MSK):%d.%m %H:%M} {icons.get(i['kind'], '•')} {i['kind']} — "
            f"{esc(i['cat'])} {esc(i.get('text', ''))}" for i in log[:30]]
        if not log:
            lines.append("Событий пока нет.")
        self.send(chat, "\n".join(lines), [[("🔄 Обновить", ("journal",)), ("🗑 Очистить", ("journal_clear",))],
                                           [("◀️ Назад", ("home",))]], mid)

    def on_journal_clear(self, chat, mid):
        self.plugin.store.set(log=[])
        self.on_journal(chat, mid)


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = RaiseScheduler(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = RaisePanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


def start(cardinal, *args):
    if PLUGIN is not None:
        PLUGIN.start()


def stop(cardinal=None, *args):
    if PLUGIN is not None:
        PLUGIN.stop_event.set()


BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [start]
BIND_TO_PRE_STOP = [stop]
BIND_TO_DELETE = None
