"""
💸 Auto Withdraw (автовывод) — плагин для FunPay Cardinal от LuckyFunpay.

Сам следит за балансом FunPay и выводит деньги на карту, кошелёк, USDT или по СБП, оставляя на балансе
«несгораемый остаток». Поддерживает двухфакторную защиту (Google Authenticator): одноразовый код
генерируется автоматически. Есть ручной вывод в 1 клик.

ИНСТРУКЦИЯ (полная версия — в боте: /autowithdraw → 📖 Инструкция)
1. Положите файл в папку plugins/ (или отправьте боту), включите плагин в
   /menu → Система и конфиги → Плагины и ОБЯЗАТЕЛЬНО перезапустите бота командой /restart.
2. /autowithdraw → «⚙️ Настроить реквизиты»: валюта → способ вывода → (банк для СБП) → реквизиты.
3. Если на FunPay включён Google Authenticator — «🔐 2FA» → «➕ Ввести секрет 2FA».
4. «💵 Триггер» — сумма, при накоплении которой начинается вывод (0 — автовывод выключен),
   «🛡️ Остаток» — сумма, которая всегда остаётся на балансе. Затем «Вкл».
Вывод запускается, когда доступный баланс ≥ Триггер + Остаток; выводится всё, кроме Остатка.
Первый вывод проверьте на небольшой сумме — бот покажет ответ FunPay.

Автор: @LuckyFunPay_Support · Канал: https://t.me/LuckyFunPay
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import html
import io
import json
import logging
import math
import os
import random
import re
import struct
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

NAME = "Auto Withdraw"
VERSION = "1.0.0"
DESCRIPTION = ("💸 Автовывод баланса FunPay на карту, кошелёк, USDT или СБП с несгораемым остатком, поддержкой "
               "Google Authenticator (2FA) и ручным выводом в 1 клик. Панель: /autowithdraw")
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "d6f1b3a8-9c42-4e75-8a0d-5b2e7c9f3d16"
SETTINGS_PAGE = True
SLUG = "AutoWithdraw"
PREFIX = "lfaw:"

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


CURRENCIES = {"rub": "₽ (RUB)", "usd": "$ (USD)", "eur": "€ (EUR)"}
SIGNS = {"rub": "₽", "usd": "$", "eur": "€"}
FALLBACK_METHODS = {
    "rub": [("card_rub", "Банковская карта (RUB)"), ("fps", "СБП (по номеру телефона)"), ("usdt_trc", "USDT TRC20"),
            ("qiwi", "QIWI")],
    "usd": [("card_usd", "Банковская карта (USD)"), ("usdt_trc", "USDT TRC20"), ("binance", "Binance Pay"),
            ("wmz", "WebMoney WMZ")],
    "eur": [("card_eur", "Банковская карта (EUR)"), ("usdt_trc", "USDT TRC20")],
}
SBP_IDS = ("fps", "sbp")
DEFAULTS = {"enabled": False, "currency": "rub", "method": "", "method_label": "", "bank": "", "bank_label": "",
            "wallet": "", "wallet_label": "", "trigger": 0.0, "reserve": 0.0, "totp": "", "history": [],
            "balance": None, "balance_t": 0}
PAGE = 8


# --- Google Authenticator (TOTP, RFC 6238)
def clean_secret(secret: str) -> str:
    value = re.sub(r"[\s-]", "", str(secret)).upper()
    if not re.fullmatch(r"[A-Z2-7]{16,128}=*", value):
        raise UserError("Секрет 2FA — это строка из латинских букв A–Z и цифр 2–7 (например CQ7K5US5WSDSNICQ).")
    return value.rstrip("=")


def totp(secret: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    counter = int((at if at is not None else time.time()) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def parse_withdraw_form(page: str) -> dict:
    """Читает форму вывода со страницы баланса FunPay: способы, банки СБП, поле кода 2FA."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(page, "lxml")
    form = soup.find("form", attrs={"action": re.compile("withdraw")}) or soup.find(
        "form", class_=re.compile("withdraw"))
    result = {"fields": {}, "methods": [], "banks": [], "bank_field": None, "code_field": None,
              "method_field": "ext_currency_id"}
    if form is None:
        return result
    for field in form.find_all("input"):
        name = field.get("name")
        if not name:
            continue
        result["fields"][name] = field.get("value") or ""
        if re.search(r"code|otp|totp|2fa|authenticator", name, re.I):
            result["code_field"] = name
    for select in form.find_all("select"):
        name = select.get("name") or ""
        options = []
        for option in select.find_all("option"):
            value = option.get("value") or ""
            if not value:
                continue
            attrs = {k: v for k, v in option.attrs.items() if k.startswith("data-")}
            group = option.find_parent("optgroup")
            options.append({"value": value, "text": option.get_text(" ", strip=True), "attrs": attrs,
                            "group": group.get("label", "") if group else ""})
        if "ext_currency" in name or name in ("method", "type_id"):
            result["methods"] = options
            result["method_field"] = name
        elif "bank" in name:
            result["banks"] = options
            result["bank_field"] = name
        elif re.search(r"code|otp", name, re.I):
            result["code_field"] = name
    return result


class Withdraw:
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.store = JsonStore("settings.json", DEFAULTS)
        self.ui = None
        self.lock = threading.Lock()
        self.timer = None
        self.form_cache = None
        self.sync = False

    def account(self):
        return self.cardinal.account

    # --- данные FunPay
    def form(self, force=False) -> dict:
        if self.form_cache and not force and time.time() - self.form_cache[0] < 600:
            return self.form_cache[1]
        try:
            response = self.account().method("get", "account/balance", {"accept": "*/*"}, {}, raise_not_200=True)
            content = response.content
            data = parse_withdraw_form(content.decode() if isinstance(content, bytes) else response.text)
        except Exception as error:
            LOG.warning("%s: форма вывода не прочитана: %s", NAME, error)
            data = parse_withdraw_form("")
        self.form_cache = (time.time(), data)
        return data

    def methods(self, currency) -> list:
        parsed = self.form()["methods"]
        if parsed:
            matched = [m for m in parsed if not m["attrs"].get("data-currency") or
                       m["attrs"].get("data-currency", "").lower() == currency]
            return [(m["value"], m["text"]) for m in (matched or parsed)]
        return FALLBACK_METHODS.get(currency, [])

    def balance(self, force=False):
        data = self.store.snapshot()
        if not force and data["balance"] and time.time() - data["balance_t"] < 4 * 3600:
            return data["balance"]
        if hasattr(self.cardinal, "get_balance"):
            bal = self.cardinal.get_balance()
        else:
            bal = self.account().get_balance()
        value = {"rub": float(bal.available_rub), "usd": float(bal.available_usd), "eur": float(bal.available_eur),
                 "total_rub": float(bal.total_rub), "total_usd": float(bal.total_usd), "total_eur": float(bal.total_eur)}
        self.store.set(balance=value, balance_t=time.time())
        return value

    def wallets(self) -> list:
        try:
            return list(self.account().get_wallets())
        except Exception:
            return []

    # --- вывод
    def ready_problem(self):
        d = self.store.snapshot()
        if not d["method"]:
            return "Сначала настройте реквизиты."
        if not d["wallet"]:
            return "Не заданы реквизиты (карта/номер/кошелёк)."
        if d["method"] in SBP_IDS and self.form()["banks"] and not d["bank"]:
            return "Для СБП выберите банк."
        return None

    def withdraw(self, amount: float, reason: str) -> tuple:
        """Отправляет запрос на вывод. Возвращает (ok, текст)."""
        problem = self.ready_problem()
        if problem:
            return False, problem
        d = self.store.snapshot()
        form = self.form(force=True)
        amount_int = int(math.floor(amount))
        if amount_int <= 0:
            return False, "Сумма к выводу меньше 1."
        account = self.account()
        payload = {k: v for k, v in form["fields"].items() if k != form.get("code_field")}
        payload.update({"csrf_token": getattr(account, "csrf_token", payload.get("csrf_token", "")),
                        "currency_id": d["currency"], form["method_field"]: d["method"], "wallet": d["wallet"],
                        "amount_int": str(amount_int)})
        if d["bank"] and form["bank_field"]:
            payload[form["bank_field"]] = d["bank"]
        elif d["bank"]:
            payload["bank"] = d["bank"]
        if d["totp"]:
            payload[form.get("code_field") or "code"] = totp(d["totp"])
        try:
            response = account.method("post", "withdraw/withdraw", {"accept": "*/*", "x-requested-with": "XMLHttpRequest"},
                                      payload, raise_not_200=True)
            data = response.json()
        except Exception as error:
            text = f"FunPay не принял запрос ({type(error).__name__})."
            self.record(amount_int, False, text, reason)
            return False, text
        if data.get("error"):
            msg = re.sub(r"<[^>]+>", " ", str(data.get("msg") or data.get("error"))).strip()
            if re.search(r"код|code|аутентиф|2fa|sms|смс|почт|email", msg, re.I):
                msg += ("\nℹ️ FunPay запросил подтверждение. Настройте Google Authenticator в «🔐 2FA» (СМС/почту бот "
                        "прочитать не может)." if not d["totp"] else
                        "\nℹ️ Проверьте секрет 2FA и время сервера (коды должны совпадать с приложением).")
            self.record(amount_int, False, msg, reason)
            return False, msg
        received = data.get("amount_ext")
        text = f"Заявка на вывод {amount_int} {SIGNS[d['currency']]} создана" + (
            f", к зачислению: {received}" if received else "") + "."
        self.record(amount_int, True, text, reason)
        self.store.set(balance=None, balance_t=0)
        return True, text

    def record(self, amount, ok, text, reason):
        entry = {"t": time.time(), "amount": amount, "ok": ok, "text": text[:300], "reason": reason}
        self.store.change(lambda d: d.__setitem__("history", ([entry] + d["history"])[:20]))

    def check(self, reason="auto") -> tuple | None:
        """Проверяет условия автовывода. Возвращает результат вывода или None."""
        d = self.store.snapshot()
        if not d["enabled"] or d["trigger"] <= 0:
            return None
        if not self.lock.acquire(blocking=False):
            return None
        try:
            try:
                available = self.balance(force=True)[d["currency"]]
            except Exception:
                LOG.warning("%s: баланс не получен", NAME)
                return None
            if available + 1e-9 < d["trigger"] + d["reserve"]:
                return None
            amount = available - d["reserve"]
            ok, text = self.withdraw(amount, reason)
            if self.ui:
                self.ui.notify(("✅ <b>Автовывод</b>: " if ok else "❌ <b>Автовывод не выполнен</b>: ") + esc(text),
                               [[("💸 Auto Withdraw", ("home",))]])
            return ok, text
        finally:
            self.lock.release()

    def schedule_check(self, delay=20):
        if self.sync:
            self.check("заказ подтверждён")
            return
        if self.timer and self.timer.is_alive():
            return
        self.timer = threading.Timer(delay, self.check, args=("заказ подтверждён",))
        self.timer.daemon = True
        self.timer.start()


class WithdrawPanel(Panel):
    COMMAND = "autowithdraw"
    ICON = "💸"
    GUIDE = [
        ("🚀 Возможности", "🤖 <b>Автоматизация</b> — бот проверяет баланс, когда покупатель подтверждает заказ; набралась "
         "сумма — деньги уходят на вывод.\n💳 <b>Реквизиты</b> — карты, USDT, электронные кошельки, СБП по номеру "
         "телефона (список способов бот берёт с FunPay).\n🛡 <b>Несгораемый остаток</b> — нужная сумма всегда остаётся на "
         "балансе.\n🔍 <b>Поиск банка</b> для СБП.\n🚀 <b>Ручной вывод в 1 клик</b>.\n🔐 <b>2FA</b> — бот сам генерирует код "
         "Google Authenticator при выводе."),
        ("⚙️ Включение", "1. /menu → Плагины → <b>Auto Withdraw</b> → включить.\n2. ⚠️ Обязательно <code>/restart</code>, "
         "чтобы зарегистрировалась команда.\n3. <code>/autowithdraw</code> — меню автовывода."),
        ("💳 Часть 1. Реквизиты", "«⚙️ Настроить реквизиты» запускает мастер:\n<b>Шаг 1.</b> Валюта баланса (обычно ₽ RUB).\n"
         "<b>Шаг 2.</b> Способ вывода (карта, USDT, СБП…).\n<b>Шаг 2.5 (СБП).</b> Банк: «🔍 Поиск банка» — отправьте часть "
         "названия (сбер, т-банк, альфа) и выберите банк.\n<b>Шаг 3.</b> Реквизиты: если они сохранены в аккаунте FunPay — "
         "нажмите на них; иначе отправьте номер карты/телефона/кошелька сообщением.\n✅ После этого вы вернётесь в меню."),
        ("🔐 Часть 2. 2FA", "Если вывод на FunPay защищён Google Authenticator:\n1. «🔐 2FA (Google Authenticator)» → "
         "«➕ Ввести секрет 2FA».\n2. Секрет берётся в настройках безопасности FunPay при включении/перенастройке "
         "аутентификатора (под QR-кодом, «Не могу отсканировать»), например <code>CQ7K5US5WSDSNICQ</code>. Добавьте его и "
         "в приложение на телефоне.\n3. Отправьте ключ боту — сообщение сразу удалится.\n4. «🔍 Показать текущий код» — "
         "код должен совпасть с приложением."),
        ("⚖️ Часть 3. Лимиты", "<b>💵 Триггер</b> — сумма, при накоплении которой начинается вывод (0 — автовывод выключен).\n"
         "<b>🛡️ Остаток</b> — сколько всегда оставлять на балансе.\n\nПример: Триггер 1000 ₽, Остаток 200 ₽. Бот ждёт "
         "доступные 1200 ₽. Покупатель подтвердил заказ, доступно 1500 ₽ → выводится 1500 − 200 = <b>1300 ₽</b>.\n"
         "FunPay принимает целые суммы — копейки остаются на балансе."),
        ("🕹 Часть 4. Ручное управление", "<b>Вкл / Выкл</b> — включить или поставить на паузу автовывод.\n"
         "<b>🚀 Вывести всё сейчас</b> — игнорирует триггер, выводит всё доступное (с учётом Остатка). Перед выводом бот "
         "покажет сумму и попросит подтвердить.\n<b>🔄 Баланс</b> — обновить баланс сейчас.\n📜 История — последние "
         "заявки и ответы FunPay."),
        ("❓ FAQ", "<b>«FunPay требует подтверждение по СМС/почте»</b> — задайте секрет 2FA (Часть 2). СМС и почту бот "
         "прочитать не может: переключите подтверждение на Google Authenticator.\n<b>Коды не совпадают</b> — время сервера "
         "отличается больше чем на 30 сек: включите синхронизацию NTP.\n<b>Деньги есть, вывода нет</b> — новичкам FunPay "
         "замораживает средства на 48 ч (выводится только доступный баланс); у способа есть минимальная сумма и комиссия.\n"
         "<b>Как часто проверяется баланс?</b> При подтверждении заказа (через 20 сек) и кэш раз в 4 часа.\n"
         "⚠️ Первый вывод проверьте на небольшой сумме: бот покажет ответ FunPay."),
    ]

    def __init__(self, plugin):
        super().__init__(plugin)
        self.wz = {}

    def on_home(self, chat, mid=None, *args):
        d = self.plugin.store.snapshot()
        cur = d["currency"]
        bal = d["balance"]
        bal_text = (f"{bal[cur]:g} {SIGNS[cur]} (всего {bal['total_' + cur]:g}) · {ago(d['balance_t'])} назад"
                    if bal else "не загружен — нажмите «🔄 Баланс»")
        req = (f"{esc(d['method_label'] or d['method'])}" + (f" · {esc(d['bank_label'])}" if d["bank_label"] else "")
               + f" · {esc(d['wallet_label'] or d['wallet'])}") if d["method"] else "не настроены"
        lines = [f"💸 <b>{esc(NAME)}</b> v{VERSION}", "",
                 f"Автовывод: {'🟢 включён' if d['enabled'] else '🔴 выключен'}"
                 + (" (триггер 0 — только ручной вывод)" if d["enabled"] and not d["trigger"] else ""),
                 f"💰 Доступно: {bal_text}",
                 f"💳 Реквизиты: {req}",
                 f"💵 Триггер: <b>{d['trigger']:g} {SIGNS[cur]}</b> · 🛡️ Остаток: <b>{d['reserve']:g} {SIGNS[cur]}</b>",
                 f"🔐 2FA: {'настроена' if d['totp'] else 'не настроена'}"]
        if d["trigger"]:
            lines.append(f"ℹ️ Вывод начнётся при доступном балансе от {d['trigger'] + d['reserve']:g} {SIGNS[cur]}.")
        if d["history"]:
            h = d["history"][0]
            lines.append(f"\n📜 Последняя заявка: {fmt_dt(h['t'])} {'✅' if h['ok'] else '❌'} {esc(h['text'][:120])}")
        rows = [[("⏸ Выкл" if d["enabled"] else "▶️ Вкл", ("toggle",)), ("🔄 Баланс", ("balance",))],
                [("⚙️ Настроить реквизиты", ("setup",))],
                [("💵 Триггер", ("ask_num", "trigger")), ("🛡️ Остаток", ("ask_num", "reserve"))],
                [("🔐 2FA (Google Authenticator)", ("twofa",))],
                [("🚀 Вывести всё сейчас", ("manual",)), ("📜 История", ("history",))]]
        rows += self.footer_rows()
        rows.append([("◀️ В плагины", ("plugins",))])
        self.send(chat, "\n".join(lines), rows, mid)

    def on_toggle(self, chat, mid):
        d = self.plugin.store.snapshot()
        if not d["enabled"]:
            problem = self.plugin.ready_problem()
            if problem:
                raise UserError(problem)
        self.plugin.store.set(enabled=not d["enabled"])
        self.on_home(chat, mid)

    def on_balance(self, chat, mid):
        try:
            self.plugin.balance(force=True)
        except Exception:
            raise UserError("Не удалось получить баланс FunPay. Попробуйте позже.") from None
        self.on_home(chat, mid)

    def on_ask_num(self, chat, mid, key):
        text = {"trigger": "💵 Минимальная сумма, при накоплении которой начинать вывод (0 — выключить автовывод):",
                "reserve": "🛡️ Сколько всегда оставлять на балансе FunPay (несгораемый остаток):"}[key]
        self.ask(chat, text, ("num", key), mid)

    def in_num(self, chat, text, key):
        if key not in ("trigger", "reserve"):
            raise UserError("Неизвестное поле.")
        self.plugin.store.set(**{key: parse_float(text, 0, 100_000_000, "Сумма")})
        self.on_home(chat)

    # --- мастер реквизитов
    def on_setup(self, chat, mid):
        self.wz[chat] = {}
        rows = [[(title, ("cur", key))] for key, title in CURRENCIES.items()] + [[("✖️ Отмена", ("home",))]]
        self.send(chat, "⚙️ <b>Шаг 1.</b> Выберите валюту вашего баланса на FunPay:", rows, mid)

    def on_cur(self, chat, mid, cur):
        if cur not in CURRENCIES:
            raise UserError("Неизвестная валюта.")
        self.wz[chat] = {"currency": cur}
        self.send(chat, "⏳ Загружаю способы вывода с FunPay…", mid=mid)
        methods = self.plugin.methods(cur)
        self.wz[chat]["methods"] = methods
        rows = [[(text[:45], ("method", i))] for i, (value, text) in enumerate(methods[:30])]
        rows.append([("✖️ Отмена", ("home",))])
        self.send(chat, "💳 <b>Шаг 2.</b> Куда выводить деньги?", rows, mid)

    def state(self, chat):
        st = self.wz.get(chat)
        if not st or "currency" not in st:
            raise UserError("Мастер устарел — начните заново.")
        return st

    def on_method(self, chat, mid, index):
        st = self.state(chat)
        value, text = st["methods"][index]
        st.update(method=value, method_label=text)
        banks = self.plugin.form()["banks"]
        if value in SBP_IDS or ("сбп" in text.lower() and banks):
            st["is_sbp"] = True
            if banks:
                self.on_banks(chat, mid, 0, "")
                return
        self.on_wallets(chat, mid)

    def on_banks(self, chat, mid, page=0, query=""):
        st = self.state(chat)
        banks = self.plugin.form()["banks"]
        if query:
            banks = [b for b in banks if query.casefold() in b["text"].casefold()]
        pages = max(1, (len(banks) + PAGE - 1) // PAGE)
        page = max(0, min(int(page), pages - 1))
        rows = [[(b["text"][:45], ("bank", b["value"], b["text"][:60]))] for b in banks[page * PAGE:(page + 1) * PAGE]]
        if pages > 1:
            rows.append([("◀️", ("banks", max(0, page - 1), query)), (f"{page + 1}/{pages}", ("noop",)),
                         ("▶️", ("banks", min(pages - 1, page + 1), query))])
        rows.append([("🔍 Поиск банка", ("bank_search",)), ("✖️ Отмена", ("home",))])
        title = f"🏦 <b>Шаг 2.5.</b> Выберите банк для СБП" + (f" (поиск «{esc(query)}»: {len(banks)})" if query else
                                                               f" ({len(banks)})")
        self.send(chat, title, rows, mid)

    def on_bank_search(self, chat, mid):
        self.state(chat)
        self.ask(chat, "🔍 Отправьте часть названия банка, например <code>сбер</code>, <code>т-банк</code>, "
                       "<code>альфа</code>.", ("bank_search",), mid)

    def in_bank_search(self, chat, text):
        self.on_banks(chat, None, 0, text.strip()[:30])

    def on_bank(self, chat, mid, value, text):
        st = self.state(chat)
        st.update(bank=value, bank_label=text)
        self.on_wallets(chat, mid)

    def on_wallets(self, chat, mid):
        st = self.state(chat)
        wallets = self.plugin.wallets()
        st["wallets"] = wallets
        rows = []
        for i, w in enumerate(wallets[:10]):
            label = f"{getattr(w, 'type_text', None) or getattr(w, 'type_id', '')}: {getattr(w, 'data', '')}"
            rows.append([(label[:45], ("wallet", i))])
        rows.append([("✖️ Отмена", ("home",))])
        prompt = ("🧾 <b>Шаг 3.</b> Реквизиты для «" + esc(st["method_label"]) + "».\n"
                  + ("Нажмите на сохранённые в FunPay реквизиты или отправьте новые сообщением "
                     "(номер карты, телефона или адрес кошелька)." if wallets else
                     "Отправьте номер карты, телефона (для СБП) или адрес кошелька сообщением."))
        self.ask(chat, prompt, ("wallet",), mid, rows=rows[:-1])

    def on_wallet(self, chat, mid, index):
        st = self.state(chat)
        w = st["wallets"][index]
        data = str(getattr(w, "data", ""))
        if "*" in data:
            raise UserError("Эти реквизиты скрыты FunPay (маска) — отправьте их полностью сообщением.")
        self.save_requisites(chat, mid, data, data)

    def in_wallet(self, chat, text):
        value = re.sub(r"\s", "", text)
        if not 4 <= len(value) <= 120:
            raise UserError("Реквизиты: от 4 до 120 символов.")
        try:
            self.bot.delete_message(chat, self.message.message_id)
        except Exception:
            pass
        masked = value if len(value) <= 8 else value[:4] + "…" + value[-4:]
        self.save_requisites(chat, None, value, masked)

    def save_requisites(self, chat, mid, value, label):
        st = self.wz.pop(chat, None)
        if not st:
            raise UserError("Мастер устарел — начните заново.")
        self.plugin.store.set(currency=st["currency"], method=st["method"], method_label=st["method_label"],
                              bank=st.get("bank", ""), bank_label=st.get("bank_label", ""), wallet=value,
                              wallet_label=label)
        self.send(chat, "✅ Реквизиты сохранены.")
        self.on_home(chat)

    # --- 2FA
    def on_twofa(self, chat, mid):
        has = bool(self.plugin.store["totp"])
        rows = [[("➕ Ввести секрет 2FA" if not has else "♻️ Заменить секрет 2FA", ("totp_ask",))]]
        if has:
            rows.append([("🔍 Показать текущий код", ("totp_show",)), ("🗑 Удалить секрет", ("totp_del",))])
        rows.append([("◀️ Назад", ("home",))])
        self.send(chat, "🔐 <b>2FA (Google Authenticator)</b>\n" + ("Секрет сохранён: коды генерируются при каждом выводе."
                                                                  if has else "Секрет не задан."), rows, mid)

    def on_totp_ask(self, chat, mid):
        self.ask(chat, "🔐 Отправьте текстовый секретный ключ Google Authenticator с FunPay (например "
                       "<code>CQ7K5US5WSDSNICQ</code>). Сообщение будет удалено.", ("totp",), mid)

    def in_totp(self, chat, text):
        try:
            self.bot.delete_message(chat, self.message.message_id)
        except Exception:
            pass
        secret = clean_secret(text)
        totp(secret)
        self.plugin.store.set(totp=secret)
        self.send(chat, "✅ Секрет сохранён. Сравните код с приложением: нажмите «🔍 Показать текущий код».")
        self.on_twofa(chat, None)

    def on_totp_show(self, chat, mid):
        secret = self.plugin.store["totp"]
        if not secret:
            raise UserError("Секрет не задан.")
        left = 30 - int(time.time()) % 30
        self.answer(self.call, f"Текущий код: {totp(secret)} (ещё {left} сек)", alert=True)

    def on_totp_del(self, chat, mid):
        self.plugin.store.set(totp="")
        self.on_twofa(chat, mid)

    # --- ручной вывод
    def on_manual(self, chat, mid):
        problem = self.plugin.ready_problem()
        if problem:
            raise UserError(problem)
        d = self.plugin.store.snapshot()
        try:
            available = self.plugin.balance(force=True)[d["currency"]]
        except Exception:
            raise UserError("Не удалось получить баланс FunPay.") from None
        amount = int(math.floor(available - d["reserve"]))
        if amount <= 0:
            raise UserError(f"Нечего выводить: доступно {available:g} {SIGNS[d['currency']]}, остаток {d['reserve']:g}.")
        self.send(chat, f"🚀 Вывести <b>{amount} {SIGNS[d['currency']]}</b> на {esc(d['method_label'])} "
                        f"{esc(d['wallet_label'])}?\nДоступно: {available:g}, остаток на балансе: {d['reserve']:g}.",
                  [[("✅ Да, вывести", ("manual_go", amount))], [("↩️ Отмена", ("home",))]], mid)

    def on_manual_go(self, chat, mid, amount):
        self.send(chat, "⏳ Отправляю заявку на вывод…", mid=mid)
        ok, text = self.plugin.withdraw(amount, "вручную")
        self.send(chat, ("✅ " if ok else "❌ ") + esc(text), [[("🏠 Меню", ("home",))]], mid)

    def on_history(self, chat, mid):
        history = self.plugin.store["history"]
        lines = ["📜 <b>История заявок</b>"] + [f"{fmt_dt(h['t'])} {'✅' if h['ok'] else '❌'} {h['amount']} · "
                                               f"{esc(h['reason'])} — {esc(h['text'][:150])}" for h in history[:15]]
        if not history:
            lines.append("Заявок ещё не было.")
        self.send(chat, "\n".join(lines), [[("◀️ Назад", ("home",))]], mid)


PLUGIN = None


def init(cardinal, *args):
    global PLUGIN
    if PLUGIN is not None:
        return
    PLUGIN = Withdraw(cardinal)
    if tg_ready(cardinal):
        PLUGIN.ui = WithdrawPanel(PLUGIN)
        PLUGIN.ui.register()
    LOG.info("%s v%s загружен. Автор %s", NAME, VERSION, CREDITS)


def status_changed(cardinal, event, *args):
    if PLUGIN is None:
        return
    if enum_name(getattr(event.order, "status", "")) == "CLOSED":
        try:
            PLUGIN.schedule_check()
        except Exception:
            LOG.exception("%s: ошибка проверки баланса", NAME)


BIND_TO_PRE_INIT = [init]
BIND_TO_ORDER_STATUS_CHANGED = [status_changed]
BIND_TO_DELETE = None
