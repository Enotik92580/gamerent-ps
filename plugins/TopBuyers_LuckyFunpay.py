"""
👑 Top Customer Analytics — ваша личная CRM. Плагин для FunPay Cardinal от LuckyFunpay.

Сканирует всю историю продаж, считает, сколько денег принёс каждый покупатель (LTV), и строит
Топ-10 покупателей с медалями. Суммы в $ и € пересчитываются в рубли по курсу FunPay.

ИНСТРУКЦИЯ (полная версия — в боте: /top_buyers → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. При первом запуске плагин в фоне просканирует историю продаж (только закрытые заказы)
   и пришлёт уведомление, когда анализ завершится.
3. /top_buyers — сводка: клиентов в базе, общая выручка и Топ-10.
4. «🔄 Обновить» — дочитать новые заказы, «🗑 Сброс статистики» — пересчитать с нуля.

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

NAME = "Top Customer Analytics"
VERSION = "1.0.0"
DESCRIPTION = ("👑 Рейтинг ваших покупателей: сколько принёс каждый клиент за всё время (с пересчётом $ и € в ₽), "
               "общая выручка и Топ-10 с медалями. Команда: /top_buyers")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "1e6b8c3f-90a2-4d7e-b4f1-5a8c2d9e3b06"
SETTINGS_PAGE = True
SLUG = "TopBuyers"
PREFIX = "lftop:"

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


MEDALS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
DEFAULTS = {"buyers": {}, "counted": [], "scanned": False, "scan_t": 0, "pages": 0,
            "rates": {"USD": 95.0, "EUR": 105.0}, "rates_t": 0}


def cur_code(currency) -> str:
    code = enum_name(currency)
    return {"$": "USD", "€": "EUR", "₽": "RUB"}.get(str(currency), code)


def rub(value) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


class TopBuyers:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("buyers.json", DEFAULTS)
        self.ui = None
        self.scanning = threading.Lock()
        self.counted = set(self.store["counted"])
        self.sync = False

    def rate(self, code) -> float:
        if code == "RUB":
            return 1.0
        data = self.store.snapshot()
        if time.time() - data["rates_t"] > 6 * 3600:
            try:
                from FunPayAPI.common.enums import Currency
                fresh = {}
                for name in ("USD", "EUR"):
                    fresh[name] = float(self.cardinal.get_exchange_rate(getattr(Currency, name), Currency.RUB, 3600))
                self.store.set(rates=fresh, rates_t=time.time())
                data["rates"] = fresh
            except Exception:
                LOG.debug("%s: курс FunPay недоступен, использую сохранённый", NAME)
        return float(data["rates"].get(code, 0) or 0)

    def add(self, order) -> bool:
        oid = str(order.id)
        if oid in self.counted:
            return False
        code = cur_code(order.currency)
        try:
            price = float(order.price)
        except (TypeError, ValueError):
            return False
        value = price * self.rate(code)
        buyer_id = str(getattr(order, "buyer_id", "") or getattr(order, "buyer_username", ""))
        name = getattr(order, "buyer_username", "") or buyer_id

        def save(d):
            b = d["buyers"].setdefault(buyer_id, {"name": name, "rub": 0.0, "orders": 0, "cur": {}})
            b["name"] = name
            b["rub"] = round(b["rub"] + value, 2)
            b["orders"] += 1
            b["cur"][code] = round(b["cur"].get(code, 0) + price, 2)
            d["counted"].append(oid)

        self.counted.add(oid)
        self.store.change(save)
        return True

    def scan(self, full=True) -> int:
        if not self.scanning.acquire(blocking=False):
            return -1
        try:
            account = self.cardinal.account
            start, added, pages = None, 0, 0
            while pages < 3000:
                result = None
                for attempt in range(3):
                    try:
                        result = account.get_sales(start_from=start, include_paid=False, include_refunded=False,
                                                   state="closed")
                        break
                    except Exception:
                        time.sleep(3)
                if result is None:
                    break
                start, orders = result[0], result[1]
                pages += 1
                new = 0
                for order in orders:
                    if enum_name(order.status) == "CLOSED" and self.add(order):
                        new += 1
                added += new
                if not start or (not full and orders and new == 0):
                    break
                time.sleep(random.uniform(0.4, 0.9))
            self.store.set(scanned=True, scan_t=time.time(), pages=pages)
            return added
        finally:
            self.scanning.release()

    def top(self, n=10):
        buyers = self.store["buyers"]
        return sorted(buyers.items(), key=lambda kv: kv[1]["rub"], reverse=True)[:n]

    def report(self) -> str:
        d = self.store.snapshot()
        total = sum(b["rub"] for b in d["buyers"].values())
        orders = sum(b["orders"] for b in d["buyers"].values())
        lines = [f"👑 <b>{esc(NAME)}</b> v{VERSION}", "",
                 f"👥 Клиентов в базе: <b>{len(d['buyers'])}</b>",
                 f"🧾 Закрытых заказов: <b>{orders}</b>",
                 f"💰 Общая выручка: <b>{rub(total)}</b>"]
        if orders:
            lines.append(f"📈 Средний чек: {rub(total / orders)}")
        if self.scanning.locked():
            lines.append("\n⏳ Идёт сканирование истории продаж… Пользуйтесь ботом как обычно.")
        elif not d["scanned"]:
            lines.append("\nℹ️ История ещё не просканирована — нажмите «🔄 Обновить».")
        else:
            lines.append(f"🕒 Обновлено: {fmt_dt(d['scan_t'])}")
        top = self.top()
        if top:
            lines += ["", "📜 <b>Топ-10 покупателей</b>"]
            for i, (bid, b) in enumerate(top):
                link = f"https://funpay.com/users/{bid}/" if bid.isdigit() else None
                name = f"<a href=\"{link}\">{esc(b['name'])}</a>" if link else esc(b["name"])
                extra = ", ".join(f"{v:g} {k}" for k, v in b["cur"].items() if k != "RUB")
                lines.append(f"{MEDALS[i]} {name} — <b>{rub(b['rub'])}</b> · заказов: {b['orders']}"
                             + (f" ({extra})" if extra else ""))
        return "\n".join(lines)


class TopPanel(Panel):
    COMMAND = "top_buyers"
    ICON = "👑"
    GUIDE = [
        ("💎 Зачем это нужно", "Успешный бизнес строится на постоянных клиентах. Плагин отвечает на вопросы:\n"
         "• Кто мои VIP-клиенты?\n• Сколько денег принёс конкретный покупатель за всё время?\n• Кого стоит поощрить "
         "скидкой или бонусом?\n\n1. <b>LTV</b> — реальная ценность клиента за всё время.\n2. <b>«Киты»</b> — те, кто "
         "делает основную кассу.\n3. <b>Прозрачная бухгалтерия</b> — общая выручка со всех покупателей."),
        ("📊 Возможности", "🔍 <b>Глубокое сканирование</b> — вся история заказов на FunPay, даже за прошлые годы.\n"
         "💱 <b>Мультивалютность</b> — $ и € пересчитываются в рубли по текущему курсу FunPay (берётся у Cardinal, "
         "обновляется раз в 6 часов).\n🏆 <b>Лидерборд</b> — Топ-10 с медалями 🥇🥈🥉 и ссылками на профили.\n"
         "⚡️ <b>Живое обновление</b> — новые закрытые заказы добавляются моментально."),
        ("📖 Как использовать", "1. Включите плагин в списке плагинов Cardinal и сделайте /restart.\n2. При первом "
         "запуске начнётся фоновое сканирование — при тысячах продаж это займёт время, бот пришлёт уведомление.\n"
         "3. <code>/top_buyers</code> — сводка: клиентов, выручка, Топ-10.\n4. «🔄 Обновить» — дочитать новые заказы "
         "прямо сейчас. «🗑 Сброс статистики» — очистить базу и пересканировать (при сбоях или смене аккаунта)."),
        ("💡 Под капотом", "1. <b>Сбор</b> — учитываются только заказы со статусом «Закрыт» (деньги получены).\n"
         "2. <b>Агрегация</b> — по ID покупателя: 10 покупок по 100₽ = один клиент с чеком 1000₽.\n"
         "3. <b>Конвертация</b> — валютные заказы в рубли.\n4. <b>Мониторинг</b> — событие закрытия заказа сразу "
         "обновляет базу, повторное сканирование не нужно. Каждый заказ учитывается один раз."),
    ]

    def on_home(self, chat, mid=None, *args):
        rows = [[("🔄 Обновить", ("refresh",)), ("🗑 Сброс статистики", ("reset",))]]
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, self.plugin.report(), rows, mid)

    def start_scan(self, chat, full):
        def run():
            added = self.plugin.scan(full=full)
            if added >= 0:
                self.send(chat, f"✅ Анализ продаж завершён. Новых закрытых заказов учтено: {added}.",
                          [[("👑 Топ покупателей", ("home",))]])

        if self.plugin.sync:
            run()
        else:
            threading.Thread(target=run, name="TopBuyersScan", daemon=True).start()

    def on_refresh(self, chat, mid):
        if self.plugin.scanning.locked():
            raise UserError("Сканирование уже идёт.")
        self.start_scan(chat, full=not self.plugin.store["scanned"])
        self.on_home(chat, mid)

    def on_reset(self, chat, mid):
        self.send(chat, "🗑 Полностью очистить базу покупателей и заново просканировать историю?",
                  [[("✅ Да, сбросить", ("reset_ok",)), ("↩️ Нет", ("home",))]], mid)

    def on_reset_ok(self, chat, mid):
        if self.plugin.scanning.locked():
            raise UserError("Дождитесь окончания текущего сканирования.")
        rates = self.plugin.store["rates"]
        self.plugin.store.change(lambda d: (d.clear(), d.update(copy.deepcopy(DEFAULTS)), d.update(rates=rates)))
        self.plugin.counted = set()
        self.start_scan(chat, full=True)
        self.on_home(chat, mid)


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = TopBuyers(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = TopPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


def start(cardinal, *args):
    if PLUGIN is None or PLUGIN.store["scanned"]:
        return

    def run():
        added = PLUGIN.scan(full=True)
        if added >= 0 and PLUGIN.ui:
            PLUGIN.ui.notify(f"👑 <b>{esc(NAME)}</b>: анализ истории продаж завершён, учтено заказов: {added}.",
                             [[("👑 Топ покупателей", ("home",))]])

    threading.Thread(target=run, name="TopBuyersScan", daemon=True).start()


def status_changed(cardinal, event, *args):
    if PLUGIN is None:
        return
    order = event.order
    if enum_name(getattr(order, "status", "")) == "CLOSED":
        try:
            PLUGIN.add(order)
        except Exception:
            LOG.exception("%s: ошибка учёта заказа", NAME)


BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [start]
BIND_TO_ORDER_STATUS_CHANGED = [status_changed]
BIND_TO_DELETE = None
