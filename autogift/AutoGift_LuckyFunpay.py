# -*- coding: utf-8 -*-
"""
AutoGift 6.2.0: verified FunPay orders and journalled Telegram Gifts.
Access is restricted to Cardinal administrators in private Telegram chats.
The control panel remains available when the Telegram dependency is missing.
"""

from __future__ import annotations

import asyncio
import csv
import copy
import hashlib
import importlib
import math
from contextlib import asynccontextmanager
import secrets
import html
import io
import json
import logging
import os
import queue as pyqueue
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from cardinal import Cardinal
except Exception:
    Cardinal = Any  # type: ignore

from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent
from FunPayAPI import enums
from FunPayAPI.types import LotFields
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Message

# Optional delivery dependency. Import only AFTER registering the control panel.
Client = None
ChatType = None


# ---------------------------------------------------------------------------
# META
# ---------------------------------------------------------------------------

NAME = "AutoGift"
VERSION = "6.2.0"
DESCRIPTION = "Безопасная автоматическая выдача Telegram Gifts за Stars через FunPay"
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "6a0b4bb2-1f47-4db1-9e4b-4d75a1e3f650"
SETTINGS_PAGE = True

LOGGER_PREFIX = "[AUTOGIFT]"
STORAGE = Path("storage") / "cache"
LOG_DIR = Path("storage") / "logs"
STORAGE.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = STORAGE / "autogift_config.json"
ACCOUNTS_PATH = STORAGE / "autogift_accounts.json"
LOTS_PATH = STORAGE / "gift_lots.json"
ORDERS_PATH = STORAGE / "autogift_orders.json"
CATALOG_PATH = STORAGE / "autogift_catalog.json"
OPERATORS_PATH = STORAGE / "autogift_operators.json"
DEPENDENCY_ROOT = STORAGE / "autogift_runtime" / f"py{sys.version_info.major}{sys.version_info.minor}"
DEPENDENCY_PACKAGE = "Kurigram==2.2.26"
LEGACY_SESSION_DIRS = [Path("/bot2/sessions"), Path("bot2/sessions"), STORAGE / "sessions"]

logger = logging.getLogger("FPC.autogift_lucky")
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = RotatingFileHandler(LOG_DIR / "autogift.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    fh.setLevel(logging.INFO)
    logger.addHandler(fh)


# ---------------------------------------------------------------------------
# DEFAULT CONFIG / STORAGE
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    "api_id": 0,
    "api_hash": "",
    "proxy": "",
    "proxy_required": False,
    "username_from_order": True,
    "quick_confirmation": True,
    "running": True,
    "auto_refunds": True,
    "auto_refund_when_no_delivery": False,
    "strict_lot_match": True,
    "max_order_units": 10,
    "max_gifts_per_order": 100,
    "max_order_stars": 10000,
    "star_cost_rub": 0.0,
    "funpay_fee_percent": 0.0,
    "hide_name": True,
    "comment_enabled": False,
    "comment": "",
    "buyer_reminder": True,
    "safe_mode": True,
    "lot_protection": True,
    "low_balance_enabled": True,
    "low_balance_threshold": 150,
    "auto_reactivate": True,
    "reminder_text": "⏳ Для получения подарка выполните команду, указанную ниже.",
    "payment_text": "🎁 Спасибо за покупку!\nЗаказ #{order_id} · наборов: {units}\nПодготовим подарки для вашего Telegram.",
    "success_text": "✅ Подарки доставлены!\n👤 Получатель: @{username}\n🎁 Отправлено: {gift_count}\n\nСпасибо за покупку! Подтвердите выполнение заказа на FunPay. Будем рады вашему отзыву ⭐",
    "refund_text": "↩️ Возврат выполнен. Приносим извинения за неудобства.",
}
DEFAULT_ACCOUNT = {
    "id": "",
    "label": "",
    "phone": "",
    "session_string": "",
    "session_name": "",
    "session_workdir": "",
    "active": True,
    "balance": None,
    "gifts_sent": 0,
    "spent_stars": 0,
    "last_error": "",
    "last_used": 0,
}
DEFAULT_CATALOG: Dict[str, Any] = {"updated_at": 0, "items": []}
DEFAULT_OPERATORS: Dict[str, Any] = {"users": []}

FILE_LOCK = threading.RLock()
CONFIG: Dict[str, Any] = {}
ACCOUNTS: List[Dict[str, Any]] = []
LOTS: Dict[str, Dict[str, Any]] = {}
ORDERS: Dict[str, Dict[str, Any]] = {}
CATALOG: Dict[str, Any] = {}
OPERATORS: set[int] = set()

RUNNING = False
CARDINAL_INSTANCE = None
BOT = None

TG_WIZARD: Dict[int, Dict[str, Any]] = {}
AUTH_THREADS: Dict[int, threading.Thread] = {}
AUTH_INPUT_QUEUES: Dict[int, pyqueue.Queue] = {}
AUTH_OUTPUT_QUEUES: Dict[int, pyqueue.Queue] = {}
LOT_WIZARDS: Dict[int, Dict[str, Any]] = {}
CATALOG_CACHE: Dict[int, List[Dict[str, Any]]] = {}
PROCESSING_ORDERS: set[str] = set()
TELEGRAM_COMMAND_SEEN: set[tuple[int, int]] = set()
ORDER_LOCK = FILE_LOCK
SESSION_LOCK = threading.RLock()
POOL_LOCK = threading.RLock()
ACCOUNT_RESERVATIONS: Dict[str, int] = {}

FUNPAY_OWN_LOTS_CACHE: Dict[int, Dict[str, Any]] = {}
FUNPAY_OWN_LOTS_CACHE_TTL = 120
FUNPAY_ORDER_RESOLVE_LOCK = threading.RLock()
STORAGE_ERROR = ""
STARTUP_ERROR = ""
INITIALIZED = False
DEPENDENCY_READY = False
DEPENDENCY_STATUS = "unchecked"
DEPENDENCY_ERROR = ""
DEPENDENCY_INSTALLING = False
DEPENDENCY_INSTALL_LOCK = threading.Lock()
WORKERS_STARTED = False
INSTANCE_FILE = None
ORDER_GUARDS = [threading.RLock() for _ in range(64)]


class StorageError(RuntimeError):
    pass


class PreflightError(RuntimeError):
    """Payment was not submitted."""


def _load_telegram_dependency():
    """Use the host library first, or the plugin's private installation if absent."""
    global Client, ChatType, DEPENDENCY_READY, DEPENDENCY_STATUS, DEPENDENCY_ERROR
    DEPENDENCY_READY = False
    private_path = str((DEPENDENCY_ROOT / "packages").resolve())
    if Path(private_path).is_dir() and private_path not in sys.path:
        # Append, not prepend: never replace another plugin's shared library.
        sys.path.append(private_path)
    importlib.invalidate_caches()
    try:
        # Pyrogram's sync wrappers need a current loop during import. Another
        # plugin may already have closed it with asyncio.run() during startup.
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = None
        if loop is None or loop.is_closed():
            asyncio.set_event_loop(asyncio.new_event_loop())
        module = importlib.import_module("pyrogram")
        raw = importlib.import_module("pyrogram.raw")
        chat_types = importlib.import_module("pyrogram.enums")
        for obj, name in ((raw.types, "InputInvoiceStarGift"), (raw.functions.payments, "GetStarGifts"),
                          (raw.functions.payments, "GetPaymentForm"), (raw.functions.payments, "SendStarsForm"),
                          (raw.types.payments, "PaymentResult")):
            if not hasattr(obj, name):
                raise RuntimeError("Установленная библиотека Telegram не поддерживает " + name)
        Client, ChatType = module.Client, chat_types.ChatType
    except Exception as exc:
        Client = ChatType = None
        DEPENDENCY_STATUS = "missing" if isinstance(exc, ModuleNotFoundError) and exc.name == "pyrogram" else "incompatible"
        DEPENDENCY_ERROR = f"{type(exc).__name__}: {exc}"[:700]
        logger.warning("%s Telegram delivery dependency: %s", LOGGER_PREFIX, DEPENDENCY_ERROR)
        return False
    DEPENDENCY_READY = True
    DEPENDENCY_STATUS = "ready"
    DEPENDENCY_ERROR = ""
    return True


def _recovery_text():
    heading = f"<b>🎁 AutoGift</b> <code>v{VERSION}</code>\n\n"
    if STARTUP_ERROR or STORAGE_ERROR:
        reason = STARTUP_ERROR or STORAGE_ERROR
        return heading + "⛔ Выдача остановлена.\n" + h(reason) + "\n\nМеню доступно. Исправьте указанную причину и выполните /restart."
    if DEPENDENCY_INSTALLING:
        return heading + "⏳ Устанавливаю библиотеку Telegram. Это может занять несколько минут. Бот продолжает работать."
    if DEPENDENCY_STATUS == "restart":
        return heading + "✅ Библиотека установлена и проверена.\n\nВыполните /restart, затем откройте /autogift."
    if DEPENDENCY_STATUS == "missing":
        return (heading + "⚠️ На сервере отсутствует библиотека Telegram (pyrogram). Поэтому выдача пока выключена.\n\n"
                "Нажмите «Установить библиотеку Telegram». Она будет установлена в папку AutoGift; после установки потребуется /restart.\n\n"
                "Команды: /autogift — меню, /autogift_install — установка, /autogift_help — инструкция.")
    if DEPENDENCY_STATUS == "incompatible":
        return (heading + "⚠️ Установленная библиотека Telegram не загружается или не поддерживает Gifts.\n"
                + h(DEPENDENCY_ERROR) + "\n\nНужна совместимая библиотека, например Kurigram 2.2.26. "
                "Общая библиотека других плагинов автоматически не заменяется. После исправления выполните /restart.")
    return heading + "⏳ Подготавливаю AutoGift. Команда /autogift откроет меню после загрузки."


def _recovery_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    if (INITIALIZED and not STARTUP_ERROR and not STORAGE_ERROR and DEPENDENCY_STATUS == "missing"
            and not DEPENDENCY_INSTALLING):
        kb.add(InlineKeyboardButton("📦 Установить библиотеку Telegram", callback_data="ag:install_dependency"))
    kb.add(InlineKeyboardButton("🔄 Обновить", callback_data="ag:main"))
    kb.row(InlineKeyboardButton("👤 Автор", url=AUTHOR_URL), InlineKeyboardButton("📣 Канал", url=CHANNEL_URL))
    return kb


def _installer_process(args, timeout):
    return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=timeout, text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)


def _install_dependency_worker(chat_id):
    global DEPENDENCY_INSTALLING, DEPENDENCY_STATUS, DEPENDENCY_ERROR
    try:
        root = DEPENDENCY_ROOT.resolve()
        root.mkdir(parents=True, exist_ok=True)
        # Install to a new directory so an interrupted pip cannot publish a partial runtime.
        stage = Path(tempfile.mkdtemp(prefix="install-", dir=str(root))).resolve()
        result = _installer_process([sys.executable, "-m", "pip", "--isolated", "install", "--no-input",
                                     "--disable-pip-version-check", "--no-cache-dir", "--timeout", "20", "--retries", "1",
                                     "--index-url", "https://pypi.org/simple", "--target", str(stage), DEPENDENCY_PACKAGE], 240)
        if result.returncode:
            logger.error("%s dependency installer failed: %s", LOGGER_PREFIX,
                         re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1***@", result.stdout or "")[-3000:])
            raise RuntimeError("Установка не завершилась. Проверьте доступ сервера к PyPI и наличие pip; подробности в storage/logs/autogift.log.")
        probe = (
            "import sys, asyncio; asyncio.set_event_loop(asyncio.new_event_loop()); sys.path.insert(0, sys.argv[1]); "
            "from pyrogram import Client, raw; from pyrogram.enums import ChatType; "
            "assert all(hasattr(raw.functions.payments, n) for n in ('GetStarGifts', 'GetPaymentForm', 'SendStarsForm')); "
            "assert hasattr(raw.types, 'InputInvoiceStarGift'); assert hasattr(raw.types.payments, 'PaymentResult'); "
            "assert callable(Client.connect)"
        )
        checked = _installer_process([sys.executable, "-c", probe, str(stage)], 45)
        if checked.returncode:
            logger.error("%s dependency probe failed: %s", LOGGER_PREFIX, (checked.stdout or "")[-1500:])
            raise RuntimeError("Библиотека скачана, но не прошла проверку импорта. Подробности в storage/logs/autogift.log.")
        target = (root / "packages").resolve()
        backup = (root / ("previous-" + secrets.token_hex(6))).resolve()
        if not all(path.is_relative_to(root) and path != root for path in (stage, target, backup)):
            raise RuntimeError("Некорректный путь библиотеки AutoGift.")
        had_target = target.exists()
        if had_target:
            os.replace(target, backup)
        try:
            os.replace(stage, target)
        except Exception:
            if had_target:
                os.replace(backup, target)
            raise
        DEPENDENCY_STATUS = "restart"
        DEPENDENCY_ERROR = ""
        BOT.send_message(chat_id, "✅ Библиотека Telegram установлена и проверена.\nТеперь выполните /restart, затем /autogift.")
    except Exception as exc:
        if DEPENDENCY_STATUS != "restart":
            DEPENDENCY_ERROR = str(exc)[:700]
        logger.exception("%s dependency installation needs attention", LOGGER_PREFIX)
        try:
            BOT.send_message(chat_id, "❌ " + str(exc)[:700] + "\nМеню /autogift остаётся доступным.")
        except Exception:
            pass
    finally:
        DEPENDENCY_INSTALLING = False
        DEPENDENCY_INSTALL_LOCK.release()


def start_dependency_install(chat_id, user_id):
    global DEPENDENCY_INSTALLING
    if str(chat_id) != str(user_id) or not is_operator(int(user_id)):
        deny_operator(chat_id)
        return
    if (not INITIALIZED or STARTUP_ERROR or STORAGE_ERROR or DEPENDENCY_STATUS != "missing"):
        send_main(chat_id)
        return
    if not DEPENDENCY_INSTALL_LOCK.acquire(blocking=False):
        BOT.send_message(chat_id, "⏳ Установка уже выполняется. Дождитесь результата.")
        return
    DEPENDENCY_INSTALLING = True
    try:
        BOT.send_message(chat_id, "⏳ Устанавливаю библиотеку Telegram в папку AutoGift. Бот продолжает работать; дождитесь результата.")
        threading.Thread(target=_install_dependency_worker, args=(chat_id,), daemon=True, name="AutoGiftInstall").start()
    except Exception:
        DEPENDENCY_INSTALLING = False
        DEPENDENCY_INSTALL_LOCK.release()
        raise


def order_guard(order_id):
    return ORDER_GUARDS[hash(str(order_id).upper()) % len(ORDER_GUARDS)]


def _storage_failed(path, exc):
    global STORAGE_ERROR, RUNNING
    STORAGE_ERROR = f"Не удалось прочитать/сохранить {path.name}: {type(exc).__name__}"
    RUNNING = False
    logger.error("%s %s; all delivery disabled until restart", LOGGER_PREFIX, STORAGE_ERROR)
    return StorageError(STORAGE_ERROR)


def _acquire_instance_lock():
    global INSTANCE_FILE
    handle = (STORAGE / "autogift.instance.lock").open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if handle.seek(0, 2) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        handle.close()
        raise RuntimeError("Другой процесс AutoGift уже использует это хранилище.") from None
    INSTANCE_FILE = handle


def _positive_int(value):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)) or int(value) <= 0:
        raise ValueError("Требуется точное положительное целое число.")
    return int(value)


def _order_quantity(order):
    # OrderShortcut.amount is inferred from text in FunPayAPI. Never use it.
    return _positive_int(getattr(order, "amount", None))


def _gift_plan(data):
    units = _positive_int(data.get("units"))
    ids = data.get("gift_ids")
    if not isinstance(ids, list) or not ids or any(_positive_int(g) not in STANDARD_GIFT_IDS for g in ids):
        raise ValueError("Допустимы только Gift ID из каталога AutoGift.")
    if units > _positive_int(CONFIG.get("max_order_units", 10)):
        raise ValueError("Превышен лимит количества в заказе.")
    if units * len(ids) > _positive_int(CONFIG.get("max_gifts_per_order", 100)):
        raise ValueError("Превышен лимит подарков в заказе.")
    return [int(gid) for _ in range(units) for gid in ids]


def _ledger_valid(data):
    try:
        items = data.get("delivery")
        expected = _gift_plan(data)
        return (isinstance(items, list) and len(items) == len(expected)
                and all(isinstance(item, dict) and item.get("gift_id") == gid
                        and item.get("sequence") == n
                        and item.get("status") in {"pending", "sending", "sent", "failed", "unknown"}
                        for n, (item, gid) in enumerate(zip(items, expected), 1)))
    except (ValueError, TypeError, KeyError):
        return False


def _safe_json_load(path: Path, default: Any) -> Any:
    with FILE_LOCK:
        try:
            if not path.exists():
                return copy.deepcopy(default)
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            raise _storage_failed(path, exc) from exc


def _safe_json_save(path: Path, data: Any) -> None:
    with FILE_LOCK:
        if STORAGE_ERROR:
            raise StorageError(STORAGE_ERROR)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            if os.name != "nt":
                os.chmod(path, 0o600)
                directory = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except Exception as exc:
            raise _storage_failed(path, exc) from exc


def load_all() -> None:
    global CONFIG, ACCOUNTS, LOTS, ORDERS, CATALOG, OPERATORS
    if CONFIG_PATH.exists() and not ORDERS_PATH.exists():
        raise _storage_failed(ORDERS_PATH, ValueError("Order journal missing in an existing installation"))
    cfg = _safe_json_load(CONFIG_PATH, DEFAULT_CONFIG.copy())
    merged = DEFAULT_CONFIG.copy()
    merged.update(cfg if isinstance(cfg, dict) else {})
    CONFIG = merged

    raw_accounts = _safe_json_load(ACCOUNTS_PATH, [])
    ACCOUNTS = []
    if isinstance(raw_accounts, list):
        for item in raw_accounts:
            if isinstance(item, dict):
                acc = DEFAULT_ACCOUNT.copy()
                acc.update(item)
                ACCOUNTS.append(acc)

    raw_lots = _safe_json_load(LOTS_PATH, {"lot_mapping": {}})
    if isinstance(raw_lots, dict) and "lot_mapping" in raw_lots:
        LOTS = dict(raw_lots.get("lot_mapping") or {})
    elif isinstance(raw_lots, dict):
        LOTS = dict(raw_lots)
    else:
        LOTS = {}

    raw_orders = _safe_json_load(ORDERS_PATH, {})
    if isinstance(raw_orders, list):
        ORDERS = {str(x.get("order_id")): x for x in raw_orders if isinstance(x, dict) and x.get("order_id") is not None}
    else:
        ORDERS = raw_orders if isinstance(raw_orders, dict) else {}

    CATALOG = _safe_json_load(CATALOG_PATH, DEFAULT_CATALOG.copy())
    raw_ops = _safe_json_load(OPERATORS_PATH, DEFAULT_OPERATORS.copy())
    ids = raw_ops.get("users", []) if isinstance(raw_ops, dict) else []
    OPERATORS = {int(x) for x in ids if str(x).isdigit()}

    if (not isinstance(cfg, dict) or not isinstance(raw_accounts, list)
            or not all(isinstance(a, dict) for a in raw_accounts)
            or not isinstance(raw_lots, dict)
            or not isinstance(raw_lots.get("lot_mapping", raw_lots), dict)
            or not all(isinstance(v, dict) for v in LOTS.values())
            or not isinstance(raw_orders, (dict, list))
            or not all(isinstance(v, dict) for v in (raw_orders.values() if isinstance(raw_orders, dict) else raw_orders))
            or not isinstance(CATALOG, dict) or not isinstance(CATALOG.get("items"), list)):
        raise _storage_failed(ORDERS_PATH, ValueError("Invalid storage structure"))
    if isinstance(raw_orders, list) and len(ORDERS) != len(raw_orders):
        raise _storage_failed(ORDERS_PATH, ValueError("Duplicate/missing order IDs"))
    normalized = {}
    for key, row in ORDERS.items():
        order_id = str(row.get("order_id") or key).upper()
        if order_id != str(key).upper() or not re.fullmatch(r"[A-Z0-9]+", order_id) or order_id in normalized:
            raise _storage_failed(ORDERS_PATH, ValueError("Invalid order ID"))
        row["order_id"] = order_id
        normalized[order_id] = row
    ORDERS = normalized
    for key in ("max_order_units", "max_gifts_per_order", "max_order_stars"):
        CONFIG[key] = _positive_int(CONFIG[key])
    _persist_all()


def _persist_all() -> None:
    _safe_json_save(CONFIG_PATH, CONFIG)
    _safe_json_save(ACCOUNTS_PATH, ACCOUNTS)
    _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
    _safe_json_save(ORDERS_PATH, ORDERS)
    _safe_json_save(CATALOG_PATH, CATALOG)
    _safe_json_save(OPERATORS_PATH, {"users": sorted(OPERATORS)})


def _cardinal_admin_ids() -> set[int]:
    """IDs authorised by Cardinal itself; this plugin never grants access."""
    telegram = getattr(CARDINAL_INSTANCE, "telegram", None)
    raw = getattr(telegram, "authorized_users", {}) if telegram is not None else {}
    values = raw.keys() if isinstance(raw, dict) else (raw or [])
    result = set()
    for value in values:
        try: result.add(int(value))
        except (TypeError, ValueError): pass
    return result

def is_operator(user_id: int) -> bool:
    return int(user_id) in _cardinal_admin_ids()

def register_operator(user_id: int) -> None:
    # Compatibility shim. Operators are managed by Cardinal, not by /autogift.
    if is_operator(user_id): OPERATORS.add(int(user_id))

def notify_operators(text: str) -> None:
    if BOT is None: return
    for uid in _cardinal_admin_ids():
        try: BOT.send_message(uid, text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as exc: logger.warning("%s notify %s failed: %s", LOGGER_PREFIX, uid, exc)

def deny_operator(chat_id: int, callback_id: Optional[str] = None) -> None:
    message = "⛔ Доступ есть только у пользователей, авторизованных в Telegram-панели Cardinal."
    try:
        if callback_id: BOT.answer_callback_query(callback_id, message, show_alert=True)
        else: BOT.send_message(chat_id, message)
    except Exception: pass

def delivery_items(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    if data.get("delivery"):
        if not _ledger_valid(data):
            raise ValueError("Повреждён журнал выдачи.")
        return data["delivery"]
    plan = _gift_plan(data)
    data["delivery"] = [{"sequence": n, "unit": (n - 1) // len(data["gift_ids"]) + 1,
                         "gift_id": gid, "status": "pending"} for n, gid in enumerate(plan, 1)]
    return data["delivery"]


def delivered_count(data: Dict[str, Any]) -> int:
    return sum(1 for i in data.get("delivery",[]) if i.get("status")=="sent")
def uncertain_count(data: Dict[str, Any]) -> int:
    return sum(1 for i in data.get("delivery", []) if i.get("status") in {"sending", "unknown"})


def order_is_refundable(data: Dict[str, Any]) -> bool:
    return (data.get("status") in {"await_username", "await_comment", "ready_confirm", "failed", "needs_review"}
            and _ledger_valid(data)
            and all(i["status"] in {"pending", "failed"} for i in data["delivery"])
            and not data.get("refund_started_at"))


def is_definitive_delivery_error(exc: Exception) -> bool:
    if isinstance(exc, PreflightError):
        return True
    try:
        from pyrogram.errors import RPCError
    except ImportError:
        return False
    return isinstance(exc, RPCError) and getattr(exc, "ID", "") in {
        "STARGIFT_NOT_FOUND", "STARGIFT_USAGE_LIMITED", "STARGIFT_USER_USAGE_LIMITED",
        "PEER_ID_INVALID", "USERNAME_INVALID", "USER_ID_INVALID", "CHAT_ID_INVALID",
        "BALANCE_TOO_LOW", "FORM_EXPIRED", "STARS_FORM_AMOUNT_MISMATCH",
        "STARGIFT_INVALID", "STARGIFT_SOLD_OUT"}


def ts(value: Any) -> float:
    try: return float(value or 0)
    except (TypeError,ValueError): return 0.0
def local_dt(value: Any) -> datetime: return datetime.fromtimestamp(ts(value) or time.time())
def order_stats(days: Optional[int] = None) -> Dict[str, Any]:
    since = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
             - timedelta(days=days - 1)).timestamp() if days else 0
    with ORDER_LOCK:
        rows = copy.deepcopy([row for row in ORDERS.values() if ts(row.get("created_at")) >= since])
    completed = [row for row in rows if row.get("status") == "completed"]
    review = [row for row in rows if row.get("status") == "needs_review"]
    delivered = sum(delivered_count(row) for row in rows)
    stars = sum(int(item.get("price") or 0) for row in rows for item in row.get("delivery", []) if item.get("status") == "sent")
    revenue = sum(float(row.get("order_price") or 0) for row in completed if row.get("currency") == "RUB")
    fee = revenue * max(0.0, float(CONFIG.get("funpay_fee_percent") or 0)) / 100
    star_cost = float(CONFIG.get("star_cost_rub") or 0)
    rub_stars = sum(int(i.get("price") or 0) for row in rows if row.get("currency") == "RUB"
                    for i in row.get("delivery", []) if i.get("status") == "sent")
    profit = revenue - fee - rub_stars * star_cost if star_cost > 0 else None
    return {"rows": rows, "completed": completed, "review": review, "delivered": delivered,
            "stars": stars, "revenue": revenue, "fee": fee, "profit": profit,
            "other_currency": sum(row.get("currency") != "RUB" for row in completed)}

def format_money(value: Any) -> str:
    try:
        return f"{float(value):,.2f}".replace(",", " ").replace(".00", "") + " ₽"
    except Exception:
        return "— ₽"

def stats_days(days: Optional[int]) -> List[Dict[str, Any]]:
    """Daily completed-sales detail for the selected period."""
    now = datetime.now()
    buckets: Dict[str, Dict[str, Any]] = {}
    if days is not None:
        for offset in range(days - 1, -1, -1):
            date = (now - timedelta(days=offset)).date()
            key = date.isoformat()
            buckets[key] = {"date": date, "orders": 0, "gifts": 0, "revenue": 0.0, "stars": 0}
    for row in order_stats(None)["rows"]:
        event_time = ts(row.get("created_at"))
        if not event_time:
            continue
        date = local_dt(event_time).date()
        key = date.isoformat()
        if days is not None and key not in buckets:
            continue
        bucket = buckets.setdefault(key, {"date": date, "orders": 0, "gifts": 0, "revenue": 0.0, "stars": 0})
        if row.get("status") == "completed":
            bucket["orders"] += 1
            if row.get("currency") == "RUB":
                bucket["revenue"] += float(row.get("order_price") or 0)
        for item in row.get("delivery", []):
            if item.get("status") != "sent":
                continue
            sent_date = date
            sent_key = sent_date.isoformat()
            if days is not None and sent_key not in buckets:
                continue
            gift_bucket = buckets.setdefault(sent_key, {"date": sent_date, "orders": 0, "gifts": 0, "revenue": 0.0, "stars": 0})
            gift_bucket["gifts"] += 1
            gift_bucket["stars"] += int(item.get("price") or 0)
    result = [bucket for bucket in buckets.values() if days is not None or bucket["orders"] or bucket["gifts"]]
    result.sort(key=lambda bucket: bucket["date"], reverse=days is None)
    return result[:14] if days is None else result


def stats_text(days: Optional[int]) -> str:
    labels = {1: "за сегодня", 7: "за 7 дней", 30: "за 30 дней", None: "за всё время"}
    st = order_stats(days)
    total = len(st["rows"])
    completed = len(st["completed"])
    completion_rate = completed / total * 100 if total else 0
    rub_orders = completed - st["other_currency"]
    average = st["revenue"] / rub_orders if rub_orders else 0
    profit = format_money(st["profit"]) if st["profit"] is not None else "укажите цену Stars"
    lines = [
        f"<b>📈 Статистика {labels[days]}</b>",
        "По дате создания заказов. Денежные суммы — только заказы в RUB.",
        f"Вне сумм RUB: {st['other_currency']} заказов (валюта и сумма — в CSV).",
        f"💰 Заработано: <b>{format_money(st['revenue'])}</b>",
        f"✅ Выполнено: <b>{completed}</b> из {total} заказов ({completion_rate:.0f}%)",
        f"🎁 Отправлено: <b>{st['delivered']}</b> Gifts · <b>{format_stars(st['stars'])}</b>",
        f"🧾 Средний чек: <b>{format_money(average)}</b>",
        f"📊 Прибыль: <b>{profit}</b>",
        f"🟠 На проверке: <b>{len(st['review'])}</b>",
        "",
        "<b>🗓 По дням</b>",
    ]
    daily = stats_days(days)
    if not daily or not any(item["orders"] or item["gifts"] for item in daily):
        lines.append("Продаж за выбранный период нет.")
    else:
        for item in daily:
            label = item["date"].strftime("%d.%m")
            lines.append(f"<code>{label}</code> · {item['orders']} зак. · 🎁 {item['gifts']} · <b>{format_money(item['revenue'])}</b>")
        if days is None and len(daily) == 14:
            lines.append("<i>Показаны последние 14 дат с продажами.</i>")
    return "\n".join(lines)

def stats_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(InlineKeyboardButton("📅 Сегодня", callback_data="ag:stats:1"),
           InlineKeyboardButton("📆 7 дней", callback_data="ag:stats:7"))
    kb.row(InlineKeyboardButton("🗓 30 дней", callback_data="ag:stats:30"),
           InlineKeyboardButton("♾ Всё время", callback_data="ag:stats:all"))
    kb.row(InlineKeyboardButton("🟠 На проверку", callback_data="ag:review"),
           InlineKeyboardButton("📄 CSV-экспорт", callback_data="ag:export"))
    kb.row(InlineKeyboardButton("🏠 Главное меню", callback_data="ag:main"))
    return kb

def build_orders_csv() -> bytes:
    out = io.StringIO(newline="")
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["Заказ", "Дата", "Статус", "Лот", "Получатель", "Штук", "Сумма", "Валюта", "Stars", "Отправлено", "Ошибка"])
    for row in sorted(order_stats(None)["rows"], key=lambda x: ts(x.get("created_at")), reverse=True):
        stars = sum(int(i.get("price") or 0) for i in row.get("delivery", []) if i.get("status") == "sent")
        values = [row.get("order_id"), local_dt(row.get("created_at")).strftime("%Y-%m-%d %H:%M"),
                  row.get("status"), row.get("lot_name"), row.get("username") or "", row.get("units"),
                  row.get("order_price"), row.get("currency") or "UNKNOWN", stars, delivered_count(row), row.get("last_error", "")]
        writer.writerow([("'" + value) if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value
                         for value in values])
    return out.getvalue().encode("utf-8-sig")
# ---------------------------------------------------------------------------
# CONFIG / HELPERS

# ---------------------------------------------------------------------------
# CONFIG / HELPERS
# ---------------------------------------------------------------------------

def h(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def masked_phone(phone: str) -> str:
    p = str(phone or "")
    if len(p) < 5:
        return p
    return p[:3] + "••••" + p[-2:]


def parse_proxy(raw: str) -> Optional[dict]:
    raw = (raw or "").strip()
    if not raw:
        return None
    scheme = "http"
    if "://" in raw:
        scheme, raw = raw.split("://", 1)
    auth, hostport = ("", raw)
    if "@" in raw:
        auth, hostport = raw.rsplit("@", 1)
    if ":" not in hostport:
        raise ValueError("Прокси должен быть в формате ip:port или login:pass@ip:port")
    host, port = hostport.rsplit(":", 1)
    result = {"scheme": scheme, "hostname": host, "port": int(port)}
    if auth:
        login, password = (auth.split(":", 1) + [""])[:2]
        result["username"] = login
        result["password"] = password
    return result


def get_api() -> Tuple[int, str]:
    api_id = int(CONFIG.get("api_id") or 0)
    api_hash = str(CONFIG.get("api_hash") or "").strip()
    return api_id, api_hash


def _make_account_client(account: Dict[str, Any]) -> Client:
    if Client is None:
        raise RuntimeError("Pyrogram не установлен. Установите совместимую версию Pyrogram.")
    if CONFIG.get("proxy_required") and not CONFIG.get("proxy"):
        raise RuntimeError("В настройках включён обязательный прокси. Сначала настройте Proxy.")
    api_id, api_hash = get_api()
    session_string = str(account.get("session_string") or "").strip()
    session_name = str(account.get("session_name") or "").strip()
    session_workdir = str(account.get("session_workdir") or "").strip()
    kwargs: Dict[str, Any] = {
        "api_id": api_id or None,
        "api_hash": api_hash or None,
        "proxy": parse_proxy(CONFIG.get("proxy", "")),
        "no_updates": True,
        "sleep_threshold": 0,
    }
    if session_string:
        kwargs.update({
            "name": str(account["id"]),
            "workdir": str(STORAGE / "tg_workdir"),
            "session_string": session_string,
            "in_memory": True,
        })
    elif session_name:
        kwargs.update({
            "name": session_name,
            "workdir": session_workdir or str(STORAGE / "tg_workdir"),
        })
    else:
        raise RuntimeError("У аккаунта отсутствует session string/session file.")
    if not kwargs.get("api_id") or not kwargs.get("api_hash"):
        # Старые Pyrogram session-файлы могут уже содержать нужные данные.
        kwargs.pop("api_id", None)
        kwargs.pop("api_hash", None)
    return Client(**kwargs)


@asynccontextmanager
async def account_client(account):
    app = _make_account_client(account)
    connected = False
    try:
        authorized = await app.connect()
        connected = True
        if not authorized:
            raise PreflightError("Telegram-сессия не авторизована. Добавьте аккаунт заново.")
        yield app
    finally:
        if connected:
            try:
                await app.disconnect()
            except Exception:
                # A failed disconnect cannot change an already confirmed payment.
                logger.warning("%s Telegram disconnect failed", LOGGER_PREFIX)


def run_coro(coro):
    # All established sessions and balances are used serially across threads.
    # No ORDER_LOCK may be held while entering this gate.
    with SESSION_LOCK:
        return asyncio.run(coro)


def normalized_username(text: str) -> Optional[str]:
    t = (text or "").strip()
    m = re.search(r"(?<!\w)@([A-Za-z0-9_]{4,32})(?!\w)", t)
    if not m:
        return None
    return m.group(1)


def format_stars(n: Any) -> str:
    try:
        return f"{int(n):,}".replace(",", " ") + " ⭐"
    except Exception:
        return "— ⭐"


def set_lot_active(funpay_lot_id: int, active: bool) -> bool:
    try:
        candidates = [v for v in LOTS.values() if _binding_valid(v)
                      and str(v['binding']['lot_id']) == str(funpay_lot_id)]
        if len(candidates) != 1 or (active and not candidates[0].get('enabled')):
            return False
        lf = CARDINAL_INSTANCE.account.get_lot_fields(int(funpay_lot_id))
        if not _live_binding_matches(lf, candidates[0]):
            logger.warning('%s refused to change foreign/modified listing %s', LOGGER_PREFIX, funpay_lot_id)
            return False
        lf.active = bool(active)
        if hasattr(lf, "renew_fields"):
            lf.renew_fields()
        CARDINAL_INSTANCE.account.save_lot(lf)
        actual = CARDINAL_INSTANCE.account.get_lot_fields(int(funpay_lot_id))
        return _live_binding_matches(actual, candidates[0]) and bool(actual.active) == bool(active)
    except Exception as exc:
        logger.error("%s lot %s active=%s failed: %s", LOGGER_PREFIX, funpay_lot_id, active, exc)
        return False


def lot_is_active(funpay_lot_id: int) -> Optional[bool]:
    try:
        lf = CARDINAL_INSTANCE.account.get_lot_fields(int(funpay_lot_id))
        return bool(getattr(lf, "active", False))
    except Exception:
        return None


def protect_all_lots(active: bool) -> int:
    changed = 0
    for data in list(LOTS.values()):
        try:
            lid = int(data.get("funpay_lot_id") or data.get("lot_id"))
        except Exception:
            continue
        if set_lot_active(lid, active):
            changed += 1
    return changed



# === ФИКСИРОВАННЫЙ КАТАЛОГ СТАНДАРТНЫХ GIFTS ===
# В каталог AutoGift намеренно попадают только эти 11 обычных подарков.
# Collectible/NFT/Market/Auction и прочие позиции из Telegram-каталога сюда не попадают.
STANDARD_GIFTS_CATALOG = [
    {"id": 5170145012310081615, "name": "Сердце", "emoji": "💝", "price": 15, "order": 1},
    {"id": 5170233102089322756, "name": "Мишка", "emoji": "🧸", "price": 15, "order": 2},
    {"id": 5170250947678437525, "name": "Подарок", "emoji": "🎁", "price": 25, "order": 3},
    {"id": 5168103777563050263, "name": "Роза", "emoji": "🌹", "price": 25, "order": 4},
    {"id": 5170144170496491616, "name": "Торт", "emoji": "🎂", "price": 50, "order": 5},
    {"id": 5170314324215857265, "name": "Букет", "emoji": "💐", "price": 50, "order": 6},
    {"id": 5170564780938756245, "name": "Ракета", "emoji": "🚀", "price": 50, "order": 7},
    {"id": 6028601630662853006, "name": "Шампанское", "emoji": "🍾", "price": 50, "order": 8},
    {"id": 5168043875654172773, "name": "Кубок", "emoji": "🏆", "price": 100, "order": 9},
    {"id": 5170690322832818290, "name": "Кольцо", "emoji": "💍", "price": 100, "order": 10},
    {"id": 5170521118301225164, "name": "Алмаз", "emoji": "💎", "price": 100, "order": 11},
]
STANDARD_GIFT_IDS = {int(x["id"]) for x in STANDARD_GIFTS_CATALOG}

# ---------------------------------------------------------------------------
# TELEGRAM GIFTS
# ---------------------------------------------------------------------------

def gift_id_of(gift: Any) -> Optional[int]:
    try:
        v = getattr(gift, "id", None)
        return int(v) if v is not None else None
    except Exception:
        return None


def gift_price_of(gift: Any) -> Optional[int]:
    for attr in ("price", "stars", "star_count"):
        try:
            value = getattr(gift, attr, None)
            if value is not None:
                return int(value)
        except Exception:
            pass
    return None


def gift_name_of(gift: Any) -> str:
    for attr in ("name", "title"):
        value = getattr(gift, attr, None)
        if value:
            return str(value)
    sticker = getattr(gift, "sticker", None)
    alt = getattr(sticker, "emoji", None) if sticker else None
    return str(alt or "🎁 Подарок")


def gift_available(gift: Any) -> bool:
    sold_out = bool(getattr(gift, "sold_out", False))
    return not sold_out


def gift_emoji_of(gift: Any) -> str:
    """Возвращает emoji обычного подарка из sticker/custom_emoji-метаданных."""
    direct = getattr(gift, "emoji", None)
    if direct:
        return str(direct)

    sticker = getattr(gift, "sticker", None)
    if sticker is not None:
        for attr in ("emoji", "alt"):
            value = getattr(sticker, attr, None)
            if value:
                return str(value)

    # В некоторых сборках emoji может приходить через модель подарка.
    for attr in ("title", "name"):
        value = getattr(gift, attr, None)
        if value and len(str(value)) <= 8:
            return str(value)

    return "🎁"


def is_standard_gift(gift: Any) -> bool:
    """
    В каталог попадают только базовые Telegram Star Gifts.

    Исключаем:
    - collectible / unique / NFT;
    - gifts, выставленные/доступные через resale/market;
    - auction-позиции;
    - sold out.

    При этом обычные limited/birthday/premium Gifts не исключаем.
    """
    cls_name = type(gift).__name__.lower()

    if any(token in cls_name for token in ("unique", "collectible", "resale", "auction")):
        return False

    # Явные market/auction/resale flags.
    for attr in (
        "auction",
        "market",
        "marketplace",
        "resale",
        "availability_resale",
        "is_market",
        "is_marketplace",
        "is_collectible",
        "is_unique",
        "is_resale",
        "is_auction",
        "sold_out",
    ):
        try:
            if bool(getattr(gift, attr, False)):
                return False
        except Exception:
            pass

    # Некоторые версии API сообщают тип через gift.kind/type.
    for attr in ("kind", "type"):
        try:
            value = getattr(gift, attr, None)
            if value and any(token in str(value).lower() for token in ("unique", "collect", "resale", "market", "auction", "nft")):
                return False
        except Exception:
            pass

    return True


async def fetch_gifts_for_account(account: Dict[str, Any]) -> List[Dict[str, Any]]:
    async with account_client(account) as app:
        items: List[Any] = []
        if hasattr(app, "get_available_gifts"):
            items = await app.get_available_gifts()
        else:
            try:
                from pyrogram import raw
                result = await app.invoke(raw.functions.payments.GetStarGifts(hash=0))
                items = list(getattr(result, "gifts", []) or [])
            except Exception as exc:
                raise RuntimeError(f"Текущая версия Pyrogram не предоставляет Gifts API: {exc}")
        result: List[Dict[str, Any]] = []
        seen_ids = set()
        for gift in items or []:
            # В каталог AutoGift попадают только обычные, реально выдаваемые Gifts.
            if not is_standard_gift(gift):
                continue
            if not gift_available(gift):
                continue

            gid = gift_id_of(gift)
            price = gift_price_of(gift)
            if gid is None or price is None:
                continue
            if gid in seen_ids:
                continue
            seen_ids.add(gid)

            result.append({
                "id": gid,
                "price": price,
                "name": gift_name_of(gift),
                "emoji": gift_emoji_of(gift),
                "sold_out": False,
                "limited": bool(getattr(gift, "limited", False)),
                "birthday": bool(getattr(gift, "birthday", False)),
                "require_premium": bool(getattr(gift, "require_premium", False)),
            })

        # Удобный и предсказуемый порядок: сначала дешёвые стандартные Gifts.
        result.sort(key=lambda x: (int(x.get("price", 0)), str(x.get("name", "")).lower()))
        return result


async def get_gift_price_live(gift_id: int) -> Optional[int]:
    # A short cache avoids reconnecting once per distinct gift in the same order.
    if time.time() - ts(CATALOG.get("live_checked_at")) > 5:
        try:
            await refresh_catalog_for_menu(0)
        except Exception:
            return None
    for gift in CATALOG.get("items", []):
        if int(gift["id"]) == int(gift_id) and gift.get("available"):
            return int(gift["price"])
    return None


async def resolve_recipient(app: Client, username: str):
    return await app.resolve_peer(username)


async def send_gift_raw(app, recipient, gift_id, hide_name, comment, expected_price, before_submit):
    from pyrogram import raw
    peer = await app.resolve_peer(recipient)
    message = raw.types.TextWithEntities(text=str(comment or "")[:128], entities=[]) if comment else None
    invoice = raw.types.InputInvoiceStarGift(peer=peer, gift_id=int(gift_id),
                                             hide_name=bool(hide_name), message=message)
    form = await app.invoke(raw.functions.payments.GetPaymentForm(invoice=invoice))
    bill = getattr(form, "invoice", None)
    prices = getattr(bill, "prices", None)
    if (getattr(bill, "currency", None) != "XTR" or not prices
            or sum(int(p.amount) for p in prices) != expected_price
            or expected_price <= 0):
        raise PreflightError("Цена/валюта платёжной формы отличается от проверенного каталога.")
    # Persist form_id before the sole payment request. Never regenerate on timeout.
    before_submit(int(form.form_id))
    result = await app.invoke(raw.functions.payments.SendStarsForm(form_id=form.form_id, invoice=invoice),
                              retries=0, timeout=30, sleep_threshold=0)
    if not isinstance(result, raw.types.payments.PaymentResult):
        raise RuntimeError("Telegram не подтвердил платёж: " + type(result).__name__)
    return {"form_id": int(form.form_id), "result_type": type(result).__name__}


async def send_one_gift(account, username, gift_id, hide_name, comment, recipient_id=None,
                        expected_price=None, before_submit=None):
    if recipient_id is None or before_submit is None or expected_price is None:
        raise PreflightError("Отсутствуют подтверждённый получатель, цена или журнал выдачи.")
    async with account_client(account) as app:
        recipient = await app.get_chat(username)
        if int(recipient.id) != int(recipient_id):
            raise PreflightError("Telegram username теперь принадлежит другому получателю.")
        if ChatType is None or recipient.type not in (ChatType.PRIVATE, ChatType.CHANNEL):
            raise PreflightError("Этот тип получателя не поддерживается.")
        return await send_gift_raw(app, int(recipient_id), gift_id, hide_name, comment,
                                   expected_price, before_submit)


# ---------------------------------------------------------------------------
# ACCOUNT POOL
# ---------------------------------------------------------------------------

def has_account_credentials(account: Dict[str, Any]) -> bool:
    """Возвращает True, если аккаунт можно открыть в Telegram.

    Баланс здесь намеренно НЕ проверяется: каталог Gifts и проверка username
    должны работать даже на аккаунте с 0 Stars.
    """
    if account.get("session_string"):
        return True
    return bool(account.get("session_name") and account.get("session_workdir"))


def get_active_accounts() -> List[Dict[str, Any]]:
    """Аккаунты, пригодные для отправки: авторизация + положительный баланс."""
    return [a for a in ACCOUNTS if a.get("active") and has_account_credentials(a)
            and a.get("balance") is not None and int(a["balance"]) > 0
            and ts(a.get("cooldown_until")) <= time.time()]


def get_telegram_accounts() -> List[Dict[str, Any]]:
    """Все валидные Telegram-сессии, независимо от баланса."""
    return [a for a in ACCOUNTS if has_account_credentials(a)]


async def refresh_account_balance(account: Dict[str, Any]) -> Optional[int]:
    if ts(account.get("cooldown_until")) > time.time():
        return None
    try:
        async with account_client(account) as app:
            if hasattr(app, "get_stars_balance"):
                balance = int(await app.get_stars_balance())
            else:
                from pyrogram import raw
                me = await app.get_me()
                peer = await app.resolve_peer(me.id)
                status = await app.invoke(raw.functions.payments.GetStarsStatus(peer=peer))
                balance = int(getattr(getattr(status, "balance", None), "amount", getattr(status, "balance", 0)))
        account["balance"] = balance
        account["last_error"] = ""
        return balance
    except Exception as exc:
        account["balance"] = None
        account["last_error"] = str(exc)
        _apply_account_cooldown(account, exc)
        logger.error("%s balance %s failed: %s", LOGGER_PREFIX, account.get("id"), exc)
        return None


def total_balance() -> int:
    return sum(int(a["balance"]) for a in get_active_accounts())


def _apply_account_cooldown(account, exc):
    """Only a structured Telegram RPC rejection permits switching senders."""
    from pyrogram.errors import FloodWait, RPCError
    if isinstance(exc, FloodWait):
        account["cooldown_until"] = time.time() + max(1, int(exc.value or 1))
        account["last_error"] = "FloodWait: " + str(int(exc.value or 1)) + " s"
        return True
    if isinstance(exc, RPCError) and getattr(exc, "ID", "") == "BALANCE_TOO_LOW":
        account["balance"] = 0
        account["last_error"] = "BALANCE_TOO_LOW"
        return True
    return False


async def refresh_all_balances(notify: bool = True) -> Dict[str, Optional[int]]:
    result = {}
    for account in list(ACCOUNTS):
        old = account.get("balance")
        value = await refresh_account_balance(account)
        result[str(account.get("id"))] = value
        if notify and old is not None and int(old) > 0 and value == 0:
            notify_operators(f"⚠️ Сессия <b>{h(account.get('label') or account.get('id'))}</b> исчерпала Stars.")
        elif notify and old == 0 and value and value > 0:
            notify_operators(f"✅ Сессия <b>{h(account.get('label') or account.get('id'))}</b> снова доступна: {format_stars(value)}")
    _safe_json_save(ACCOUNTS_PATH, ACCOUNTS)
    await update_lot_protection()
    return result


async def choose_accounts(required: int) -> List[Dict[str, Any]]:
    candidates = []
    await refresh_all_balances(notify=False)
    for account in sorted(get_active_accounts(), key=lambda x: (x.get("last_used", 0), -(int(x.get("balance") or 0)))):
        if int(account.get("balance") or 0) >= required:
            candidates.append(account)
    return candidates


async def update_lot_protection() -> None:
    if not RUNNING or STORAGE_ERROR or not CONFIG.get("lot_protection", True):
        return
    balances = [int(a["balance"]) for a in get_active_accounts()]
    best_balance = max(balances, default=0)
    threshold = int(CONFIG.get("low_balance_threshold") or 0)
    below_threshold = best_balance < threshold if CONFIG.get("low_balance_enabled", True) else best_balance <= 0
    if time.time() - ts(CATALOG.get("live_checked_at")) > 300:
        try:
            await refresh_catalog_for_menu(0)
        except Exception:
            logger.warning("%s live catalog unavailable for lot protection", LOGGER_PREFIX)
    fresh = time.time() - ts(CATALOG.get("live_checked_at")) <= 300
    prices = {int(g["id"]): int(g["price"]) for g in CATALOG.get("items", [])
              if g.get("available") and g.get("price") and fresh}
    changed = []
    # Per-listing ownership of pauses: never reactivate a listing paused by its owner.
    for lot in list(LOTS.values()):
        if not lot.get("enabled") or not _binding_valid(lot):
            continue
        lid = int(lot["binding"]["lot_id"])
        ids = [int(gid) for gid in lot["gift_ids"]]
        unit_cost = sum(prices.get(gid, 0) for gid in ids)
        required = max(unit_cost, int(lot.get("capacity_required") or 0))
        pause = below_threshold or any(gid not in prices for gid in ids) or best_balance < required
        if pause and not lot.get("paused_by_autogift"):
            if lot_is_active(lid) is True and set_lot_active(lid, False):
                lot["paused_by_autogift"] = True
                lot["pause_reason"] = "Недостаточно Stars на одном аккаунте / не подтверждена доступность Gifts"
                _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
                changed.append(f"⏸ {lot.get('name', lid)}")
        elif not pause and lot.get("paused_by_autogift") and CONFIG.get("auto_reactivate", True):
            if set_lot_active(lid, True):
                lot["paused_by_autogift"] = False
                lot.pop("capacity_required", None)
                lot.pop("pause_reason", None)
                _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
                changed.append(f"▶️ {lot.get('name', lid)}")
    CONFIG["lots_operational"] = any(lot.get("enabled") and not lot.get("paused_by_autogift") for lot in LOTS.values())
    _safe_json_save(CONFIG_PATH, CONFIG)
    if changed:
        notify_operators("🛡️ AutoGift: изменение доступности лотов\n" + h("\n".join(changed)[:2500])
                         + f"\nМаксимальный баланс аккаунта: {format_stars(best_balance)}")


async def initialize_accounts() -> None:
    for account in list(ACCOUNTS):
        if not has_account_credentials(account):
            account["active"] = False
            continue
        await refresh_account_balance(account)
    _safe_json_save(ACCOUNTS_PATH, ACCOUNTS)
    await update_lot_protection()


# ---------------------------------------------------------------------------
# AUTHENTICATION WIZARD
# ---------------------------------------------------------------------------

def auth_worker(user_id: int, phone: str) -> None:
    async def flow():
        if Client is None:
            raise RuntimeError("Pyrogram не установлен.")
        api_id, api_hash = get_api()
        if not api_id or not api_hash:
            raise RuntimeError("Сначала укажите API ID и API Hash.")
        session_name = f"ag_auth_{user_id}_{int(time.time())}"
        proxy = parse_proxy(CONFIG.get("proxy", ""))
        if CONFIG.get("proxy_required") and not proxy:
            raise RuntimeError("Сначала настройте обязательный Proxy.")
        app = Client(
            name=session_name,
            api_id=api_id,
            api_hash=api_hash,
            proxy=proxy,
            workdir=str(STORAGE / "tg_workdir"),
            in_memory=True,
        )
        await app.connect()
        try:
            sent = await app.send_code(phone)
            _auth_output(user_id, {"kind": "code", "sent": sent})
            code = str(await asyncio.to_thread(_auth_input_wait, user_id, 180)).strip().replace(" ", "").replace("-", "")
            if code.lower() == "/cancel":
                raise RuntimeError("Авторизация отменена.")
            try:
                await app.sign_in(phone_number=phone, phone_code_hash=sent.phone_code_hash, phone_code=code)
            except Exception as exc:
                if getattr(exc, "ID", "") == "SESSION_PASSWORD_NEEDED" or type(exc).__name__ == "SessionPasswordNeeded":
                    _auth_output(user_id, {"kind": "password"})
                    password = str(await asyncio.to_thread(_auth_input_wait, user_id, 180))
                    if password.lower() == "/cancel":
                        raise RuntimeError("Авторизация отменена.")
                    await app.check_password(password)
                else:
                    raise
            me = await app.get_me()
            if not is_operator(user_id):
                raise RuntimeError("Доступ оператора был отозван; добавление аккаунта отменено.")
            session_string = await app.export_session_string()
            account_id = f"tg_{int(me.id)}"
            existing = next((a for a in ACCOUNTS if a.get("id") == account_id), None)
            data = DEFAULT_ACCOUNT.copy()
            data.update({
                "id": account_id,
                "label": ((me.first_name or "") + (" " + me.last_name if me.last_name else "")).strip() or account_id,
                "phone": phone,
                "session_string": session_string,
                "active": True,
                "balance": None,
                "last_error": "",
            })
            if existing:
                for counter in ("gifts_sent", "spent_stars", "last_used"):
                    data[counter] = existing.get(counter, data[counter])
                existing.update(data)
            else:
                ACCOUNTS.append(data)
            _safe_json_save(ACCOUNTS_PATH, ACCOUNTS)
            _auth_output(user_id, {"kind": "done", "account_id": account_id, "name": data["label"]})
        finally:
            await app.disconnect()

    try:
        asyncio.run(flow())
    except Exception as exc:
        logger.error("%s auth worker %s failed: %s", LOGGER_PREFIX, user_id, exc)
        _auth_output(user_id, {"kind": "error", "error": str(exc)})


def _auth_output(user_id: int, value: dict) -> None:
    AUTH_OUTPUT_QUEUES.setdefault(user_id, pyqueue.Queue()).put(value)


def _auth_input_wait(user_id: int, timeout: int = 180) -> str:
    q = AUTH_INPUT_QUEUES.setdefault(user_id, pyqueue.Queue())
    return str(q.get(timeout=timeout))


def _auth_input_put(user_id: int, value: str) -> None:
    AUTH_INPUT_QUEUES.setdefault(user_id, pyqueue.Queue()).put(value)


# ---------------------------------------------------------------------------
# LOTS / ORDERS
# ---------------------------------------------------------------------------

def discover_legacy_sessions() -> None:
    """Подхватывает существующие stars_*.session из старой версии AutoGift."""
    existing_paths = {str(a.get("session_workdir", "")) + "/" + str(a.get("session_name", "")) for a in ACCOUNTS if a.get("session_name")}
    changed = False
    for directory in LEGACY_SESSION_DIRS:
        try:
            if not directory.exists():
                continue
            for file in sorted(directory.glob("stars_*.session")):
                key = f"{directory}/{file.stem}"
                if key in existing_paths:
                    continue
                account_id = f"tg_file_{file.stem}"
                if any(str(a.get("id")) == account_id for a in ACCOUNTS):
                    continue
                data = DEFAULT_ACCOUNT.copy()
                data.update({
                    "id": account_id,
                    "label": file.stem,
                    "session_name": file.stem,
                    "session_workdir": str(directory),
                    "session_string": "",
                    "active": True,
                })
                ACCOUNTS.append(data)
                existing_paths.add(key)
                changed = True
                logger.info("%s imported legacy session %s", LOGGER_PREFIX, file)
        except Exception as exc:
            logger.warning("%s legacy session scan %s failed: %s", LOGGER_PREFIX, directory, exc)
    if changed:
        _safe_json_save(ACCOUNTS_PATH, ACCOUNTS)


BINDING_VERSION = 1
MARKER_RE = re.compile(r"\[AutoGift:([a-f0-9]{32})\]")


def _canonical(value: Any) -> str:
    return " ".join(html.unescape(str(value or "")).split())


def _fingerprint(summary: Any, description: Any) -> str:
    payload = json.dumps([_canonical(summary), _canonical(description)], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _gift_signature(lot: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(lot.get("gift_ids", [])).encode()).hexdigest()


def _binding_valid(lot: Dict[str, Any]) -> bool:
    try:
        b = lot.get("binding") or {}
        return (b.get("version") == BINDING_VERSION
                and bool(re.fullmatch(r"[a-f0-9]{32}", str(b.get("token") or "")))
                and str(b.get("lot_id")) == str(lot.get("funpay_lot_id") or lot.get("lot_id"))
                and _positive_int(b.get("seller_id")) > 0
                and b.get("gift_signature") == _gift_signature(lot)
                and isinstance(b.get("fingerprints"), dict) and bool(b["fingerprints"])
                and bool(b.get("subcategory_id")) and bool(_gift_plan({"units": 1, "gift_ids": lot.get("gift_ids")})))
    except (TypeError, ValueError, AttributeError):
        return False


def _lot_snapshot(lf: Any) -> Dict[str, Any]:
    fields = lf.fields
    sub = getattr(lf, "subcategory", None)
    sub_id = getattr(sub, "id", None) or fields.get("node_id")
    if not sub_id:
        raise ValueError("FunPay не вернул раздел лота. Привязка остановлена.")
    variants = {}
    for lang in ("ru", "en"):
        title = fields.get("fields[summary][" + lang + "]") or fields.get("fields[name][" + lang + "]")
        desc = fields.get("fields[desc][" + lang + "]")
        if title and desc:
            variants[lang] = {"title": str(title), "description": str(desc),
                              "fingerprint": _fingerprint(title, desc)}
    if not variants:
        raise ValueError("Для привязки заполните краткое и полное описание лота.")
    return {"subcategory_id": str(sub_id), "variants": variants}


def _live_binding_matches(lf: Any, lot: Dict[str, Any]) -> bool:
    if not _binding_valid(lot):
        return False
    b = lot["binding"]
    snap = _lot_snapshot(lf)
    return (str(getattr(lf, "lot_id", None)) == str(b["lot_id"])
            and snap["subcategory_id"] == b["subcategory_id"]
            and {lang: v["fingerprint"] for lang, v in snap["variants"].items()} == b["fingerprints"]
            and all(MARKER_RE.findall(v["description"]) == [b["token"]]
                    for v in snap["variants"].values()))


def migrate_lots() -> None:
    # Old bindings were built with unsafe heuristics. Never grandfather them in.
    for lot in LOTS.values():
        if not isinstance(lot, dict):
            continue
        lot.setdefault("gift_ids", [lot["gift_id"]] if lot.get("gift_id") else [])
        lot.setdefault("gift_names", [lot.get("gift_name", "Подарок")])
        lot.setdefault("gift_cost", 0)
        if not _binding_valid(lot):
            lot["enabled"] = False
            lot["safety_blocked_reason"] = "Требуется подтверждение привязки AutoGift"
    for row in ORDERS.values():
        if row.get("status") in ("completed", "refunded", "ignored_foreign"):
            continue
        if not row.get("binding_token"):
            row["status"] = "needs_review"
            row["last_error"] = "Старый заказ без подтверждённой привязки; автоматическая выдача запрещена."
        if row.get("status") in {"ready", "sending", "refunding"} or not _ledger_valid(row):
            row["status"] = "needs_review"
            row["last_error"] = "Незавершённая операция или старый журнал; проверьте историю вручную."
        if not isinstance(row.get("delivery", []), list) or not all(isinstance(x, dict) for x in row.get("delivery", [])):
            raise _storage_failed(ORDERS_PATH, ValueError("Malformed delivery ledger"))
        if any(x.get("status") == "sending" for x in row.get("delivery", [])):
            for item in row["delivery"]:
                if item.get("status") == "sending":
                    item["status"] = "unknown"
            row["status"] = "needs_review"
    _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
    _safe_json_save(ORDERS_PATH, ORDERS)


def _explicit_lot_ids(order: Any) -> set:
    # Never read event-level IDs, HTML chat links, category IDs or arbitrary numbers.
    ids = set()
    for key in ("lot_id", "offer_id", "offerId", "lotId", "lotID"):
        v = getattr(order, key, None)
        if v is not None and str(v).isdigit() and int(v) > 0:
            ids.add(str(int(v)))
    for key in ("lot", "offer"):
        obj = getattr(order, key, None)
        if obj is None:
            continue
        for field in ("id", "lot_id", "offer_id"):
            v = obj.get(field) if isinstance(obj, dict) else getattr(obj, field, None)
            if v is not None and str(v).isdigit() and int(v) > 0:
                ids.add(str(int(v)))
    return ids


def find_lot_by_order(order: Any, event: Any = None) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Authenticate a seller-controlled purchase snapshot, never buyer/chat text."""
    account = getattr(CARDINAL_INSTANCE, "account", None)
    if account is None or str(getattr(order, "seller_id", "")) != str(account.id):
        return None
    paid = getattr(enums.OrderStatuses, "PAID", None)
    if paid is None or getattr(order, "status", None) != paid:
        return None
    description = getattr(order, "full_description", None)
    title = getattr(order, "short_description", None) or getattr(order, "title", None)
    tokens = MARKER_RE.findall(str(description or ""))
    if len(tokens) != 1 or not title:
        return None
    ids = _explicit_lot_ids(order)
    fingerprint = _fingerprint(title, description)
    sub_id = str(getattr(getattr(order, "subcategory", None), "id", ""))
    candidates = []
    for key, lot in LOTS.items():
        if not isinstance(lot, dict) or not lot.get("enabled") or not _binding_valid(lot):
            continue
        b = lot["binding"]
        if (tokens[0] != b["token"] or str(b["seller_id"]) != str(account.id)
                or sub_id != b["subcategory_id"] or fingerprint not in b["fingerprints"].values()):
            continue
        if ids and ids != {str(b["lot_id"])}:
            continue
        candidates.append((key, lot))
    if len(candidates) != 1:
        return None
    key, lot = candidates[0]
    # Also detect repurposed/replaced live listings before taking ownership.
    try:
        lf = account.get_lot_fields(int(lot["binding"]["lot_id"]))
        if not _live_binding_matches(lf, lot):
            return None
    except Exception:
        logger.warning("%s ownership check unavailable for #%s; no action", LOGGER_PREFIX, getattr(order, "id", "?"))
        return None
    return key, lot


def _active_order_is_still_own(c: Cardinal, data: Dict[str, Any]) -> bool:
    if STORAGE_ERROR or not data.get("binding_token") or not data.get("order_id"):
        return False
    try:
        current = c.account.get_order(data["order_id"])
        found = find_lot_by_order(current)
    except Exception:
        logger.warning("%s pending ownership check unavailable; delivery blocked", LOGGER_PREFIX)
        return False
    if not found:
        return False
    key, lot = found
    try:
        return (str(current.id) == str(data["order_id"])
                and str(key) == str(data.get("lot_key"))
                and lot["binding"]["token"] == data["binding_token"]
                and str(getattr(current, "buyer_id", "")) == str(data.get("buyer_id"))
                and lot.get("gift_ids") == data.get("gift_ids")
                and _order_quantity(current) == data.get("units")
                and _order_chat_id(c, current) == str(data.get("chat_id")))
    except Exception:
        return False


def _prepare_binding(c: Cardinal, key: str) -> Dict[str, Any]:
    lot = LOTS[key]
    lid = int(lot.get("funpay_lot_id") or lot.get("lot_id") or 0)
    if lid <= 0:
        raise ValueError("Нет корректного ID лота.")
    duplicates = [k for k, v in LOTS.items()
                  if k != key and str(v.get("funpay_lot_id") or v.get("lot_id")) == str(lid)]
    if duplicates:
        raise ValueError("Этот ID есть в нескольких привязках. Удалите дубликат в AutoGift.")
    _gift_plan({"units": 1, "gift_ids": lot.get("gift_ids")})
    lf = c.account.get_lot_fields(lid)
    if str(getattr(lf, "lot_id", "")) != str(lid):
        raise ValueError("FunPay вернул другой ID лота.")
    return {"key": key, "lot_id": lid, "snapshot": _lot_snapshot(lf),
            "gift_signature": _gift_signature(lot),
            "nonce": secrets.token_hex(3), "expires": time.time() + 600}


def _confirm_binding(c: Cardinal, preview: Dict[str, Any], operator_id: int) -> None:
    if time.time() > preview["expires"]:
        raise ValueError("Предпросмотр устарел. Откройте привязку заново.")
    lot = LOTS[preview["key"]]
    if _gift_signature(lot) != preview["gift_signature"]:
        raise ValueError("Состав Gifts изменился. Откройте привязку заново.")
    lf = c.account.get_lot_fields(preview["lot_id"])
    if (str(getattr(lf, "lot_id", "")) != str(preview["lot_id"])
            or _lot_snapshot(lf) != preview["snapshot"]):
        raise ValueError("Лот изменён после предпросмотра. Привязка остановлена.")
    # Disable in memory and on disk before any external write or network timeout.
    lot["enabled"] = False
    lot.pop("binding", None)
    _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
    token = secrets.token_hex(16)
    wanted_descriptions = {}
    for lang, variant in preview["snapshot"]["variants"].items():
        desc = MARKER_RE.sub("", variant["description"]).rstrip()
        wanted_descriptions[lang] = desc + "\n\n[AutoGift:" + token + "]"
        setattr(lf, 'description_' + lang, wanted_descriptions[lang])
    if not callable(getattr(lf, 'renew_fields', None)):
        raise ValueError('FunPayAPI не поддерживает обновление полей лота.')
    lf.renew_fields()
    c.account.save_lot(lf)
    actual = c.account.get_lot_fields(preview["lot_id"])
    snap = _lot_snapshot(actual)
    if str(getattr(actual, "lot_id", "")) != str(preview["lot_id"]):
        raise ValueError("Не удалось подтвердить ID после сохранения.")
    for lang, v in snap["variants"].items():
        wanted = wanted_descriptions.get(lang, '')
        if (MARKER_RE.findall(v["description"]) != [token]
                or _canonical(v["description"]) != _canonical(wanted)
                or _canonical(v["title"]) != _canonical(preview["snapshot"]["variants"][lang]["title"])):
            raise ValueError("FunPay не подтвердил сохранение метки/описания.")
    if set(snap["variants"]) != set(preview["snapshot"]["variants"]):
        raise ValueError("Языковые варианты лота изменились.")
    lot["binding"] = {"version": BINDING_VERSION, "token": token,
                      "lot_id": str(preview["lot_id"]), "seller_id": str(c.account.id),
                      "subcategory_id": snap["subcategory_id"],
                      "fingerprints": {k: v["fingerprint"] for k, v in snap["variants"].items()},
                      "gift_signature": _gift_signature(lot), "operator_id": operator_id,
                      "bound_at": time.time()}
    lot.pop("safety_blocked_reason", None)
    lot["enabled"] = True
    _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})


def prepare_manual_tag(key: str) -> str:
    lot = LOTS[key]
    if _binding_valid(lot):
        return "[AutoGift:" + lot["binding"]["token"] + "]"
    lot.setdefault("pending_token", secrets.token_hex(16))
    lot["enabled"] = False
    _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
    return "[AutoGift:" + lot["pending_token"] + "]"


def confirm_manual_tag(c: Cardinal, key: str, operator_id: int) -> None:
    lot = LOTS[key]
    token = lot.get("pending_token") or (lot.get("binding") or {}).get("token")
    if not re.fullmatch(r"[a-f0-9]{32}", str(token or "")):
        raise ValueError("Сначала получите тег для этого лота.")
    if any(k != key and ((v.get("binding") or {}).get("token") == token or v.get("pending_token") == token)
           for k, v in LOTS.items()):
        raise ValueError("Тег используется другим лотом.")
    lid = _positive_int(lot.get("funpay_lot_id") or lot.get("lot_id"))
    if any(k != key and str(v.get("funpay_lot_id") or v.get("lot_id")) == str(lid) for k, v in LOTS.items()):
        raise ValueError("Этот ID уже указан в другой привязке.")
    _gift_plan({"units": 1, "gift_ids": lot.get("gift_ids")})
    fields = c.account.get_lot_fields(lid)
    if str(fields.lot_id) != str(lid):
        raise ValueError("FunPay вернул другой ID лота.")
    snap = _lot_snapshot(fields)
    if not all(MARKER_RE.findall(v["description"]) == [token] for v in snap["variants"].values()):
        raise ValueError("Добавьте ровно один полученный тег в каждый заполненный язык подробного описания и сохраните лот.")
    # No write to FunPay: authenticate the listing's saved seller-controlled snapshot.
    lot["binding"] = {"version": BINDING_VERSION, "token": token, "lot_id": str(lid),
                      "seller_id": str(c.account.id), "subcategory_id": snap["subcategory_id"],
                      "fingerprints": {lang: v["fingerprint"] for lang, v in snap["variants"].items()},
                      "gift_signature": _gift_signature(lot), "operator_id": operator_id, "bound_at": time.time()}
    lot.pop("pending_token", None)
    lot.pop("safety_blocked_reason", None)
    lot["enabled"] = True
    _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})


def lot_cost(data: Dict[str, Any], live_catalog: Optional[List[Dict[str, Any]]] = None) -> int:
    ids = [int(x) for x in data.get("gift_ids", [])]
    prices = {int(x["id"]): int(x["price"]) for x in (live_catalog or []) if x.get("id") is not None and x.get("price") is not None}
    cost = 0
    for gid in ids:
        if gid in prices:
            cost += prices[gid]
        else:
            for old_name in ("gift_cost",):
                try:
                    if len(ids) == 1:
                        cost += int(data.get(old_name) or 0)
                    break
                except Exception:
                    pass
    return cost


async def validate_username(username: str) -> Tuple[bool, str, Optional[int]]:
    # Проверка получателя не должна ломаться только потому, что на аккаунте 0 Stars.
    for account in get_telegram_accounts():
        try:
            async with account_client(account) as app:
                user = await app.get_chat(username)
                typ = getattr(user, "type", None)
                if ChatType is not None and typ in (ChatType.PRIVATE, ChatType.CHANNEL):
                    name = getattr(user, "first_name", None) or getattr(user, "title", None) or username
                    return True, str(name), int(user.id)
        except Exception as exc:
            logger.debug("%s username %s failed on %s: %s", LOGGER_PREFIX, username, account.get("id"), exc)
    return False, "Пользователь не найден", None


def save_order(order: Dict[str, Any]) -> None:
    with ORDER_LOCK:
        order["updated_at"] = time.time()
        ORDERS[str(order["order_id"])] = order
        _safe_json_save(ORDERS_PATH, ORDERS)


def mark_order_status(order_id: str, status: str, **extra) -> None:
    with ORDER_LOCK:
        item = ORDERS.get(str(order_id), {"order_id": str(order_id)})
        item["status"] = status
        item.update(extra)
        item["updated_at"] = time.time()
        ORDERS[str(order_id)] = item
        _safe_json_save(ORDERS_PATH, ORDERS)


def calc_profit(order: Any, star_cost: int) -> float:
    try:
        return round(float(order.price) - int(order.amount) * star_cost, 2)
    except Exception:
        return 0.0


async def execute_order(c: Cardinal, data: Dict[str, Any]) -> str:
    try:
        return await _execute_order_impl(c, data)
    finally:
        # Re-check availability immediately after spending, as well as every minute.
        try:
            await update_lot_protection()
        except Exception:
            logger.exception("%s listing protection needs attention", LOGGER_PREFIX)
            notify_operators("🟠 AutoGift: не удалось обновить защиту лотов; проверьте их активность.")


async def _execute_order_impl(c: Cardinal, data: Dict[str, Any]) -> str:
    if (not RUNNING or STORAGE_ERROR or data.get("status") not in {"ready", "sending"}
            or not _delivery_consent_valid(data) or not _active_order_is_still_own(c, data)
            or not _ledger_valid(data)):
        data["last_error"] = "Заказ, количество, привязка, журнал или подтверждение не прошли проверку."
        save_order(data)
        return "needs_review"
    items = data["delivery"]
    if any(i["status"] not in {"pending", "sent"} for i in items):
        data["last_error"] = "Предыдущая попытка требует ручной проверки."
        save_order(data)
        return "needs_review"
    pending = [i for i in items if i["status"] == "pending"]
    if not pending:
        data.update(status="completed", completed_at=time.time())
        save_order(data)
        return "completed"
    prices = {}
    for gid in {i["gift_id"] for i in pending}:
        price = await get_gift_price_live(gid)
        if price is None or price <= 0:
            data["last_error"] = f"Gift {gid} недоступен или его цена не подтверждена."
            save_order(data)
            return "needs_review" if delivered_count(data) else "failed_no_delivery"
        prices[gid] = int(price)
    required = sum(prices[i["gift_id"]] for i in pending)
    data["required_stars"] = required + sum(int(i.get("price", 0)) for i in items if i["status"] == "sent")
    if data["required_stars"] > _positive_int(CONFIG.get("max_order_stars", 10000)):
        data["last_error"] = "Превышен лимит Stars на заказ."
        save_order(data)
        return "needs_review"
    await refresh_all_balances(notify=False)
    # One account covers the entire remaining set. Telegram cannot pool a single payment.
    account = next((a for a in sorted(get_active_accounts(), key=lambda a: a.get("last_used", 0))
                    if int(a["balance"]) >= required), None)
    remaining = required
    attempted = set()
    key = None
    try:
        for item in pending:
            while True:
                if account is None:
                    account = next((a for a in sorted(get_active_accounts(), key=lambda a: a.get("last_used", 0))
                                    if str(a["id"]) not in attempted and int(a["balance"]) >= remaining), None)
                if account is None:
                    data["last_error"] = f"Для оставшихся Gifts нужно {remaining} Stars на одном доступном аккаунте."
                    lot = LOTS.get(data.get("lot_key"))
                    if lot and lot.get("binding", {}).get("token") == data.get("binding_token"):
                        lot["capacity_required"] = required
                        _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
                    save_order(data)
                    return "needs_review" if delivered_count(data) else "failed_no_delivery"
                key = str(account["id"])
                ACCOUNT_RESERVATIONS[key] = remaining
                submitted = False
                def before_submit(form_id):
                    nonlocal submitted
                    if (not RUNNING or STORAGE_ERROR or not account.get("active")
                            or not any(a is account for a in ACCOUNTS)
                            or not _delivery_consent_valid(data) or not _active_order_is_still_own(c, data)):
                        raise PreflightError("Выдача остановлена: изменились заказ, получатель, доступ или состояние плагина.")
                    item.update(status="sending", account_id=key, started_at=time.time(),
                                price=prices[item["gift_id"]], form_id=form_id)
                    data["status"] = "sending"
                    save_order(data)  # Must succeed BEFORE any Stars payment.
                    submitted = True
                try:
                    receipt = await send_one_gift(account, data["username"], item["gift_id"],
                                                   bool(data.get("hide_name", True)), data.get("comment") or "",
                                                   data["recipient_id"], prices[item["gift_id"]], before_submit)
                    if not submitted or not isinstance(receipt, dict) or receipt.get("result_type") != "PaymentResult":
                        raise RuntimeError("Отправитель не вернул подтверждение платежа.")
                except StorageError:
                    raise
                except Exception as exc:
                    if _apply_account_cooldown(account, exc):
                        # FloodWait / BALANCE_TOO_LOW explicitly reject this request.
                        # A timeout/unknown result never enters this retry path.
                        item.setdefault("attempts", []).append({"account_id": key,
                            "form_id": item.get("form_id") if submitted else None,
                            "status": "rejected", "error": type(exc).__name__, "at": time.time()})
                        item["status"] = "pending"
                        save_order(data)
                        _safe_json_save(ACCOUNTS_PATH, ACCOUNTS)
                        attempted.add(key)
                        ACCOUNT_RESERVATIONS.pop(key, None)
                        account = None
                        continue
                    definitive = not submitted or is_definitive_delivery_error(exc)
                    item.update(status="failed" if definitive else "unknown",
                                error=f"{type(exc).__name__}: {exc}"[:700])
                    data.update(status="needs_review" if not definitive or delivered_count(data) else "failed",
                                last_error=item["error"])
                    save_order(data)
                    return "failed_no_delivery" if data["status"] == "failed" else "needs_review"
                break
            item.update(status="sent", sent_at=time.time(), receipt=receipt)
            save_order(data)  # A later stats/UI failure never resends this gift.
            account["balance"] = max(0, int(account["balance"]) - prices[item["gift_id"]])
            account["gifts_sent"] = int(account.get("gifts_sent") or 0) + 1
            account["spent_stars"] = int(account.get("spent_stars") or 0) + prices[item["gift_id"]]
            account["last_used"] = time.time()
            _safe_json_save(ACCOUNTS_PATH, ACCOUNTS)
            remaining -= prices[item["gift_id"]]
        data.update(status="completed", completed_at=time.time(), last_error="")
        save_order(data)
        return "completed"
    finally:
        ACCOUNT_RESERVATIONS.pop(key, None)


def background_worker() -> None:
    global RUNNING
    while True:
        if RUNNING:
            try:
                run_coro(refresh_all_balances(notify=True))
                send_buyer_reminders()
            except Exception as exc:
                logger.error("%s background balance error: %s", LOGGER_PREFIX, exc)
            time.sleep(60)
        else:
            time.sleep(2)


# ---------------------------------------------------------------------------
# TELEGRAM UI
# ---------------------------------------------------------------------------

def main_text() -> str:
    if not INITIALIZED or not DEPENDENCY_READY or STARTUP_ERROR or STORAGE_ERROR:
        return _recovery_text()
    today = order_stats(1)
    review = len(order_stats(None)["review"])
    active = len(get_active_accounts())
    bound = sum(_binding_valid(lot) for lot in LOTS.values())
    paused = sum(bool(lot.get("paused_by_autogift")) for lot in LOTS.values())
    return (f"<b>🎁 AutoGift</b> <code>v{VERSION}</code>\n"
            f"{'🟢 Выдача включена' if RUNNING else '🔴 Выдача выключена'}\n\n"
            "<b>Аккаунты и лоты</b>\n"
            f"👥 Доступно: {active}/{len(ACCOUNTS)} · 🌟 {format_stars(total_balance())} Stars\n"
            f"📦 Привязано: {bound}/{len(LOTS)} · ⏸ Отключено защитой: {paused}\n\n"
            "<b>Сегодня</b>\n"
            f"✅ Заказов: {len(today['completed'])} · 🎁 Подарков: {today['delivered']}\n"
            f"💳 Выручка: {format_money(today['revenue'])} · 🟠 На проверке: {review}\n\n"
            f"Автор: <a href='{AUTHOR_URL}'>{CREDITS}</a> · <a href='{CHANNEL_URL}'>Канал</a>")

def main_keyboard() -> InlineKeyboardMarkup:
    if not INITIALIZED or not DEPENDENCY_READY or STARTUP_ERROR or STORAGE_ERROR:
        return _recovery_keyboard()
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(InlineKeyboardButton("⏸ Выключить" if RUNNING else "▶️ Включить", callback_data="ag:toggle_running"),
           InlineKeyboardButton("⚙️ Настройки", callback_data="ag:settings"))
    kb.row(InlineKeyboardButton("📦 Мои лоты", callback_data="ag:lots"),
           InlineKeyboardButton("🎁 Каталог подарков", callback_data="ag:catalog"))
    kb.row(InlineKeyboardButton("🌟 Проверить баланс", callback_data="ag:status"),
           InlineKeyboardButton("🧪 Тестовый заказ", callback_data="ag:test"))
    kb.row(InlineKeyboardButton("📈 Продажи", callback_data="ag:stats:1"),
           InlineKeyboardButton("🟠 Проверка", callback_data="ag:review"))
    kb.row(InlineKeyboardButton("👤 Автор", url=AUTHOR_URL), InlineKeyboardButton("📣 Канал", url=CHANNEL_URL))
    kb.row(InlineKeyboardButton("📖 Инструкция", callback_data="ag:help"))
    return kb

def back_keyboard(target="ag:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup().add(InlineKeyboardButton("🏠 Главное меню", callback_data=target))


def send_main(chat_id: int, message_id: Optional[int] = None) -> None:
    text = main_text()
    kb = main_keyboard()
    if message_id:
        try:
            BOT.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=kb)
            return
        except Exception:
            pass
    BOT.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)


def help_text() -> str:
    return (
        "<b>📖 AutoGift — настройка</b>\n\n"
        "<b>1. Доступ.</b> Управлять плагином могут только пользователи, которые уже авторизованы в Telegram-панели Cardinal.\n\n"
        "<b>2. API Telegram.</b> Укажите API ID и API Hash с <code>my.telegram.org</code>, затем добавьте Telegram-аккаунт по номеру или Session String.\n\n"
        "<b>3. Каталог и лоты.</b> Обновите каталог, добавьте лот по точному FunPay ID и вставьте один или несколько Gift ID. Набор отправляется полностью за одну единицу заказа.\n\n"
        "<b>4. Привязка.</b> Добавьте ID лота, выберите Gift ID. Нажмите «Получить тег», скопируйте его в подробное описание и нажмите «Проверить тег». Можно также добавить тег автоматически через подтверждение. После изменения описания проверьте привязку заново.\n\n"
        "<b>Выдача.</b> Username берётся из поля заказа FunPay и проверяется в Telegram. Покупатель подтверждает его через <code>+ НОМЕР_ЗАКАЗА</code> или меняет через <code>- НОМЕР_ЗАКАЗА</code>. Одиночные +/− и новый @username работают только в проверенном контексте одного заказа. При нескольких покупках используйте адресные команды из сообщения бота.\n\n"
        "<b>5. Сбои.</b> Каждый Gift записывается отдельно. Если Telegram не подтвердил итог запроса или часть набора отправлена, заказ попадёт в «🟠 Проверка» без повторной выдачи и без автоматического возврата.\n\n"
        "<b>6. Баланс.</b> Проверка после выдачи и каждую минуту. Лот отключается, если ни один доступный аккаунт не покрывает набор или заданный порог. Пополнение включает только лоты, выключенные AutoGift. При FloodWait используется другой подходящий аккаунт; при таймауте платежа повтор запрещён.\n\n"
        "<b>7. Тест.</b> «Тестовый заказ» проверяет лот, каталог, количество, аккаунт и получателя без списания Stars. Результат виден здесь и в журнале Cardinal.\n\n"
        f"Автор: <a href='{AUTHOR_URL}'>{CREDITS}</a> · <a href='{CHANNEL_URL}'>Канал LuckyFunPay</a>"
    )

def settings_text() -> str:
    return (
        "<b>⚙️ Настройки AutoGift</b>\n\n"
        f"🔑 API: {'✅ задан' if CONFIG.get('api_id') and CONFIG.get('api_hash') else '❌ не задан'}\n"
        f"🌐 Proxy: {'✅ задан' if CONFIG.get('proxy') else '➖ нет'}\n"
        f"↩️ Автовозврат при нулевой выдаче: {'✅' if CONFIG.get('auto_refund_when_no_delivery') else '❌'}\n"
        f"🙈 Скрывать имя: {'✅' if CONFIG.get('hide_name') else '❌'}\n"
        f"💬 Комментарий: {'✅' if CONFIG.get('comment_enabled') else '❌'}\n"
        f"🔔 Напоминание: {'✅' if CONFIG.get('buyer_reminder') else '❌'}\n"
        f"🛡️ Защита лотов: {'✅' if CONFIG.get('lot_protection') else '❌'}\n"
        "🧯 Подтверждение заказа и получателя: обязательно\n"
        f"⭐ Минимум на одном аккаунте: <code>{int(CONFIG.get('low_balance_threshold') or 0)}</code>\n"
        f"🔁 Автовключение: {'✅' if CONFIG.get('auto_reactivate') else '❌'}\n"
        f"💬 Сообщения: {'✅ настроены' if any(CONFIG.get(k) for k in ('payment_text','success_text','refund_text','reminder_text')) else '➖ по умолчанию'}\n"
        f"💱 Цена Stars: <code>{float(CONFIG.get('star_cost_rub') or 0):.2f} ₽</code> · комиссия: <code>{float(CONFIG.get('funpay_fee_percent') or 0):.1f}%</code>"
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("🔑 API Telegram", callback_data="ag:set_api"),
        InlineKeyboardButton("🌐 Proxy", callback_data="ag:set_proxy"),
    )
    kb.row(
        InlineKeyboardButton("💬 Комментарий", callback_data="ag:set_comment"),
        InlineKeyboardButton("🛡 Защита баланса", callback_data="ag:protection"),
    )
    kb.row(
        InlineKeyboardButton("💱 Цена Stars", callback_data="ag:set_star_cost"),
        InlineKeyboardButton("💸 Комиссия FunPay", callback_data="ag:set_fee"),
    )
    kb.row(
        InlineKeyboardButton(f"↩️ Возврат без выдачи {'ВКЛ' if CONFIG.get('auto_refund_when_no_delivery') else 'ВЫКЛ'}", callback_data="ag:toggle_refund"),
        InlineKeyboardButton(f"🙈 Скрывать имя {'ВКЛ' if CONFIG.get('hide_name') else 'ВЫКЛ'}", callback_data="ag:toggle_hide"),
    )
    kb.row(InlineKeyboardButton("👥 Аккаунты Telegram", callback_data="ag:accounts"),
           InlineKeyboardButton("👤 Получатель", callback_data="ag:recipient_settings"))
    kb.row(
        InlineKeyboardButton("💬 Сообщения", callback_data="ag:messages"),
        InlineKeyboardButton("🔄 Проверить всё", callback_data="ag:status"),
    )
    kb.row(InlineKeyboardButton("🏠 Главное меню", callback_data="ag:main"))
    return kb


def protection_panel() -> Tuple[str, InlineKeyboardMarkup]:
    text = ("<b>🛡 Защита баланса</b>\n\n"
            f"Автоотключение лотов: {'🟢 включено' if CONFIG.get('lot_protection') else '🔴 выключено'}\n"
            f"Порог одного аккаунта: {int(CONFIG.get('low_balance_threshold') or 0)} ⭐\n"
            f"Автовключение после пополнения: {'ВКЛ' if CONFIG.get('auto_reactivate') else 'ВЫКЛ'}\n\n"
            "Проверяется стоимость набора на одном аккаунте. Балансы разных аккаунтов не складываются для оплаты.\n"
            "Порог 0 оставляет проверку стоимости. Возвращаются в продажу только лоты, отключённые AutoGift.")
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(InlineKeyboardButton("🛡 Автоотключение", callback_data="ag:toggle_protect"),
           InlineKeyboardButton("⭐ Лимит деактивации", callback_data="ag:set_threshold"))
    kb.row(InlineKeyboardButton("🔁 Автовключение", callback_data="ag:toggle_reactivate"),
           InlineKeyboardButton("🌟 Проверить баланс", callback_data="ag:status"))
    kb.row(InlineKeyboardButton("⬅️ Настройки", callback_data="ag:settings"))
    return text, kb


def recipient_panel() -> Tuple[str, InlineKeyboardMarkup]:
    text = ("<b>👤 Получатель и подтверждение</b>\n\n"
            f"Username из заказа FunPay: {'ВКЛ' if CONFIG.get('username_from_order', True) else 'ВЫКЛ'}\n"
            f"Короткие ответы +/−: {'ВКЛ' if CONFIG.get('quick_confirmation', True) else 'ВЫКЛ'}\n\n"
            "Отправка начинается после подтверждения покупателя.\n"
            "Одиночный знак допустим только при одном оплаченном заказе и проверенном ответе на сообщение AutoGift. "
            "В остальных случаях используются + НОМЕР, − НОМЕР или адресная команда с кодом.")
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(InlineKeyboardButton("👤 Username из заказа", callback_data="ag:toggle_checkout"),
           InlineKeyboardButton("➕ Короткие ответы", callback_data="ag:toggle_quick"))
    kb.row(InlineKeyboardButton("⬅️ Настройки", callback_data="ag:settings"))
    return text, kb


def balance_text() -> str:
    text = "<b>🌟 Балансы Telegram-аккаунтов</b>\n\n"
    for account in ACCOUNTS[:20]:
        value = account.get("balance")
        status = "✅" if account in get_active_accounts() else "⚠️"
        text += f"{status} {h(account.get('label') or account.get('id'))}: {format_stars(value) if value is not None else 'не проверен'} ⭐\n"
        if account.get("last_error"):
            text += f"<i>{h(str(account['last_error'])[:100])}</i>\n"
    maximum = max((int(a["balance"]) for a in get_active_accounts()), default=0)
    return text + f"\nВсего: <b>{format_stars(total_balance())} ⭐</b>\nНа одном доступном аккаунте: <b>{maximum} ⭐</b>"


async def test_lot_order(key: str, username: str, units: int) -> str:
    """Preflight only: no payment form, gift, buyer message or order-journal entry."""
    lot = copy.deepcopy(LOTS.get(key) or {})
    if not _binding_valid(lot) or not lot.get("enabled"):
        raise ValueError("Привязка лота не подтверждена или выключена.")
    if units > int(CONFIG.get("max_order_units", 10)):
        raise ValueError("Количество превышает лимит заказа.")
    plan = _gift_plan({"units": units, "gift_ids": lot["gift_ids"]})
    fields = CARDINAL_INSTANCE.account.get_lot_fields(int(lot["binding"]["lot_id"]))
    if not _live_binding_matches(fields, lot):
        raise ValueError("Описание/тег лота изменились. Повторите привязку.")
    catalog = await refresh_catalog_for_menu(0)
    prices = {int(g["id"]): int(g["price"]) for g in catalog if g.get("available")}
    if any(gid not in prices for gid in plan):
        raise ValueError("Часть подарков недоступна в актуальном каталоге Telegram.")
    required = sum(prices[gid] for gid in plan)
    if required > int(CONFIG.get("max_order_stars", 10000)):
        raise ValueError("Стоимость превышает лимит Stars на заказ.")
    # Refresh without update_lot_protection: the test cannot alter FunPay listings.
    for account in list(ACCOUNTS):
        await refresh_account_balance(account)
    candidates = [a for a in get_active_accounts() if int(a.get("balance") or 0) >= required]
    if not candidates:
        raise ValueError(f"Ни один аккаунт не покрывает {required} Stars для всего заказа.")
    ok, name, recipient_id = await validate_username(username)
    if not ok or not recipient_id:
        raise ValueError("Telegram не подтвердил получателя.")
    if _gift_signature(LOTS.get(key) or {}) != _gift_signature(lot):
        raise ValueError("Состав лота изменился во время теста.")
    logger.info("%s TEST OK lot=%s units=%s gifts=%s stars=%s recipient_id=%s; no payment submitted",
                LOGGER_PREFIX, lot["binding"]["lot_id"], units, len(plan), required, recipient_id)
    return ("<b>✅ Тестовый заказ: проверка пройдена</b>\n\n"
            f"📦 Лот: {h(lot['name'])} · ID {h(lot['binding']['lot_id'])}\n"
            f"🎁 Наборов: {units} · подарков: {len(plan)} · {required} ⭐\n"
            f"👤 @{h(username)} · {h(name)}\n"
            f"👥 Достаточный баланс: {len(candidates)} аккаунт(ов)\n\n"
            f"Выдача сейчас: {'включена' if RUNNING else 'выключена'}\n"
            "Stars не списаны, подарки не отправлены. Реальный заказ дополнительно проходит проверку оплаты FunPay и платёжной формы Telegram.")


def start_test_order(chat_id: int, key: str, username: str, units: int) -> None:
    def worker():
        try:
            text = run_coro(test_lot_order(key, username, units))
        except Exception as exc:
            logger.warning("%s TEST FAILED lot=%s: %s", LOGGER_PREFIX, key, exc)
            text = "<b>❌ Тестовый заказ: проверка не пройдена</b>\n\n" + h(exc) + "\n\nStars не списаны."
        BOT.send_message(chat_id, text, parse_mode="HTML", reply_markup=back_keyboard())
    BOT.send_message(chat_id, "🧪 Проверяю привязку, каталог, баланс и получателя. Результат появится здесь и в журнале.")
    threading.Thread(target=worker, daemon=True, name="AutoGiftTest").start()


def messages_text() -> str:
    def preview(value: Any) -> str:
        s = str(value or "")
        return s[:450] + ("…" if len(s) > 450 else "")

    return (
        "<b>💬 Сообщения покупателю</b>\n\n"
        "<b>После оплаты:</b>\n"
        f"<code>{h(preview(CONFIG.get('payment_text')))}</code>\n\n"
        "<b>После успешной выдачи:</b>\n"
        f"<code>{h(preview(CONFIG.get('success_text')))}</code>\n\n"
        "<b>При возврате:</b>\n"
        f"<code>{h(preview(CONFIG.get('refund_text')))}</code>\n\n"
        "<b>Напоминание:</b>\n"
        f"<code>{h(preview(CONFIG.get('reminder_text')))}</code>\n\n"
        "Доступные переменные: "
        "<code>{username}</code>, <code>{gift_name}</code>, <code>{units}</code>, "
        "<code>{order_id}</code>, <code>{stars}</code>, <code>{gift_count}</code> (всего подарков)."
    )


def messages_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("💳 После оплаты", callback_data="ag:msg:payment"),
        InlineKeyboardButton("🎁 После выдачи", callback_data="ag:msg:success"),
    )
    kb.row(
        InlineKeyboardButton("↩️ После возврата", callback_data="ag:msg:refund"),
        InlineKeyboardButton("🔔 Напоминание", callback_data="ag:msg:reminder"),
    )
    kb.row(InlineKeyboardButton("✨ Готовые шаблоны", callback_data="ag:message_presets"),
           InlineKeyboardButton("↩️ Назад", callback_data="ag:settings"))
    return kb


def _plain_funpay_text(value: Any) -> str:
    """Удаляет HTML/Markdown-подобную разметку перед отправкой в FunPay чат.

    Cardinal/FunPay account.send_message() в данной сборке не интерпретирует
    Telegram-style HTML теги так, как это делает TeleBot, поэтому теги вроде
    <b> и <code> должны быть удалены заранее.
    """
    s = "" if value is None else str(value)
    s = re.sub(r"</?(?:b|strong|i|em|u|s|strike|code|pre|a)(?:\s+[^>]*)?>", "", s, flags=re.I)
    s = s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    return s.strip()


def send_funpay_message(c: Cardinal, chat_id: Any, text: Any) -> bool:
    """Единая отправка чистого текста в FunPay чат."""
    clean_text = _plain_funpay_text(text)
    try:
        target = int(chat_id) if str(chat_id).isdigit() else chat_id
        c.account.send_message(target, clean_text)
        return True
    except Exception as exc:
        logger.error("%s FunPay message send failed to %s: %s", LOGGER_PREFIX, chat_id, exc)
        notify_operators(f"🟠 AutoGift: не удалось отправить сообщение в FunPay-чат {h(chat_id)}. Проверьте заказ.")
        return False


def send_buyer_reminders():
    if not RUNNING or STORAGE_ERROR or not CONFIG.get("buyer_reminder"):
        return
    with ORDER_LOCK:
        order_ids = [oid for oid, row in ORDERS.items() if row.get("status") == "await_username"
                     and not row.get("reminder_sent_at") and 300 <= time.time() - ts(row.get("created_at")) < 86400]
    for oid in order_ids:
        with order_guard(oid):
            row = ORDERS[oid]
            if (not RUNNING or row.get("status") != "await_username" or row.get("reminder_sent_at")
                    or not _active_order_is_still_own(CARDINAL_INSTANCE, row)):
                continue
            # Intent first: notification timeouts must not spam every minute.
            row["reminder_sent_at"] = time.time()
            save_order(row)
            text = format_customer_message(CONFIG.get("reminder_text") or "Напоминание о выдаче подарка.", row)
            send_funpay_message(CARDINAL_INSTANCE, row["chat_id"], text[:1500]
                               + f"\nAutoGift #{oid}: !gift {oid} @username")


def format_customer_message(template: str, data: Dict[str, Any]) -> str:
    values = {
        "username": data.get("username") or "",
        "gift_name": data.get("gift_names", ["🎁 Подарок"])[0] if data.get("gift_names") else "🎁 Подарок",
        "units": data.get("units", 1),
        "order_id": data.get("order_id", ""),
        "stars": data.get("required_stars", 0),
        "gift_count": len(data.get("delivery", [])) or int(data.get("units") or 1) * len(data.get("gift_ids") or []),
        "total_gifts": len(data.get("delivery", [])) or int(data.get("units") or 1) * len(data.get("gift_ids") or []),
    }
    try:
        result = str(template or "").format(**values)
    except Exception:
        result = str(template or "")
    return _plain_funpay_text(result)



def accounts_text() -> str:
    if not ACCOUNTS:
        return "<b>👥 Telegram-аккаунты</b>\n\n❌ Аккаунты ещё не добавлены."
    text = "<b>👥 Telegram-аккаунты</b>\n\n"
    for i, a in enumerate(ACCOUNTS, 1):
        status = "🟢" if a.get("active") else "🔴"
        text += (
            f"{status} <b>{i}. {h(a.get('label') or a.get('id'))}</b>\n"
            f"   📱 {h(masked_phone(a.get('phone','')))}\n"
            f"   🌟 {format_stars(a.get('balance') or 0)}\n"
            f"   🎁 Отправлено: {int(a.get('gifts_sent') or 0)}\n"
            f"   💸 Потрачено: {format_stars(a.get('spent_stars') or 0)}\n\n"
        )
    return text.strip()


def accounts_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(InlineKeyboardButton("📱 По номеру", callback_data="ag:add_phone"),
           InlineKeyboardButton("🔑 Session String", callback_data="ag:add_session"))
    for a in ACCOUNTS:
        kb.add(InlineKeyboardButton(f"🗑 Удалить {a.get('label') or a.get('id')}", callback_data=f"ag:del_account:{a['id']}"))
    kb.row(InlineKeyboardButton("🔄 Обновить балансы", callback_data="ag:status"))
    kb.add(InlineKeyboardButton("🏠 Главное меню", callback_data="ag:main"))
    return kb


def lot_price_label(lot: Dict[str, Any]) -> str:
    prices = {int(g["id"]): int(g["price"]) for g in CATALOG.get("items", []) if g.get("available")}
    ids = lot.get("gift_ids") or []
    if time.time() - ts(CATALOG.get("live_checked_at")) > 300 or any(int(g) not in prices for g in ids):
        return "обновите каталог"
    return str(sum(prices[int(g)] for g in ids)) + " ⭐"


def lots_text(page: int = 0) -> str:
    if not LOTS:
        return "<b>📦 Лоты</b>\n\n❌ Лоты ещё не привязаны."
    text = "<b>📦 Лоты</b>\n\n"
    page = min(max(page, 0), (len(LOTS) - 1) // 8)
    for key, lot in list(LOTS.items())[page * 8:(page + 1) * 8]:
        ids = ", ".join(str(x) for x in lot.get("gift_ids", []))
        text += (
            f"{'🟢' if lot.get('enabled', True) else '🔴'} <b>{h(lot.get('name') or key)}</b>\n"
            f"   🆔 FunPay: <code>{h(lot.get('funpay_lot_id') or lot.get('lot_id') or '?')}</code>\n"
            f"   🔐 Привязка: {'подтверждена' if _binding_valid(lot) else 'требует проверки продавцом'}\n"
            f"   🎁 Gift ID: <code>{h(ids or '?')}</code>\n"
            f"   📦 В наборе: <b>{len(lot.get('gift_ids', []))}</b>\n\n"
        )
    return text.strip() + f"\n\nСтраница {page + 1}/{max(1, (len(LOTS) + 7) // 8)}"


def lots_keyboard(page: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("➕ Добавить лот", callback_data="ag:add_lot"))
    page = min(max(page, 0), max(0, (len(LOTS) - 1) // 8))
    for key, lot in list(LOTS.items())[page * 8:(page + 1) * 8]:
        kb.add(InlineKeyboardButton(f"📦 {str(lot.get('name') or key)[:29]} · {len(lot.get('gift_ids', []))} 🎁 · {lot_price_label(lot)}", callback_data=f"ag:edit_lot:{key}"))
    nav = []
    if page:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"ag:lots_page:{page - 1}"))
    if (page + 1) * 8 < len(LOTS):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"ag:lots_page:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.add(InlineKeyboardButton("🏠 Главное меню", callback_data="ag:main"))
    return kb


async def refresh_catalog_for_menu(user_id: int) -> List[Dict[str, Any]]:
    """The catalog is deliberately limited to the 11 configured standard Gifts."""
    base = [dict(item, new=False, available=False, sold_out=True, limited=False, birthday=False, require_premium=False)
            for item in STANDARD_GIFTS_CATALOG]
    found = False
    for account in get_telegram_accounts():
        try:
            live = await fetch_gifts_for_account(account)
            live_map = {int(item["id"]): item for item in live if int(item.get("id", -1)) in STANDARD_GIFT_IDS}
            for item in base:
                actual = live_map.get(int(item["id"]))
                if actual:
                    item["available"] = True
                    item["sold_out"] = False
                    item["price"] = int(actual.get("price") or item["price"])
                    item["emoji"] = str(actual.get("emoji") or item["emoji"])
                    item["name"] = str(actual.get("name") or item["name"])
            found = True
            break
        except Exception as exc:
            logger.warning("%s catalog refresh via %s failed: %s", LOGGER_PREFIX, account.get("id"), exc)
    if not found:
        raise RuntimeError("Не удалось получить актуальный каталог Telegram. Сохранённые цены не подтверждены.")
    base.sort(key=lambda item: int(item["order"]))
    CATALOG["items"] = base
    CATALOG["updated_at"] = time.time()
    CATALOG["live_checked_at"] = time.time()
    _safe_json_save(CATALOG_PATH, CATALOG)
    CATALOG_CACHE[user_id] = base
    return base

def catalog_text(items: List[Dict[str, Any]], page: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
    allowed = {int(item["id"]): item for item in STANDARD_GIFTS_CATALOG}
    clean = []
    for item in items or []:
        try:
            gift_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if gift_id not in allowed:
            continue
        row = dict(allowed[gift_id])
        row.update({key: value for key, value in item.items() if value is not None})
        clean.append(row)
    if not clean:
        clean = [dict(item, available=False) for item in STANDARD_GIFTS_CATALOG]
    clean.sort(key=lambda item: int(allowed[int(item["id"])]["order"]))
    per_page = 8
    total_pages = 2
    page = max(0, min(int(page), total_pages - 1))
    chunk = clean[page * per_page:(page + 1) * per_page]
    text = (
        "<b>🎁 Каталог стандартных Telegram Gifts</b>\n\n"
        "Показаны только 11 обычных подарков.\n"
        + ("⚠️ Цены устарели — обновите каталог.\n" if time.time() - ts(CATALOG.get("live_checked_at")) > 300 else "") +
        "Нажмите на подарок, чтобы скопировать ID для лота.\n\n"
        f"Страница: <b>{page + 1}/{total_pages}</b>"
    )
    kb = InlineKeyboardMarkup(row_width=1)
    for item in chunk:
        kb.add(InlineKeyboardButton(
            f"{item.get('emoji') or '🎁'} {item.get('name') or 'Подарок'} · "
            + (f"{int(item.get('price') or 0)}⭐" if item.get('available') else "недоступен"),
            callback_data=f"ag:gift:{item['id']}",
        ))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"ag:cat:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"ag:cat:{page + 1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton("🔄 Обновить цены", callback_data="ag:catalog_refresh"),
           InlineKeyboardButton("⬅️ Назад", callback_data="ag:main"))
    return text, kb

def _telegram_message_filter(message):
    if (getattr(message.chat, "type", None) != "private"
            or not is_operator(int(message.from_user.id))):
        return False
    text = (message.text or "").strip().lower()
    if text in {"/autogift", "/auto_gifts", "/autogifts", "/start_gifts", "/stop_gifts", "/autogift_help", "/autogift_install"}:
        return True
    if text.startswith("/") and text != "/cancel":
        return False
    try:
        state = CARDINAL_INSTANCE.telegram.get_state(message.chat.id, message.from_user.id)
        return isinstance(state, dict) and str(state.get("state", "")).startswith("autogift_")
    except Exception:
        return False


def start_handler(message: Message):
    if message.chat.type != "private" or not is_operator(int(message.from_user.id)):
        deny_operator(message.chat.id)
        return
    clear_state(message.chat.id, message.from_user.id)
    send_main(message.chat.id)


def state_send(chat_id: int, user_id: int, text: str, state: str) -> None:
    try:
        msg = BOT.send_message(chat_id, text, parse_mode="HTML")
        tg = CARDINAL_INSTANCE.telegram
        tg.set_state(chat_id, msg.message_id, user_id, state)
    except Exception as exc:
        logger.error("%s state_send failed: %s", LOGGER_PREFIX, exc)


def set_state(chat_id: int, user_id: int, state: str) -> None:
    try:
        CARDINAL_INSTANCE.telegram.set_state(chat_id, 0, user_id, state)
    except Exception:
        pass


def clear_state(chat_id: int, user_id: int) -> None:
    try:
        CARDINAL_INSTANCE.telegram.clear_state(chat_id, user_id, True)
    except Exception:
        pass


def message_dispatch(message: Message):
    user_id = int(message.from_user.id)
    chat_id = int(message.chat.id)
    text = (message.text or "").strip()
    if not is_operator(user_id) or message.chat.type != "private":
        return

    # Commands may already have been handled by the PRE_INIT middleware.
    # Do not render the menu twice.
    msg_key = (chat_id, int(getattr(message, "message_id", getattr(message, "id", 0)) or 0))
    if msg_key in TELEGRAM_COMMAND_SEEN:
        TELEGRAM_COMMAND_SEEN.discard(msg_key)
        return

    if _handle_telegram_command(message):
        return
    if not INITIALIZED or not DEPENDENCY_READY or STARTUP_ERROR or STORAGE_ERROR:
        send_main(chat_id)
        return

    if text == "/cancel":
        if user_id in AUTH_THREADS and AUTH_THREADS[user_id].is_alive():
            _auth_input_put(user_id, "/cancel")
        clear_state(chat_id, user_id)
        TG_WIZARD.pop(user_id, None)
        LOT_WIZARDS.pop(user_id, None)
        BOT.send_message(chat_id, "❌ Операция отменена.")
        return

    try:
        sd = CARDINAL_INSTANCE.telegram.get_state(chat_id, user_id)
    except Exception:
        sd = None
    state = sd.get("state", "") if isinstance(sd, dict) else ""
    if not state.startswith("autogift_"):
        return

    if state in {"autogift_api_hash", "autogift_auth_code", "autogift_auth_password", "autogift_session", "autogift_proxy"}:
        try:
            BOT.delete_message(chat_id, message.message_id)
        except Exception:
            pass

    if state == "autogift_api_id":
        clear_state(chat_id, user_id)
        try:
            CONFIG["api_id"] = int(text)
            _safe_json_save(CONFIG_PATH, CONFIG)
            state_send(chat_id, user_id, "✅ API ID сохранён.\n\nВведите API Hash:", "autogift_api_hash")
        except ValueError:
            state_send(chat_id, user_id, "❌ API ID должен состоять только из цифр.", state)
        return

    if state == "autogift_api_hash":
        clear_state(chat_id, user_id)
        if not text:
            state_send(chat_id, user_id, "❌ API Hash не может быть пустым.", state)
            return
        CONFIG["api_hash"] = text
        _safe_json_save(CONFIG_PATH, CONFIG)
        BOT.send_message(chat_id, "✅ API ID/Hash сохранены.", reply_markup=back_keyboard())
        return

    if state == "autogift_proxy":
        text = "" if text == "-" else text
        clear_state(chat_id, user_id)
        try:
            parse_proxy(text)
            CONFIG["proxy"] = text
            _safe_json_save(CONFIG_PATH, CONFIG)
            BOT.send_message(chat_id, "✅ Proxy сохранён.", reply_markup=back_keyboard("ag:settings"))
        except Exception as exc:
            state_send(chat_id, user_id, f"❌ {h(exc)}\n\nПовторите или /cancel.", state)
        return

    if state == "autogift_comment":
        text = "" if text == "-" else text
        clear_state(chat_id, user_id)
        CONFIG["comment"] = text[:128]
        CONFIG["comment_enabled"] = bool(text)
        _safe_json_save(CONFIG_PATH, CONFIG)
        BOT.send_message(chat_id, "✅ Комментарий сохранён.", reply_markup=back_keyboard("ag:settings"))
        return

    if state == "autogift_test_username":
        username = normalized_username(text)
        if not username or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username):
            state_send(chat_id, user_id, "Введите Telegram @username для проверки.", state)
            return
        TG_WIZARD.setdefault(user_id, {})["test_username"] = username
        state_send(chat_id, user_id, "📦 Сколько единиц проверить? Например: 2. Набор будет умножен на это количество.", "autogift_test_units")
        return

    if state == "autogift_test_units":
        try:
            units = _positive_int(text)
            wiz = TG_WIZARD[user_id]
            _gift_plan({"units": units, "gift_ids": LOTS[wiz["test_lot"]]["gift_ids"]})
        except (ValueError, KeyError):
            state_send(chat_id, user_id, "Укажите допустимое положительное количество. Если лот удалён — откройте тест заново.", state)
            return
        clear_state(chat_id, user_id)
        TG_WIZARD.pop(user_id, None)
        start_test_order(chat_id, wiz["test_lot"], wiz["test_username"], units)
        return

    if state == "autogift_threshold":
        clear_state(chat_id, user_id)
        try:
            CONFIG["low_balance_threshold"] = max(0, int(text))
            _safe_json_save(CONFIG_PATH, CONFIG)
            BOT.send_message(chat_id, "✅ Порог баланса сохранён.", reply_markup=back_keyboard("ag:settings"))
        except ValueError:
            state_send(chat_id, user_id, "❌ Укажите целое число Stars.", state)
        return

    if state in ("autogift_star_cost", "autogift_fee"):
        clear_state(chat_id, user_id)
        try:
            value = float(text.replace(",", "."))
            if not math.isfinite(value) or value < 0:
                raise ValueError
            key = "star_cost_rub" if state == "autogift_star_cost" else "funpay_fee_percent"
            if key == "funpay_fee_percent" and value > 100:
                raise ValueError
            CONFIG[key] = value
            _safe_json_save(CONFIG_PATH, CONFIG)
            BOT.send_message(chat_id, "✅ Настройка сохранена.", reply_markup=back_keyboard("ag:settings"))
        except ValueError:
            state_send(chat_id, user_id, "❌ Укажите число от 0 до 100.", state)
        return

    if state.startswith("autogift_message:"):
        kind = state.split(":", 1)[1]
        clear_state(chat_id, user_id)
        key_map = {
            "payment": "payment_text",
            "success": "success_text",
            "refund": "refund_text",
            "reminder": "reminder_text",
        }
        key = key_map.get(kind)
        if not key:
            return
        CONFIG[key] = text[:3500]
        _safe_json_save(CONFIG_PATH, CONFIG)
        BOT.send_message(chat_id, "✅ Шаблон сообщения сохранён.", reply_markup=back_keyboard("ag:messages"))
        return

    if state == "autogift_auth_phone":
        clear_state(chat_id, user_id)
        phone = re.sub(r"[^\d+]", "", text)
        if len(phone) < 7:
            state_send(chat_id, user_id, "❌ Неверный номер телефона. Пример: +79991234567", state)
            return
        if user_id in AUTH_THREADS and AUTH_THREADS[user_id].is_alive():
            BOT.send_message(chat_id, "⏳ Авторизация уже выполняется.")
            return
        AUTH_INPUT_QUEUES[user_id] = pyqueue.Queue()
        thread = threading.Thread(target=auth_worker, args=(user_id, phone), daemon=True)
        AUTH_THREADS[user_id] = thread
        thread.start()
        BOT.send_message(chat_id, "📨 Код отправлен в Telegram.\nВведите код подтверждения. Можно написать с пробелами.")
        set_state(chat_id, user_id, "autogift_auth_code")
        return

    if state in ("autogift_auth_code", "autogift_auth_password"):
        _auth_input_put(user_id, message.text if state == "autogift_auth_password" else text)
        clear_state(chat_id, user_id)
        return

    if state == "autogift_session":
        clear_state(chat_id, user_id)
        session_string = text.strip()
        if len(session_string) < 50:
            state_send(chat_id, user_id, "❌ Session String выглядит слишком короткой.", state)
            return
        account_id = f"session_{int(time.time())}_{user_id}"
        ACCOUNTS.append({
            **DEFAULT_ACCOUNT,
            "id": account_id,
            "label": account_id,
            "phone": "",
            "session_string": session_string,
            "active": True,
        })
        _safe_json_save(ACCOUNTS_PATH, ACCOUNTS)
        BOT.send_message(chat_id, "✅ Session String добавлен.", reply_markup=back_keyboard("ag:accounts"))
        return

    if state == "autogift_lot_funpay":
        clear_state(chat_id, user_id)
        try:
            lot_id = int(text)
        except ValueError:
            state_send(chat_id, user_id, "❌ FunPay lot ID должен быть числом.", state)
            return
        try:
            lf = CARDINAL_INSTANCE.account.get_lot_fields(lot_id)
            name = (lf.fields.get("fields[summary][ru]") or lf.fields.get("fields[name][ru]") or f"Лот {lot_id}")
        except Exception as exc:
            state_send(chat_id, user_id, f"❌ Не удалось получить лот:\n<code>{h(exc)}</code>", state)
            return
        LOT_WIZARDS[user_id] = {"funpay_lot_id": lot_id, "name": name, "gift_ids": [], "gift_names": []}
        state_send(
            chat_id,
            user_id,
            "🎁 <b>Gift ID</b>\n\nОтправьте один ID или несколько ID через новую строку.\n"
            "Для набора все указанные Gifts будут отправлены за одну единицу заказа.",
            "autogift_lot_gifts",
        )
        return

    if state == "autogift_lot_gifts":
        clear_state(chat_id, user_id)
        wiz = LOT_WIZARDS.get(user_id, {})
        parts = text.split()
        ids = [int(x) for x in parts] if parts and all(x.isascii() and x.isdigit() for x in parts) else []
        if not ids or len(ids) > 100 or any(gid not in STANDARD_GIFT_IDS for gid in ids):
            state_send(chat_id, user_id, "❌ Не найден ни один Gift ID.", state)
            return
        wiz["gift_ids"] = ids
        # Try to name gifts from catalog
        cmap = {int(x["id"]): x for x in CATALOG.get("items", []) if str(x.get("id")).isdigit()}
        wiz["gift_names"] = [str(cmap.get(x, {}).get("name") or "🎁 Подарок") for x in ids]
        LOT_WIZARDS[user_id] = wiz
        # Price always comes from Telegram; no manual cost is required to add a lot.
        state, text = "autogift_lot_cost", "0"

    if state == "autogift_lot_cost":
        clear_state(chat_id, user_id)
        wiz = LOT_WIZARDS.get(user_id, {})
        try:
            cost = max(0, int(text))
        except ValueError:
            state_send(chat_id, user_id, "❌ Укажите целое число Stars.", state)
            return
        wiz["gift_cost"] = cost
        key = "lot_" + secrets.token_hex(6)
        LOTS[key] = {
            "name": wiz["name"],
            "funpay_lot_id": wiz["funpay_lot_id"],
            "lot_id": wiz["funpay_lot_id"],
            "gift_ids": wiz["gift_ids"],
            "gift_names": wiz["gift_names"],
            "gift_id": wiz["gift_ids"][0],
            "gift_name": wiz["gift_names"][0] if wiz["gift_names"] else "🎁 Подарок",
            "gift_cost": cost,
            "enabled": False,
        }
        _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
        LOT_WIZARDS.pop(user_id, None)
        tag = prepare_manual_tag(key)
        kb = InlineKeyboardMarkup(row_width=2)
        kb.row(InlineKeyboardButton("✅ Проверить тег", callback_data=f"ag:verify_tag:{key}"),
               InlineKeyboardButton("📦 Открыть лот", callback_data=f"ag:edit_lot:{key}"))
        BOT.send_message(chat_id, f"✅ Лот добавлен.\n\nСкопируйте тег в конец подробного описания FunPay, сохраните лот и нажмите «Проверить тег»:\n\n<code>{tag}</code>\n\nЕсли заполнены русский и английский тексты — добавьте тег в оба. Пока тег не проверен, выдача выключена.", parse_mode="HTML", reply_markup=kb)
        return

    if state == 'autogift_binding_confirm':
        preview = TG_WIZARD.get(user_id, {}).get('binding_preview')
        if not preview or preview.get('chat_id') != chat_id:
            return
        expected = f"ПРИВЯЗАТЬ {preview['lot_id']} {preview['nonce']}"
        if text != expected:
            state_send(chat_id, user_id, 'Подтверждение не совпало. Вставьте строку из предпросмотра или /cancel.', state)
            return
        try:
            _confirm_binding(CARDINAL_INSTANCE, preview, user_id)
        except Exception as exc:
            BOT.send_message(chat_id, f'❌ Привязка не включена: {h(exc)}', parse_mode='HTML')
            return
        clear_state(chat_id, user_id)
        TG_WIZARD.pop(user_id, None)
        BOT.send_message(chat_id, '✅ Привязка подтверждена и проверена чтением с FunPay. Только новые покупки с меткой AutoGift смогут запускать выдачу.', reply_markup=back_keyboard('ag:lots'))
        return

    if state == "autogift_lot_rename":
        clear_state(chat_id, user_id)
        key = TG_WIZARD.get(user_id, {}).get("lot_key")
        if key in LOTS:
            LOTS[key]["name"] = text[:120]
            _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
            BOT.send_message(chat_id, "✅ Название изменено.", reply_markup=back_keyboard("ag:lots"))
        return


def auth_watcher_worker() -> None:
    while True:
        time.sleep(1)
        for user_id, q in list(AUTH_OUTPUT_QUEUES.items()):
            try:
                while True:
                    item = q.get_nowait()
                    if not isinstance(item, dict):
                        continue
                    chat_id = user_id
                    kind = item.get("kind")
                    if kind == "code":
                        # Code was just requested by worker. UI already asks for code.
                        set_state(chat_id, chat_id, "autogift_auth_code")
                    elif kind == "password":
                        msg = BOT.send_message(chat_id, "🔐 У аккаунта включён 2FA. Введите облачный пароль:")
                        try:
                            CARDINAL_INSTANCE.telegram.set_state(chat_id, msg.message_id, user_id, "autogift_auth_password")
                        except Exception:
                            pass
                    elif kind == "done":
                        BOT.send_message(chat_id, f"✅ Аккаунт <b>{h(item.get('name'))}</b> успешно добавлен.", parse_mode="HTML", reply_markup=back_keyboard("ag:accounts"))
                        clear_state(chat_id, chat_id)
                    elif kind == "error":
                        # Telegram Bot API ограничивает длину текстовых сообщений.
                        # Не отправляем сюда полный stack/error от MTProto.
                        raw_error = str(item.get("error") or "Неизвестная ошибка")
                        short_error = raw_error.strip()[:1200]
                        if len(raw_error.strip()) > 1200:
                            short_error += "…"
                        BOT.send_message(
                            chat_id,
                            f"❌ Авторизация не удалась:\n<code>{h(short_error)}</code>",
                            parse_mode="HTML",
                            reply_markup=back_keyboard("ag:accounts"),
                        )
                        clear_state(chat_id, chat_id)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# CALLBACK ROUTER
# ---------------------------------------------------------------------------

def cb(call: types.CallbackQuery):
    global RUNNING
    if call.message is None:
        return
    data = call.data or ""
    chat_id = int(call.message.chat.id)
    user_id = int(call.from_user.id)
    if not is_operator(user_id) or call.message.chat.type != "private":
        deny_operator(chat_id, call.id)
        return
    try:
        BOT.answer_callback_query(call.id)
    except Exception:
        pass

    if data == "ag:main" or data == "ag:refresh":
        send_main(chat_id, call.message.message_id)
        return

    if data == "ag:install_dependency":
        start_dependency_install(chat_id, user_id)
        return
    if not INITIALIZED or not DEPENDENCY_READY or STARTUP_ERROR or STORAGE_ERROR:
        send_main(chat_id, call.message.message_id)
        return

    if data == "ag:toggle_running":
        RUNNING = not RUNNING
        CONFIG["running"] = RUNNING
        _safe_json_save(CONFIG_PATH, CONFIG)
        send_main(chat_id, call.message.message_id)
        return

    if data == "ag:protection":
        text, kb = protection_panel()
        BOT.edit_message_text(text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        return

    if data == "ag:recipient_settings":
        text, kb = recipient_panel()
        BOT.edit_message_text(text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        return

    if data == "ag:test":
        kb = InlineKeyboardMarkup(row_width=1)
        for key, lot in list(LOTS.items())[:30]:
            if _binding_valid(lot):
                kb.add(InlineKeyboardButton(str(lot.get("name") or key)[:50], callback_data=f"ag:testlot:{key}"))
        kb.row(InlineKeyboardButton("⬅️ Главное меню", callback_data="ag:main"))
        BOT.edit_message_text("<b>🧪 Тестовый заказ</b>\n\nВыберите подтверждённый лот. Проверим количество, каталог, баланс и Telegram-получателя. Подарок не отправляется, Stars не списываются.",
                              chat_id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("ag:testlot:"):
        key = data.split(":", 2)[2]
        if key not in LOTS:
            return
        TG_WIZARD[user_id] = {"test_lot": key}
        state_send(chat_id, user_id, "👤 Введите @username для тестового заказа. Получатель будет проверен без отправки подарка.", "autogift_test_username")
        return

    if data.startswith("ag:stats:"):
        raw = data.rsplit(":", 1)[1]
        days = None if raw == "all" else int(raw)
        BOT.edit_message_text(stats_text(days), chat_id, call.message.message_id, parse_mode="HTML", reply_markup=stats_keyboard())
        return

    if data == "ag:review":
        rows = sorted((x for x in ORDERS.values() if x.get("status") == "needs_review"), key=lambda x: ts(x.get("created_at")), reverse=True)
        if not rows:
            text = "<b>🟠 Заказы для ручной проверки</b>\n\n✅ Таких заказов нет."
        else:
            text = "<b>🟠 Заказы для ручной проверки</b>\n\n"
            for row in rows[:15]:
                text += f"#{h(row.get('order_id'))} · @{h(row.get('username') or '—')} · отправлено {delivered_count(row)}/{len(row.get('delivery', []))}\n<code>{h(str(row.get('last_error') or 'Неизвестный результат')[:170])}</code>\n\n"
        BOT.edit_message_text(text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=back_keyboard("ag:stats:all"))
        return

    if data == "ag:export":
        payload = io.BytesIO(build_orders_csv())
        payload.name = "autogift_orders.csv"
        BOT.send_document(chat_id, payload, caption="📄 Экспорт заказов AutoGift")
        return

    if data == "ag:help":
        BOT.edit_message_text(help_text(), chat_id, call.message.message_id, parse_mode="HTML", reply_markup=back_keyboard())
        return

    if data == "ag:settings":
        BOT.edit_message_text(settings_text(), chat_id, call.message.message_id, parse_mode="HTML", reply_markup=settings_keyboard())
        return

    if data == "ag:set_api":
        BOT.edit_message_text("🔑 <b>API Telegram</b>\n\nВведите API ID:", chat_id, call.message.message_id, parse_mode="HTML")
        set_state(chat_id, user_id, "autogift_api_id")
        return

    if data == "ag:set_proxy":
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton(f"Обязательный: {'ВКЛ' if CONFIG.get('proxy_required') else 'ВЫКЛ'}", callback_data="ag:toggle_proxy_required"))
        BOT.edit_message_text("🌐 <b>Proxy для Telegram</b>\n\nФормат: <code>socks5://login:pass@ip:port</code> или <code>ip:port</code>.\n\nВведите -, чтобы отключить. Можно включить требование прокси кнопкой:", chat_id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        set_state(chat_id, user_id, "autogift_proxy")
        return

    if data == "ag:set_comment":
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton(f"Текст подарка: {'ВКЛ' if CONFIG.get('comment_enabled') else 'ВЫКЛ'}", callback_data="ag:toggle_comment"))
        BOT.edit_message_text(f"<b>💬 Текст к подарку</b>\n\nСохранённый текст: <code>{h(CONFIG.get('comment') or 'нет')}</code>\n\nОтправьте новый текст (до 128 символов). Знак - очистит его. Выключение кнопкой сохраняет шаблон.", chat_id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        set_state(chat_id, user_id, "autogift_comment")
        return

    if data == "ag:set_threshold":
        BOT.edit_message_text("⭐ Введите минимальный баланс одного аккаунта. Если ни один аккаунт не достигает порога, лоты выключаются. 0 — проверять только стоимость набора:", chat_id, call.message.message_id)
        set_state(chat_id, user_id, "autogift_threshold")
        return

    if data == "ag:set_star_cost":
        BOT.edit_message_text("💱 Укажите закупочную цену одной Stars в рублях. Пример: <code>1.45</code>. Ноль отключит расчёт прибыли.", chat_id, call.message.message_id, parse_mode="HTML")
        set_state(chat_id, user_id, "autogift_star_cost")
        return

    if data == "ag:set_fee":
        BOT.edit_message_text("💸 Укажите комиссию FunPay в процентах. Пример: <code>10</code>.", chat_id, call.message.message_id, parse_mode="HTML")
        set_state(chat_id, user_id, "autogift_fee")
        return

    if data == "ag:messages":
        BOT.edit_message_text(messages_text(), chat_id, call.message.message_id, parse_mode="HTML", reply_markup=messages_keyboard())
        return

    if data == "ag:message_presets":
        kb = InlineKeyboardMarkup(row_width=2)
        kb.row(InlineKeyboardButton("✅ Применить", callback_data="ag:apply_message_presets"),
               InlineKeyboardButton("⬅️ Назад", callback_data="ag:messages"))
        preview = ("<b>✨ Готовые сообщения</b>\n\nПосле оплаты:\n<code>" + h(DEFAULT_CONFIG["payment_text"])
                   + "</code>\n\nПосле выдачи:\n<code>" + h(DEFAULT_CONFIG["success_text"])
                   + "</code>\n\nКнопка заменит эти два шаблона. Тексты возврата и напоминания настраиваются отдельно.")
        BOT.edit_message_text(preview, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        return

    if data == "ag:apply_message_presets":
        for key in ("payment_text", "success_text"):
            CONFIG[key] = DEFAULT_CONFIG[key]
        _safe_json_save(CONFIG_PATH, CONFIG)
        BOT.edit_message_text(messages_text(), chat_id, call.message.message_id, parse_mode="HTML", reply_markup=messages_keyboard())
        return

    if data.startswith("ag:msg:"):
        kind = data.split(":", 2)[2]
        label_map = {
            "payment": "💳 сообщение после оплаты",
            "success": "🎁 сообщение после успешной выдачи подарка",
            "refund": "↩️ сообщение при возврате",
            "reminder": "🔔 напоминание покупателю",
        }
        key_map = {
            "payment": "payment_text",
            "success": "success_text",
            "refund": "refund_text",
            "reminder": "reminder_text",
        }
        label = label_map.get(kind)
        key = key_map.get(kind)
        if not label or not key:
            BOT.answer_callback_query(call.id, "Неизвестный тип сообщения", show_alert=True)
            return
        current = CONFIG.get(key) or ""
        msg = BOT.edit_message_text(
            f"<b>{label}</b>\n\n"
            f"Текущий текст:\n<code>{h(current[:1200])}</code>\n\n"
            "Введите новый текст. Можно использовать переменные:\n"
            "<code>{username}</code> <code>{gift_name}</code> <code>{units}</code> "
            "<code>{order_id}</code> <code>{stars}</code>",
            chat_id,
            call.message.message_id,
            parse_mode="HTML",
        )
        set_state(chat_id, user_id, f"autogift_message:{kind}")
        return

    toggles = {
        "ag:toggle_comment": ("comment_enabled", "💬 Текст подарка"),
        "ag:toggle_proxy_required": ("proxy_required", "🌐 Обязательный прокси"),
        "ag:toggle_reactivate": ("auto_reactivate", "🔁 Автовключение"),
        "ag:toggle_checkout": ("username_from_order", "👤 Username из заказа"),
        "ag:toggle_quick": ("quick_confirmation", "➕ Короткие ответы"),
        "ag:toggle_refund": ("auto_refund_when_no_delivery", "↩️ Автовозврат при нулевой выдаче"),
        "ag:toggle_hide": ("hide_name", "🙈 Скрывать имя"),
        "ag:toggle_reminder": ("buyer_reminder", "🔔 Напоминание"),
        "ag:toggle_protect": ("lot_protection", "🛡️ Защита лотов"),
    }
    if data in toggles:
        key, label = toggles[data]
        CONFIG[key] = not bool(CONFIG.get(key))
        _safe_json_save(CONFIG_PATH, CONFIG)
        clear_state(chat_id, user_id)
        if data in {"ag:toggle_protect", "ag:toggle_reactivate"}:
            text, kb = protection_panel()
        elif data in {"ag:toggle_checkout", "ag:toggle_quick"}:
            text, kb = recipient_panel()
        else:
            text, kb = settings_text(), settings_keyboard()
        BOT.edit_message_text(text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        return

    if data == "ag:accounts":
        text = accounts_text()
        BOT.edit_message_text(text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=accounts_keyboard())
        return

    if data == "ag:add_phone":
        BOT.edit_message_text("📱 Введите номер Telegram-аккаунта в международном формате, например <code>+79991234567</code>.", chat_id, call.message.message_id, parse_mode="HTML")
        set_state(chat_id, user_id, "autogift_auth_phone")
        return

    if data == "ag:add_session":
        BOT.edit_message_text("🔑 Отправьте Session String Pyrogram от Telegram-аккаунта.", chat_id, call.message.message_id, parse_mode="HTML")
        set_state(chat_id, user_id, "autogift_session")
        return

    if data.startswith("ag:del_account:"):
        aid = data.split(":", 2)[2]
        global ACCOUNTS
        if aid in ACCOUNT_RESERVATIONS:
            BOT.send_message(chat_id, "Аккаунт выполняет заказ. Удаление доступно после окончания выдачи.")
            return
        ACCOUNTS = [a for a in ACCOUNTS if a.get("id") != aid]
        _safe_json_save(ACCOUNTS_PATH, ACCOUNTS)
        BOT.edit_message_text(accounts_text(), chat_id, call.message.message_id, parse_mode="HTML", reply_markup=accounts_keyboard())
        return

    if data == "ag:lots" or data.startswith("ag:lots_page:"):
        page = int(data.rsplit(":", 1)[1]) if data.startswith("ag:lots_page:") else 0
        BOT.edit_message_text(lots_text(page), chat_id, call.message.message_id, parse_mode="HTML", reply_markup=lots_keyboard(page))
        return

    if data.startswith("ag:tag:"):
        key = data.split(":", 2)[2]
        if key not in LOTS:
            return
        tag = prepare_manual_tag(key)
        kb = InlineKeyboardMarkup(row_width=2)
        kb.row(InlineKeyboardButton("✅ Проверить тег", callback_data=f"ag:verify_tag:{key}"),
               InlineKeyboardButton("⬅️ Лот", callback_data=f"ag:edit_lot:{key}"))
        BOT.edit_message_text(f"<b>🔖 Тег привязки</b>\n\n<code>{tag}</code>\n\nДобавьте в конец подробного описания этого лота, сохраните изменения на FunPay и нажмите «Проверить тег». Для двух языков добавьте в оба текста. Не копируйте тег в другие лоты.", chat_id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("ag:verify_tag:"):
        key = data.split(":", 2)[2]
        try:
            confirm_manual_tag(CARDINAL_INSTANCE, key, user_id)
            BOT.send_message(chat_id, "✅ Тег и лот проверены. Привязка включена.", reply_markup=back_keyboard("ag:lots"))
        except Exception as exc:
            BOT.send_message(chat_id, "❌ " + str(exc), reply_markup=back_keyboard("ag:lots"))
        return

    if data == "ag:add_lot":
        BOT.edit_message_text("📦 Введите ID лота FunPay, который нужно привязать:", chat_id, call.message.message_id)
        set_state(chat_id, user_id, "autogift_lot_funpay")
        return

    if data.startswith('ag:bind:'):
        key = data.split(':', 2)[2]
        try:
            preview = _prepare_binding(CARDINAL_INSTANCE, key)
        except Exception as exc:
            BOT.send_message(chat_id, f'❌ Не удалось подготовить привязку: {h(exc)}', parse_mode='HTML')
            return
        preview['chat_id'] = chat_id
        TG_WIZARD[user_id] = {'binding_preview': preview}
        v = next(iter(preview['snapshot']['variants'].values()))
        text = (f"🔐 <b>Подтверждение AutoGift</b>\nFunPay ID: {preview['lot_id']}\n"
                f"Раздел: {h(preview['snapshot']['subcategory_id'])}\n"
                f"Название: {h(v['title'])}\nОписание: {h(v['description'][:1800])}\n"
                f"Gift ID: {h(LOTS[key].get('gift_ids'))}\n\n"
                "Проверьте: это продажа выбранных Telegram Gifts, а НЕ товар другого плагина. "
                "Подтверждение добавит служебную метку в описание лота. Не копируйте её в другие лоты.\n\n"
                f"Для привязки напишите: <code>ПРИВЯЗАТЬ {preview['lot_id']} {preview['nonce']}</code>\n"
                "Отмена: /cancel")
        state_send(chat_id, user_id, text, 'autogift_binding_confirm')
        return

    if data.startswith("ag:edit_lot:"):
        key = data.split(":", 2)[2]
        lot = LOTS.get(key)
        if not lot:
            BOT.answer_callback_query(call.id, "Лот не найден", show_alert=True)
            return
        txt = (
            f"<b>📦 {h(key)}</b>\n\n"
            f"Название: <code>{h(lot.get('name'))}</code>\n"
            f"FunPay ID: <code>{h(lot.get('funpay_lot_id') or lot.get('lot_id'))}</code>\n"
            f"Gift ID: <code>{h(', '.join(map(str, lot.get('gift_ids', []))))}</code>\n"
            f"Стоимость набора: <b>{h(lot_price_label(lot))}</b>\n"
        )
        if _binding_valid(lot):
            txt += f"\nТег: <code>[AutoGift:{lot['binding']['token']}]</code>\n"
        txt += f"Привязка: {'✅ подтверждена' if _binding_valid(lot) else '⚠️ нужна проверка'}\n"
        kb = InlineKeyboardMarkup(row_width=2)
        kb.row(InlineKeyboardButton("🔖 Получить тег", callback_data=f"ag:tag:{key}"),
               InlineKeyboardButton("✅ Проверить тег", callback_data=f"ag:verify_tag:{key}"))
        kb.row(InlineKeyboardButton('🔐 Добавить автоматически', callback_data=f'ag:bind:{key}'),
               InlineKeyboardButton("🧪 Тест", callback_data=f"ag:testlot:{key}"))
        kb.row(InlineKeyboardButton("✏️ Название", callback_data=f"ag:rename_lot:{key}"),
               InlineKeyboardButton("🟢/🔴 Выдача", callback_data=f"ag:toggle_lot:{key}"))
        kb.row(InlineKeyboardButton("🗑 Удалить", callback_data=f"ag:delete_lot:{key}"))
        kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="ag:lots"))
        BOT.edit_message_text(txt, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("ag:rename_lot:"):
        key = data.split(":", 2)[2]
        TG_WIZARD[user_id] = {"lot_key": key}
        BOT.edit_message_text("✏️ Введите новое название лота:", chat_id, call.message.message_id)
        set_state(chat_id, user_id, "autogift_lot_rename")
        return

    if data.startswith("ag:toggle_lot:"):
        key = data.split(":", 2)[2]
        if key in LOTS:
            if not _binding_valid(LOTS[key]):
                BOT.send_message(chat_id, 'Сначала подтвердите привязку лота. Старый ID сам по себе не разрешает выдачу.')
                return
            LOTS[key]["enabled"] = not bool(LOTS[key].get("enabled", True))
            lid = LOTS[key].get("funpay_lot_id") or LOTS[key].get("lot_id")
            try:
                set_lot_active(int(lid), bool(LOTS[key]["enabled"]))
            except Exception:
                pass
            _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
        BOT.edit_message_text(lots_text(), chat_id, call.message.message_id, parse_mode="HTML", reply_markup=lots_keyboard())
        return

    if data.startswith("ag:delete_lot:"):
        key = data.split(":", 2)[2]
        LOTS.pop(key, None)
        _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
        BOT.edit_message_text(lots_text(), chat_id, call.message.message_id, parse_mode="HTML", reply_markup=lots_keyboard())
        return

    if data == "ag:catalog":
        items = CATALOG_CACHE.get(user_id) or CATALOG.get("items") or []
        if not items:
            try:
                items = run_coro(refresh_catalog_for_menu(user_id))
            except Exception as exc:
                BOT.edit_message_text(f"❌ Не удалось загрузить каталог:\n<code>{h(exc)}</code>", chat_id, call.message.message_id, parse_mode="HTML", reply_markup=back_keyboard())
                return
        text, kb = catalog_text(items, 0)
        BOT.edit_message_text(text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        return

    if data == "ag:catalog_refresh":
        try:
            items = run_coro(refresh_catalog_for_menu(user_id))
            text, kb = catalog_text(items, 0)
            BOT.edit_message_text(text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        except Exception as exc:
            BOT.edit_message_text(f"❌ Ошибка каталога:\n<code>{h(exc)}</code>", chat_id, call.message.message_id, parse_mode="HTML", reply_markup=back_keyboard())
        return

    if data.startswith("ag:cat:"):
        page = max(0, int(data.split(":")[2]))
        items = CATALOG_CACHE.get(user_id) or CATALOG.get("items") or []
        text, kb = catalog_text(items, page)
        BOT.edit_message_text(text, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("ag:gift:"):
        gid = int(data.split(":")[2])
        item = next((x for x in (CATALOG.get("items") or []) if int(x.get("id", -1)) == gid), None)
        if not item:
            BOT.answer_callback_query(call.id, "Gift не найден", show_alert=True)
            return
        emoji = h(item.get("emoji") or "🎁")
        name = h(item.get("name") or "Подарок")
        price = format_stars(item.get("price"))
        txt = (
            f"{emoji} <b>Подарок за {price}⭐</b>\n\n"
            f"Название: <b>{name}</b>\n\n"
            f"ID для копирования:\n<code>{gid}</code>"
        )
        BOT.edit_message_text(txt, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=back_keyboard("ag:catalog"))
        return

    if data == "ag:orders":
        # last 10 orders
        rows = sorted(ORDERS.values(), key=lambda x: x.get("created_at", 0), reverse=True)[:10]
        if not rows:
            txt = "<b>📊 Заказы</b>\n\nПока заказов нет."
        else:
            txt = "<b>📊 Последние заказы</b>\n\n"
            for row in rows:
                txt += f"#{h(row.get('order_id'))} · {h(row.get('status','?'))} · {format_stars(row.get('required_stars',0))}\n"
        BOT.edit_message_text(txt, chat_id, call.message.message_id, parse_mode="HTML", reply_markup=back_keyboard())
        return

    if data == "ag:status":
        try:
            run_coro(refresh_all_balances(notify=False))
        except Exception as exc:
            logger.error("%s status refresh failed: %s", LOGGER_PREFIX, exc)
        kb = InlineKeyboardMarkup(row_width=2)
        kb.row(InlineKeyboardButton("🔄 Обновить", callback_data="ag:status"),
               InlineKeyboardButton("👥 Аккаунты", callback_data="ag:accounts"))
        kb.row(InlineKeyboardButton("⬅️ Главное меню", callback_data="ag:main"))
        BOT.edit_message_text(balance_text(), chat_id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
        return


# ---------------------------------------------------------------------------
# FUNPAY ORDER FLOW
# ---------------------------------------------------------------------------

def buyer_refund(c: Cardinal, order_id: str, chat_id: Any) -> bool:
    with order_guard(order_id):
        data = ORDERS.get(str(order_id))
        if (not RUNNING or STORAGE_ERROR or not data or str(data.get("chat_id")) != str(chat_id)
                or str(order_id) in PROCESSING_ORDERS or not _active_order_is_still_own(c, data)):
            return False
        if not CONFIG.get("auto_refunds", True) or not order_is_refundable(data):
            send_funpay_message(c, chat_id, "Автовозврат недоступен. Свяжитесь с продавцом.")
            return False
        data.update(status="refunding", refund_started_at=time.time())
        save_order(data)
        try:
            c.account.refund(order_id)
            current = c.account.get_order(order_id)
            if current.status != enums.OrderStatuses.REFUNDED:
                raise RuntimeError("FunPay не подтвердил возврат")
        except Exception as exc:
            data.update(status="needs_review", last_error=f"Возврат не подтверждён: {type(exc).__name__}" )
            save_order(data)
            notify_operators(f"🟠 Возврат #{h(order_id)} требует проверки. Повторный возврат заблокирован.")
            return False
        data.update(status="refunded", refunded_at=time.time())
        save_order(data)
        send_funpay_message(c, chat_id, format_customer_message(CONFIG.get("refund_text"), data))
        return True


def username_from_order(order: Any) -> Optional[str]:
    """Read only dedicated purchase fields, never descriptions or chat text."""
    values = [getattr(order, "player", None)]
    fields = getattr(order, "fields", None)
    if isinstance(fields, dict):
        for key, field in fields.items():
            label = getattr(field, "name", "")
            if isinstance(label, dict):
                label = " ".join(str(v) for v in label.values())
            normalized_label = re.sub(r"[\s_-]+", " ", str(label or "")).lower().strip()
            if (str(key).lower() in {"telegram_username", "tg_username", "telegram", "username", "recipient"}
                    or normalized_label in {"telegram username", "telegram юзернейм", "telegram никнейм", "telegram аккаунт"}):
                value = getattr(field, "value", field)
                values.extend(value.values() if isinstance(value, dict) else [value])
    candidates = set()
    for value in values:
        if not value:
            continue
        text = str(value).strip()
        match = re.fullmatch(r"(?:@|https?://t\.me/|t\.me/)?([A-Za-z][A-Za-z0-9_]{3,31})", text)
        if not match:
            return None  # Ambiguous or invalid purchase data requires manual input.
        candidates.add(match.group(1).lower())
    return next(iter(candidates)) if len(candidates) == 1 else None


def _send_buyer_prompt(c: Cardinal, data: Dict[str, Any], text: str) -> bool:
    """Persist a unique dialog anchor; quick replies require its server message ID."""
    data["dialog_nonce"] = secrets.token_hex(6)
    data["dialog_expires"] = time.time() + 600
    data["dialog_message_id"] = 0
    save_order(data)
    anchor = f"AutoGift · #{data['order_id']} · {data['dialog_nonce']}"
    try:
        message = c.account.send_message(int(data["chat_id"]) if str(data["chat_id"]).isdigit() else data["chat_id"],
                                         _plain_funpay_text(text) + "\n\n" + anchor)
        data["dialog_message_id"] = int(getattr(message, "id", 0) or 0)
        save_order(data)
        return True
    except StorageError:
        raise
    except Exception:
        logger.exception("%s buyer prompt failed for #%s", LOGGER_PREFIX, data["order_id"])
        notify_operators(f"🟠 AutoGift #{h(data['order_id'])}: не удалось отправить подтверждение покупателю.")
        return False


def _username_prompt(data: Dict[str, Any]) -> str:
    return (f"🎁 Заказ #{data['order_id']}\n\n"
            f"Наборов: {data['units']}. Подарков: {len(data.get('delivery', []))}.\n\n"
            "👤 Напишите новый Telegram username: @username\n"
            f"Если в чате несколько заказов: !gift {data['order_id']} @username\n"
            "Мы покажем получателя перед отправкой подарков.")


def _quick_reply_order(c: Cardinal, msg: Any) -> Optional[Dict[str, Any]]:
    """Fail closed unless ALL paid purchases and the live dialog identify one order."""
    if not CONFIG.get("quick_confirmation", True):
        return None
    chat, author = str(getattr(msg, "chat_id", "")), str(getattr(msg, "author_id", ""))
    rows = [row for row in ORDERS.values() if str(row.get("chat_id")) == chat and str(row.get("buyer_id")) == author
            and row.get("status") in {"await_username", "ready_confirm"} and row.get("dialog_message_id")
            and ts(row.get("dialog_expires")) >= time.time()]
    if len(rows) != 1:
        return None
    data = rows[0]
    try:
        message_id = _positive_int(getattr(msg, "id", None))
        prompt_id = _positive_int(data["dialog_message_id"])
        if message_id <= prompt_id:
            return None
        current = c.account.get_order(data["order_id"])
        buyer_name = getattr(current, "buyer_username", None)
        if not buyer_name:
            return None
        paid_ids, cursor, seen = set(), None, set()
        for _ in range(10):
            result = c.account.get_sales(start_from=cursor, include_paid=True, include_closed=False,
                                         include_refunded=False, buyer=buyer_name)
            if not isinstance(result, (tuple, list)) or len(result) < 2 or not isinstance(result[1], list):
                return None
            for order in result[1]:
                if str(getattr(order, "buyer_id", "")) == author and getattr(order, "status", None) == enums.OrderStatuses.PAID:
                    paid_ids.add(str(order.id).upper())
            cursor = result[0]
            if not cursor:
                break
            if cursor in seen:
                return None
            seen.add(cursor)
        else:
            return None
        if paid_ids != {data["order_id"]}:
            return None
        history = c.account.get_chat_history(int(chat) if chat.isdigit() else chat)
        before = [m for m in history if prompt_id <= int(m.id) < message_id]
        incoming = [m for m in history if int(m.id) == message_id]
        if (len(before) != 1 or len(incoming) != 1 or int(before[0].id) != prompt_id
                or str(getattr(before[0], "author_id", "")) != str(c.account.id)
                or str(getattr(incoming[0], "author_id", "")) != author
                or str(getattr(incoming[0], "text", "")).strip() != str(msg.text).strip()
                or f"AutoGift · #{data['order_id']} · {data['dialog_nonce']}" not in str(before[0].text)):
            return None
        return data
    except Exception:
        logger.info("%s quick reply context could not be verified; use addressed command", LOGGER_PREFIX)
        return None


def _confirmation_prompt(data: Dict[str, Any]) -> str:
    data["status"] = "ready_confirm"
    data["confirm_nonce"] = secrets.token_hex(4)
    data["confirm_expires"] = time.time() + 600
    data.pop("confirmed", None)
    save_order(data)
    return (f"🎁 Подтвердите получателя · заказ #{data['order_id']}\n\n"
            f"👤 @{data['username']} · {data.get('display_name') or 'Telegram'}\n"
            f"📦 Наборов: {data['units']} · подарков: {len(data['delivery'])}\n\n"
            "Всё верно? Напишите + для отправки или - для смены username.\n"
            f"Если есть другие заказы: + {data['order_id']} или - {data['order_id']}\n"
            f"Команда с кодом: !gift {data['order_id']} confirm {data['confirm_nonce']}\n\n"
            "Подтверждение действует 10 минут. После отправки изменить получателя нельзя.")


def _delivery_consent_valid(data: Dict[str, Any]) -> bool:
    proof = data.get("confirmed") or {}
    return (bool(data.get("recipient_id")) and bool(data.get("binding_token"))
            and proof.get("order_id") == data.get("order_id")
            and proof.get("binding_token") == data.get("binding_token")
            and proof.get("recipient_id") == data.get("recipient_id")
            and proof.get("username") == data.get("username")
            and proof.get("gift_ids") == data.get("gift_ids")
            and proof.get("units") == data.get("units")
            and proof.get("comment") == data.get("comment")
            and proof.get("hide_name") == data.get("hide_name")
            and float(proof.get("expires", 0)) >= time.time())


def message_hook(c: Cardinal, e: NewMessageEvent):
    msg = getattr(e, "message", None)
    if not RUNNING or msg is None:
        return
    text = str(getattr(msg, "text", "") or "").strip()
    match = re.fullmatch(r"!gift\s+#?([A-Za-z0-9]+)\s+(.+)", text, re.I)
    addressed_sign = re.fullmatch(r"([+\-−])\s+#?([A-Za-z0-9]+)", text)
    quick_anchor = None
    if match:
        order_id, payload = match.group(1).upper(), match.group(2).strip()
    elif addressed_sign:
        order_id, payload = addressed_sign.group(2).upper(), addressed_sign.group(1)
    elif text in {"+", "-", "−"} or re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", text):
        row = _quick_reply_order(c, msg)
        if not row:
            return
        order_id, payload = row["order_id"], text
        quick_anchor = (row.get("dialog_message_id"), row.get("dialog_nonce"))
    else:
        return
    chat_id, author_id = str(getattr(msg, "chat_id", "")), str(getattr(msg, "author_id", ""))
    if author_id in ("", "0", str(c.account.id)):
        return
    # Serialize each plugin conversation and reject races with a running send.
    with order_guard(order_id):
        data = ORDERS.get(order_id)
        if (not data or order_id in PROCESSING_ORDERS
                or str(data.get("buyer_id")) != author_id or str(data.get("chat_id")) != chat_id
                or data.get("status") not in ("await_username", "await_comment", "ready_confirm")):
            return
        if quick_anchor and quick_anchor != (data.get("dialog_message_id"), data.get("dialog_nonce")):
            return
        if not _active_order_is_still_own(c, data):
            logger.warning("%s addressed input rejected for #%s: ownership not verified", LOGGER_PREFIX, order_id)
            return
        if payload.lower() == "refund":
            buyer_refund(c, order_id, chat_id)
            return
        if payload in {"-", "−"}:
            for key in ("confirmed", "confirm_nonce", "username", "recipient_id", "display_name"):
                data.pop(key, None)
            data["status"] = "await_username"
            _send_buyer_prompt(c, data, _username_prompt(data))
            return
        if payload == "+":
            if data.get("status") != "ready_confirm" or not data.get("confirm_nonce"):
                return
            try:
                if _positive_int(getattr(msg, "id", None)) <= _positive_int(data.get("dialog_message_id")):
                    return  # A delayed reply to a previous recipient is never consent.
            except (ValueError, TypeError):
                return  # Older APIs can use the addressed nonce command instead.
            payload = "confirm " + data["confirm_nonce"]
        recipient = re.fullmatch(r"@([A-Za-z0-9_]{4,32})", payload)
        if recipient:
            # Remove old consent before validation so it cannot survive a change.
            data.pop("confirmed", None)
            data.pop("confirm_nonce", None)
            data["status"] = "await_username"
            save_order(data)
            ok, name, recipient_id = run_coro(validate_username(recipient.group(1)))
            if not ok or not recipient_id:
                send_funpay_message(c, chat_id, "❌ Не удалось проверить Telegram-получателя. Повторите команду с @username.")
                return
            data.update(username=recipient.group(1), display_name=name, recipient_id=recipient_id)
            _send_buyer_prompt(c, data, _confirmation_prompt(data))
            return
        if data.get("status") == "await_comment" and payload.lower().startswith("comment "):
            data["comment"] = "" if payload[8:].strip() == "-" else payload[8:].strip()[:128]
            _send_buyer_prompt(c, data, _confirmation_prompt(data))
            return
        if data.get("status") == "ready_confirm":
            expected = "confirm " + str(data.get("confirm_nonce") or "")
            if payload == expected and data.get("confirm_nonce"):
                if time.time() > float(data.get("confirm_expires", 0)):
                    _send_buyer_prompt(c, data, _confirmation_prompt(data))
                    return
                data["confirmed"] = {k: copy.deepcopy(data.get(k)) for k in
                                     ("order_id", "binding_token", "recipient_id", "username", "gift_ids", "units", "comment", "hide_name")}
                data["confirmed"]["expires"] = data["confirm_expires"]
                data.pop("confirm_nonce", None)
                data["status"] = "ready"
                save_order(data)
            else:
                send_funpay_message(c, chat_id, f"Используйте команду confirm с кодом для заказа #{order_id}.")
                return
        else:
            return
    _execute_customer_order(c, data, chat_id)


def _execute_customer_order(c: Cardinal, data: Dict[str, Any], chat_id: str) -> None:
    with order_guard(data["order_id"]):
        return _execute_customer_order_locked(c, data, chat_id)


def _execute_customer_order_locked(c: Cardinal, data: Dict[str, Any], chat_id: str) -> None:
    """The only entry point for delivery after recipient confirmation."""
    order_id = str(data["order_id"])
    with ORDER_LOCK:
        if (not RUNNING or STORAGE_ERROR or order_id in PROCESSING_ORDERS or data.get('status') != 'ready'
                or str(data.get('chat_id')) != str(chat_id) or not _delivery_consent_valid(data)):
            return
        if not _active_order_is_still_own(c, data):
            return
        data['status'] = 'sending'
        save_order(data)
        PROCESSING_ORDERS.add(order_id)
    try:
        send_funpay_message(c, chat_id, "⏳ Проверяю актуальные цены и резерв Stars. Начинаю выдачу…")
        try:
            outcome = run_coro(execute_order(c, data))
        except StorageError:
            notify_operators(f"⛔ AutoGift #{h(order_id)}: ошибка хранилища. Выдача выключена; проверьте журнал и историю Telegram.")
            return
        except Exception as exc:
            logger.exception("%s execute_order crashed for #%s: %s", LOGGER_PREFIX, order_id, exc)
            data["status"] = "needs_review"; data["last_error"] = f"{type(exc).__name__}: {exc}"[:1000]; save_order(data); outcome = "needs_review"
        if outcome == "completed":
            data["status"] = "completed"; data["completed_at"] = time.time(); save_order(data)
            send_funpay_message(c, chat_id, f"🎁 AutoGift #{order_id}: отправлено {delivered_count(data)}/{len(data['delivery'])} подарков.\n"
                                + format_customer_message(CONFIG.get("success_text") or "Спасибо за покупку! Подтвердите выполнение заказа.", data)[:2500])
            notify_operators(f"✅ Заказ <b>#{h(order_id)}</b> выполнен\n👤 @{h(data.get('username'))}\n🎁 Gifts: {delivered_count(data)}\n⭐ Потрачено: {format_stars(sum(int(i.get('price') or 0) for i in data.get('delivery', []) if i.get('status') == 'sent'))}")
            return
        if outcome == 'failed_no_delivery' and CONFIG.get('auto_refund_when_no_delivery', False):
            data['status'] = 'failed'
            save_order(data)
            PROCESSING_ORDERS.discard(order_id)
            if buyer_refund(c, order_id, chat_id):
                return
        data["status"] = "needs_review"; save_order(data)
        send_funpay_message(c, chat_id, "🟠 Выдача остановлена и передана продавцу на ручную проверку. Не отправляйте новые данные — продавец напишет в этом чате.")
        notify_operators(f"🟠 Заказ <b>#{h(order_id)}</b> требует проверки\n👤 @{h(data.get('username'))}\n🎁 Отправлено: {delivered_count(data)}/{len(data.get('delivery', []))}\n<code>{h(str(data.get('last_error') or 'Неизвестный результат')[:700])}</code>")
    finally:
        with ORDER_LOCK:
            PROCESSING_ORDERS.discard(order_id)


def _order_chat_id(c, order):
    explicit = getattr(order, "chat_id", None)
    if explicit:
        return str(explicit)
    # Older Cardinal has integer chat IDs; resolve from its server-side buyer data.
    buyer_name = getattr(order, "buyer_username", None)
    if not buyer_name:
        raise ValueError("FunPay не вернул чат покупателя.")
    chat = c.account.get_chat_by_name(buyer_name, True)
    if not chat or not getattr(chat, "id", None):
        raise ValueError("Не удалось подтвердить чат покупателя.")
    return str(chat.id)


def accept_paid_order(c: Cardinal, order: Any, event: Any = None, fallback_chat_id: Any = None) -> None:
    if not RUNNING or STORAGE_ERROR:
        return
    order_id = str(getattr(order, "id", "") or "").upper()
    if not re.fullmatch(r"[A-Z0-9]+", order_id):
        return
    with order_guard(order_id):
        if order_id in ORDERS:
            return
        try:
            full = c.account.get_order(order_id)
            if str(getattr(full, "id", "")).upper() != order_id:
                return
            found = find_lot_by_order(full)
        except Exception:
            logger.warning("%s cannot verify purchase #%s; ignored without buyer messages", LOGGER_PREFIX, order_id)
            return
        if not found:
            logger.info("%s purchase #%s has no verified AutoGift binding; ignored", LOGGER_PREFIX, order_id)
            return
        lot_key, lot = found
        try:
            units = _order_quantity(full)
        except (ValueError, TypeError):
            data = {"order_id": order_id, "status": "needs_review", "created_at": time.time(),
                    "lot_key": lot_key, "last_error": "FunPay не вернул точное количество в полной карточке заказа."}
            save_order(data)
            notify_operators(f"🟠 AutoGift #{h(order_id)}: количество не подтверждено, нужна проверка.")
            return
        buyer_id = getattr(full, "buyer_id", None)
        if not buyer_id or str(buyer_id) == str(c.account.id):
            return
        chat_id = _order_chat_id(c, full)
        if fallback_chat_id is not None and str(fallback_chat_id) != chat_id:
            logger.warning("%s chat mismatch for purchase #%s; ignored", LOGGER_PREFIX, order_id)
            return
        data = {"order_id": order_id, "buyer_id": int(buyer_id), "chat_id": chat_id,
                "status": "await_username", "lot_key": lot_key, "lot_name": lot.get("name"),
                "binding_token": lot["binding"]["token"], "gift_ids": list(lot["gift_ids"]),
                "gift_names": list(lot.get("gift_names", [])), "units": units,
                "order_price": float(getattr(full, "sum", getattr(full, "price", 0)) or 0),
                "currency": str(getattr(getattr(full, "currency", None), "name", getattr(full, "currency", "UNKNOWN"))),
                "created_at": time.time(), "username": None,
                "comment": CONFIG.get("comment") if CONFIG.get("comment_enabled") else "",
                "hide_name": CONFIG.get("hide_name", True), "delivery": []}
        if (units > max(1, int(CONFIG.get("max_order_units") or 10))
                or units * len(data["gift_ids"]) > int(CONFIG.get("max_gifts_per_order", 100))):
            data.update(status="needs_review", last_error="Превышен лимит Gift-наборов.")
            save_order(data)
            notify_operators(f"🟠 AutoGift #{h(order_id)}: превышен лимит, отправка остановлена.")
            return
        delivery_items(data)
        save_order(data)  # Durable acceptance BEFORE chat/network calls.
    with order_guard(order_id):
        if not RUNNING or data.get("status") != "await_username" or not _active_order_is_still_own(c, data):
            return
        username = username_from_order(full) if CONFIG.get("username_from_order", True) else None
        prompt = _username_prompt(data)
        if username:
            try:
                ok, name, recipient_id = run_coro(validate_username(username))
            except Exception:
                ok, name, recipient_id = False, "", None
                logger.info("%s checkout recipient lookup unavailable for #%s", LOGGER_PREFIX, order_id)
            if ok and recipient_id:
                data.update(username=username, display_name=name, recipient_id=recipient_id, username_source="funpay")
                prompt = _confirmation_prompt(data)
            else:
                prompt = "⚠️ Username из заказа не удалось проверить.\n\n" + prompt
        _send_buyer_prompt(c, data, format_customer_message(CONFIG.get("payment_text") or "Заказ принят.", data)[:1500]
                           + "\n\n" + prompt + f"\nВозврат до отправки: !gift {order_id} refund")

def payment_notice_hook(c: Cardinal, e: NewMessageEvent):
    """Fallback only for a genuine FunPay system payment message."""
    if not RUNNING: return
    msg=getattr(e,"message",None)
    if msg is None or str(getattr(msg, "author_id", "")) != "0": return
    text=str(getattr(msg,"text","") or "")
    match=re.search(r"(?:оплатил|оплатила|оплачено)\s+заказ\s+#([A-Za-z0-9]+)",text,re.I)
    if not match: return
    order_id=str(match.group(1))
    with order_guard(order_id):
        if order_id in ORDERS: return
    def worker():
        try: accept_paid_order(c,c.account.get_order(order_id),e,getattr(msg,"chat_id",None))
        except Exception as exc: logger.exception("%s payment fallback failed for #%s: %s",LOGGER_PREFIX,order_id,exc)
    threading.Thread(target=worker,daemon=True,name=f"AutoGiftOrderFallback-{order_id}").start()

def order_hook(c: Cardinal, e: NewOrderEvent):
    """Accept only a configured lot and always ask the buyer for a recipient."""
    if not RUNNING: return
    order=getattr(e,"order",None)
    if order is None: return
    try:
        accept_paid_order(c,order,e,getattr(order,"chat_id",None))
    except Exception as exc:
        logger.exception("%s order_hook failed for #%s: %s",LOGGER_PREFIX,getattr(order,"id","?"),exc)

def _handle_telegram_command(message):
    global RUNNING
    text = (getattr(message, "text", "") or "").strip().lower()
    if text not in {"/autogift", "/auto_gifts", "/autogifts", "/start_gifts", "/stop_gifts", "/autogift_help", "/autogift_install"}:
        return False
    if message.chat.type != "private" or not is_operator(int(message.from_user.id)):
        return False
    chat_id = message.chat.id
    if text in {"/autogift", "/auto_gifts", "/autogifts"}:
        start_handler(message)
    elif text == "/autogift_install":
        start_dependency_install(chat_id, message.from_user.id)
    elif text == "/autogift_help":
        BOT.send_message(chat_id, help_text(), parse_mode="HTML", reply_markup=back_keyboard())
    elif text == "/stop_gifts":
        RUNNING = False
        if INITIALIZED and not STORAGE_ERROR and not STARTUP_ERROR:
            CONFIG["running"] = False
            _safe_json_save(CONFIG_PATH, CONFIG)
        BOT.send_message(chat_id, "🔴 Выдача AutoGift выключена. Меню: /autogift")
    elif text == "/start_gifts":
        if not INITIALIZED or not DEPENDENCY_READY or STARTUP_ERROR or STORAGE_ERROR:
            send_main(chat_id)
            return True
        CONFIG["running"] = True
        _safe_json_save(CONFIG_PATH, CONFIG)
        RUNNING = True
        BOT.send_message(chat_id, "🟢 AutoGift включён.")
    return True


def _telegram_command_middleware(_bot, message):
    """Own commands still work if an earlier plugin consumes ordinary handlers."""
    try:
        if _handle_telegram_command(message):
            if len(TELEGRAM_COMMAND_SEEN) >= 512:
                TELEGRAM_COMMAND_SEEN.clear()
            TELEGRAM_COMMAND_SEEN.add((int(message.chat.id), int(message.message_id)))
    except Exception:
        logger.exception("%s Telegram command failed", LOGGER_PREFIX)
        try:
            BOT.send_message(message.chat.id, "AutoGift: ошибка обработки команды. Меню: /autogift")
        except Exception:
            pass


def _plugin_settings_callback(call):
    if call.message is None:
        return
    if call.message.chat.type != "private" or not is_operator(int(call.from_user.id)):
        deny_operator(call.message.chat.id, call.id)
        return
    try:
        BOT.answer_callback_query(call.id)
    except Exception:
        pass
    clear_state(call.message.chat.id, call.from_user.id)
    send_main(call.message.chat.id, call.message.message_id)


def _register_control_panel(c):
    # Register commands BEFORE optional imports, storage migration or network I/O.
    # add_telegram_commands also supplies the command list on Cardinal's plugin card.
    c.add_telegram_commands(UUID, [
        ("autogift", "🎁 AutoGift — меню", True),
        ("start_gifts", "🟢 Запустить выдачу AutoGift", True),
        ("stop_gifts", "🔴 Остановить выдачу AutoGift", True),
        ("autogift_install", "📦 Установить недостающую библиотеку Telegram", True),
        ("autogift_help", "📖 Инструкция AutoGift", True),
    ])
    c.telegram.msg_handler(message_dispatch, func=_telegram_message_filter, content_types=["text"])
    try:
        c.telegram.mdw_handler(_telegram_command_middleware, update_types=["message"])
    except Exception:
        # Ordinary filtered command handlers still work on forks without middleware.
        logger.warning("%s command middleware unavailable; using ordinary handlers", LOGGER_PREFIX)
    c.telegram.cbq_handler(cb, lambda call: bool(call.data) and call.data.startswith("ag:"))
    try:
        from tg_bot import CBT
        prefix = f"{CBT.PLUGIN_SETTINGS}:{UUID}"
        c.telegram.cbq_handler(_plugin_settings_callback,
                              lambda call: bool(call.data) and (call.data == prefix or call.data.startswith(prefix + ":")))
    except ImportError:
        logger.warning("%s plugin settings callback unavailable; menu remains at /autogift", LOGGER_PREFIX)


def _start_background_workers():
    global WORKERS_STARTED
    if WORKERS_STARTED:
        return
    WORKERS_STARTED = True
    threading.Thread(target=auth_watcher_worker, daemon=True, name="AutoGiftAuthWatcher").start()
    # This worker performs the first balance/protection refresh without blocking
    # Cardinal startup and then repeats it once per minute.
    threading.Thread(target=background_worker, daemon=True, name="AutoGiftBalance").start()


def init_commands(c: Cardinal):
    global CARDINAL_INSTANCE, BOT, RUNNING, INITIALIZED, STARTUP_ERROR
    if getattr(c, "_autogift_safe_owner", None) is not None:
        logger.warning("%s AutoGift handlers already have an owner; duplicate init ignored", LOGGER_PREFIX)
        return
    CARDINAL_INSTANCE = c
    BOT = c.telegram.bot
    RUNNING = False
    INITIALIZED = False
    STARTUP_ERROR = ""
    c._autogift_safe_owner = UUID
    _register_control_panel(c)
    try:
        _acquire_instance_lock()
        load_all()
        migrate_lots()
        INITIALIZED = True
    except Exception as exc:
        STARTUP_ERROR = str(exc)[:700]
        logger.exception("%s delivery startup blocked; control panel remains available", LOGGER_PREFIX)
        notify_operators("⛔ AutoGift: выдача остановлена. Меню доступно: /autogift\n" + h(STARTUP_ERROR))
        return
    if not _load_telegram_dependency():
        notify_operators("⚠️ AutoGift: нужна библиотека Telegram.\nОткройте /autogift или «Настройки» в карточке плагина. Меню и команды доступны.")
        return
    RUNNING = bool(CONFIG.get("running", True))
    _start_background_workers()
    logger.info("%s initialized: %s accounts, %s lots; control panel ready", LOGGER_PREFIX, len(ACCOUNTS), len(LOTS))


BIND_TO_PRE_INIT = [init_commands]
BIND_TO_NEW_MESSAGE = [payment_notice_hook, message_hook]
BIND_TO_NEW_ORDER = [order_hook]
BIND_TO_DELETE = None
BIND_TO_API = {}
