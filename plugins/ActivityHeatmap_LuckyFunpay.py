"""
📊 Activity Heatmap — пульс вашего магазина. Плагин для FunPay Cardinal от LuckyFunpay.

Строит тепловую карту активности покупателей по дням недели и времени суток: заказы и первые
сообщения клиентов. Показывает лучший день недели и пиковый час.

ИНСТРУКЦИЯ (полная версия — в боте: /activity → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. При первом запуске плагин сам просканирует до 500 последних заказов.
3. /activity — карта: 🌃 ночь, 🌅 утро, ☀️ день, 🌆 вечер; ⚪️ тишина → 🟢 → 🟡 → 🟠 → 🔴 пик.
4. «🔄 Обновить» — пересчитать, «ℹ️ Цвета» — легенда, «🌍 Пояс» — ваш часовой пояс.

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

NAME = "Activity Heatmap"
VERSION = "1.0.0"
DESCRIPTION = ("📊 Тепловая карта активности покупателей по дням недели и времени суток: лучший день, пиковый час, "
               "анализ до 500 прошлых заказов и живое обновление. Команда: /activity")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "4b9a2c7e-d315-4f86-a0e2-6c8d1f5b3a79"
SETTINGS_PAGE = True
SLUG = "ActivityHeatmap"
PREFIX = "lfheat:"

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


DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DAYS_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
BLOCKS = [("🌃", "Ночь", 0), ("🌅", "Утро", 6), ("☀️", "День", 12), ("🌆", "Вечер", 18)]
LEGEND = ("⚪️ — тишина (нет активности)\n🟢 — низкая\n🟡 — средняя\n🟠 — высокая, нужно быть начеку\n"
          "🔴 — ПИК, прайм-тайм")
DEFAULTS = {"orders": [[0] * 24 for _ in range(7)], "messages": [[0] * 24 for _ in range(7)], "offset": 3,
            "scanned": False, "scan_t": 0, "seen": [], "last_msg": {}, "max_orders": 500, "gap_hours": 6}


def level(value, peak) -> str:
    if not value or not peak:
        return "⚪️"
    ratio = value / peak
    if ratio > 0.75:
        return "🔴"
    if ratio > 0.5:
        return "🟠"
    if ratio > 0.25:
        return "🟡"
    return "🟢"


class Heatmap:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("heatmap.json", DEFAULTS)
        self.ui = None
        self.scanning = threading.Lock()

    def slot(self, ts) -> tuple:
        dt = datetime.fromtimestamp(ts, timezone(timedelta(hours=self.store["offset"])))
        return dt.weekday(), dt.hour

    def add(self, kind, ts):
        day, hour = self.slot(ts)
        self.store.change(lambda d: d[kind][day].__setitem__(hour, d[kind][day][hour] + 1))

    def add_order(self, oid, ts):
        oid = str(oid)

        def save(d):
            if oid in d["seen"]:
                return False
            d["seen"] = (d["seen"] + [oid])[-3000:]
            day, hour = self.slot(ts)
            d["orders"][day][hour] += 1
            return True

        return self.store.change(save)

    def on_message(self, chat, author_id, ts=None):
        ts = ts or time.time()
        key = str(chat)

        def save(d):
            last = d["last_msg"].get(key, 0)
            d["last_msg"][key] = ts
            if len(d["last_msg"]) > 5000:
                for old in sorted(d["last_msg"], key=d["last_msg"].get)[:1000]:
                    d["last_msg"].pop(old, None)
            if ts - last < d["gap_hours"] * 3600:
                return False
            day, hour = self.slot(ts)
            d["messages"][day][hour] += 1
            return True

        return self.store.change(save)

    def scan(self) -> int:
        if not self.scanning.acquire(blocking=False):
            return -1
        try:
            account = self.cardinal.account
            start, counted, pages = None, 0, 0
            limit = self.store["max_orders"]
            while counted < limit and pages < 60:
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
                    if counted >= limit:
                        break
                    if enum_name(order.status) == "REFUNDED":
                        continue
                    date = getattr(order, "date", None)
                    try:
                        ts = date.timestamp()
                    except Exception:
                        continue
                    if self.add_order(order.id, ts):
                        counted += 1
                if not start:
                    break
                time.sleep(random.uniform(0.5, 1.0))
            self.store.set(scanned=True, scan_t=time.time())
            return counted
        finally:
            self.scanning.release()

    def totals(self):
        d = self.store.snapshot()
        grid = [[d["orders"][day][h] + d["messages"][day][h] for h in range(24)] for day in range(7)]
        return d, grid

    def render(self) -> str:
        d, grid = self.totals()
        cells = [[sum(grid[day][start:start + 6]) for _, _, start in BLOCKS] for day in range(7)]
        peak = max(max(row) for row in cells)
        lines = [f"📊 <b>{NAME}</b> — пульс магазина", f"Часовой пояс: UTC{d['offset']:+d}", "",
                 "<code>    " + "  ".join(icon for icon, _, _ in BLOCKS) + "</code>"]
        for day in range(7):
            lines.append(f"<code>{DAYS[day]}</code>  " + "  ".join(level(v, peak) for v in cells[day]))
        orders = sum(map(sum, d["orders"]))
        messages = sum(map(sum, d["messages"]))
        lines += ["", f"🧾 Заказов учтено: <b>{orders}</b> · 💬 диалогов: <b>{messages}</b>"]
        if orders + messages:
            day_sum = [sum(row) for row in grid]
            best_day = max(range(7), key=lambda i: day_sum[i])
            hours = [sum(grid[day][h] for day in range(7)) for h in range(24)]
            peak_hour = max(range(24), key=lambda h: hours[h])
            quiet_day = min(range(7), key=lambda i: day_sum[i])
            lines += [f"🏆 Лучший день недели: <b>{DAYS_FULL[best_day]}</b>",
                      f"⏰ Пиковый час: <b>{peak_hour:02d}:00–{(peak_hour + 1) % 24:02d}:00</b>",
                      f"😴 Самый тихий день: {DAYS_FULL[quiet_day]}"]
        else:
            lines.append("Данных пока нет — дождитесь первого сканирования или заказов.")
        if not d["scanned"]:
            lines.append("\n⏳ Идёт первичный анализ истории заказов…" if self.scanning.locked()
                         else "\nℹ️ История заказов ещё не загружена — нажмите «🔄 Обновить».")
        lines.append("\n🌃 00–06 · 🌅 06–12 · ☀️ 12–18 · 🌆 18–24")
        return "\n".join(lines)


class HeatmapPanel(Panel):
    COMMAND = "activity"
    ICON = "📊"
    GUIDE = [
        ("🚀 Для чего это нужно", "Продажи зависят не только от цены, но и от вашего онлайна. Зная пиковые часы, вы:\n"
         "1. <b>Максимизируете прибыль</b> — будете у компьютера, когда клиенты активнее всего.\n2. <b>Оптимизируете "
         "график</b> — не дежурите в «мёртвые часы».\n3. <b>Настроите автоподнятие</b> на «горячие» часы.\n"
         "4. <b>Спланируете выходные</b> — в дни с наименьшей активностью."),
        ("⚙️ Возможности", "📅 <b>Анализ истории</b> — при первом запуске плагин сканирует до 500 последних заказов.\n"
         "👀 <b>Визуализация</b> — тепловая карта прямо в Telegram.\n⚡️ <b>Real-time</b> — учитываются новые заказы и "
         "первые сообщения клиентов (сообщение считается новым диалогом, если покупатель не писал 6 часов).\n"
         "🏆 <b>Умная сводка</b> — лучший день недели и пиковый час."),
        ("📖 Как читать карту", "Команда <code>/activity</code>. Сутки разделены на 4 блока по 6 часов:\n"
         "🌃 Ночь (00:00–06:00)\n🌅 Утро (06:00–12:00)\n☀️ День (12:00–18:00)\n🌆 Вечер (18:00–00:00)\n\n" + LEGEND +
         "\n\nКнопки: «🔄 Обновить» — пересчитать с учётом новых событий; «ℹ️ Цвета» — легенда; «🌍 Пояс» — смещение "
         "от UTC (Москва = 3); «🗑 Сброс» — очистить и пересканировать историю."),
        ("💡 Под капотом", "1. <b>Сбор</b> — время успешных сделок (без возвратов) и первых сообщений клиентов.\n"
         "2. <b>Агрегация</b> — по дням недели (Пн–Вс) и часам.\n3. <b>Нормализация</b> — находится максимум и "
         "строится градиент относительно него, поэтому карта подходит и для 5, и для 500 заказов в день.\n"
         "4. <b>Фон</b> — каждое новое событие записывается, статистика точнее с каждым днём."),
    ]

    def on_home(self, chat, mid=None, *args):
        rows = [[("🔄 Обновить", ("refresh",)), ("ℹ️ Цвета", ("legend",))],
                [("🌍 Пояс: UTC" + f"{self.plugin.store['offset']:+d}", ("offset",)), ("🗑 Сброс", ("reset",))]]
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, self.plugin.render(), rows, mid)

    def on_refresh(self, chat, mid):
        if not self.plugin.store["scanned"] and not self.plugin.scanning.locked():
            self.start_scan(chat)
        self.on_home(chat, mid)

    def start_scan(self, chat=None):
        def run():
            count = self.plugin.scan()
            if chat is not None and count >= 0:
                self.send(chat, f"✅ Анализ истории завершён: учтено заказов — {count}.", [[("📊 Карта", ("home",))]])

        if getattr(self.plugin, "sync", False):
            run()
        else:
            threading.Thread(target=run, name="HeatmapScan", daemon=True).start()

    def on_legend(self, chat, mid):
        self.answer(self.call, LEGEND, alert=True)

    def on_offset(self, chat, mid):
        self.ask(chat, "🌍 Смещение от UTC в часах (от -12 до 14). Москва — 3. Новые события будут учитываться в этом "
                       "поясе; для пересчёта истории нажмите «🗑 Сброс».", ("offset",), mid)

    def in_offset(self, chat, text):
        self.plugin.store.set(offset=parse_int(text.replace("+", ""), -12, 14, "Смещение"))
        self.on_home(chat)

    def on_reset(self, chat, mid):
        self.send(chat, "🗑 Очистить статистику и заново просканировать историю заказов?",
                  [[("✅ Да", ("reset_ok",)), ("↩️ Нет", ("home",))]], mid)

    def on_reset_ok(self, chat, mid):
        offset = self.plugin.store["offset"]
        self.plugin.store.change(lambda d: (d.clear(), d.update(copy.deepcopy(DEFAULTS)), d.update(offset=offset)))
        self.start_scan(chat)
        self.on_home(chat, mid)


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = Heatmap(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = HeatmapPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


def start(cardinal, *args):
    if PLUGIN is not None and not PLUGIN.store["scanned"]:
        threading.Thread(target=PLUGIN.scan, name="HeatmapScan", daemon=True).start()


def new_order(cardinal, event, *args):
    if PLUGIN is not None:
        try:
            PLUGIN.add_order(event.order.id, time.time())
        except Exception:
            LOG.exception("%s: ошибка учёта заказа", NAME)


def new_message(cardinal, event, *args):
    if PLUGIN is None:
        return
    msg = getattr(event, "message", None)
    author = getattr(msg, "author_id", None)
    if msg is None or author in (None, 0, getattr(cardinal.account, "id", None)) or getattr(msg, "by_bot", False):
        return
    chat = getattr(msg, "chat_id", None)
    if not str(chat).isdigit():
        return
    try:
        PLUGIN.on_message(chat, author)
    except Exception:
        LOG.exception("%s: ошибка учёта сообщения", NAME)


BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [start]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_NEW_MESSAGE = [new_message]
BIND_TO_DELETE = None
