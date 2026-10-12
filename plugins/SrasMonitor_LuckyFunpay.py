"""
🛡️ SRAS Monitor (скрытые ограничения рейтинга) — плагин для FunPay Cardinal от LuckyFunpay.

Следит за страницей FunPay sras/info, где показаны разделы, в которых ваши лоты скрыты
из поиска из-за недостатка рейтинга, и присылает уведомления об ужесточении/снятии ограничений.

ИНСТРУКЦИЯ (полная версия — в боте: /sras → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и перезапустите бота командой /restart.
2. Откройте /sras — увидите статус: «✅ Всё чисто!» или список разделов с ограничениями.
3. Нажмите «🔔 Включить уведомления» — бот сам будет проверять страницу и писать вам при изменениях.
4. «🔄 Проверить сейчас» — внеплановая проверка, «⏱ Интервал» — как часто проверять.

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

NAME = "SRAS Monitor"
VERSION = "1.0.0"
DESCRIPTION = ("🛡️ Мониторинг скрытых ограничений рейтинга FunPay (SRAS): уведомит, если лоты пропали из поиска "
               "из-за нехватки отзывов, и когда ограничение снимут. Панель: /sras")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "c3f1a7e2-58b4-4d6a-8e0b-2a9d4f6c1b37"
SETTINGS_PAGE = True
SLUG = "SrasMonitor"
PREFIX = "lfsras:"

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


SRAS_URL = "https://funpay.com/sras/info"
DEFAULTS = {"notify": False, "interval": 30, "items": {}, "checked": 0, "error": "", "history": [], "baseline": False}
SECTION_RE = re.compile(r"/(lots|chips)/(\d+)/?")
NUM_RE = re.compile(r"(?<![\d.,])(\d{1,5})(?![\d.,])")


def clean(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def parse_sras(page: str) -> dict:
    """Разбирает страницу sras/info: {ключ раздела: {name, link, text, required}}."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page, "lxml")
    if not soup.find(class_="user-link-name") and not soup.find(attrs={"data-app-data": True}):
        raise ValueError("FunPay вернул страницу без авторизации")
    content = soup.find(class_="page-content") or soup.find("main") or soup.body or soup
    items = {}
    for link in content.find_all("a", href=True):
        match = SECTION_RE.search(link["href"])
        if not match:
            continue
        row = link.find_parent(["tr", "li"]) or link.find_parent(
            lambda tag: tag.name == "div" and any(c for c in tag.get("class", []) if "item" in c or "row" in c))
        row = row or link.parent
        name = clean(link.get_text(" ")) or f"Раздел {match.group(2)}"
        text = clean(row.get_text(" "))
        rest = text.replace(name, " ", 1)
        numbers = [int(n) for n in NUM_RE.findall(rest)]
        key = f"{match.group(1)}-{match.group(2)}"
        items[key] = {"name": name, "link": f"https://funpay.com/{match.group(1)}/{match.group(2)}/",
                      "text": text[:300], "required": max(numbers) if numbers else None}
    return items


def diff(old: dict, new: dict):
    worse, better = [], []
    for key, item in new.items():
        before = old.get(key)
        if before is None:
            worse.append((item, None, item.get("required")))
        elif before.get("required") != item.get("required"):
            pair = (item, before.get("required"), item.get("required"))
            if (item.get("required") or 0) > (before.get("required") or 0):
                worse.append(pair)
            else:
                better.append(pair)
    for key, item in old.items():
        if key not in new:
            better.append((item, item.get("required"), None))
    return worse, better


def req_text(value) -> str:
    return "0 (нет)" if value is None else f"{value}⭐"


class SrasMonitor:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("settings.json", DEFAULTS)
        self.ui = None
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_event = threading.Event()
        self.thread = None

    def fetch(self) -> dict:
        response = self.cardinal.account.method("get", "sras/info", {"accept": "*/*"}, {}, raise_not_200=True)
        status = getattr(response, "status_code", 200)
        if status != 200:
            raise ValueError(f"HTTP {status}")
        return parse_sras(response.content.decode() if isinstance(response.content, bytes) else response.text)

    def check(self, notify=True):
        """Проверяет страницу. Возвращает (items, worse, better) или бросает UserError."""
        if not self.lock.acquire(blocking=False):
            raise UserError("Проверка уже выполняется.")
        try:
            try:
                items = self.fetch()
            except Exception as error:
                self.store.set(error=f"{type(error).__name__}: {error}"[:200], checked=time.time())
                LOG.warning("%s: не удалось получить sras/info: %s", NAME, error)
                raise UserError("Не удалось открыть страницу FunPay sras/info. Попробуйте позже.") from None
            data = self.store.snapshot()
            first = not data["baseline"]
            worse, better = diff(data["items"], items)

            def save(d):
                d.update(items=items, checked=time.time(), error="", baseline=True)
                if worse or better:
                    d["history"] = ([{"t": time.time(), "worse": len(worse), "better": len(better)}]
                                    + d["history"])[:30]

            self.store.change(save)
            if notify and not first and data["notify"] and (worse or better) and self.ui:
                self.alert(worse, better)
            return items, worse, better
        finally:
            self.lock.release()

    def alert(self, worse, better):
        parts = []
        if worse:
            parts.append("🔴 <b>ОГРАНИЧЕНИЯ УЖЕСТОЧЕНЫ!</b> FunPay требует больше рейтинга:")
            for item, before, after in worse:
                parts.append(f"• <a href=\"{item['link']}\">{esc(item['name'])}</a>\n"
                             f"  └ Требование: <code>{req_text(before)}</code> ➔ <b>{req_text(after)}</b>")
            parts.append("Лоты в этих разделах скрыты из поиска, пока вы не наберёте свежие отзывы.\n")
        if better:
            parts.append("🟢 <b>ОГРАНИЧЕНИЯ ОСЛАБЛЕНЫ!</b>")
            for item, before, after in better:
                parts.append(f"• <a href=\"{item['link']}\">{esc(item['name'])}</a>\n"
                             f"  └ Требование: <code>{req_text(before)}</code> ➔ <b>{req_text(after)}</b>")
            if any(after is None for _, _, after in better):
                parts.append("Ваши лоты в снятых разделах снова видны покупателям.")
        self.ui.notify("\n".join(parts), [[("🛡️ Открыть SRAS Monitor", ("home",)), ("🌐 sras/info", SRAS_URL)]])

    # --- фон
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.loop, name="SrasMonitor", daemon=True)
        self.thread.start()

    def loop(self):
        self.stop_event.wait(20)
        while not self.stop_event.is_set():
            try:
                if account_ready(self.cardinal) and self.store["notify"]:
                    self.check()
            except UserError:
                pass
            except Exception:
                LOG.exception("%s: ошибка фоновой проверки", NAME)
            delay = max(5, int(self.store["interval"])) * 60
            self.wake.wait(delay + random.randint(0, 60))
            self.wake.clear()

    def stop(self):
        self.stop_event.set()
        self.wake.set()


class SrasPanel(Panel):
    COMMAND = "sras"
    ICON = "🛡️"
    GUIDE = [
        ("🕵️‍♂️ Что такое SRAS", "На FunPay есть система <b>SRAS</b> — скрытые требования к рейтингу. Иногда лоты "
         "пропадают из общего списка, хотя они активны: FunPay включил для раздела требование вроде «нужно 5 "
         "отзывов в этой категории за последние 30 дней».\n\nПлагин следит за страницей "
         "<code>funpay.com/sras/info</code> и сообщает:\n🔴 об ужесточении — ваши лоты скрыты;\n"
         "🟢 об ослаблении — лимит снят или снижен, лоты снова видны."),
        ("🚀 Включение", "1. Положите <code>SrasMonitor_LuckyFunpay.py</code> в папку <code>plugins</code> или "
         "отправьте файл боту.\n2. /menu → Система и конфиги → Плагины → <b>SRAS Monitor</b> → включить.\n"
         "3. Перезапустите бота: /restart.\n4. Команда /sras открывает панель статуса."),
        ("📊 Панель", "✅ <b>Всё чисто!</b> — ограничений нет, ваши лоты видны всем.\n"
         "⚠️ <b>Найдены ограничения</b> — список разделов, где лоты скрыты, с требуемым количеством звёзд и "
         "ссылкой на раздел.\n\nПод списком — строка, как её показывает FunPay, чтобы вы видели точные цифры."),
        ("🔔 Уведомления", "Нажмите «🔔 Включить уведомления» — бот будет проверять страницу в фоне "
         "(по умолчанию раз в 30 минут, меняется кнопкой «⏱ Интервал») и пришлёт сообщение, если FunPay изменит "
         "правила для вашего аккаунта.\n\n«🔄 Проверить сейчас» — принудительная проверка вне расписания, "
         "если кажется, что продажи упали.\n\nПервая проверка запоминает текущее состояние, уведомления приходят "
         "об изменениях после неё."),
        ("💡 Пример уведомления", "🔴 <b>ОГРАНИЧЕНИЯ УЖЕСТОЧЕНЫ!</b> FunPay требует больше рейтинга:\n"
         "• Dota 2 &gt; Предметы\n  └ Требование: <code>0 (нет)</code> ➔ <b>5⭐</b>\n\n"
         "Это значит, что ваши лоты в Доте скрыты, пока вы не получите 5 свежих отзывов в этой категории.\n\n"
         "<i>Плагин читает страницу так, как её отдаёт FunPay. Если FunPay поменяет вёрстку, бот сообщит об "
         "ошибке проверки — напишите автору.</i>"),
    ]

    def on_home(self, chat, mid=None, *args):
        data = self.plugin.store.snapshot()
        lines = [f"🛡️ <b>{esc(NAME)}</b> v{VERSION}", ""]
        if not data["checked"]:
            lines.append("⏳ Ещё не проверялось. Нажмите «🔄 Проверить сейчас».")
        elif data["error"]:
            lines.append(f"❌ Последняя проверка не удалась ({fmt_dt(data['checked'])}).\n<code>{esc(data['error'])}</code>")
        elif not data["items"]:
            lines.append("✅ <b>Все чисто!</b> Ограничений нет, ваши лоты видны всем.")
        else:
            lines.append(f"⚠️ <b>Найдены ограничения: {len(data['items'])}</b>")
            for item in list(data["items"].values())[:25]:
                lines.append(f"• <a href=\"{item['link']}\">{esc(item['name'])}</a> — нужно "
                             f"<b>{req_text(item.get('required'))}</b>\n  <i>{esc(item.get('text', ''))[:160]}</i>")
        if data["checked"]:
            lines.append(f"\n🕒 Проверено: {fmt_dt(data['checked'])}")
        lines.append(f"🔔 Уведомления: {'🟢 включены' if data['notify'] else '🔴 выключены'} · "
                     f"интервал {data['interval']} мин")
        rows = [[("🔕 Выключить уведомления" if data["notify"] else "🔔 Включить уведомления", ("notify",))],
                [("🔄 Проверить сейчас", ("check",)), ("⏱ Интервал", ("interval",))],
                [("🌐 Страница SRAS", SRAS_URL)]]
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_notify(self, chat, mid):
        enabled = self.plugin.store.change(lambda d: d.update(notify=not d["notify"]) or d["notify"])
        if enabled:
            self.plugin.start()
        self.on_home(chat, mid)

    def on_check(self, chat, mid):
        self.send(chat, "⏳ Проверяю sras/info…", mid=mid)
        try:
            self.plugin.check(notify=True)
        except UserError:
            pass
        self.on_home(chat, mid)

    def on_interval(self, chat, mid):
        self.ask(chat, "⏱ Как часто проверять (в минутах, от 10 до 720)?", ("interval",), mid)

    def in_interval(self, chat, text):
        value = parse_int(text, 10, 720, "Интервал")
        self.plugin.store.set(interval=value)
        self.plugin.wake.set()
        self.on_home(chat)


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = SrasMonitor(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = SrasPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


def start(cardinal, *args):
    if PLUGIN is not None:
        PLUGIN.start()


def stop(cardinal=None, *args):
    if PLUGIN is not None:
        PLUGIN.stop()


BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [start]
BIND_TO_PRE_STOP = [stop]
BIND_TO_DELETE = None
