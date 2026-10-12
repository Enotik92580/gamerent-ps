"""
🕰️ Old Orders Scanner — плагин для FunPay Cardinal от LuckyFunpay.

Находит оплаченные, но не подтверждённые покупателями заказы старше 24 часов и
составляет готовое обращение в техподдержку FunPay со списком номеров.

ИНСТРУКЦИЯ (полная версия — в боте: /old_orders → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. Откройте /old_orders и нажмите «🔎 Начать сканирование».
3. Если найдены «висяки» — нажмите «📝 Сформировать отчет для ТП», скопируйте текст
   (одним нажатием) и отправьте его в тикет поддержки FunPay.
Совет: запускайте сканер раз в 2-3 дня.

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

NAME = "Old Orders Scanner"
VERSION = "1.0.0"
DESCRIPTION = ("🕰️ Находит заказы «Оплачен», которые висят дольше 24 часов, и формирует готовое обращение "
               "в поддержку FunPay для их закрытия. Панель: /old_orders")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "8e2b6d14-0a7f-4c93-b5e1-d47c09a3f562"
SETTINGS_PAGE = True
SLUG = "OldOrders"
PREFIX = "lfold:"

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


SUPPORT_URL = "https://support.funpay.com/tickets/new"
DEFAULT_REPORT = ("Здравствуйте! Прошу подтвердить выполнение следующих заказов (покупатели не отвечают более "
                  "$hours часов): $orders\nВсе обязательства выполнены. Заранее благодарю!")
DEFAULTS = {"hours": 24, "max_pages": 100, "report": DEFAULT_REPORT, "result": [], "scanned": 0, "pages": 0}
SPINNER = "◐◓◑◒"


def order_ts(date) -> float | None:
    if date is None:
        return None
    if isinstance(date, (int, float)):
        return float(date)
    try:
        return date.timestamp()
    except Exception:
        return None


class OldOrders:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("settings.json", DEFAULTS)
        self.ui = None
        self.job = None
        self.lock = threading.Lock()

    def scan(self, job, progress=None) -> list:
        """Обходит страницы продаж со статусом «Оплачен» и возвращает заказы старше порога."""
        account = self.cardinal.account
        hours = self.store["hours"]
        border = time.time() - hours * 3600
        found, start, pages, seen = [], None, 0, set()
        while not job.stop.is_set() and pages < self.store["max_pages"]:
            for attempt in range(3):
                try:
                    result = account.get_sales(start_from=start, include_paid=True, include_closed=False,
                                               include_refunded=False, state="paid")
                    break
                except Exception as error:
                    if attempt == 2:
                        raise UserError(f"FunPay не отдал страницу продаж: {type(error).__name__}") from None
                    time.sleep(2)
            start, orders = result[0], result[1]
            pages += 1
            for order in orders:
                status = enum_name(getattr(order, "status", ""))
                oid = str(order.id).lstrip("#")
                if status != "PAID" or oid in seen:
                    continue
                seen.add(oid)
                ts = order_ts(getattr(order, "date", None))
                if ts is not None and ts <= border:
                    found.append({"id": oid, "ts": ts, "buyer": getattr(order, "buyer_username", ""),
                                  "price": getattr(order, "price", 0), "currency": str(getattr(order, "currency", "")),
                                  "desc": (getattr(order, "description", "") or "")[:80]})
            job.done = pages
            if progress:
                progress(pages, len(found))
            if not start:
                break
            time.sleep(random.uniform(0.6, 1.2))
        found.sort(key=lambda o: o["ts"])
        self.store.set(result=found, scanned=time.time(), pages=pages)
        return found

    def report(self, orders=None) -> list:
        orders = self.store["result"] if orders is None else orders
        template = self.store["report"] or DEFAULT_REPORT
        texts = []
        for i in range(0, len(orders), 40):
            ids = ", ".join("#" + o["id"] for o in orders[i:i + 40])
            texts.append(template.replace("$orders", ids).replace("$hours", str(self.store["hours"])))
        return texts


class OldOrdersPanel(Panel):
    COMMAND = "old_orders"
    ICON = "🕰️"
    GUIDE = [
        ("💰 Зачем это нужно", "На FunPay деньги замораживаются до подтверждения покупателем. Если покупатель молчит, "
         "ваши деньги «висят в воздухе». По правилам площадки, если покупатель не отвечает более 24 часов после "
         "выдачи товара, продавец может обратиться в поддержку для принудительного завершения сделки.\n\n"
         "Плагин:\n1. Находит «висяки» — заказы в статусе «Оплачен» старше 24 часов.\n"
         "2. Составляет готовый текст обращения в ТП со списком номеров.\n3. Экономит время — не нужно листать "
         "сотни страниц истории вручную."),
        ("🚀 Запуск", "1. Положите <code>OldOrders_LuckyFunpay.py</code> в <code>plugins</code> или отправьте файл боту.\n"
         "2. /menu → Система и конфиги → Плагины → <b>Old Orders Scanner</b> → включить, затем /restart.\n"
         "3. Откройте /old_orders и нажмите «🔎 Начать сканирование». Бот проверит страницы продаж — "
         "вы увидите анимацию прогресса. Сканирование можно остановить кнопкой «⛔️ Стоп»."),
        ("📋 Результат и отчёт", "• Всё чисто — бот сообщит, что просроченных заказов нет.\n"
         "• Найдены заказы — бот покажет количество и номера (#AbCdEf12…).\n\n"
         "Нажмите «📝 Сформировать отчет для ТП» — придёт готовый текст:\n<i>Здравствуйте! Прошу подтвердить "
         "выполнение следующих заказов (покупатели не отвечают более 24 часов): #12345678, #87654321 Все "
         "обязательства выполнены. Заранее благодарю!</i>\n\nНажмите на текст — он скопируется. Отправьте его в "
         "тикет (кнопка «🆘 Поддержка FunPay»). Если заказов больше 40, текст делится на несколько частей."),
        ("⚙️ Настройки и совет", "«⏱ Порог» — сколько часов должен висеть заказ (по умолчанию 24).\n"
         "«✏️ Текст отчёта» — свой шаблон обращения, переменные <code>$orders</code> (список номеров) и "
         "<code>$hours</code> (порог).\n\n💡 Запускайте сканер раз в 2–3 дня, чтобы быстрее получать деньги на баланс."),
    ]

    def result_text(self, data):
        orders = data["result"]
        if not data["scanned"]:
            return "Сканирование ещё не запускалось."
        head = f"🕒 Последнее сканирование: {fmt_dt(data['scanned'])} · страниц: {data['pages']}\n"
        if not orders:
            return head + "✅ <b>Всё чисто!</b> Просроченных заказов нет."
        lines = [head + f"⚠️ <b>Найдено «зависших» сделок: {len(orders)}</b> (старше {data['hours']} ч)"]
        for o in orders[:30]:
            lines.append(f"• <a href=\"https://funpay.com/orders/{o['id']}/\">#{esc(o['id'])}</a> — "
                         f"{esc(o['buyer'])}, {o['price']:g} {esc(o['currency'])}, висит {ago(o['ts'])}")
        if len(orders) > 30:
            lines.append(f"… и ещё {len(orders) - 30}")
        return "\n".join(lines)

    def on_home(self, chat, mid=None, *args):
        data = self.plugin.store.snapshot()
        text = (f"🕰️ <b>{esc(NAME)}</b> v{VERSION}\n\nИщет заказы «Оплачен», которые висят дольше "
                f"<b>{data['hours']} ч</b>, и готовит обращение в поддержку.\n\n" + self.result_text(data))
        rows = [[("🔎 Начать сканирование", ("scan",))]]
        if data["result"]:
            rows.append([("📝 Сформировать отчет для ТП", ("report",))])
        rows.append([("⏱ Порог: " + str(data["hours"]) + " ч", ("hours",)), ("✏️ Текст отчёта", ("text",))])
        rows.append([("🆘 Поддержка FunPay", SUPPORT_URL)])
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, text, rows, mid)

    def on_scan(self, chat, mid):
        plugin = self.plugin
        with plugin.lock:
            if plugin.job is not None:
                raise UserError("Сканирование уже идёт.")
            job = plugin.job = Job("scan")
        msg = self.send(chat, "🔎 Начинаю сканирование продаж…", [[("⛔️ Стоп", ("stop",))]], mid)
        mid = getattr(msg, "message_id", mid)
        last = [0.0]

        def progress(pages, found):
            if time.time() - last[0] < 1.5:
                return
            last[0] = time.time()
            self.send(chat, f"{SPINNER[pages % 4]} Сканирую продажи… страница <b>{pages}</b>\n"
                            f"Найдено «висяков»: <b>{found}</b>", [[("⛔️ Стоп", ("stop",))]], mid)

        def run():
            try:
                found = plugin.scan(job, progress)
                stopped = "⛔️ Остановлено вручную. " if job.stop.is_set() else ""
                self.send(chat, stopped + ("✅ Сканирование завершено." if not found else
                                           f"⚠️ Найдено {len(found)} заказ(ов)."), mid=mid)
            except UserError as error:
                self.send(chat, "❌ " + esc(error), mid=mid)
            except Exception:
                LOG.exception("%s: ошибка сканирования", NAME)
                self.send(chat, "❌ Ошибка сканирования, подробности в логе.", mid=mid)
            finally:
                with plugin.lock:
                    plugin.job = None
                self.on_home(chat)

        threading.Thread(target=run, name="OldOrdersScan", daemon=True).start()

    def on_stop(self, chat, mid):
        job = self.plugin.job
        if job:
            job.stop.set()

    def on_report(self, chat, mid):
        texts = self.plugin.report()
        if not texts:
            raise UserError("Нет заказов для отчёта — сначала запустите сканирование.")
        for i, text in enumerate(texts, 1):
            head = f"📝 <b>Обращение в ТП{f' (часть {i}/{len(texts)})' if len(texts) > 1 else ''}</b> — нажмите на текст, чтобы скопировать:\n\n"
            self.send(chat, head + f"<code>{esc(text)}</code>",
                      [[("🆘 Создать тикет", SUPPORT_URL)]] if i == len(texts) else None)

    def on_hours(self, chat, mid):
        self.ask(chat, "⏱ Через сколько часов после оплаты считать заказ «висяком»? (от 1 до 720, обычно 24)",
                 ("hours",), mid)

    def in_hours(self, chat, text):
        self.plugin.store.set(hours=parse_int(text, 1, 720, "Порог"))
        self.on_home(chat)

    def on_text(self, chat, mid):
        self.ask(chat, "✏️ Отправьте шаблон обращения (до 1500 символов). Переменные: <code>$orders</code> — "
                       "номера заказов, <code>$hours</code> — порог.\nОтправьте <code>-</code> для стандартного текста.\n\n"
                       f"Сейчас:\n<i>{esc(self.plugin.store['report'])}</i>", ("report",), mid)

    def in_report(self, chat, text):
        if len(text) > 1500:
            raise UserError("Максимум 1500 символов.")
        value = DEFAULT_REPORT if text.strip() == "-" else text.strip()
        if "$orders" not in value:
            raise UserError("В тексте должна быть переменная $orders — на её место встанут номера заказов.")
        self.plugin.store.set(report=value)
        self.on_home(chat)


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = OldOrders(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = OldOrdersPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


BIND_TO_PRE_INIT = [init]
BIND_TO_DELETE = None
