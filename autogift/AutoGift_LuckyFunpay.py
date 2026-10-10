# -*- coding: utf-8 -*-
"""
AutoGift 6.3.0: verified FunPay orders and journalled Telegram Gifts.
Access is restricted to Cardinal administrators in private Telegram chats.
The control panel remains available when the Telegram dependency is missing.

6.3.0: persistent Telegram connections on a dedicated event loop, a delivery
queue outside Cardinal's event thread, a simpler buyer dialogue (!вз/!бэк/!refund,
t.me links), review actions for operators, paid-order backfill and storage backups.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import csv
import copy
import hashlib
import importlib
import math
from contextlib import asynccontextmanager
import secrets
import shutil
import string
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
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Message

# Optional delivery dependency. Import only AFTER registering the control panel.
Client = None
ChatType = None


# ---------------------------------------------------------------------------
# META
# ---------------------------------------------------------------------------

NAME = "AutoGift"
VERSION = "6.3.0"
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
SEEN_PATH = STORAGE / "autogift_seen.json"
ARCHIVE_PATH = STORAGE / "autogift_orders_archive.jsonl"
DEPENDENCY_ROOT = STORAGE / "autogift_runtime" / f"py{sys.version_info.major}{sys.version_info.minor}"
DEPENDENCY_PACKAGE = "Kurigram==2.2.26"

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
    "require_confirmation": True,
    "running": True,
    "auto_refunds": True,
    "auto_refund_when_no_delivery": False,
    "max_order_units": 10,
    "max_gifts_per_order": 100,
    "max_order_stars": 10000,
    "star_cost_rub": 0.0,
    "funpay_fee_percent": 0.0,
    "hide_name": True,
    "comment_enabled": False,
    "comment": "",
    "buyer_reminder": True,
    "lot_protection": True,
    "low_balance_enabled": True,
    "low_balance_threshold": 150,
    "auto_reactivate": True,
    "reminder_text": "⏳ Напоминаем: чтобы получить подарок по заказу #{order_id}, отправьте ваш Telegram @username.",
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
ACCOUNT_RESERVATIONS: Dict[str, int] = {}
SEEN_ORDERS: Dict[str, float] = {}

# Work that must never run inside Cardinal's FunPay event thread.
EVENT_QUEUE: "pyqueue.Queue[Tuple[Any, tuple]]" = pyqueue.Queue()
DELIVERY_QUEUE: "pyqueue.Queue[Tuple[str, str]]" = pyqueue.Queue()
QUEUED_ORDERS: set[str] = set()
QUEUE_LOCK = threading.Lock()
BALANCE_REFRESH_REQUEST = threading.Event()
SCAN_REQUEST = threading.Event()
PROTECTION_STATE: Dict[str, Any] = {"bad_cycles": 0, "catalog_warned": False}
NOTIFY_ONCE: Dict[str, float] = {}

BALANCE_INTERVAL = 180
SCAN_INTERVAL = 300
SCAN_MAX_AGE = 48 * 3600
CONSENT_TTL = 24 * 3600
GIFT_SEND_DELAY = 1.0
OWNERSHIP_RECHECK = 20
HINT_INTERVAL = 180
ARCHIVE_AFTER_DAYS = 60
BACKUP_INTERVAL = 600
ACTIVE_BUYER_STATUSES = {"await_username", "ready_confirm"}
TERMINAL_STATUSES = {"completed", "refunded", "closed_external", "ignored_foreign"}
DEVICE_MODEL = "AutoGift (FunPay Cardinal)"

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


def _storage_failed(path, exc, hint=""):
    global STORAGE_ERROR, RUNNING
    STORAGE_ERROR = f"Не удалось прочитать/сохранить {path.name}: {type(exc).__name__}" + (f". {hint}" if hint else "")
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


LAST_BACKUP: Dict[str, float] = {}


def _backup_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".bak")


def _safe_json_load(path: Path, default: Any) -> Any:
    with FILE_LOCK:
        try:
            if not path.exists():
                return copy.deepcopy(default)
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            backup = _backup_path(path)
            # The order journal is never rolled back: an older copy could show a sent
            # Gift as pending and the order could be paid twice. Fail closed instead.
            if path != ORDERS_PATH and backup.exists():
                try:
                    with backup.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    logger.error("%s %s is damaged (%s); restored from %s", LOGGER_PREFIX, path.name, exc, backup.name)
                    NOTIFY_ONCE["restored:" + path.name] = time.time()
                    return data
                except Exception:
                    pass
            raise _storage_failed(path, exc) from exc


def _safe_json_save(path: Path, data: Any) -> None:
    with FILE_LOCK:
        if STORAGE_ERROR:
            raise StorageError(STORAGE_ERROR)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
            # A periodic copy of the last good file survives a damaged write/disk.
            if path.exists() and time.time() - LAST_BACKUP.get(str(path), 0) > BACKUP_INTERVAL:
                shutil.copy2(path, _backup_path(path))
                LAST_BACKUP[str(path)] = time.time()
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
        # Without the journal a paid, already delivered order could be delivered again.
        raise _storage_failed(ORDERS_PATH, ValueError("Order journal missing in an existing installation"),
                              "Журнал заказов пропал. Верните файл (или его копию .bak) в storage/cache. "
                              "Если это чистая установка — удалите autogift_config.json. Затем /restart.")
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
    raw_seen = _safe_json_load(SEEN_PATH, {})
    SEEN_ORDERS.clear()
    if isinstance(raw_seen, dict):
        SEEN_ORDERS.update({str(k).upper(): ts(v) for k, v in raw_seen.items()})

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
    # Paid-order backfill never reaches orders older than the first 6.3 start.
    if not ts(CONFIG.get("scan_since")):
        CONFIG["scan_since"] = time.time()
    _persist_all()


def _persist_all() -> None:
    # The journal first and the config last: a crash in between never leaves
    # "config exists, journal missing", which would block the next start.
    _safe_json_save(ORDERS_PATH, ORDERS)
    _safe_json_save(ACCOUNTS_PATH, ACCOUNTS)
    _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
    _safe_json_save(CATALOG_PATH, CATALOG)
    _safe_json_save(OPERATORS_PATH, {"users": sorted(OPERATORS)})
    save_seen()
    _safe_json_save(CONFIG_PATH, CONFIG)


def save_seen() -> None:
    cutoff = time.time() - 7 * 86400
    snapshot = dict(SEEN_ORDERS)
    for key in [k for k, v in snapshot.items() if v < cutoff]:
        SEEN_ORDERS.pop(key, None)
        snapshot.pop(key, None)
    _safe_json_save(SEEN_PATH, snapshot)


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

def notify_operators(text: str, reply_markup: Any = None) -> None:
    if BOT is None: return
    if threading.current_thread() is RUNTIME.thread:
        # Never block the Telegram event loop on Bot API HTTP calls.
        threading.Thread(target=notify_operators, args=(text, reply_markup), daemon=True).start()
        return
    for uid in _cardinal_admin_ids():
        try: BOT.send_message(uid, text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=reply_markup)
        except Exception as exc: logger.warning("%s notify %s failed: %s", LOGGER_PREFIX, uid, exc)


def notify_once(key: str, text: str, interval: float = 3600, reply_markup: Any = None) -> None:
    """Rate-limited notification: repeated failures do not flood the admin chat."""
    if time.time() - NOTIFY_ONCE.get(key, 0) < interval:
        return
    NOTIFY_ONCE[key] = time.time()
    notify_operators(text, reply_markup)

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
def load_archive() -> List[Dict[str, Any]]:
    """Finished orders moved out of the live journal (one JSON object per line)."""
    rows: Dict[str, Dict[str, Any]] = {}
    try:
        if ARCHIVE_PATH.exists():
            with ARCHIVE_PATH.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("order_id"):
                        rows[str(row["order_id"])] = row
    except OSError as exc:
        logger.warning("%s archive read failed: %s", LOGGER_PREFIX, exc)
    return list(rows.values())


def archive_old_orders() -> int:
    """Move long-finished orders to the archive so each journal write stays small."""
    cutoff = time.time() - ARCHIVE_AFTER_DAYS * 86400
    with ORDER_LOCK:
        old = [row for row in ORDERS.values() if row.get("status") in TERMINAL_STATUSES
               and max(ts(row.get("updated_at")), ts(row.get("created_at"))) < cutoff]
        if not old or STORAGE_ERROR:
            return 0
        # Archive first (append + fsync), then shrink the journal. A crash in between
        # only duplicates rows, which load_archive() de-duplicates by order ID.
        with ARCHIVE_PATH.open("a", encoding="utf-8") as f:
            for row in old:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        for row in old:
            ORDERS.pop(str(row["order_id"]), None)
        _safe_json_save(ORDERS_PATH, ORDERS)
    logger.info("%s archived %s finished orders", LOGGER_PREFIX, len(old))
    return len(old)


def order_stats(days: Optional[int] = None) -> Dict[str, Any]:
    since = (datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
             - timedelta(days=days - 1)).timestamp() if days else 0
    with ORDER_LOCK:
        rows = copy.deepcopy([row for row in ORDERS.values() if ts(row.get("created_at")) >= since])
    if days is None or days > ARCHIVE_AFTER_DAYS:
        live = {str(row.get("order_id")) for row in rows}
        rows += [row for row in load_archive() if str(row.get("order_id")) not in live
                 and ts(row.get("created_at")) >= since]
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


PROXY_SCHEMES = {"http": "http", "https": "http", "socks5": "socks5", "socks5h": "socks5", "socks4": "socks4"}


def parse_proxy(raw: str) -> Optional[dict]:
    raw = (raw or "").strip()
    if not raw:
        return None
    scheme = "http"
    if "://" in raw:
        scheme, raw = raw.split("://", 1)
        scheme = PROXY_SCHEMES.get(scheme.strip().lower(), "")
        if not scheme:
            raise ValueError("Поддерживаются прокси http://, socks5:// и socks4://")
    raw = raw.strip().rstrip("/")
    auth, hostport = ("", raw)
    if "@" in raw:
        auth, hostport = raw.rsplit("@", 1)
    if ":" not in hostport:
        raise ValueError("Прокси должен быть в формате ip:port или login:pass@ip:port")
    host, port = hostport.rsplit(":", 1)
    host = host.strip().strip("[]")
    if not host or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("Неверный адрес или порт прокси. Пример: 1.2.3.4:8080 или login:pass@1.2.3.4:8080")
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


def _client_signature(account: Dict[str, Any]) -> str:
    """Any change of session, API keys or proxy reconnects the persistent client."""
    payload = [str(account.get("session_string") or ""), str(account.get("session_name") or ""),
               str(account.get("session_workdir") or ""), str(CONFIG.get("api_id") or ""),
               str(CONFIG.get("api_hash") or ""), str(CONFIG.get("proxy") or "")]
    return hashlib.sha256(json.dumps(payload).encode("utf-8")).hexdigest()


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
        "device_model": DEVICE_MODEL,
        "app_version": f"AutoGift {VERSION}",
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
    try:
        return Client(**kwargs)
    except TypeError:
        # Older forks without device/app metadata arguments.
        kwargs.pop("device_model", None)
        kwargs.pop("app_version", None)
        return Client(**kwargs)


class TelegramRuntime:
    """One event loop thread that owns every Telegram connection.

    Clients stay connected between operations (no reconnect per Gift or per
    balance check). Each account has its own lock, so one account never runs
    two requests at once while different accounts work in parallel.
    """

    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self.clients: Dict[str, Tuple[Any, str]] = {}
        self.locks: Dict[str, asyncio.Lock] = {}
        self._start_lock = threading.Lock()

    def ensure(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self.loop is not None and self.thread is not None and self.thread.is_alive():
                return self.loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def runner():
                asyncio.set_event_loop(loop)
                ready.set()
                loop.run_forever()

            thread = threading.Thread(target=runner, daemon=True, name="AutoGiftTelegram")
            thread.start()
            ready.wait(5)
            self.loop, self.thread = loop, thread
            self.clients.clear()
            self.locks.clear()
            return loop

    def run(self, coro, timeout: Optional[float] = None):
        loop = self.ensure()
        if threading.current_thread() is self.thread:
            coro.close()
            raise RuntimeError("Nested Telegram runtime call")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError("Telegram не ответил вовремя. Проверьте прокси и подключение.") from None

    def lock_for(self, account_id: str) -> asyncio.Lock:
        lock = self.locks.get(account_id)
        if lock is None:
            lock = self.locks[account_id] = asyncio.Lock()
        return lock

    def is_busy(self, account_id: str) -> bool:
        lock = self.locks.get(str(account_id))
        return bool(lock and lock.locked())

    async def client_for(self, account: Dict[str, Any]):
        key = str(account["id"])
        signature = _client_signature(account)
        entry = self.clients.get(key)
        if entry and entry[1] == signature and getattr(entry[0], "is_connected", False):
            return entry[0]
        if entry:
            await self._close(entry[0])
            self.clients.pop(key, None)
        app = _make_account_client(account)
        try:
            authorized = await asyncio.wait_for(app.connect(), 40)
        except BaseException:
            await self._close(app)
            raise
        if not authorized:
            await self._close(app)
            raise PreflightError("Telegram-сессия не авторизована. Добавьте аккаунт заново.")
        self.clients[key] = (app, signature)
        return app

    async def drop(self, account_id: str) -> None:
        entry = self.clients.pop(str(account_id), None)
        if entry:
            await self._close(entry[0])

    @staticmethod
    async def _close(app) -> None:
        try:
            if getattr(app, "is_connected", False):
                await asyncio.wait_for(app.disconnect(), 15)
        except Exception:
            logger.warning("%s Telegram disconnect failed", LOGGER_PREFIX)

    def drop_sync(self, account_id: str) -> None:
        if self.loop is None or self.thread is None or not self.thread.is_alive():
            return
        try:
            self.run(self.drop(account_id), timeout=20)
        except Exception:
            logger.warning("%s could not close client %s", LOGGER_PREFIX, account_id)


RUNTIME = TelegramRuntime()

CONNECTION_ERRORS = (OSError, ConnectionError, asyncio.TimeoutError, TimeoutError)
SESSION_DEAD_IDS = {"AUTH_KEY_UNREGISTERED", "SESSION_REVOKED", "SESSION_EXPIRED", "USER_DEACTIVATED",
                    "USER_DEACTIVATED_BAN", "AUTH_KEY_DUPLICATED", "AUTH_KEY_INVALID"}


def _rpc_id(exc: BaseException) -> str:
    return str(getattr(exc, "ID", "") or "")


@asynccontextmanager
async def account_client(account):
    """Exclusive use of the account's persistent, connected client."""
    key = str(account["id"])
    async with RUNTIME.lock_for(key):
        app = await RUNTIME.client_for(account)
        try:
            yield app
        except BaseException as exc:
            # A broken transport or a revoked session must not be reused.
            if isinstance(exc, CONNECTION_ERRORS) or _rpc_id(exc) in SESSION_DEAD_IDS:
                await RUNTIME.drop(key)
            raise


def run_coro(coro, timeout: Optional[float] = None):
    """Run a coroutine on the Telegram runtime. Never call with ORDER_LOCK held."""
    return RUNTIME.run(coro, timeout)


USERNAME_BODY = r"[A-Za-z][A-Za-z0-9_]{3,31}"
RESERVED_WORDS = {"refund", "confirm", "comment", "gift", "help", "start"}


def parse_buyer_username(text: str, allow_bare: bool) -> Optional[str]:
    """@name, t.me/name, https://t.me/name and (when allowed) a bare name."""
    t = (text or "").strip()
    m = (re.fullmatch(r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/@?(" + USERNAME_BODY + r")/?", t, re.I)
         or re.fullmatch(r"@(" + USERNAME_BODY + r")", t))
    if not m and allow_bare:
        m = re.fullmatch(r"(" + USERNAME_BODY + r")", t)
        if m and m.group(1).lower() in RESERVED_WORDS:
            return None
    return m.group(1) if m else None


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


def catalog_prices(max_age: float) -> Dict[int, int]:
    """Prices confirmed by Telegram within max_age seconds; empty when stale."""
    if time.time() - ts(CATALOG.get("live_checked_at")) > max_age:
        return {}
    return {int(g["id"]): int(g["price"]) for g in CATALOG.get("items", [])
            if g.get("available") and g.get("price")}


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


async def verify_recipient(app, username: str, recipient_id: int) -> None:
    """The username must still point to the confirmed Telegram user/channel."""
    recipient = await app.get_chat(username)
    if int(recipient.id) != int(recipient_id):
        raise PreflightError("Telegram username теперь принадлежит другому получателю.")
    if ChatType is None or recipient.type not in (ChatType.PRIVATE, ChatType.CHANNEL):
        raise PreflightError("Этот тип получателя не поддерживается.")


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
    """Все валидные Telegram-сессии, независимо от баланса.

    Свободные и исправные аккаунты идут первыми: проверка username не ждёт
    аккаунт, который сейчас отправляет подарки.
    """
    accounts = [a for a in ACCOUNTS if has_account_credentials(a)]
    return sorted(accounts, key=lambda a: (bool(a.get("auth_failed")),
                                           ts(a.get("cooldown_until")) > time.time(),
                                           RUNTIME.is_busy(str(a.get("id")))))


def account_title(account: Dict[str, Any]) -> str:
    return str(account.get("label") or account.get("id"))


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
        if account.pop("auth_failed", None):
            notify_operators(f"✅ AutoGift: сессия <b>{h(account_title(account))}</b> снова работает.")
        return balance
    except Exception as exc:
        account["balance"] = None
        account["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
        _apply_account_cooldown(account, exc)
        if _rpc_id(exc) in SESSION_DEAD_IDS or (isinstance(exc, PreflightError) and "не авторизована" in str(exc)):
            if not account.get("auth_failed"):
                account["auth_failed"] = True
                notify_operators(f"⛔ AutoGift: Telegram-сессия <b>{h(account_title(account))}</b> отозвана или "
                                 "больше не авторизована. Удалите аккаунт и добавьте его заново.")
        logger.error("%s balance %s failed: %s", LOGGER_PREFIX, account.get("id"), exc)
        return None


def total_balance() -> int:
    return sum(int(a["balance"]) for a in get_active_accounts())


def _apply_account_cooldown(account, exc):
    """Only a structured Telegram RPC rejection permits switching senders."""
    try:
        from pyrogram.errors import FloodWait, RPCError
    except ImportError:
        return False
    if isinstance(exc, FloodWait):
        account["cooldown_until"] = time.time() + max(1, int(exc.value or 1))
        account["last_error"] = "FloodWait: " + str(int(exc.value or 1)) + " s"
        return True
    if isinstance(exc, RPCError) and getattr(exc, "ID", "") == "BALANCE_TOO_LOW":
        account["balance"] = 0
        account["last_error"] = "BALANCE_TOO_LOW"
        return True
    return False


async def refresh_all_balances(notify: bool = True, protect: bool = True,
                               skip_busy: bool = False) -> Dict[str, Optional[int]]:
    accounts = [a for a in list(ACCOUNTS) if not (skip_busy and RUNTIME.is_busy(str(a.get("id"))))]
    olds = [a.get("balance") for a in accounts]
    values = await asyncio.gather(*(refresh_account_balance(a) for a in accounts))
    result = {}
    for account, old, value in zip(accounts, olds, values):
        result[str(account.get("id"))] = value
        if notify and old is not None and int(old) > 0 and value == 0:
            notify_operators(f"⚠️ Сессия <b>{h(account_title(account))}</b> исчерпала Stars.")
        elif notify and old == 0 and value and value > 0:
            notify_operators(f"✅ Сессия <b>{h(account_title(account))}</b> снова доступна: {format_stars(value)}")
    _safe_json_save(ACCOUNTS_PATH, ACCOUNTS)
    if protect:
        await update_lot_protection()
    return result


def protection_decision(lot: Dict[str, Any], best_balance: int, threshold_hit: bool,
                        prices: Dict[int, int], fallback_prices: Dict[int, int], catalog_fresh: bool) -> Optional[bool]:
    """True = pause, False = may be active, None = not enough data to decide.

    A missing gift only counts when a fresh catalog says so; a failed catalog
    request never pauses every listing at once.
    """
    ids = [int(gid) for gid in lot.get("gift_ids", [])]
    known = prices if catalog_fresh else fallback_prices
    if catalog_fresh and any(gid not in prices for gid in ids):
        return True
    if any(gid not in known for gid in ids):
        return None
    required = max(sum(known[gid] for gid in ids), int(lot.get("capacity_required") or 0))
    if threshold_hit or best_balance < required:
        return True
    return False if catalog_fresh else None


async def update_lot_protection() -> None:
    if not RUNNING or STORAGE_ERROR or not CONFIG.get("lot_protection", True):
        return
    usable = [a for a in ACCOUNTS if a.get("active") and has_account_credentials(a)]
    if usable and all(a.get("balance") is None for a in usable):
        # Every balance request failed (proxy/network): wait for 3 cycles in a row
        # before treating it as "no Stars", so a short outage does not flap listings.
        PROTECTION_STATE["bad_cycles"] = PROTECTION_STATE.get("bad_cycles", 0) + 1
        if PROTECTION_STATE["bad_cycles"] < 3:
            return
        notify_once("balances_unavailable", "🟠 AutoGift: балансы аккаунтов недоступны уже несколько проверок подряд. "
                    "Проверьте прокси и сессии. Лоты будут поставлены на паузу.")
    else:
        PROTECTION_STATE["bad_cycles"] = 0
    balances = [int(a["balance"]) for a in get_active_accounts()]
    best_balance = max(balances, default=0)
    threshold = int(CONFIG.get("low_balance_threshold") or 0)
    threshold_hit = best_balance < threshold if CONFIG.get("low_balance_enabled", True) else best_balance <= 0
    if time.time() - ts(CATALOG.get("live_checked_at")) > 300:
        try:
            await refresh_catalog_for_menu(0)
        except Exception:
            logger.warning("%s live catalog unavailable for lot protection", LOGGER_PREFIX)
    catalog_fresh = time.time() - ts(CATALOG.get("live_checked_at")) <= 600
    prices = catalog_prices(600)
    fallback = {int(g["id"]): int(g["price"]) for g in (CATALOG.get("items") or STANDARD_GIFTS_CATALOG) if g.get("price")}
    if not catalog_fresh:
        notify_once("catalog_stale", "🟠 AutoGift: каталог Telegram давно не обновлялся. Лоты не включаются "
                    "обратно, пока цены не подтверждены.", 6 * 3600)
    changed = []
    # Per-listing ownership of pauses: never reactivate a listing paused by its owner.
    for lot in list(LOTS.values()):
        if not lot.get("enabled") or not _binding_valid(lot):
            continue
        lid = int(lot["binding"]["lot_id"])
        pause = protection_decision(lot, best_balance, threshold_hit, prices, fallback, catalog_fresh)
        if pause is True and not lot.get("paused_by_autogift"):
            if await asyncio.to_thread(lot_is_active, lid) is True and await asyncio.to_thread(set_lot_active, lid, False):
                lot["paused_by_autogift"] = True
                lot["pause_reason"] = "Недостаточно Stars на одном аккаунте / не подтверждена доступность Gifts"
                _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
                changed.append(f"⏸ {lot.get('name', lid)}")
        elif pause is False and lot.get("paused_by_autogift") and CONFIG.get("auto_reactivate", True):
            if await asyncio.to_thread(set_lot_active, lid, True):
                lot["paused_by_autogift"] = False
                lot.pop("capacity_required", None)
                lot.pop("pause_reason", None)
                _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
                changed.append(f"▶️ {lot.get('name', lid)}")
    operational = any(lot.get("enabled") and not lot.get("paused_by_autogift") for lot in LOTS.values())
    if CONFIG.get("lots_operational") != operational:
        CONFIG["lots_operational"] = operational
        _safe_json_save(CONFIG_PATH, CONFIG)
    if changed:
        notify_operators("🛡️ AutoGift: изменение доступности лотов\n" + h("\n".join(changed)[:2500])
                         + f"\nМаксимальный баланс аккаунта: {format_stars(best_balance)}")


# ---------------------------------------------------------------------------
# AUTHENTICATION WIZARD
# ---------------------------------------------------------------------------

AUTH_TIMEOUT = 300
AUTH_ERRORS = {
    "PHONE_NUMBER_INVALID": "Номер телефона некорректен. Проверьте формат: +79991234567.",
    "PHONE_NUMBER_BANNED": "Этот номер заблокирован в Telegram.",
    "PHONE_NUMBER_FLOOD": "Слишком много попыток входа с этим номером. Попробуйте позже.",
    "PHONE_CODE_EXPIRED": "Код истёк. Начните добавление аккаунта заново и введите новый код через пробелы.",
    "API_ID_INVALID": "API ID / API Hash неверны. Проверьте их в настройках.",
    "API_ID_PUBLISHED_FLOOD": "Эти API ID / Hash заблокированы Telegram. Создайте новое приложение на my.telegram.org.",
}


def _auth_error_text(exc: BaseException) -> str:
    rpc = _rpc_id(exc)
    if rpc.startswith("FLOOD_WAIT") or type(exc).__name__ == "FloodWait":
        return f"Слишком много попыток. Подождите {int(getattr(exc, 'value', 0) or 0)} сек. и повторите."
    if rpc in AUTH_ERRORS:
        return AUTH_ERRORS[rpc]
    if isinstance(exc, CONNECTION_ERRORS):
        return "Не удалось подключиться к Telegram. Проверьте прокси и API ID/Hash."
    return f"{type(exc).__name__}: {exc}"


def _auth_code_digits(raw: str) -> str:
    return re.sub(r"\D", "", str(raw or ""))


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
        kwargs = dict(name=session_name, api_id=api_id, api_hash=api_hash, proxy=proxy,
                      workdir=str(STORAGE / "tg_workdir"), in_memory=True,
                      device_model=DEVICE_MODEL, app_version=f"AutoGift {VERSION}")
        try:
            app = Client(**kwargs)
        except TypeError:
            kwargs.pop("device_model"); kwargs.pop("app_version")
            app = Client(**kwargs)
        await asyncio.wait_for(app.connect(), 40)
        try:
            sent = await app.send_code(phone)
            _auth_output(user_id, {"kind": "code"})
            signed = False
            for attempt in range(1, 4):
                raw_code = str(await asyncio.to_thread(_auth_input_wait, user_id, AUTH_TIMEOUT)).strip()
                if raw_code.lower() == "/cancel":
                    raise RuntimeError("Авторизация отменена.")
                code = _auth_code_digits(raw_code)
                if not 5 <= len(code) <= 6:
                    _auth_output(user_id, {"kind": "code_retry", "error": "Код должен состоять из 5 цифр."})
                    continue
                try:
                    result = await app.sign_in(phone_number=phone, phone_code_hash=sent.phone_code_hash, phone_code=code)
                    if result is False or type(result).__name__ == "TermsOfService":
                        raise RuntimeError("Этот номер не зарегистрирован в Telegram. Сначала создайте аккаунт в приложении.")
                    signed = True
                    break
                except Exception as exc:
                    rpc = _rpc_id(exc)
                    if rpc == "SESSION_PASSWORD_NEEDED" or type(exc).__name__ == "SessionPasswordNeeded":
                        await _auth_password_flow(app, user_id)
                        signed = True
                        break
                    if rpc in {"PHONE_CODE_INVALID", "PHONE_CODE_EMPTY"} and attempt < 3:
                        _auth_output(user_id, {"kind": "code_retry", "error": "Неверный код."})
                        continue
                    raise
            if not signed:
                raise RuntimeError("Слишком много неверных попыток. Начните добавление заново.")
            me = await app.get_me()
            if not is_operator(user_id):
                raise RuntimeError("Доступ оператора был отозван; добавление аккаунта отменено.")
            session_string = await app.export_session_string()
            data = store_account(me, session_string, phone)
            _auth_output(user_id, {"kind": "done", "account_id": data["id"], "name": data["label"],
                                   "username": getattr(me, "username", None), "phone": phone})
        finally:
            try:
                await app.disconnect()
            except Exception:
                pass

    try:
        asyncio.run(flow())
    except pyqueue.Empty:
        _auth_output(user_id, {"kind": "error", "error": "⏱ Время ожидания истекло. Начните добавление заново."})
    except Exception as exc:
        logger.error("%s auth worker %s failed: %s", LOGGER_PREFIX, user_id, exc)
        _auth_output(user_id, {"kind": "error", "error": _auth_error_text(exc)})


async def _auth_password_flow(app, user_id: int) -> None:
    _auth_output(user_id, {"kind": "password"})
    for attempt in range(1, 4):
        password = str(await asyncio.to_thread(_auth_input_wait, user_id, AUTH_TIMEOUT))
        if password.strip().lower() == "/cancel":
            raise RuntimeError("Авторизация отменена.")
        try:
            await app.check_password(password)
            return
        except Exception as exc:
            if _rpc_id(exc) == "PASSWORD_HASH_INVALID" and attempt < 3:
                _auth_output(user_id, {"kind": "password_retry"})
                continue
            raise
    raise RuntimeError("Неверный облачный пароль.")


def store_account(me: Any, session_string: str, phone: str = "") -> Dict[str, Any]:
    """Add or refresh one Telegram account; the same user never appears twice."""
    account_id = f"tg_{int(me.id)}"
    existing = next((a for a in ACCOUNTS if a.get("id") == account_id), None)
    data = DEFAULT_ACCOUNT.copy()
    name = ((getattr(me, "first_name", None) or "") + (" " + me.last_name if getattr(me, "last_name", None) else "")).strip()
    username = getattr(me, "username", None)
    data.update({
        "id": account_id,
        "label": (name or account_id) + (f" (@{username})" if username else ""),
        "phone": phone or (existing or {}).get("phone", ""),
        "session_string": session_string,
        "active": True,
        "balance": None,
        "last_error": "",
    })
    if existing:
        for counter in ("gifts_sent", "spent_stars", "last_used"):
            data[counter] = existing.get(counter, data[counter])
        existing.clear()
        existing.update(data)
    else:
        ACCOUNTS.append(data)
    _safe_json_save(ACCOUNTS_PATH, ACCOUNTS)
    BALANCE_REFRESH_REQUEST.set()
    return existing or data


async def probe_session_string(session_string: str):
    """Log in with a Session String through the configured proxy and return the user."""
    temp = dict(DEFAULT_ACCOUNT, id="probe_" + secrets.token_hex(4), session_string=session_string)
    app = _make_account_client(temp)
    authorized = await asyncio.wait_for(app.connect(), 40)
    try:
        if not authorized:
            raise PreflightError("Session String не авторизована.")
        return await app.get_me()
    finally:
        try:
            await app.disconnect()
        except Exception:
            pass


def add_session_worker(chat_id: int, session_string: str) -> None:
    try:
        me = run_coro(probe_session_string(session_string), timeout=90)
        data = store_account(me, session_string)
        RUNTIME.drop_sync(data["id"])
        BOT.send_message(chat_id, f"✅ Аккаунт <b>{h(data['label'])}</b> добавлен.\n"
                                  "🗑 Удалите сообщение с Session String из чата, если оно осталось.",
                         parse_mode="HTML", reply_markup=back_keyboard("ag:accounts"))
    except Exception as exc:
        BOT.send_message(chat_id, f"❌ Не удалось войти по Session String:\n<code>{h(_auth_error_text(exc))[:800]}</code>",
                         parse_mode="HTML", reply_markup=back_keyboard("ag:accounts"))


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
        if row.get("status") in TERMINAL_STATUSES:
            continue
        if not isinstance(row.get("delivery", []), list) or not all(isinstance(x, dict) for x in row.get("delivery", [])):
            raise _storage_failed(ORDERS_PATH, ValueError("Malformed delivery ledger"))
        if any(x.get("status") == "sending" for x in row.get("delivery", [])):
            # The payment request may have reached Telegram: never resend automatically.
            for item in row["delivery"]:
                if item.get("status") == "sending":
                    item["status"] = "unknown"
            row["status"] = "needs_review"
            row["last_error"] = "Перезапуск во время оплаты подарка. Проверьте историю Telegram и отметьте результат."
            continue
        if not row.get("binding_token") and row.get("status") != "needs_review":
            row["status"] = "needs_review"
            row["last_error"] = "Старый заказ без подтверждённой привязки; автоматическая выдача запрещена."
            continue
        if row.get("status") == "needs_review":
            continue
        if not _ledger_valid(row):
            row["status"] = "needs_review"
            row["last_error"] = "Незавершённая операция или старый журнал; проверьте историю вручную."
        elif row.get("status") == "refunding":
            row["status"] = "needs_review"
            row["last_error"] = "Перезапуск во время возврата. Проверьте статус заказа на FunPay."
        elif row.get("status") in {"ready", "sending"}:
            # Every item is pending/sent: nothing was in flight, so the queue can resume.
            if _delivery_consent_valid(row):
                row["status"] = "ready"
                row["resume_after_restart"] = True
            else:
                row["status"] = "needs_review"
                row["last_error"] = "Подтверждение получателя устарело во время перезапуска."
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


def match_order_to_lot(order: Any, approved_fingerprint: Optional[str] = None
                       ) -> Tuple[Optional[Tuple[str, Dict[str, Any]]], str]:
    """Authenticate a seller-controlled purchase snapshot, never buyer/chat text.

    Returns (match, reason). reason is "ok", "foreign" (not an AutoGift order),
    "not_paid", "disabled" (its lot has delivery switched off), "binding"
    (carries a lot tag but does not match its binding) or "error".
    """
    account = getattr(CARDINAL_INSTANCE, "account", None)
    if account is None or str(getattr(order, "seller_id", "")) != str(account.id):
        return None, "foreign"
    description = getattr(order, "full_description", None)
    title = getattr(order, "short_description", None) or getattr(order, "title", None)
    tokens = MARKER_RE.findall(str(description or ""))
    if len(tokens) != 1 or not title:
        return None, "foreign"
    tagged = [(k, v) for k, v in LOTS.items() if isinstance(v, dict)
              and tokens[0] in {(v.get("binding") or {}).get("token"), v.get("pending_token")}]
    if not tagged:
        return None, "foreign"
    paid = getattr(enums.OrderStatuses, "PAID", None)
    if paid is None or getattr(order, "status", None) != paid:
        return None, "not_paid"
    if not any(v.get("enabled") and _binding_valid(v) for _, v in tagged):
        return None, "disabled"
    ids = _explicit_lot_ids(order)
    fingerprint = _fingerprint(title, description)
    sub_id = str(getattr(getattr(order, "subcategory", None), "id", ""))
    candidates = []
    for key, lot in tagged:
        if not lot.get("enabled") or not _binding_valid(lot):
            continue
        b = lot["binding"]
        allowed = set(b["fingerprints"].values()) | ({approved_fingerprint} if approved_fingerprint else set())
        if (tokens[0] != b["token"] or str(b["seller_id"]) != str(account.id)
                or sub_id != b["subcategory_id"] or fingerprint not in allowed):
            continue
        if ids and ids != {str(b["lot_id"])}:
            continue
        candidates.append((key, lot))
    if len(candidates) != 1:
        return None, "binding"
    key, lot = candidates[0]
    # Also detect repurposed/replaced live listings before taking ownership.
    try:
        lf = account.get_lot_fields(int(lot["binding"]["lot_id"]))
        if not _live_binding_matches(lf, lot):
            return None, "binding"
    except Exception:
        logger.warning("%s ownership check unavailable for #%s", LOGGER_PREFIX, getattr(order, "id", "?"))
        return None, "error"
    return (key, lot), "ok"


def tagged_lot_of_order(order: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
    """The lot whose AutoGift tag the purchase carries, even if the binding check failed."""
    tokens = MARKER_RE.findall(str(getattr(order, "full_description", "") or ""))
    if len(tokens) != 1:
        return None
    return next(((k, v) for k, v in LOTS.items() if isinstance(v, dict)
                 and tokens[0] in {(v.get("binding") or {}).get("token"), v.get("pending_token")}), None)


def _call_with_retry(func, *args, attempts: int = 3, delay: float = 1.5):
    for attempt in range(1, attempts + 1):
        try:
            return func(*args)
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(delay * attempt)


def order_ownership(c: Cardinal, data: Dict[str, Any]) -> str:
    """"ok", "refunded", "closed", "binding", "mismatch" or "error" (FunPay unreachable)."""
    if STORAGE_ERROR or not data.get("binding_token") or not data.get("order_id"):
        return "mismatch"
    try:
        current = _call_with_retry(c.account.get_order, data["order_id"])
    except Exception:
        logger.warning("%s ownership check for #%s unavailable", LOGGER_PREFIX, data.get("order_id"))
        return "error"
    status = getattr(current, "status", None)
    if status == getattr(enums.OrderStatuses, "REFUNDED", object()):
        return "refunded"
    if status == getattr(enums.OrderStatuses, "CLOSED", object()):
        return "closed"
    found, reason = match_order_to_lot(current, data.get("approved_fingerprint"))
    if not found:
        return reason if reason in {"error", "disabled"} else "binding"
    key, lot = found
    try:
        same = (str(current.id).upper() == str(data["order_id"]).upper()
                and str(key) == str(data.get("lot_key"))
                and lot["binding"]["token"] == data["binding_token"]
                and str(getattr(current, "buyer_id", "")) == str(data.get("buyer_id"))
                and lot.get("gift_ids") == data.get("gift_ids")
                and _order_quantity(current) == data.get("units"))
        if not same:
            return "mismatch"
        return "ok" if _call_with_retry(_order_chat_id, c, current) == str(data.get("chat_id")) else "mismatch"
    except Exception:
        return "error"


OWNERSHIP_TEXT = {
    "refunded": "заказ возвращён на FunPay",
    "closed": "заказ уже закрыт на FunPay",
    "binding": "описание/тег лота изменились — проверьте привязку («Проверить тег»)",
    "mismatch": "данные заказа на FunPay не совпадают с журналом AutoGift",
    "disabled": "выдача по этому лоту выключена в AutoGift",
    "error": "FunPay не ответил",
}


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
    # A re-added lot keeps its old tag, so the listing does not need a new one.
    lot.setdefault("pending_token", (lot.get("binding") or {}).get("token") or secrets.token_hex(16))
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


async def validate_username(username: str) -> Tuple[str, str, Optional[int]]:
    """("ok"|"not_found"|"unsupported"|"error", display name or reason, Telegram ID).

    Проверка получателя не должна ломаться только потому, что на аккаунте 0 Stars.
    """
    errors = []
    for account in get_telegram_accounts():
        try:
            async with account_client(account) as app:
                user = await app.get_chat(username)
            typ = getattr(user, "type", None)
            if ChatType is not None and typ in (ChatType.PRIVATE, ChatType.CHANNEL):
                name = getattr(user, "first_name", None) or getattr(user, "title", None) or username
                return "ok", str(name), int(user.id)
            return "unsupported", "Это бот или группа — подарок можно отправить только пользователю или каналу.", None
        except Exception as exc:
            rpc = _rpc_id(exc)
            if rpc in {"USERNAME_NOT_OCCUPIED", "USERNAME_INVALID"} or type(exc).__name__ in {"UsernameNotOccupied", "UsernameInvalid"}:
                return "not_found", "Пользователь не найден", None
            _apply_account_cooldown(account, exc)
            errors.append(f"{account.get('id')}: {type(exc).__name__}")
            logger.warning("%s username %s failed on %s: %s", LOGGER_PREFIX, username, account.get("id"), exc)
    return "error", "; ".join(errors)[:300] or "Нет подключённых Telegram-аккаунтов", None


def save_order(order: Dict[str, Any]) -> None:
    with ORDER_LOCK:
        order["updated_at"] = time.time()
        ORDERS[str(order["order_id"])] = order
        _safe_json_save(ORDERS_PATH, ORDERS)


async def execute_order(c: Cardinal, data: Dict[str, Any]) -> str:
    try:
        return await _execute_order_impl(c, data)
    finally:
        # Re-check availability immediately after spending, as well as periodically.
        try:
            await update_lot_protection()
        except Exception:
            logger.exception("%s listing protection needs attention", LOGGER_PREFIX)
            notify_operators("🟠 AutoGift: не удалось обновить защиту лотов; проверьте их активность.")


def _pick_sender(required: int, attempted: set) -> Optional[Dict[str, Any]]:
    # One account covers the entire remaining set. Telegram cannot pool a single payment.
    return next((a for a in sorted(get_active_accounts(), key=lambda a: ts(a.get("last_used")))
                 if str(a["id"]) not in attempted and int(a["balance"]) >= required), None)


async def _execute_order_impl(c: Cardinal, data: Dict[str, Any]) -> str:
    if (not RUNNING or STORAGE_ERROR or data.get("status") not in {"ready", "sending"}
            or not _delivery_consent_valid(data) or not _ledger_valid(data)):
        data["last_error"] = "Заказ, количество, привязка, журнал или подтверждение не прошли проверку."
        save_order(data)
        return "needs_review"
    ownership = await asyncio.to_thread(order_ownership, c, data)
    if ownership != "ok":
        data["last_error"] = "Выдача не начата: " + OWNERSHIP_TEXT.get(ownership, ownership)
        data["ownership"] = ownership
        save_order(data)
        return "failed_no_delivery" if ownership in {"error"} and not delivered_count(data) else "needs_review"
    items = data["delivery"]
    if any(i["status"] not in {"pending", "sent"} for i in items):
        data["last_error"] = "Предыдущая попытка требует ручной проверки."
        save_order(data)
        return "needs_review"
    if not any(i["status"] == "pending" for i in items):
        data.update(status="completed", completed_at=time.time())
        save_order(data)
        return "completed"
    try:
        await refresh_catalog_for_menu(0)
    except Exception as exc:
        logger.warning("%s catalog refresh before order failed: %s", LOGGER_PREFIX, exc)
    prices = catalog_prices(120)
    for gid in {i["gift_id"] for i in items if i["status"] == "pending"}:
        if prices.get(gid, 0) <= 0:
            data["last_error"] = f"Gift {gid} недоступен или его цена не подтверждена Telegram."
            save_order(data)
            return "needs_review" if delivered_count(data) else "failed_no_delivery"
    pending_cost = lambda: sum(prices[i["gift_id"]] for i in items if i["status"] == "pending")
    data["required_stars"] = pending_cost() + sum(int(i.get("price", 0)) for i in items if i["status"] == "sent")
    if data["required_stars"] > _positive_int(CONFIG.get("max_order_stars", 10000)):
        data["last_error"] = "Превышен лимит Stars на заказ."
        save_order(data)
        return "needs_review"
    await refresh_all_balances(notify=False, protect=False)
    attempted: set = set()
    last_check = time.time()
    key = None
    try:
        while any(i["status"] == "pending" for i in items):
            remaining = pending_cost()
            account = _pick_sender(remaining, attempted)
            if account is None:
                data["last_error"] = f"Для оставшихся Gifts нужно {remaining} Stars на одном доступном аккаунте."
                lot = LOTS.get(data.get("lot_key"))
                if lot and lot.get("binding", {}).get("token") == data.get("binding_token"):
                    lot["capacity_required"] = data["required_stars"]
                    _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
                save_order(data)
                return "needs_review" if delivered_count(data) else "failed_no_delivery"
            key = str(account["id"])
            ACCOUNT_RESERVATIONS[key] = remaining
            item: Optional[Dict[str, Any]] = None
            submitted = False
            try:
                # One connection and one username lookup for the whole order on this account.
                async with account_client(account) as app:
                    await verify_recipient(app, data["username"], data["recipient_id"])
                    for item in [i for i in items if i["status"] == "pending"]:
                        submitted = False
                        if time.time() - last_check > OWNERSHIP_RECHECK:
                            state = await asyncio.to_thread(order_ownership, c, data)
                            if state != "ok":
                                raise PreflightError("Выдача остановлена: " + OWNERSHIP_TEXT.get(state, state))
                            last_check = time.time()

                        def before_submit(form_id, item=item):
                            nonlocal submitted
                            if (not RUNNING or STORAGE_ERROR or not account.get("active")
                                    or not any(a is account for a in ACCOUNTS)
                                    or not _delivery_consent_valid(data)):
                                raise PreflightError("Выдача остановлена: изменились получатель, доступ или состояние плагина.")
                            item.update(status="sending", account_id=key, started_at=time.time(),
                                        price=prices[item["gift_id"]], form_id=form_id)
                            data["status"] = "sending"
                            save_order(data)  # Must succeed BEFORE any Stars payment.
                            submitted = True

                        receipt = await send_gift_raw(app, int(data["recipient_id"]), item["gift_id"],
                                                      bool(data.get("hide_name", True)), data.get("comment") or "",
                                                      prices[item["gift_id"]], before_submit)
                        if not submitted or not isinstance(receipt, dict) or receipt.get("result_type") != "PaymentResult":
                            raise RuntimeError("Отправитель не вернул подтверждение платежа.")
                        item.update(status="sent", sent_at=time.time(), receipt=receipt)
                        save_order(data)  # A later stats/UI failure never resends this gift.
                        account["balance"] = max(0, int(account.get("balance") or 0) - prices[item["gift_id"]])
                        account["gifts_sent"] = int(account.get("gifts_sent") or 0) + 1
                        account["spent_stars"] = int(account.get("spent_stars") or 0) + prices[item["gift_id"]]
                        account["last_used"] = time.time()
                        _safe_json_save(ACCOUNTS_PATH, ACCOUNTS)
                        if any(i["status"] == "pending" for i in items):
                            await asyncio.sleep(GIFT_SEND_DELAY)
            except StorageError:
                raise
            except Exception as exc:
                if _apply_account_cooldown(account, exc):
                    # FloodWait / BALANCE_TOO_LOW explicitly reject this request.
                    # A timeout/unknown result never enters this retry path.
                    if item is not None and item["status"] in {"pending", "sending"}:
                        item.setdefault("attempts", []).append({"account_id": key,
                            "form_id": item.get("form_id") if submitted else None,
                            "status": "rejected", "error": type(exc).__name__, "at": time.time()})
                        item["status"] = "pending"
                    save_order(data)
                    _safe_json_save(ACCOUNTS_PATH, ACCOUNTS)
                    attempted.add(key)
                    ACCOUNT_RESERVATIONS.pop(key, None)
                    continue
                if not submitted and isinstance(exc, CONNECTION_ERRORS):
                    # Nothing was paid on this account: try the next suitable sender.
                    account["last_error"] = f"{type(exc).__name__}: {exc}"[:300]
                    attempted.add(key)
                    ACCOUNT_RESERVATIONS.pop(key, None)
                    continue
                definitive = not submitted or is_definitive_delivery_error(exc)
                error = f"{type(exc).__name__}: {exc}"[:700]
                if item is not None and item["status"] in {"pending", "sending"}:
                    item.update(status="failed" if definitive else "unknown", error=error)
                data.update(status="needs_review" if not definitive or delivered_count(data) else "failed",
                            last_error=error)
                save_order(data)
                return "failed_no_delivery" if data["status"] == "failed" else "needs_review"
            finally:
                ACCOUNT_RESERVATIONS.pop(key, None)
        data.update(status="completed", completed_at=time.time(), last_error="")
        save_order(data)
        return "completed"
    finally:
        if key:
            ACCOUNT_RESERVATIONS.pop(key, None)


# ---------------------------------------------------------------------------
# WORKERS: events, delivery queue, periodic checks
# ---------------------------------------------------------------------------

def submit_event(func, *args) -> None:
    """Hand work to the AutoGift event thread; Cardinal's thread returns at once."""
    EVENT_QUEUE.put((func, args))


def events_worker() -> None:
    while True:
        func, args = EVENT_QUEUE.get()
        try:
            func(*args)
        except StorageError:
            logger.exception("%s storage failure in event", LOGGER_PREFIX)
        except Exception:
            logger.exception("%s event %s failed", LOGGER_PREFIX, getattr(func, "__name__", func))


def enqueue_delivery(order_id: str, chat_id: str) -> int:
    """Put a confirmed order in the delivery queue; returns its position (1 = next)."""
    with QUEUE_LOCK:
        if order_id not in QUEUED_ORDERS:
            QUEUED_ORDERS.add(order_id)
            DELIVERY_QUEUE.put((order_id, str(chat_id)))
        return len(QUEUED_ORDERS)


def delivery_worker() -> None:
    while True:
        order_id, chat_id = DELIVERY_QUEUE.get()
        try:
            data = ORDERS.get(order_id)
            if data is not None and CARDINAL_INSTANCE is not None:
                _execute_customer_order(CARDINAL_INSTANCE, data, chat_id)
        except Exception:
            logger.exception("%s delivery worker failed for #%s", LOGGER_PREFIX, order_id)
        finally:
            with QUEUE_LOCK:
                QUEUED_ORDERS.discard(order_id)


def resume_ready_orders() -> None:
    """Orders confirmed before a restart, with nothing in flight, continue in the queue."""
    with ORDER_LOCK:
        rows = [row for row in ORDERS.values() if row.get("status") == "ready"]
    for row in sorted(rows, key=lambda r: ts(r.get("created_at"))):
        enqueue_delivery(str(row["order_id"]), str(row.get("chat_id")))


def scan_paid_orders(c: Cardinal) -> int:
    """Backfill: paid orders that arrived while AutoGift was off, restarting or FunPay failed."""
    if not RUNNING or STORAGE_ERROR or not any(l.get("enabled") for l in LOTS.values()):
        return 0
    since = max(ts(CONFIG.get("scan_since")), time.time() - SCAN_MAX_AGE)
    result = c.account.get_sales(include_paid=True, include_closed=False, include_refunded=False)
    orders = result[1] if isinstance(result, (tuple, list)) and len(result) >= 2 else []
    queued = 0
    for order in orders:
        oid = str(getattr(order, "id", "") or "").upper()
        if (not oid or oid in ORDERS or oid in SEEN_ORDERS
                or getattr(order, "status", None) != getattr(enums.OrderStatuses, "PAID", None)):
            continue
        date = getattr(order, "date", None)
        if not isinstance(date, datetime) or date.timestamp() < since:
            continue  # Unknown age or older than the window: never auto-start.
        submit_event(accept_paid_order, c, oid, None, "scan")
        queued += 1
    return queued


def background_worker() -> None:
    last_balance = last_scan = last_reminder = last_archive = 0.0
    first_scan_at = time.time() + 30
    while True:
        time.sleep(3)
        if not RUNNING or STORAGE_ERROR:
            continue
        now = time.time()
        if now - last_balance >= BALANCE_INTERVAL or BALANCE_REFRESH_REQUEST.is_set():
            BALANCE_REFRESH_REQUEST.clear()
            last_balance = now
            try:
                run_coro(refresh_all_balances(notify=True, skip_busy=True), timeout=300)
            except Exception as exc:
                logger.error("%s background balance error: %s", LOGGER_PREFIX, exc)
        if now >= first_scan_at and (now - last_scan >= SCAN_INTERVAL or SCAN_REQUEST.is_set()):
            SCAN_REQUEST.clear()
            last_scan = now
            try:
                scan_paid_orders(CARDINAL_INSTANCE)
            except Exception as exc:
                logger.warning("%s paid-order scan failed: %s", LOGGER_PREFIX, exc)
        if now - last_reminder >= 60:
            last_reminder = now
            try:
                send_buyer_reminders()
            except Exception:
                logger.exception("%s reminders failed", LOGGER_PREFIX)
        if now - last_archive >= 86400:
            last_archive = now
            try:
                archive_old_orders()
                with FILE_LOCK:
                    save_seen()
            except Exception:
                logger.exception("%s archive failed", LOGGER_PREFIX)


# ---------------------------------------------------------------------------
# TELEGRAM UI
# ---------------------------------------------------------------------------

def main_text() -> str:
    if not INITIALIZED or not DEPENDENCY_READY or STARTUP_ERROR or STORAGE_ERROR:
        return _recovery_text()
    today = order_stats(1)
    review = sum(1 for row in list(ORDERS.values()) if row.get("status") in REVIEW_STATUSES)
    active = len(get_active_accounts())
    bound = sum(_binding_valid(lot) for lot in LOTS.values())
    paused = sum(bool(lot.get("paused_by_autogift")) for lot in LOTS.values())
    broken = sum(bool(a.get("auth_failed")) for a in ACCOUNTS)
    queued = len(QUEUED_ORDERS)
    return (f"<b>🎁 AutoGift</b> <code>v{VERSION}</code>\n"
            f"{'🟢 Выдача включена' if RUNNING else '🔴 Выдача выключена'}\n\n"
            "<b>Аккаунты и лоты</b>\n"
            f"👥 Доступно: {active}/{len(ACCOUNTS)} · 🌟 {format_stars(total_balance())}\n"
            + (f"⛔ Сессий требуют повторного входа: {broken}\n" if broken else "")
            + f"📦 Привязано: {bound}/{len(LOTS)} · ⏸ Отключено защитой: {paused}\n\n"
            "<b>Сегодня</b>\n"
            f"✅ Заказов: {len(today['completed'])} · 🎁 Подарков: {today['delivered']}\n"
            f"💳 Выручка: {format_money(today['revenue'])} · 🟠 На проверке: {review}\n"
            + (f"⏳ В очереди на выдачу: {queued}\n" if queued else "")
            + f"\nАвтор: <a href='{AUTHOR_URL}'>{CREDITS}</a> · <a href='{CHANNEL_URL}'>Канал</a>")

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
    label = "🏠 Главное меню" if target == "ag:main" else "⬅️ Назад"
    return InlineKeyboardMarkup().add(InlineKeyboardButton(label, callback_data=target))


def cancel_keyboard(target="ag:main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup().add(InlineKeyboardButton("🚫 Отмена", callback_data="ag:cancel:" + target))


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
        "<b>2. API и прокси.</b> Укажите API ID и API Hash с <code>my.telegram.org</code> и прокси, затем добавьте Telegram-аккаунт по номеру или Session String. "
        "Код из Telegram вводите <b>через пробелы</b>: <code>1 2 3 4 5</code>.\n\n"
        "<b>3. Каталог и лоты.</b> Добавьте лот по FunPay ID и вставьте один или несколько Gift ID из каталога (каждый с новой строки). Набор отправляется целиком за одну единицу заказа.\n\n"
        "<b>4. Привязка.</b> Нажмите «Получить тег», вставьте его в конец подробного описания лота на FunPay и нажмите «Проверить тег». "
        "Или «Добавить автоматически». После правки описания лота проверьте тег заново — AutoGift пришлёт уведомление, если заказ не совпал с привязкой.\n\n"
        "<b>5. Покупатель.</b> После оплаты бот просит Telegram-ник. Принимаются <code>@username</code>, <code>t.me/username</code> и просто <code>username</code>. "
        "Бот показывает получателя, покупатель отвечает <code>+</code> (или присылает другой ник). Заказы выдаются по очереди. "
        "Возврат до отправки: <code>!вз</code>, <code>!бэк</code> или <code>!refund</code>. При нескольких заказах в одном чате — <code>+ НОМЕР</code>, <code>!gift НОМЕР @username</code>.\n\n"
        "<b>6. Сбои.</b> Каждый Gift записывается отдельно. Если Telegram не подтвердил итог оплаты или часть набора отправлена, заказ попадёт в «🟠 Проверка» без повторной выдачи. "
        "Там можно отметить результат, продолжить выдачу или вернуть деньги.\n\n"
        "<b>7. Баланс.</b> Балансы проверяются каждые 3 минуты и после каждого заказа. Лот отключается, если ни один аккаунт не покрывает набор или порог. "
        "Короткий сбой прокси лоты не отключает. Пополнение включает только лоты, выключенные AutoGift.\n\n"
        "<b>8. Пропущенные заказы.</b> Заказы, оплаченные во время выключения или перезапуска (до 48 ч), подхватываются автоматически.\n\n"
        f"Автор: <a href='{AUTHOR_URL}'>{CREDITS}</a> · <a href='{CHANNEL_URL}'>Канал LuckyFunPay</a>"
    )

def onoff(value: Any) -> str:
    return "✅" if value else "❌"


def settings_text() -> str:
    return (
        "<b>⚙️ Настройки AutoGift</b>\n\n"
        f"🔑 API: {'✅ задан' if CONFIG.get('api_id') and CONFIG.get('api_hash') else '❌ не задан'}\n"
        f"🌐 Proxy: {'✅ задан' if CONFIG.get('proxy') else '➖ нет'}{' (обязателен)' if CONFIG.get('proxy_required') else ''}\n"
        f"↩️ Автовозврат при нулевой выдаче: {onoff(CONFIG.get('auto_refund_when_no_delivery'))}\n"
        f"🙈 Скрывать имя отправителя: {onoff(CONFIG.get('hide_name'))}\n"
        f"💬 Текст к подарку: {onoff(CONFIG.get('comment_enabled'))}\n"
        f"👤 Подтверждение получателя: {onoff(CONFIG.get('require_confirmation', True))}\n"
        f"🔔 Напоминание покупателю: {onoff(CONFIG.get('buyer_reminder'))}\n"
        f"💸 Возврат командой !вз: {onoff(CONFIG.get('auto_refunds', True))}\n"
        f"🛡️ Защита лотов: {onoff(CONFIG.get('lot_protection'))} · порог <code>{int(CONFIG.get('low_balance_threshold') or 0)}</code> ⭐\n"
        f"🔢 Лимиты заказа: {CONFIG.get('max_order_units')} шт · {CONFIG.get('max_gifts_per_order')} 🎁 · {CONFIG.get('max_order_stars')} ⭐\n"
        f"💱 Цена Stars: <code>{float(CONFIG.get('star_cost_rub') or 0):.2f} ₽</code> · комиссия: <code>{float(CONFIG.get('funpay_fee_percent') or 0):.1f}%</code>"
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(InlineKeyboardButton("🔑 API Telegram", callback_data="ag:set_api"),
           InlineKeyboardButton("🌐 Proxy", callback_data="ag:set_proxy"))
    kb.row(InlineKeyboardButton("👥 Аккаунты Telegram", callback_data="ag:accounts"),
           InlineKeyboardButton("👤 Покупатель", callback_data="ag:recipient_settings"))
    kb.row(InlineKeyboardButton("💬 Текст к подарку", callback_data="ag:set_comment"),
           InlineKeyboardButton("📝 Сообщения", callback_data="ag:messages"))
    kb.row(InlineKeyboardButton("🛡 Защита баланса", callback_data="ag:protection"),
           InlineKeyboardButton("🔢 Лимиты", callback_data="ag:limits"))
    kb.row(InlineKeyboardButton("💱 Цена Stars", callback_data="ag:set_star_cost"),
           InlineKeyboardButton("💸 Комиссия FunPay", callback_data="ag:set_fee"))
    kb.row(InlineKeyboardButton(f"↩️ Возврат без выдачи {'ВКЛ' if CONFIG.get('auto_refund_when_no_delivery') else 'ВЫКЛ'}", callback_data="ag:toggle_refund"),
           InlineKeyboardButton(f"🙈 Скрывать имя {'ВКЛ' if CONFIG.get('hide_name') else 'ВЫКЛ'}", callback_data="ag:toggle_hide"))
    kb.row(InlineKeyboardButton("🔄 Проверить балансы", callback_data="ag:status"),
           InlineKeyboardButton("🏠 Главное меню", callback_data="ag:main"))
    return kb


def protection_panel() -> Tuple[str, InlineKeyboardMarkup]:
    text = ("<b>🛡 Защита баланса</b>\n\n"
            f"Автоотключение лотов: {'🟢 включено' if CONFIG.get('lot_protection') else '🔴 выключено'}\n"
            f"Порог одного аккаунта: {int(CONFIG.get('low_balance_threshold') or 0)} ⭐\n"
            f"Автовключение после пополнения: {'ВКЛ' if CONFIG.get('auto_reactivate') else 'ВЫКЛ'}\n\n"
            "Проверяется стоимость набора на одном аккаунте. Балансы разных аккаунтов не складываются для оплаты.\n"
            "Порог 0 оставляет проверку стоимости. Возвращаются в продажу только лоты, отключённые AutoGift.\n"
            "Если балансы недоступны (сбой прокси), лоты отключаются только после 3 неудачных проверок подряд.")
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(InlineKeyboardButton("🛡 Автоотключение", callback_data="ag:toggle_protect"),
           InlineKeyboardButton("⭐ Лимит деактивации", callback_data="ag:set_threshold"))
    kb.row(InlineKeyboardButton("🔁 Автовключение", callback_data="ag:toggle_reactivate"),
           InlineKeyboardButton("🌟 Проверить баланс", callback_data="ag:status"))
    kb.row(InlineKeyboardButton("⬅️ Настройки", callback_data="ag:settings"))
    return text, kb


def recipient_panel() -> Tuple[str, InlineKeyboardMarkup]:
    text = ("<b>👤 Покупатель и получатель</b>\n\n"
            f"Username из поля заказа FunPay: {'ВКЛ' if CONFIG.get('username_from_order', True) else 'ВЫКЛ'}\n"
            f"Подтверждение получателя («+»): {'ВКЛ' if CONFIG.get('require_confirmation', True) else 'ВЫКЛ'}\n"
            f"Напоминание через 5 минут: {'ВКЛ' if CONFIG.get('buyer_reminder') else 'ВЫКЛ'}\n"
            f"Возврат командой !вз / !бэк / !refund: {'ВКЛ' if CONFIG.get('auto_refunds', True) else 'ВЫКЛ'}\n\n"
            "С подтверждением покупатель видит имя получателя и отвечает «+». Тогда принимается и ник без @.\n"
            "Без подтверждения выдача начинается сразу после проверки ника; принимаются только @username и t.me-ссылки.")
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(InlineKeyboardButton("👤 Username из заказа", callback_data="ag:toggle_checkout"),
           InlineKeyboardButton("➕ Подтверждение", callback_data="ag:toggle_confirm"))
    kb.row(InlineKeyboardButton("🔔 Напоминание", callback_data="ag:toggle_reminder"),
           InlineKeyboardButton("💸 Возврат командой", callback_data="ag:toggle_refunds"))
    kb.row(InlineKeyboardButton("⬅️ Настройки", callback_data="ag:settings"))
    return text, kb


LIMIT_KEYS = {
    "units": ("max_order_units", "Максимум единиц лота в одном заказе"),
    "gifts": ("max_gifts_per_order", "Максимум подарков в одном заказе"),
    "stars": ("max_order_stars", "Максимум Stars на один заказ"),
}


def limits_panel() -> Tuple[str, InlineKeyboardMarkup]:
    text = ("<b>🔢 Лимиты заказа</b>\n\n"
            f"Единиц лота в заказе: <b>{CONFIG.get('max_order_units')}</b>\n"
            f"Подарков в заказе: <b>{CONFIG.get('max_gifts_per_order')}</b>\n"
            f"Stars на заказ: <b>{CONFIG.get('max_order_stars')}</b>\n\n"
            "Заказ сверх лимита не выдаётся автоматически и попадает в «🟠 Проверка».")
    kb = InlineKeyboardMarkup(row_width=3)
    kb.row(InlineKeyboardButton("📦 Единиц", callback_data="ag:set_limit:units"),
           InlineKeyboardButton("🎁 Подарков", callback_data="ag:set_limit:gifts"),
           InlineKeyboardButton("⭐ Stars", callback_data="ag:set_limit:stars"))
    kb.row(InlineKeyboardButton("⬅️ Настройки", callback_data="ag:settings"))
    return text, kb


def balance_text() -> str:
    text = "<b>🌟 Балансы Telegram-аккаунтов</b>\n\n"
    active = get_active_accounts()
    for account in ACCOUNTS[:20]:
        value = account.get("balance")
        status = "⛔" if account.get("auth_failed") else ("✅" if account in active else "⚠️")
        text += f"{status} {h(account_title(account))}: {format_stars(value) if value is not None else 'ошибка 🔴'}\n"
        if account.get("last_error"):
            text += f"<i>{h(str(account['last_error'])[:100])}</i>\n"
    if not ACCOUNTS:
        text += "❌ Аккаунты не добавлены.\n"
    maximum = max((int(a["balance"]) for a in active), default=0)
    return (text + f"\n📊 Всего: <b>{format_stars(total_balance())}</b>"
            f"\n🏆 На одном аккаунте (для 1 заказа): <b>{format_stars(maximum)}</b>")


async def test_lot_order(key: str, username: str, units: int) -> str:
    """Preflight only: no payment form, gift, buyer message or order-journal entry."""
    lot = copy.deepcopy(LOTS.get(key) or {})
    if not _binding_valid(lot) or not lot.get("enabled"):
        raise ValueError("Привязка лота не подтверждена или выключена.")
    if units > int(CONFIG.get("max_order_units", 10)):
        raise ValueError("Количество превышает лимит заказа.")
    plan = _gift_plan({"units": units, "gift_ids": lot["gift_ids"]})
    fields = await asyncio.to_thread(CARDINAL_INSTANCE.account.get_lot_fields, int(lot["binding"]["lot_id"]))
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
    await refresh_all_balances(notify=False, protect=False)
    candidates = [a for a in get_active_accounts() if int(a.get("balance") or 0) >= required]
    if not candidates:
        raise ValueError(f"Ни один аккаунт не покрывает {required} Stars для всего заказа.")
    status, name, recipient_id = await validate_username(username)
    if status != "ok" or not recipient_id:
        raise ValueError("Telegram не подтвердил получателя: " + name)
    if _gift_signature(LOTS.get(key) or {}) != _gift_signature(lot):
        raise ValueError("Состав лота изменился во время теста.")
    logger.info("%s TEST OK lot=%s units=%s gifts=%s stars=%s recipient_id=%s; no payment submitted",
                LOGGER_PREFIX, lot["binding"]["lot_id"], units, len(plan), required, recipient_id)
    return ("<b>✅ Тестовый заказ: проверка пройдена</b>\n\n"
            f"📦 Лот: {h(lot['name'])} · ID {h(lot['binding']['lot_id'])}\n"
            f"🎁 Наборов: {units} · подарков: {len(plan)} · {format_stars(required)}\n"
            f"👤 @{h(username)} · {h(name)}\n"
            f"👥 Достаточный баланс: {len(candidates)} аккаунт(ов)\n\n"
            f"Выдача сейчас: {'включена' if RUNNING else 'выключена'}\n"
            "Stars не списаны, подарки не отправлены. Реальный заказ дополнительно проходит проверку оплаты FunPay и платёжной формы Telegram.")


def start_test_order(chat_id: int, key: str, username: str, units: int) -> None:
    def worker():
        try:
            text = run_coro(test_lot_order(key, username, units), timeout=180)
        except Exception as exc:
            logger.warning("%s TEST FAILED lot=%s: %s", LOGGER_PREFIX, key, exc)
            text = "<b>❌ Тестовый заказ: проверка не пройдена</b>\n\n" + h(exc) + "\n\nStars не списаны."
        BOT.send_message(chat_id, text, parse_mode="HTML", reply_markup=back_keyboard())
    BOT.send_message(chat_id, "🧪 Проверяю привязку, каталог, баланс и получателя. Результат появится здесь и в журнале.")
    threading.Thread(target=worker, daemon=True, name="AutoGiftTest").start()


TEMPLATE_VARS = {
    "username": "ник получателя без @",
    "tg_user": "имя получателя в Telegram",
    "tg_user_at": "@ник получателя",
    "order_id": "номер заказа FunPay",
    "buyer_id": "ID покупателя на FunPay",
    "units": "количество единиц лота",
    "amount": "то же, что {units}",
    "gift_name": "название первого подарка",
    "gift_types": "сколько разных подарков в лоте",
    "gift_count": "всего подарков в заказе",
    "total_gifts": "то же, что {gift_count}",
    "total_sent": "отправлено подарков",
    "stars": "Stars на заказ",
    "total_stars": "то же, что {stars}",
    "star_cost": "Stars за 1 единицу лота",
    "price": "цена заказа на FunPay",
}
TEMPLATE_SAMPLE = {"order_id": "ABCD1234", "username": "durov", "display_name": "Pavel", "buyer_id": 123456,
                   "units": 2, "gift_ids": [STANDARD_GIFTS_CATALOG[1]["id"]], "gift_names": ["Мишка"],
                   "required_stars": 30, "order_price": 59.0,
                   "delivery": [{"status": "sent", "price": 15}, {"status": "sent", "price": 15}]}


def template_values(data: Dict[str, Any]) -> Dict[str, Any]:
    gift_ids = data.get("gift_ids") or []
    units = int(data.get("units") or 1)
    total = len(data.get("delivery", [])) or units * len(gift_ids)
    username = data.get("username") or ""
    stars = int(data.get("required_stars") or 0) or sum(int(i.get("price") or 0) for i in data.get("delivery", []))
    price = data.get("order_price")
    return {
        "username": username,
        "tg_user": data.get("display_name") or username,
        "tg_user_at": f"@{username}" if username else "",
        "order_id": data.get("order_id", ""),
        "buyer_id": data.get("buyer_id", ""),
        "units": units,
        "amount": units,
        "gift_name": (data.get("gift_names") or ["🎁 Подарок"])[0],
        "gift_types": len(set(gift_ids)),
        "gift_count": total,
        "total_gifts": total,
        "total_sent": delivered_count(data),
        "stars": stars,
        "total_stars": stars,
        "star_cost": stars // units if units else stars,
        "price": (f"{float(price):g}" if isinstance(price, (int, float)) else ""),
    }


def validate_template(template: str) -> Optional[str]:
    """None when the template is valid, otherwise a human-readable error."""
    try:
        fields = [name for _, name, _, _ in string.Formatter().parse(template) if name is not None]
    except ValueError as exc:
        return f"Ошибка в фигурных скобках: {exc}. Чтобы вывести скобку, удвойте её: {{{{ или }}}}."
    unknown = sorted({f.split(".")[0].split("[")[0] for f in fields} - set(TEMPLATE_VARS))
    if any(not f for f in fields):
        return "Пустые фигурные скобки {}. Укажите имя переменной."
    if unknown:
        return "Неизвестные переменные: " + ", ".join("{" + u + "}" for u in unknown)
    try:
        template.format(**template_values(TEMPLATE_SAMPLE))
    except Exception as exc:
        return f"Шаблон не форматируется: {type(exc).__name__}: {exc}"
    return None


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
        "Переменные: " + " ".join(f"<code>{{{k}}}</code>" for k in TEMPLATE_VARS)
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


def _funpay_send(c: Cardinal, chat_id: Any, text: Any) -> Optional[int]:
    """Send plain text to a FunPay chat. Returns the message ID (0 if unknown) or None on failure.

    Cardinal's own send_message (watermark, retries, chat bookkeeping) is used when
    available; FunPayAPI's account.send_message is the fallback for other forks.
    """
    clean_text = _plain_funpay_text(text)
    target = int(chat_id) if str(chat_id).isdigit() else chat_id
    sender = getattr(c, "send_message", None)
    if callable(sender):
        try:
            result = sender(target, clean_text)
        except (TypeError, AttributeError):
            result = NotImplemented
        except Exception as exc:
            logger.error("%s FunPay message send failed to %s: %s", LOGGER_PREFIX, chat_id, exc)
            return None
        if result is not NotImplemented:
            if not result:
                return None
            last = result[-1] if isinstance(result, (list, tuple)) and result else result
            try:
                return int(getattr(last, "id", 0) or 0)
            except (TypeError, ValueError):
                return 0
    try:
        message = c.account.send_message(target, clean_text)
        return int(getattr(message, "id", 0) or 0)
    except Exception as exc:
        logger.error("%s FunPay message send failed to %s: %s", LOGGER_PREFIX, chat_id, exc)
        return None


def send_funpay_message(c: Cardinal, chat_id: Any, text: Any) -> bool:
    """Единая отправка чистого текста в FunPay чат."""
    if _funpay_send(c, chat_id, text) is None:
        notify_once(f"chat_fail:{chat_id}", f"🟠 AutoGift: не удалось отправить сообщение в FunPay-чат {h(chat_id)}. Проверьте заказ.", 600)
        return False
    return True


def send_buyer_reminders():
    if not RUNNING or STORAGE_ERROR or not CONFIG.get("buyer_reminder"):
        return
    with ORDER_LOCK:
        order_ids = [oid for oid, row in ORDERS.items() if row.get("status") in ACTIVE_BUYER_STATUSES
                     and not row.get("reminder_sent_at") and 300 <= time.time() - ts(row.get("created_at")) < 86400]
    for oid in order_ids:
        with order_guard(oid):
            row = ORDERS[oid]
            if not RUNNING or row.get("status") not in ACTIVE_BUYER_STATUSES or row.get("reminder_sent_at"):
                continue
            # Intent first: notification timeouts must not spam every minute.
            row["reminder_sent_at"] = time.time()
            save_order(row)
        if order_ownership(CARDINAL_INSTANCE, row) != "ok":
            continue
        if row.get("status") == "ready_confirm":
            text = (f"⏳ Заказ #{oid}: подтвердите получателя @{row.get('username')} — напишите + "
                    "или пришлите другой @username.")
        else:
            text = format_customer_message(CONFIG.get("reminder_text") or "Напоминание о выдаче подарка.", row)
            text = text[:1500] + f"\nОтправьте @username или ссылку t.me/username. Если заказов несколько: !gift {oid} @username"
        send_funpay_message(CARDINAL_INSTANCE, row["chat_id"], text)


def format_customer_message(template: str, data: Dict[str, Any]) -> str:
    try:
        result = str(template or "").format(**template_values(data))
    except Exception:
        logger.warning("%s template could not be formatted; sent as is", LOGGER_PREFIX)
        result = str(template or "")
    return _plain_funpay_text(result)


def accounts_text() -> str:
    if not ACCOUNTS:
        text = "<b>👥 Telegram-аккаунты</b>\n\n❌ Аккаунты ещё не добавлены."
    else:
        text = f"<b>👥 Telegram-аккаунты (всего: {len(ACCOUNTS)})</b>\n\n"
        active = get_active_accounts()
        for i, a in enumerate(ACCOUNTS, 1):
            status = "⛔" if a.get("auth_failed") else ("🟢" if a in active else "🔴")
            text += (
                f"{status} <b>{i}. {h(account_title(a))}</b>\n"
                f"   📱 {h(masked_phone(a.get('phone','')) or '—')}\n"
                f"   🌟 {format_stars(a.get('balance')) if a.get('balance') is not None else 'не проверен'}\n"
                f"   🎁 Отправлено: {int(a.get('gifts_sent') or 0)} · 💸 {format_stars(a.get('spent_stars') or 0)}\n"
            )
            if a.get("last_error"):
                text += f"   <i>{h(str(a['last_error'])[:90])}</i>\n"
            text += "\n"
        text = text.strip()
    if not CONFIG.get("proxy"):
        text += "\n\n⚠️ Прокси не настроен."
    return text


def accounts_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(InlineKeyboardButton("📱 По номеру", callback_data="ag:add_phone"),
           InlineKeyboardButton("🔑 Session String", callback_data="ag:add_session"))
    for a in ACCOUNTS:
        kb.add(InlineKeyboardButton(f"🗑 Удалить {account_title(a)[:40]}", callback_data=f"ag:del_account:{a['id']}"))
    kb.row(InlineKeyboardButton("🔄 Обновить балансы", callback_data="ag:status"),
           InlineKeyboardButton("⬅️ Настройки", callback_data="ag:settings"))
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


def state_send(chat_id: int, user_id: int, text: str, state: str, back: str = "ag:main") -> None:
    try:
        msg = BOT.send_message(chat_id, text, parse_mode="HTML", reply_markup=cancel_keyboard(back))
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


def prompt_input(call, text: str, state: str, back: str = "ag:main", markup: Any = None) -> None:
    """Edit the menu message into an input prompt with a cancel button."""
    kb = markup or InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🚫 Отмена", callback_data="ag:cancel:" + back))
    BOT.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML", reply_markup=kb)
    set_state(call.message.chat.id, call.from_user.id, state)


def cancel_operation(chat_id: int, user_id: int) -> None:
    if user_id in AUTH_THREADS and AUTH_THREADS[user_id].is_alive():
        _auth_input_put(user_id, "/cancel")
    clear_state(chat_id, user_id)
    TG_WIZARD.pop(user_id, None)
    LOT_WIZARDS.pop(user_id, None)


def message_dispatch(message: Message):
    try:
        _message_dispatch(message)
    except Exception as exc:
        logger.exception("%s Telegram input failed", LOGGER_PREFIX)
        try:
            BOT.send_message(message.chat.id, f"❌ Ошибка: <code>{h(str(exc)[:300])}</code>", parse_mode="HTML",
                             reply_markup=back_keyboard())
        except Exception:
            pass


def parse_gift_ids(text: str) -> Tuple[List[int], List[str]]:
    """Gift IDs one per line (or separated by spaces/commas); other lines are ignored."""
    ids: List[int] = []
    unknown: List[str] = []
    for token in re.findall(r"\d{6,}", text or ""):
        if int(token) in STANDARD_GIFT_IDS:
            ids.append(int(token))
        else:
            unknown.append(token)
    return ids, unknown


def _message_dispatch(message: Message):
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
        cancel_operation(chat_id, user_id)
        BOT.send_message(chat_id, "❌ Операция отменена.", reply_markup=back_keyboard())
        return

    try:
        sd = CARDINAL_INSTANCE.telegram.get_state(chat_id, user_id)
    except Exception:
        sd = None
    state = sd.get("state", "") if isinstance(sd, dict) else ""
    if not state.startswith("autogift_"):
        return

    if state in {"autogift_api_hash", "autogift_auth_code", "autogift_auth_password", "autogift_session",
                 "autogift_proxy", "autogift_auth_phone"}:
        try:
            BOT.delete_message(chat_id, message.message_id)
        except Exception:
            pass

    if state == "autogift_api_id":
        clear_state(chat_id, user_id)
        if not text.isdigit():
            state_send(chat_id, user_id, "❌ API ID должен состоять только из цифр. Введите API ID:", state, "ag:settings")
            return
        CONFIG["api_id"] = int(text)
        _safe_json_save(CONFIG_PATH, CONFIG)
        state_send(chat_id, user_id, "✅ API ID сохранён.\n\nВведите API Hash:", "autogift_api_hash", "ag:settings")
        return

    if state == "autogift_api_hash":
        clear_state(chat_id, user_id)
        if not re.fullmatch(r"[0-9a-fA-F]{32}", text):
            state_send(chat_id, user_id, "❌ API Hash — это 32 символа (цифры и буквы a–f). Введите API Hash:", state, "ag:settings")
            return
        CONFIG["api_hash"] = text
        _safe_json_save(CONFIG_PATH, CONFIG)
        BOT.send_message(chat_id, "✅ API ID/Hash сохранены. Подключения обновятся автоматически.", reply_markup=back_keyboard("ag:settings"))
        return

    if state == "autogift_proxy":
        text = "" if text == "-" else text
        clear_state(chat_id, user_id)
        try:
            parse_proxy(text)
        except Exception as exc:
            state_send(chat_id, user_id, f"❌ {h(exc)}\n\nОтправьте прокси ещё раз:", state, "ag:settings")
            return
        CONFIG["proxy"] = text
        _safe_json_save(CONFIG_PATH, CONFIG)
        BALANCE_REFRESH_REQUEST.set()
        BOT.send_message(chat_id, "✅ Прокси сохранён. Аккаунты переподключатся через него автоматически." if text
                         else "🗑 Прокси удалён.", reply_markup=back_keyboard("ag:settings"))
        return

    if state == "autogift_comment":
        text = "" if text == "-" else text
        clear_state(chat_id, user_id)
        CONFIG["comment"] = text[:128]
        CONFIG["comment_enabled"] = bool(text)
        _safe_json_save(CONFIG_PATH, CONFIG)
        note = " (обрезан до 128 символов)" if len(text) > 128 else ""
        BOT.send_message(chat_id, ("✅ Текст к подарку сохранён" + note) if text else "🗑 Текст к подарку удалён.",
                         reply_markup=back_keyboard("ag:settings"))
        return

    if state == "autogift_test_username":
        username = parse_buyer_username(text, allow_bare=True)
        if not username:
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
        if not text.isdigit():
            state_send(chat_id, user_id, "❌ Укажите целое число Stars.", state, "ag:protection")
            return
        CONFIG["low_balance_threshold"] = int(text)
        _safe_json_save(CONFIG_PATH, CONFIG)
        BALANCE_REFRESH_REQUEST.set()
        BOT.send_message(chat_id, "✅ Порог баланса сохранён.", reply_markup=back_keyboard("ag:protection"))
        return

    if state.startswith("autogift_limit:"):
        kind = state.split(":", 1)[1]
        clear_state(chat_id, user_id)
        if kind not in LIMIT_KEYS:
            return
        try:
            value = _positive_int(text)
        except ValueError:
            state_send(chat_id, user_id, "❌ Укажите целое положительное число.", state, "ag:limits")
            return
        CONFIG[LIMIT_KEYS[kind][0]] = value
        _safe_json_save(CONFIG_PATH, CONFIG)
        BOT.send_message(chat_id, "✅ Лимит сохранён.", reply_markup=back_keyboard("ag:limits"))
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
            state_send(chat_id, user_id, "❌ Укажите число от 0 до 100.", state, "ag:settings")
        return

    if state.startswith("autogift_message:"):
        kind = state.split(":", 1)[1]
        clear_state(chat_id, user_id)
        key = MESSAGE_KEYS.get(kind)
        if not key:
            return
        if text == "-":
            CONFIG[key] = DEFAULT_CONFIG[key]
            _safe_json_save(CONFIG_PATH, CONFIG)
            BOT.send_message(chat_id, "♻️ Шаблон сброшен к стандартному.", reply_markup=back_keyboard("ag:messages"))
            return
        error = validate_template(text)
        if error:
            state_send(chat_id, user_id, f"❌ {h(error)}\n\nОтправьте исправленный текст или «-» для стандартного.",
                       state, "ag:messages")
            return
        CONFIG[key] = text[:3500]
        _safe_json_save(CONFIG_PATH, CONFIG)
        preview = format_customer_message(CONFIG[key], TEMPLATE_SAMPLE)
        BOT.send_message(chat_id, "✅ Шаблон сохранён. Так его увидит покупатель (тестовые данные):\n\n"
                         f"<code>{h(preview[:1500])}</code>", parse_mode="HTML", reply_markup=back_keyboard("ag:messages"))
        return

    if state == "autogift_auth_phone":
        clear_state(chat_id, user_id)
        phone = re.sub(r"[^\d+]", "", text)
        if not re.fullmatch(r"\+?\d{7,15}", phone):
            state_send(chat_id, user_id, "❌ Неверный номер телефона. Пример: +79991234567", state, "ag:accounts")
            return
        if not phone.startswith("+"):
            phone = "+" + phone
        if user_id in AUTH_THREADS and AUTH_THREADS[user_id].is_alive():
            BOT.send_message(chat_id, "⏳ Авторизация уже выполняется. /cancel — отменить.")
            return
        AUTH_INPUT_QUEUES[user_id] = pyqueue.Queue()
        AUTH_OUTPUT_QUEUES[user_id] = pyqueue.Queue()
        thread = threading.Thread(target=auth_worker, args=(user_id, phone), daemon=True, name="AutoGiftAuth")
        AUTH_THREADS[user_id] = thread
        thread.start()
        BOT.send_message(chat_id, f"⏳ Отправляю код на {h(masked_phone(phone))} через ваш прокси…", parse_mode="HTML",
                         reply_markup=cancel_keyboard("ag:accounts"))
        return

    if state in ("autogift_auth_code", "autogift_auth_password"):
        _auth_input_put(user_id, message.text if state == "autogift_auth_password" else text)
        clear_state(chat_id, user_id)
        BOT.send_message(chat_id, "⏳ Проверяю…")
        return

    if state == "autogift_session":
        clear_state(chat_id, user_id)
        session_string = text.strip()
        if len(session_string) < 50:
            state_send(chat_id, user_id, "❌ Session String выглядит слишком короткой. Отправьте её целиком:", state, "ag:accounts")
            return
        BOT.send_message(chat_id, "⏳ Проверяю сессию через ваш прокси…")
        threading.Thread(target=add_session_worker, args=(chat_id, session_string), daemon=True,
                         name="AutoGiftSession").start()
        return

    if state == "autogift_lot_funpay":
        clear_state(chat_id, user_id)
        if not text.isdigit():
            state_send(chat_id, user_id, "❌ FunPay lot ID должен быть числом (цифры из ссылки offer?id=…).", state, "ag:lots")
            return
        lot_id = int(text)
        BOT.send_message(chat_id, "⏳ Получаю информацию о лоте…")
        try:
            lf = CARDINAL_INSTANCE.account.get_lot_fields(lot_id)
            name = (lf.fields.get("fields[summary][ru]") or lf.fields.get("fields[name][ru]")
                    or lf.fields.get("fields[summary][en]") or f"Лот {lot_id}")
        except Exception as exc:
            state_send(chat_id, user_id, f"❌ Не удалось получить лот (проверьте ID):\n<code>{h(exc)}</code>", state, "ag:lots")
            return
        existing = next((k for k, v in LOTS.items() if str(v.get("funpay_lot_id") or v.get("lot_id")) == str(lot_id)), None)
        LOT_WIZARDS[user_id] = {"funpay_lot_id": lot_id, "name": name, "existing": existing}
        state_send(
            chat_id,
            user_id,
            f"✅ Лот найден: <b>{h(name)}</b>\n"
            + ("♻️ Этот лот уже привязан — новые Gift ID заменят старый набор.\n" if existing else "")
            + "\n🎁 <b>Gift ID</b>\n\nОтправьте ID из каталога, каждый с новой строки. "
            "Повторите ID, чтобы отправить подарок несколько раз. Все подарки набора отправляются за одну единицу заказа.",
            "autogift_lot_gifts", "ag:lots",
        )
        return

    if state == "autogift_lot_gifts":
        clear_state(chat_id, user_id)
        wiz = LOT_WIZARDS.get(user_id)
        if not wiz:
            BOT.send_message(chat_id, "Мастер устарел. Откройте «➕ Добавить лот» заново.", reply_markup=back_keyboard("ag:lots"))
            return
        ids, unknown = parse_gift_ids(text)
        if unknown:
            state_send(chat_id, user_id, "❌ Этих ID нет в каталоге AutoGift: <code>" + h(", ".join(map(str, unknown[:10])))
                       + "</code>\nСкопируйте ID в «🎁 Каталог подарков» и отправьте снова.", state, "ag:lots")
            return
        if not ids or len(ids) > 100:
            state_send(chat_id, user_id, "❌ Не найден ни один Gift ID (или их больше 100). Отправьте ID из каталога.", state, "ag:lots")
            return
        cmap = {int(x["id"]): x for x in (CATALOG.get("items") or STANDARD_GIFTS_CATALOG) if str(x.get("id")).isdigit()}
        names = [str(cmap.get(x, {}).get("name") or "🎁 Подарок") for x in ids]
        key = wiz.get("existing") if wiz.get("existing") in LOTS else "lot_" + secrets.token_hex(6)
        lot = LOTS.setdefault(key, {})
        lot.update({
            "name": lot.get("name") or wiz["name"],
            "funpay_lot_id": wiz["funpay_lot_id"],
            "lot_id": wiz["funpay_lot_id"],
            "gift_ids": ids,
            "gift_names": names,
            "gift_id": ids[0],
            "gift_name": names[0],
            "gift_cost": 0,
            "enabled": False,
        })
        _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
        LOT_WIZARDS.pop(user_id, None)
        tag = prepare_manual_tag(key)
        try:
            # The listing may already carry this lot's tag (re-added lot): verify at once.
            confirm_manual_tag(CARDINAL_INSTANCE, key, user_id)
            BOT.send_message(chat_id, f"🎉 Лот привязан: {len(ids)} 🎁 за единицу. Тег в описании уже на месте — выдача включена.",
                             reply_markup=back_keyboard("ag:lots"))
            return
        except Exception:
            pass
        kb = InlineKeyboardMarkup(row_width=2)
        kb.row(InlineKeyboardButton("✅ Проверить тег", callback_data=f"ag:verify_tag:{key}"),
               InlineKeyboardButton("🔐 Добавить автоматически", callback_data=f"ag:bind:{key}"))
        kb.row(InlineKeyboardButton("📦 Открыть лот", callback_data=f"ag:edit_lot:{key}"))
        BOT.send_message(chat_id, f"✅ Лот сохранён ({len(ids)} 🎁 за единицу).\n\nСкопируйте тег в конец подробного описания лота на FunPay, "
                         f"сохраните лот и нажмите «Проверить тег»:\n\n<code>{tag}</code>\n\n"
                         "Если заполнены русский и английский тексты — добавьте тег в оба. Или нажмите «Добавить автоматически». "
                         "Пока тег не проверен, выдача по лоту выключена.", parse_mode="HTML", reply_markup=kb)
        return

    if state == 'autogift_binding_confirm':
        preview = TG_WIZARD.get(user_id, {}).get('binding_preview')
        if not preview or preview.get('chat_id') != chat_id:
            clear_state(chat_id, user_id)
            return
        expected = f"ПРИВЯЗАТЬ {preview['lot_id']} {preview['nonce']}"
        if text.upper() != expected.upper():
            state_send(chat_id, user_id, 'Подтверждение не совпало. Вставьте строку из предпросмотра.', state, "ag:lots")
            return
        try:
            _confirm_binding(CARDINAL_INSTANCE, preview, user_id)
        except Exception as exc:
            clear_state(chat_id, user_id)
            BOT.send_message(chat_id, f'❌ Привязка не включена: {h(exc)}', parse_mode='HTML', reply_markup=back_keyboard("ag:lots"))
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


MESSAGE_KEYS = {"payment": "payment_text", "success": "success_text", "refund": "refund_text", "reminder": "reminder_text"}


CODE_HINT = ("Введите код <b>через пробелы или дефисы</b>: <code>1 2 3 4 5</code>.\n"
             "⚠️ Не отправляйте код слитно — Telegram аннулирует код, отправленный в чат одним числом.")


def _auth_event(user_id: int, item: Dict[str, Any]) -> None:
    chat_id = user_id
    kind = item.get("kind")
    if kind == "code":
        msg = BOT.send_message(chat_id, "📨 Код отправлен в приложение Telegram (чат «Telegram» на устройствах с этим аккаунтом).\n\n"
                               + CODE_HINT, parse_mode="HTML", reply_markup=cancel_keyboard("ag:accounts"))
        CARDINAL_INSTANCE.telegram.set_state(chat_id, msg.message_id, user_id, "autogift_auth_code")
    elif kind == "code_retry":
        msg = BOT.send_message(chat_id, f"❌ {h(item.get('error') or 'Неверный код.')} Попробуйте ещё раз.\n\n" + CODE_HINT,
                               parse_mode="HTML", reply_markup=cancel_keyboard("ag:accounts"))
        CARDINAL_INSTANCE.telegram.set_state(chat_id, msg.message_id, user_id, "autogift_auth_code")
    elif kind in ("password", "password_retry"):
        prefix = "❌ Неверный пароль. " if kind == "password_retry" else "🔐 У аккаунта включён облачный пароль (2FA). "
        msg = BOT.send_message(chat_id, prefix + "Введите пароль — сообщение будет сразу удалено, пароль не сохраняется.",
                               reply_markup=cancel_keyboard("ag:accounts"))
        CARDINAL_INSTANCE.telegram.set_state(chat_id, msg.message_id, user_id, "autogift_auth_password")
    elif kind == "done":
        clear_state(chat_id, user_id)
        username = f" (@{h(item.get('username'))})" if item.get("username") else ""
        BOT.send_message(chat_id, f"✅ Аккаунт успешно добавлен!\n👤 <b>{h(item.get('name'))}</b>{username}\n"
                         f"📱 {h(masked_phone(item.get('phone') or ''))}\n\nБаланс проверится через несколько секунд.",
                         parse_mode="HTML", reply_markup=back_keyboard("ag:accounts"))
    elif kind == "error":
        clear_state(chat_id, user_id)
        # Telegram Bot API ограничивает длину текстовых сообщений.
        raw_error = str(item.get("error") or "Неизвестная ошибка").strip()
        short_error = raw_error[:1200] + ("…" if len(raw_error) > 1200 else "")
        BOT.send_message(chat_id, f"❌ Авторизация не удалась:\n<code>{h(short_error)}</code>",
                         parse_mode="HTML", reply_markup=back_keyboard("ag:accounts"))


def auth_watcher_worker() -> None:
    while True:
        time.sleep(0.5)
        for user_id, q in list(AUTH_OUTPUT_QUEUES.items()):
            while True:
                try:
                    item = q.get_nowait()
                except pyqueue.Empty:
                    break
                if not isinstance(item, dict):
                    continue
                try:
                    _auth_event(user_id, item)
                except Exception:
                    logger.exception("%s auth notification failed", LOGGER_PREFIX)


# ---------------------------------------------------------------------------
# CALLBACK ROUTER
# ---------------------------------------------------------------------------

def run_in_background(chat_id: int, message_id: int, waiting_text: str, func) -> None:
    """Slow Telegram/FunPay work runs off the bot thread; the menu shows progress."""
    try:
        BOT.edit_message_text(waiting_text, chat_id, message_id)
    except Exception:
        pass

    def worker():
        try:
            text, kb = func()
        except Exception as exc:
            logger.warning("%s background action failed: %s", LOGGER_PREFIX, exc)
            text, kb = f"❌ {h(str(exc)[:700])}", back_keyboard()
        try:
            BOT.edit_message_text(text, chat_id, message_id, parse_mode="HTML", reply_markup=kb)
        except Exception:
            try:
                BOT.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.exception("%s background reply failed", LOGGER_PREFIX)

    threading.Thread(target=worker, daemon=True, name="AutoGiftUI").start()


def cb(call: types.CallbackQuery):
    try:
        _cb(call)
    except Exception as exc:
        if "message is not modified" in str(exc):
            return
        logger.exception("%s callback %s failed", LOGGER_PREFIX, getattr(call, "data", ""))
        try:
            BOT.send_message(call.message.chat.id, f"❌ Ошибка: <code>{h(str(exc)[:300])}</code>",
                             parse_mode="HTML", reply_markup=back_keyboard())
        except Exception:
            pass


def edit(call, text: str, kb: Optional[InlineKeyboardMarkup] = None) -> None:
    BOT.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML",
                          reply_markup=kb, disable_web_page_preview=True)


def lot_card(key: str) -> Tuple[str, InlineKeyboardMarkup]:
    lot = LOTS[key]
    active_orders = sum(1 for r in list(ORDERS.values()) if r.get("lot_key") == key
                        and r.get("status") in ACTIVE_BUYER_STATUSES | {"ready", "sending"})
    txt = (
        f"<b>📦 {h(lot.get('name') or key)}</b>\n\n"
        f"FunPay ID: <code>{h(lot.get('funpay_lot_id') or lot.get('lot_id'))}</code>\n"
        f"Gift ID: <code>{h(', '.join(map(str, lot.get('gift_ids', []))))}</code>\n"
        f"В наборе: {len(lot.get('gift_ids', []))} 🎁 · стоимость: <b>{h(lot_price_label(lot))}</b>\n"
        f"Привязка: {'✅ подтверждена' if _binding_valid(lot) else '⚠️ нужна проверка тега'}\n"
        f"Выдача: {'🟢 включена' if lot.get('enabled') else '🔴 выключена'}\n"
    )
    if lot.get("paused_by_autogift"):
        txt += f"⏸ На паузе защитой: {h(lot.get('pause_reason') or '')}\n"
    if active_orders:
        txt += f"⏳ Активных заказов: {active_orders}\n"
    if _binding_valid(lot):
        txt += f"\nТег: <code>[AutoGift:{lot['binding']['token']}]</code>\n"
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(InlineKeyboardButton("🔖 Получить тег", callback_data=f"ag:tag:{key}"),
           InlineKeyboardButton("✅ Проверить тег", callback_data=f"ag:verify_tag:{key}"))
    kb.row(InlineKeyboardButton('🔐 Добавить автоматически', callback_data=f'ag:bind:{key}'),
           InlineKeyboardButton("🧪 Тест", callback_data=f"ag:testlot:{key}"))
    kb.row(InlineKeyboardButton("✏️ Название", callback_data=f"ag:rename_lot:{key}"),
           InlineKeyboardButton("🟢/🔴 Выдача", callback_data=f"ag:toggle_lot:{key}"))
    kb.row(InlineKeyboardButton("🗑 Удалить", callback_data=f"ag:delete_lot:{key}"))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="ag:lots"))
    return txt, kb


def review_list() -> Tuple[str, InlineKeyboardMarkup]:
    rows = sorted((x for x in list(ORDERS.values()) if x.get("status") in REVIEW_STATUSES),
                  key=lambda x: ts(x.get("created_at")), reverse=True)
    kb = InlineKeyboardMarkup(row_width=1)
    if not rows:
        text = "<b>🟠 Заказы для ручной проверки</b>\n\n✅ Таких заказов нет."
    else:
        text = f"<b>🟠 Заказы для ручной проверки ({len(rows)})</b>\n\nВыберите заказ, чтобы отметить результат, продолжить выдачу или вернуть деньги.\n\n"
        for row in rows[:15]:
            text += (f"#{h(row.get('order_id'))} · @{h(row.get('username') or '—')} · {delivered_count(row)}/{len(row.get('delivery', []))}\n"
                     f"<code>{h(str(row.get('last_error') or 'Неизвестный результат')[:150])}</code>\n\n")
            kb.add(InlineKeyboardButton(f"#{row.get('order_id')} · {delivered_count(row)}/{len(row.get('delivery', []))} 🎁",
                                        callback_data=f"ag:rv:{row.get('order_id')}"))
    kb.add(InlineKeyboardButton("🏠 Главное меню", callback_data="ag:main"))
    return text, kb


def _cb(call: types.CallbackQuery):
    global RUNNING, ACCOUNTS
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

    if data.startswith("ag:cancel"):
        cancel_operation(chat_id, user_id)
        target = data.split(":", 2)[2] if data.count(":") >= 2 else "ag:main"
        data = target if target.startswith("ag:") and not target.startswith("ag:cancel") else "ag:main"

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
        if not RUNNING:
            problem = ("Добавьте хотя бы один Telegram-аккаунт." if not ACCOUNTS else
                       "Не настроен обязательный прокси." if CONFIG.get("proxy_required") and not CONFIG.get("proxy") else "")
            if problem:
                BOT.send_message(chat_id, "⚠️ Невозможно включить выдачу: " + problem, reply_markup=back_keyboard("ag:settings"))
                return
        RUNNING = not RUNNING
        CONFIG["running"] = RUNNING
        _safe_json_save(CONFIG_PATH, CONFIG)
        if RUNNING:
            BALANCE_REFRESH_REQUEST.set()
            SCAN_REQUEST.set()
            resume_ready_orders()
        send_main(chat_id, call.message.message_id)
        return

    if data == "ag:protection":
        edit(call, *protection_panel())
        return

    if data == "ag:recipient_settings":
        edit(call, *recipient_panel())
        return

    if data == "ag:limits":
        edit(call, *limits_panel())
        return

    if data.startswith("ag:set_limit:"):
        kind = data.rsplit(":", 1)[1]
        if kind in LIMIT_KEYS:
            prompt_input(call, f"🔢 {LIMIT_KEYS[kind][1]}. Сейчас: <b>{CONFIG.get(LIMIT_KEYS[kind][0])}</b>\n\nВведите новое число:",
                         f"autogift_limit:{kind}", "ag:limits")
        return

    if data == "ag:test":
        kb = InlineKeyboardMarkup(row_width=1)
        for key, lot in list(LOTS.items())[:30]:
            if _binding_valid(lot):
                kb.add(InlineKeyboardButton(str(lot.get("name") or key)[:50], callback_data=f"ag:testlot:{key}"))
        kb.row(InlineKeyboardButton("⬅️ Главное меню", callback_data="ag:main"))
        edit(call, "<b>🧪 Тестовый заказ</b>\n\nВыберите подтверждённый лот. Проверим количество, каталог, баланс и Telegram-получателя. "
                   "Подарок не отправляется, Stars не списываются.", kb)
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
        edit(call, stats_text(days), stats_keyboard())
        return

    if data == "ag:review":
        edit(call, *review_list())
        return

    if data.startswith("ag:rv:"):
        oid = data.split(":", 2)[2].upper()
        edit(call, *review_card(oid))
        return

    if data.startswith("ag:rva:"):
        _, _, action, oid = data.split(":", 3)
        if action in {"refund_ask", "done_ask"}:
            edit(call, *review_confirm(action, oid.upper()))
            return
        run_in_background(chat_id, call.message.message_id, "⏳ Выполняю…",
                          lambda: review_action(action, oid.upper(), user_id))
        return

    if data.startswith("ag:reaccept:"):
        oid = data.split(":", 2)[2].upper()
        run_in_background(chat_id, call.message.message_id, "⏳ Проверяю заказ на FunPay…",
                          lambda: operator_reaccept(oid))
        return

    if data == "ag:export":
        payload = io.BytesIO(build_orders_csv())
        payload.name = "autogift_orders.csv"
        BOT.send_document(chat_id, payload, caption="📄 Экспорт заказов AutoGift")
        return

    if data == "ag:help":
        edit(call, help_text(), back_keyboard())
        return

    if data == "ag:settings":
        edit(call, settings_text(), settings_keyboard())
        return

    if data == "ag:set_api":
        prompt_input(call, "🔑 <b>API Telegram</b>\n\nВведите API ID (только цифры) с my.telegram.org:", "autogift_api_id", "ag:settings")
        return

    if data == "ag:set_proxy":
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton(
            f"Обязательный прокси: {'ВКЛ' if CONFIG.get('proxy_required') else 'ВЫКЛ'}", callback_data="ag:toggle_proxy_required"))
        prompt_input(call, "🌐 <b>Proxy для Telegram</b>\n\n"
                     f"Сейчас: {'✅ настроен' if CONFIG.get('proxy') else '➖ нет'}\n\n"
                     "Отправьте прокси: <code>ip:port</code> или <code>login:pass@ip:port</code>. "
                     "Префикс <code>http://</code> можно оставить; поддерживается и <code>socks5://</code>. "
                     "Рекомендуется платный IPv4 HTTP-прокси.\n\nОтправьте <code>-</code>, чтобы удалить прокси.",
                     "autogift_proxy", "ag:settings", kb)
        return

    if data == "ag:set_comment":
        kb = InlineKeyboardMarkup().add(InlineKeyboardButton(f"Текст подарка: {'ВКЛ' if CONFIG.get('comment_enabled') else 'ВЫКЛ'}", callback_data="ag:toggle_comment"))
        prompt_input(call, f"<b>💬 Текст к подарку</b>\n\nСохранённый текст: <code>{h(CONFIG.get('comment') or 'нет')}</code>\n\n"
                     "Отправьте новый текст (до 128 символов). Знак - очистит его. Выключение кнопкой сохраняет текст.",
                     "autogift_comment", "ag:settings", kb)
        return

    if data == "ag:set_threshold":
        prompt_input(call, "⭐ Введите минимальный баланс одного аккаунта. Если ни один аккаунт не достигает порога, лоты выключаются. "
                     "0 — проверять только стоимость набора:", "autogift_threshold", "ag:protection")
        return

    if data == "ag:set_star_cost":
        prompt_input(call, "💱 Укажите закупочную цену одной Stars в рублях. Пример: <code>1.45</code>. Ноль отключит расчёт прибыли.",
                     "autogift_star_cost", "ag:settings")
        return

    if data == "ag:set_fee":
        prompt_input(call, "💸 Укажите комиссию FunPay в процентах. Пример: <code>10</code>.", "autogift_fee", "ag:settings")
        return

    if data == "ag:messages":
        edit(call, messages_text(), messages_keyboard())
        return

    if data == "ag:message_presets":
        kb = InlineKeyboardMarkup(row_width=2)
        kb.row(InlineKeyboardButton("✅ Применить", callback_data="ag:apply_message_presets"),
               InlineKeyboardButton("⬅️ Назад", callback_data="ag:messages"))
        preview = ("<b>✨ Готовые сообщения</b>\n\nПосле оплаты:\n<code>" + h(DEFAULT_CONFIG["payment_text"])
                   + "</code>\n\nПосле выдачи:\n<code>" + h(DEFAULT_CONFIG["success_text"])
                   + "</code>\n\nКнопка заменит все четыре шаблона стандартными.")
        edit(call, preview, kb)
        return

    if data == "ag:apply_message_presets":
        for key in MESSAGE_KEYS.values():
            CONFIG[key] = DEFAULT_CONFIG[key]
        _safe_json_save(CONFIG_PATH, CONFIG)
        edit(call, messages_text(), messages_keyboard())
        return

    if data.startswith("ag:msg:"):
        kind = data.split(":", 2)[2]
        label = {"payment": "💳 сообщение после оплаты", "success": "🎁 сообщение после успешной выдачи",
                 "refund": "↩️ сообщение при возврате", "reminder": "🔔 напоминание покупателю"}.get(kind)
        key = MESSAGE_KEYS.get(kind)
        if not label or not key:
            return
        current = CONFIG.get(key) or ""
        prompt_input(call, f"<b>{label}</b>\n\nТекущий текст:\n<code>{h(current[:1200])}</code>\n\n"
                     "Отправьте новый текст. HTML не поддерживается чатом FunPay. Переменные:\n"
                     + "\n".join(f"<code>{{{k}}}</code> — {h(v)}" for k, v in TEMPLATE_VARS.items())
                     + "\n\nЧтобы вывести фигурную скобку, удвойте её. Отправьте <code>-</code>, чтобы вернуть стандартный текст.",
                     f"autogift_message:{kind}", "ag:messages")
        return

    toggles = {
        "ag:toggle_comment": ("comment_enabled", "settings"),
        "ag:toggle_proxy_required": ("proxy_required", "settings"),
        "ag:toggle_reactivate": ("auto_reactivate", "protection"),
        "ag:toggle_protect": ("lot_protection", "protection"),
        "ag:toggle_checkout": ("username_from_order", "recipient"),
        "ag:toggle_confirm": ("require_confirmation", "recipient"),
        "ag:toggle_reminder": ("buyer_reminder", "recipient"),
        "ag:toggle_refunds": ("auto_refunds", "recipient"),
        "ag:toggle_refund": ("auto_refund_when_no_delivery", "settings"),
        "ag:toggle_hide": ("hide_name", "settings"),
    }
    if data in toggles:
        key, panel = toggles[data]
        CONFIG[key] = not bool(CONFIG.get(key, DEFAULT_CONFIG.get(key)))
        _safe_json_save(CONFIG_PATH, CONFIG)
        clear_state(chat_id, user_id)
        if panel == "protection":
            edit(call, *protection_panel())
        elif panel == "recipient":
            edit(call, *recipient_panel())
        else:
            edit(call, settings_text(), settings_keyboard())
        return

    if data == "ag:accounts":
        edit(call, accounts_text(), accounts_keyboard())
        return

    if data == "ag:add_phone":
        api_id, api_hash = get_api()
        if not api_id or not api_hash:
            BOT.send_message(chat_id, "⚠️ Сначала укажите API ID и API Hash в настройках!", reply_markup=back_keyboard("ag:settings"))
            return
        if CONFIG.get("proxy_required") and not CONFIG.get("proxy"):
            BOT.send_message(chat_id, "⚠️ Сначала настройте прокси.", reply_markup=back_keyboard("ag:settings"))
            return
        prompt_input(call, "📱 Введите номер Telegram-аккаунта в международном формате, например <code>+79991234567</code>.\n"
                     "Пробелы, дефисы и скобки можно не убирать.", "autogift_auth_phone", "ag:accounts")
        return

    if data == "ag:add_session":
        prompt_input(call, "🔑 Отправьте Session String Pyrogram от Telegram-аккаунта одним сообщением.\n"
                     "Бот проверит вход через прокси и удалит ваше сообщение.", "autogift_session", "ag:accounts")
        return

    if data.startswith("ag:del_account:") or data.startswith("ag:del_account_ok:"):
        confirmed = data.startswith("ag:del_account_ok:")
        aid = data.split(":", 2)[2]
        account = next((a for a in ACCOUNTS if str(a.get("id")) == aid), None)
        if account is None:
            edit(call, accounts_text(), accounts_keyboard())
            return
        if aid in ACCOUNT_RESERVATIONS or RUNTIME.is_busy(aid):
            BOT.send_message(chat_id, "Аккаунт выполняет заказ. Удаление доступно после окончания выдачи.")
            return
        if not confirmed:
            kb = InlineKeyboardMarkup(row_width=2)
            kb.row(InlineKeyboardButton("🗑 Да, удалить", callback_data=f"ag:del_account_ok:{aid}"),
                   InlineKeyboardButton("⬅️ Нет", callback_data="ag:accounts"))
            edit(call, f"Удалить аккаунт <b>{h(account_title(account))}</b> из AutoGift?\n\n"
                       "Сессия в самом Telegram останется активной: завершите её в Настройки → Устройства, "
                       f"если нужно полностью отозвать доступ («{DEVICE_MODEL}»).", kb)
            return
        ACCOUNTS = [a for a in ACCOUNTS if a.get("id") != aid]
        _safe_json_save(ACCOUNTS_PATH, ACCOUNTS)
        threading.Thread(target=RUNTIME.drop_sync, args=(aid,), daemon=True).start()
        if not ACCOUNTS and RUNNING:
            RUNNING = False
            CONFIG["running"] = False
            _safe_json_save(CONFIG_PATH, CONFIG)
            BOT.send_message(chat_id, "🔴 Удалён последний аккаунт — выдача выключена.")
        edit(call, accounts_text(), accounts_keyboard())
        return

    if data == "ag:lots" or data.startswith("ag:lots_page:"):
        page = int(data.rsplit(":", 1)[1]) if data.startswith("ag:lots_page:") else 0
        edit(call, lots_text(page), lots_keyboard(page))
        return

    if data.startswith("ag:tag:"):
        key = data.split(":", 2)[2]
        if key not in LOTS:
            return
        tag = prepare_manual_tag(key)
        kb = InlineKeyboardMarkup(row_width=2)
        kb.row(InlineKeyboardButton("✅ Проверить тег", callback_data=f"ag:verify_tag:{key}"),
               InlineKeyboardButton("⬅️ Лот", callback_data=f"ag:edit_lot:{key}"))
        edit(call, f"<b>🔖 Тег привязки</b>\n\n<code>{tag}</code>\n\nДобавьте в конец подробного описания этого лота, сохраните изменения на FunPay "
                   "и нажмите «Проверить тег». Для двух языков добавьте в оба текста. Не копируйте тег в другие лоты.", kb)
        return

    if data.startswith("ag:verify_tag:"):
        key = data.split(":", 2)[2]

        def verify():
            if key not in LOTS:
                raise ValueError("Лот не найден.")
            confirm_manual_tag(CARDINAL_INSTANCE, key, user_id)
            BALANCE_REFRESH_REQUEST.set()
            text, kb = lot_card(key)
            return "✅ Тег и лот проверены. Привязка включена.\n\n" + text, kb
        run_in_background(chat_id, call.message.message_id, "⏳ Проверяю тег на FunPay…", verify)
        return

    if data == "ag:add_lot":
        prompt_input(call, "📦 Введите ID лота FunPay — цифры из ссылки <code>funpay.com/lots/offer?id=12345678</code>:",
                     "autogift_lot_funpay", "ag:lots")
        return

    if data.startswith('ag:bind:'):
        key = data.split(':', 2)[2]
        try:
            preview = _prepare_binding(CARDINAL_INSTANCE, key)
        except Exception as exc:
            BOT.send_message(chat_id, f'❌ Не удалось подготовить привязку: {h(exc)}', parse_mode='HTML', reply_markup=back_keyboard("ag:lots"))
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
                f"Для привязки напишите: <code>ПРИВЯЗАТЬ {preview['lot_id']} {preview['nonce']}</code>")
        state_send(chat_id, user_id, text, 'autogift_binding_confirm', "ag:lots")
        return

    if data.startswith("ag:edit_lot:"):
        key = data.split(":", 2)[2]
        if key not in LOTS:
            edit(call, lots_text(), lots_keyboard())
            return
        edit(call, *lot_card(key))
        return

    if data.startswith("ag:rename_lot:"):
        key = data.split(":", 2)[2]
        TG_WIZARD[user_id] = {"lot_key": key}
        prompt_input(call, "✏️ Введите новое название лота (только для меню AutoGift):", "autogift_lot_rename", f"ag:edit_lot:{key}")
        return

    if data.startswith("ag:toggle_lot:"):
        key = data.split(":", 2)[2]
        if key not in LOTS:
            return
        if not _binding_valid(LOTS[key]):
            BOT.send_message(chat_id, 'Сначала подтвердите привязку лота («Проверить тег»).')
            return
        lot = LOTS[key]
        lot["enabled"] = not bool(lot.get("enabled", True))
        lot["paused_by_autogift"] = False
        _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
        lid = int(lot.get("funpay_lot_id") or lot.get("lot_id"))
        enabled = bool(lot["enabled"])

        def apply():
            ok = set_lot_active(lid, enabled)
            text, kb = lot_card(key)
            note = ("✅ Лот на FunPay " + ("включён." if enabled else "выключен.")) if ok else "⚠️ Не удалось изменить активность лота на FunPay — проверьте вручную."
            return note + "\n\n" + text, kb
        run_in_background(chat_id, call.message.message_id, "⏳ Обновляю лот на FunPay…", apply)
        return

    if data.startswith("ag:delete_lot:") or data.startswith("ag:delete_lot_ok:"):
        confirmed = data.startswith("ag:delete_lot_ok:")
        key = data.split(":", 2)[2]
        lot = LOTS.get(key)
        if lot is None:
            edit(call, lots_text(), lots_keyboard())
            return
        busy = [r for r in list(ORDERS.values()) if r.get("lot_key") == key
                and r.get("status") in ACTIVE_BUYER_STATUSES | {"ready", "sending"}]
        if busy:
            BOT.send_message(chat_id, f"⏳ По лоту есть активные заказы ({len(busy)}). Удаление доступно после их завершения; "
                             "можно выключить выдачу кнопкой «🟢/🔴 Выдача».")
            return
        if not confirmed:
            kb = InlineKeyboardMarkup(row_width=2)
            kb.row(InlineKeyboardButton("🗑 Да, удалить", callback_data=f"ag:delete_lot_ok:{key}"),
                   InlineKeyboardButton("⬅️ Нет", callback_data=f"ag:edit_lot:{key}"))
            edit(call, f"Удалить привязку <b>{h(lot.get('name') or key)}</b>?\nСам лот на FunPay не удаляется.", kb)
            return
        LOTS.pop(key, None)
        _safe_json_save(LOTS_PATH, {"lot_mapping": LOTS})
        edit(call, lots_text(), lots_keyboard())
        return

    if data in ("ag:catalog", "ag:catalog_refresh"):
        items = CATALOG_CACHE.get(user_id) or CATALOG.get("items") or []
        if items and data == "ag:catalog":
            edit(call, *catalog_text(items, 0))
            return

        def load():
            return catalog_text(run_coro(refresh_catalog_for_menu(user_id), timeout=120), 0)
        run_in_background(chat_id, call.message.message_id, "⏳ Загружаю каталог из Telegram…", load)
        return

    if data.startswith("ag:cat:"):
        page = max(0, int(data.split(":")[2]))
        items = CATALOG_CACHE.get(user_id) or CATALOG.get("items") or []
        edit(call, *catalog_text(items, page))
        return

    if data.startswith("ag:gift:"):
        gid = int(data.split(":")[2])
        item = next((x for x in (CATALOG.get("items") or STANDARD_GIFTS_CATALOG) if int(x.get("id", -1)) == gid), None)
        if not item:
            return
        txt = (
            f"{h(item.get('emoji') or '🎁')} <b>Подарок за {format_stars(item.get('price'))}</b>\n\n"
            f"Название: <b>{h(item.get('name') or 'Подарок')}</b>\n"
            f"Статус: {'✅ доступен' if item.get('available') else '⚠️ не подтверждён — обновите каталог'}\n\n"
            f"ID для копирования (нажмите):\n<code>{gid}</code>"
        )
        edit(call, txt, back_keyboard("ag:catalog"))
        return

    if data == "ag:status":
        def status():
            run_coro(refresh_all_balances(notify=False), timeout=120)
            kb = InlineKeyboardMarkup(row_width=2)
            kb.row(InlineKeyboardButton("🔄 Обновить", callback_data="ag:status"),
                   InlineKeyboardButton("👥 Аккаунты", callback_data="ag:accounts"))
            kb.row(InlineKeyboardButton("⬅️ Главное меню", callback_data="ag:main"))
            return balance_text() + f"\n\n<i>Обновлено: {datetime.now().strftime('%H:%M:%S')}</i>", kb
        run_in_background(chat_id, call.message.message_id, "⏳ Проверяю балансы аккаунтов…", status)
        return


# ---------------------------------------------------------------------------
# FUNPAY ORDER FLOW
# ---------------------------------------------------------------------------

REFUND_WORDS = {"!вз", "!бэк", "!refund", "!возврат", "!back"}
ORDER_ID_RE = r"#?([A-Za-z0-9]{6,12})"


def _chat_orders(chat_id: str, buyer_id: str, statuses: set) -> List[Dict[str, Any]]:
    with ORDER_LOCK:
        return [row for row in ORDERS.values() if str(row.get("chat_id")) == str(chat_id)
                and str(row.get("buyer_id")) == str(buyer_id) and row.get("status") in statuses]


def _multi(data: Dict[str, Any]) -> bool:
    return len(_chat_orders(data.get("chat_id"), data.get("buyer_id"),
                            ACTIVE_BUYER_STATUSES | {"ready", "sending"})) > 1


def _hint(c: Cardinal, key: str, chat_id: Any, text: str) -> None:
    """At most one helper reply per order/chat every few minutes."""
    if time.time() - NOTIFY_ONCE.get("hint:" + key, 0) < HINT_INTERVAL:
        return
    NOTIFY_ONCE["hint:" + key] = time.time()
    send_funpay_message(c, chat_id, text)


def buyer_refund(c: Cardinal, order_id: str, chat_id: Any, by_operator: bool = False) -> bool:
    order_id = str(order_id).upper()
    with order_guard(order_id):
        data = ORDERS.get(order_id)
        if not data or order_id in PROCESSING_ORDERS:
            return False
        chat_id = data.get("chat_id") if by_operator else chat_id
        if not by_operator:
            if not RUNNING or STORAGE_ERROR or str(data.get("chat_id")) != str(chat_id):
                return False
            if not CONFIG.get("auto_refunds", True) or not order_is_refundable(data):
                send_funpay_message(c, chat_id, f"Автовозврат по заказу #{order_id} недоступен. Продавец ответит вам в этом чате.")
                return False
            state = order_ownership(c, data)
            if state == "error":
                send_funpay_message(c, chat_id, "⏳ FunPay сейчас не отвечает. Повторите команду через минуту.")
                return False
            if state in {"refunded", "closed"}:
                _sync_external_status(data, state)
                return False
            if state != "ok":
                return False
        data.update(status="refunding", refund_started_at=time.time())
        save_order(data)
        try:
            c.account.refund(order_id)
            current = _call_with_retry(c.account.get_order, order_id)
            if current.status != enums.OrderStatuses.REFUNDED:
                raise RuntimeError("FunPay не подтвердил возврат")
        except Exception as exc:
            data.update(status="needs_review", last_error=f"Возврат не подтверждён: {type(exc).__name__}: {exc}"[:500])
            save_order(data)
            notify_operators(f"🟠 Возврат #{h(order_id)} требует проверки. Повторный автоматический возврат заблокирован.",
                             review_button(order_id))
            return False
        data.update(status="refunded", refunded_at=time.time(), refunded_by="operator" if by_operator else "buyer")
        save_order(data)
    send_funpay_message(c, chat_id, format_customer_message(CONFIG.get("refund_text"), data))
    if not by_operator:
        notify_operators(f"↩️ AutoGift: Возврат по команде #{h(order_id)} (покупатель {h(data.get('buyer_id'))}).")
    return True


def _sync_external_status(data: Dict[str, Any], state: str) -> None:
    """The order was refunded/closed on FunPay outside AutoGift: stop handling it."""
    if data.get("status") in TERMINAL_STATUSES or delivered_count(data) or uncertain_count(data):
        if state == "refunded" and data.get("status") not in TERMINAL_STATUSES:
            data["last_error"] = "Заказ возвращён на FunPay, но часть подарков уже отправлена."
            data["status"] = "needs_review"
            save_order(data)
        return
    data.update(status="refunded" if state == "refunded" else "closed_external",
                last_error="Заказ " + ("возвращён" if state == "refunded" else "закрыт") + " на FunPay вне AutoGift.")
    save_order(data)


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
                    or normalized_label in {"telegram username", "telegram юзернейм", "telegram никнейм", "telegram аккаунт",
                                            "username", "ссылка на профиль"}):
                value = getattr(field, "value", field)
                values.extend(value.values() if isinstance(value, dict) else [value])
    candidates = set()
    for value in values:
        if not value:
            continue
        name = parse_buyer_username(str(value), allow_bare=True)
        if not name:
            return None  # Ambiguous or invalid purchase data requires manual input.
        candidates.add(name.lower())
    return next(iter(candidates)) if len(candidates) == 1 else None


def _send_buyer_prompt(c: Cardinal, data: Dict[str, Any], text: str) -> bool:
    """Send a dialogue step and remember its FunPay message ID (for '+' ordering)."""
    save_order(data)
    message_id = _funpay_send(c, data["chat_id"], text)
    if message_id is None:
        notify_once(f"prompt:{data['order_id']}", f"🟠 AutoGift #{h(data['order_id'])}: не удалось отправить сообщение покупателю.", 600,
                    review_button(data["order_id"]))
        return False
    data["dialog_message_id"] = int(message_id or 0)
    data["dialog_sent_at"] = time.time()
    save_order(data)
    return True


def _username_prompt(data: Dict[str, Any]) -> str:
    text = (f"🎁 Заказ #{data['order_id']}: наборов {data['units']}, подарков {len(data.get('delivery', []))}.\n\n"
            "👤 Отправьте ваш Telegram @username или ссылку t.me/username одним сообщением.")
    if _multi(data):
        text += f"\nУ вас несколько заказов — укажите номер: !gift {data['order_id']} @username"
    if CONFIG.get("auto_refunds", True):
        text += "\nВозврат до отправки: !вз"
    return text


def _confirmation_prompt(data: Dict[str, Any]) -> str:
    data["status"] = "ready_confirm"
    data["confirm_nonce"] = secrets.token_hex(4)
    data["confirm_expires"] = time.time() + 1800
    data.pop("confirmed", None)
    save_order(data)
    text = (f"🎁 Заказ #{data['order_id']}: получатель @{data['username']} ({data.get('display_name') or 'Telegram'})\n"
            f"📦 Наборов: {data['units']} · подарков: {len(data['delivery'])}\n\n"
            "Всё верно? Напишите + — и подарки будут отправлены.\n"
            "Чтобы сменить получателя, пришлите другой @username.")
    if _multi(data):
        text += f"\nУ вас несколько заказов — укажите номер: + {data['order_id']}"
    if CONFIG.get("auto_refunds", True):
        text += "\nВозврат до отправки: !вз"
    return text


def give_consent(data: Dict[str, Any], by_operator: bool = False) -> None:
    data["confirmed"] = {k: copy.deepcopy(data.get(k)) for k in
                         ("order_id", "binding_token", "recipient_id", "username", "gift_ids", "units", "comment", "hide_name")}
    data["confirmed"].update(confirmed_at=time.time(), by_operator=bool(by_operator))
    data.pop("confirm_nonce", None)
    data["status"] = "ready"
    save_order(data)


def _delivery_consent_valid(data: Dict[str, Any]) -> bool:
    """Consent is checked once, when it is given; it never expires mid-delivery.

    It covers exactly this order, lot binding, recipient, set and options, and is
    accepted for CONSENT_TTL so a stale confirmation does not start a delivery.
    """
    proof = data.get("confirmed") or {}
    given_at = ts(proof.get("confirmed_at")) or (ts(proof.get("expires")) - 600)
    return (bool(data.get("recipient_id")) and bool(data.get("binding_token"))
            and proof.get("order_id") == data.get("order_id")
            and proof.get("binding_token") == data.get("binding_token")
            and proof.get("recipient_id") == data.get("recipient_id")
            and proof.get("username") == data.get("username")
            and proof.get("gift_ids") == data.get("gift_ids")
            and proof.get("units") == data.get("units")
            and proof.get("comment") == data.get("comment")
            and proof.get("hide_name") == data.get("hide_name")
            and given_at > 0 and time.time() - given_at <= CONSENT_TTL)


def _queue_after_consent(c: Cardinal, data: Dict[str, Any]) -> None:
    # Tell the buyer first, then queue: "in the queue" never arrives after "delivering".
    ahead = len(QUEUED_ORDERS - {str(data["order_id"])})
    send_funpay_message(c, data["chat_id"], f"✅ Получатель @{data['username']} подтверждён. Заказ #{data['order_id']} в очереди на выдачу"
                        + (f" (перед вами заказов: {ahead})." if ahead else ".") + " Подарки придут в течение пары минут.")
    enqueue_delivery(str(data["order_id"]), str(data["chat_id"]))


def message_hook(c: Cardinal, e: NewMessageEvent):
    """Cheap pre-filter in Cardinal's thread; the dialogue runs in the AutoGift event thread."""
    msg = getattr(e, "message", None)
    if not RUNNING or msg is None or STORAGE_ERROR:
        return
    author_id, chat_id = str(getattr(msg, "author_id", "") or ""), str(getattr(msg, "chat_id", "") or "")
    text = str(getattr(msg, "text", "") or "").strip()
    if not text or author_id in ("", "0") or author_id == str(getattr(c.account, "id", "")):
        return
    addressed = text.lower().startswith("!gift")
    if not addressed and not _chat_orders(chat_id, author_id, ACTIVE_BUYER_STATUSES | {"ready", "sending", "failed", "needs_review"}):
        return
    submit_event(handle_buyer_message, c, chat_id, author_id, int(getattr(msg, "id", 0) or 0), text)


def _parse_buyer_command(text: str) -> Tuple[Optional[str], str]:
    """(explicit order ID or None, payload)."""
    t = text.strip()
    m = re.fullmatch(r"!gift\s+" + ORDER_ID_RE + r"\s+(.+)", t, re.I | re.S)
    if m:
        payload = m.group(2).strip()
        if payload.lower() in {"refund", "вз", "возврат"}:
            payload = "refund"
        elif payload.lower().startswith("confirm"):
            payload = "+"
        return m.group(1).upper(), payload
    m = re.fullmatch(r"([+\-−])\s*" + ORDER_ID_RE, t)
    if m:
        return m.group(2).upper(), "-" if m.group(1) in "-−" else "+"
    words = t.lower().split()
    if words and words[0] in REFUND_WORDS:
        if len(words) == 2 and re.fullmatch(ORDER_ID_RE, words[1]):
            return words[1].lstrip("#").upper(), "refund"
        if len(words) == 1:
            return None, "refund"
    if t in {"−"}:
        t = "-"
    return None, t


def handle_buyer_message(c: Cardinal, chat_id: str, author_id: str, message_id: int, text: str) -> None:
    order_id, payload = _parse_buyer_command(text)
    if order_id is None:
        statuses = ACTIVE_BUYER_STATUSES | {"ready", "sending"}
        if payload == "refund":
            statuses = statuses | {"failed", "needs_review"}
        rows = _chat_orders(chat_id, author_id, statuses)
        if payload == "refund":
            rows = [r for r in rows if order_is_refundable(r)] or rows
        if not rows:
            return
        if len(rows) > 1:
            ids = ", ".join("#" + str(r["order_id"]) for r in rows[:5])
            example = "!вз НОМЕР" if payload == "refund" else "!gift НОМЕР @username или + НОМЕР"
            _hint(c, f"multi:{chat_id}", chat_id, f"У вас несколько заказов ({ids}). Укажите номер заказа: {example}")
            return
        order_id = str(rows[0]["order_id"])
    with order_guard(order_id):
        data = ORDERS.get(order_id)
        if not data or str(data.get("buyer_id")) != str(author_id) or str(data.get("chat_id")) != str(chat_id):
            return
        status = data.get("status")
        if order_id in PROCESSING_ORDERS or status in {"ready", "sending"}:
            _hint(c, order_id, chat_id, f"⏳ Заказ #{order_id} уже выдаётся" + (". Возврат во время выдачи недоступен." if payload == "refund" else "."))
            return
    if payload == "refund":
        buyer_refund(c, order_id, chat_id)
        return
    username = None
    if payload not in {"+", "-"}:
        username = parse_buyer_username(payload, allow_bare=bool(CONFIG.get("require_confirmation", True)))
        if not username:
            # Ordinary chatter: a short hint, no FunPay/Telegram requests.
            need = "напишите + или пришлите другой @username" if status == "ready_confirm" \
                else "отправьте ваш Telegram @username или ссылку t.me/username"
            _hint(c, order_id, chat_id, f"Заказ #{order_id}: {need}.")
            return
    with order_guard(order_id):
        data = ORDERS.get(order_id)
        if not data or data.get("status") not in ACTIVE_BUYER_STATUSES or order_id in PROCESSING_ORDERS:
            return
        state = order_ownership(c, data)
        if state == "error":
            _hint(c, order_id, chat_id, "⏳ FunPay сейчас не отвечает. Повторите сообщение через минуту.")
            return
        if state in {"refunded", "closed"}:
            _sync_external_status(data, state)
            return
        if state != "ok":
            data.update(status="needs_review", last_error="Заказ не прошёл проверку: " + OWNERSHIP_TEXT.get(state, state))
            save_order(data)
            send_funpay_message(c, chat_id, f"🟠 Заказ #{order_id} передан продавцу на проверку. Продавец ответит в этом чате.")
            notify_operators(f"🟠 AutoGift #{h(order_id)}: {h(OWNERSHIP_TEXT.get(state, state))}.", review_button(order_id))
            return
        if payload == "-":
            for key in ("confirmed", "confirm_nonce", "username", "recipient_id", "display_name"):
                data.pop(key, None)
            data["status"] = "await_username"
            _send_buyer_prompt(c, data, _username_prompt(data))
            return
        if payload == "+":
            if data.get("status") != "ready_confirm":
                _hint(c, order_id, chat_id, f"Сначала отправьте Telegram @username для заказа #{order_id}.")
                return
            prompt_id = int(data.get("dialog_message_id") or 0)
            if message_id and prompt_id and message_id <= prompt_id:
                return  # A delayed reply to a previous recipient is never consent.
            if time.time() > ts(data.get("confirm_expires")):
                _send_buyer_prompt(c, data, "⏱ Подтверждение устарело.\n\n" + _confirmation_prompt(data))
                return
            give_consent(data)
            queued = True
        else:
            queued = False
            # Remove old consent before validation so it cannot survive a change.
            for key in ("confirmed", "confirm_nonce"):
                data.pop(key, None)
            data["status"] = "await_username"
            save_order(data)
            try:
                result, name, recipient_id = run_coro(validate_username(username), timeout=90)
            except Exception as exc:
                result, name, recipient_id = "error", str(exc), None
            if result == "not_found":
                send_funpay_message(c, chat_id, f"❌ Пользователь @{username} не найден в Telegram. Проверьте ник и отправьте его ещё раз.")
                return
            if result != "ok" or not recipient_id:
                if result == "error":
                    notify_once("validate_error", f"🟠 AutoGift: не удалось проверить Telegram-ник покупателя ({h(name)}). "
                                "Проверьте прокси и аккаунты.", 900)
                    send_funpay_message(c, chat_id, "⏳ Не удалось проверить ник прямо сейчас. Отправьте его ещё раз через минуту.")
                else:
                    send_funpay_message(c, chat_id, f"❌ {name}")
                return
            data.update(username=username, display_name=name, recipient_id=recipient_id, username_source="chat")
            if CONFIG.get("require_confirmation", True):
                _send_buyer_prompt(c, data, _confirmation_prompt(data))
                return
            give_consent(data)
            queued = True
    if queued:
        _queue_after_consent(c, data)


def _execute_customer_order(c: Cardinal, data: Dict[str, Any], chat_id: str) -> None:
    """The only entry point for delivery after recipient confirmation (delivery thread)."""
    order_id = str(data["order_id"])
    with order_guard(order_id):
        data = ORDERS.get(order_id, data)
        if (not RUNNING or STORAGE_ERROR or order_id in PROCESSING_ORDERS or data.get('status') != 'ready'
                or str(data.get('chat_id')) != str(chat_id)):
            return  # A "ready" order stays queued for the next start/resume.
        if not _delivery_consent_valid(data):
            data.update(status="needs_review", last_error="Подтверждение получателя устарело.")
            save_order(data)
            notify_operators(f"🟠 AutoGift #{h(order_id)}: подтверждение устарело, выдача не начата.", review_button(order_id))
            return
        data['status'] = 'sending'
        save_order(data)
        PROCESSING_ORDERS.add(order_id)
    try:
        send_funpay_message(c, chat_id, f"⏳ Начинаю выдачу заказа #{order_id}…")
        try:
            outcome = run_coro(execute_order(c, data))
        except StorageError:
            notify_operators(f"⛔ AutoGift #{h(order_id)}: ошибка хранилища. Выдача выключена; проверьте журнал и историю Telegram.")
            return
        except Exception as exc:
            logger.exception("%s execute_order crashed for #%s: %s", LOGGER_PREFIX, order_id, exc)
            for item in data.get("delivery", []):
                if item.get("status") == "sending":
                    item["status"] = "unknown"
            data["status"] = "needs_review"
            data["last_error"] = f"{type(exc).__name__}: {exc}"[:1000]
            save_order(data)
            outcome = "needs_review"
        if outcome == "completed":
            data["status"] = "completed"; data["completed_at"] = time.time(); save_order(data)
            send_funpay_message(c, chat_id, format_customer_message(CONFIG.get("success_text") or "Спасибо за покупку! Подтвердите выполнение заказа.", data)[:2500])
            notify_operators(f"✅ Заказ <b>#{h(order_id)}</b> выполнен\n👤 @{h(data.get('username'))}\n🎁 Gifts: {delivered_count(data)}\n"
                             f"⭐ Потрачено: {format_stars(sum(int(i.get('price') or 0) for i in data.get('delivery', []) if i.get('status') == 'sent'))}")
            return
        if data.get("ownership") in {"refunded", "closed"}:
            _sync_external_status(data, data["ownership"])
            if data.get("status") in TERMINAL_STATUSES:
                return
        if outcome == 'failed_no_delivery':
            data['status'] = 'failed'
            save_order(data)
            PROCESSING_ORDERS.discard(order_id)
            if CONFIG.get('auto_refund_when_no_delivery', False) and buyer_refund(c, order_id, chat_id, by_operator=True):
                notify_operators(f"↩️ AutoGift #{h(order_id)}: выдача невозможна, деньги возвращены автоматически.\n"
                                 f"<code>{h(str(data.get('last_error') or '')[:500])}</code>")
                return
            refund_hint = " Напишите !вз для возврата денег или дождитесь ответа продавца." if CONFIG.get("auto_refunds", True) else \
                " Продавец свяжется с вами в этом чате."
            send_funpay_message(c, chat_id, f"⚠️ Технические неполадки: заказ #{order_id} пока не может быть выполнен.{refund_hint}")
            notify_operators(f"⚠️ AutoGift: заказ <b>#{h(order_id)}</b> не выполнен, подарки не отправлены\n"
                             f"<code>{h(str(data.get('last_error') or 'Неизвестная причина')[:700])}</code>", review_button(order_id))
            return
        data["status"] = "needs_review"; save_order(data)
        sent, total = delivered_count(data), len(data.get("delivery", []))
        partial = f" Отправлено {sent} из {total}." if sent else ""
        send_funpay_message(c, chat_id, f"🟠 Выдача заказа #{order_id} приостановлена и передана продавцу на проверку.{partial} "
                                        "Не отправляйте новые данные — продавец напишет в этом чате.")
        notify_operators(f"🟠 Заказ <b>#{h(order_id)}</b> требует проверки\n👤 @{h(data.get('username'))}\n🎁 Отправлено: {sent}/{total}\n"
                         f"<code>{h(str(data.get('last_error') or 'Неизвестный результат')[:700])}</code>", review_button(order_id))
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


def _mark_seen(order_id: str) -> None:
    SEEN_ORDERS[str(order_id).upper()] = time.time()
    try:
        with FILE_LOCK:
            save_seen()
    except StorageError:
        pass


def accept_paid_order(c: Cardinal, order_id: Any, fallback_chat_id: Any = None, source: str = "event",
                      approved_fingerprint: Optional[str] = None) -> None:
    """Verify a purchase and open the buyer dialogue. Runs in the AutoGift event thread."""
    if STORAGE_ERROR:
        return
    order_id = str(getattr(order_id, "id", order_id) or "").upper()
    if not re.fullmatch(r"[A-Z0-9]+", order_id):
        return
    with order_guard(order_id):
        if order_id in ORDERS:
            return
        try:
            full = _call_with_retry(c.account.get_order, order_id)
            if str(getattr(full, "id", "")).upper() != order_id:
                return
        except Exception:
            logger.warning("%s cannot load purchase #%s; the periodic scan will retry", LOGGER_PREFIX, order_id)
            return
        found, reason = match_order_to_lot(full, approved_fingerprint)
        if reason == "error":
            logger.warning("%s purchase #%s: FunPay check failed; the periodic scan will retry", LOGGER_PREFIX, order_id)
            return
        if reason in {"foreign", "not_paid"}:
            _mark_seen(order_id)
            return
        if not found:
            _mark_seen(order_id)
            tagged = tagged_lot_of_order(full)
            lot_name = (tagged[1].get("name") if tagged else None) or "?"
            why = ("выдача по лоту выключена в AutoGift" if reason == "disabled" else
                   "описание или раздел лота не совпали с привязкой (лот изменён или скопирован?)")
            kb = InlineKeyboardMarkup(row_width=1)
            kb.add(InlineKeyboardButton("🔁 Принять этот заказ", callback_data=f"ag:reaccept:{order_id}"))
            if tagged:
                kb.add(InlineKeyboardButton("📦 Открыть лот", callback_data=f"ag:edit_lot:{tagged[0]}"))
            notify_operators(f"⚠️ AutoGift: оплаченный заказ <b>#{h(order_id)}</b> по лоту «{h(lot_name)}» не принят: {h(why)}.\n\n"
                             "Покупатель ничего не получил. Проверьте тег лота и нажмите «Принять этот заказ» "
                             "или обработайте заказ вручную.", kb)
            return
        lot_key, lot = found
        if not RUNNING:
            notify_once(f"off:{order_id}", f"⏸ AutoGift выключен, а заказ <b>#{h(order_id)}</b> («{h(lot.get('name'))}») оплачен. "
                        "Он будет обработан автоматически после включения выдачи (в течение 48 ч).", 86400)
            return
        try:
            units = _order_quantity(full)
        except (ValueError, TypeError):
            data = {"order_id": order_id, "status": "needs_review", "created_at": time.time(),
                    "lot_key": lot_key, "lot_name": lot.get("name"),
                    "last_error": "FunPay не вернул точное количество в полной карточке заказа."}
            save_order(data)
            notify_operators(f"🟠 AutoGift #{h(order_id)}: количество не подтверждено, нужна проверка.", review_button(order_id))
            return
        buyer_id = getattr(full, "buyer_id", None)
        if not buyer_id or str(buyer_id) == str(c.account.id):
            return
        chat_id = _order_chat_id(c, full)
        if fallback_chat_id is not None and str(fallback_chat_id) != chat_id:
            logger.warning("%s chat mismatch for purchase #%s; left for the periodic scan", LOGGER_PREFIX, order_id)
            return
        data = {"order_id": order_id, "buyer_id": int(buyer_id), "chat_id": chat_id,
                "status": "await_username", "lot_key": lot_key, "lot_name": lot.get("name"),
                "binding_token": lot["binding"]["token"], "gift_ids": list(lot["gift_ids"]),
                "gift_names": list(lot.get("gift_names", [])), "units": units,
                "order_price": float(getattr(full, "sum", getattr(full, "price", 0)) or 0),
                "currency": str(getattr(getattr(full, "currency", None), "name", getattr(full, "currency", "UNKNOWN"))),
                "created_at": time.time(), "username": None, "source": source,
                "comment": CONFIG.get("comment") if CONFIG.get("comment_enabled") else "",
                "hide_name": CONFIG.get("hide_name", True), "delivery": []}
        if approved_fingerprint:
            data["approved_fingerprint"] = approved_fingerprint
        if (units > max(1, int(CONFIG.get("max_order_units") or 10))
                or units * len(data["gift_ids"]) > int(CONFIG.get("max_gifts_per_order", 100))):
            data.update(status="needs_review", last_error="Превышен лимит количества/подарков в заказе.")
            save_order(data)
            notify_operators(f"🟠 AutoGift #{h(order_id)}: превышен лимит заказа ({units} шт), выдача не начата.", review_button(order_id))
            send_funpay_message(c, chat_id, f"🎁 Заказ #{order_id} принят. Продавец обработает его вручную и напишет в этом чате.")
            return
        delivery_items(data)
        save_order(data)  # Durable acceptance BEFORE chat/network calls.
    queued = False
    with order_guard(order_id):
        if data.get("status") != "await_username":
            return
        username = username_from_order(full) if CONFIG.get("username_from_order", True) else None
        prompt = _username_prompt(data)
        if username:
            try:
                result, name, recipient_id = run_coro(validate_username(username), timeout=90)
            except Exception:
                result, name, recipient_id = "error", "", None
                logger.info("%s checkout recipient lookup unavailable for #%s", LOGGER_PREFIX, order_id)
            if result == "ok" and recipient_id:
                data.update(username=username, display_name=name, recipient_id=recipient_id, username_source="funpay")
                if CONFIG.get("require_confirmation", True):
                    prompt = _confirmation_prompt(data)
                else:
                    give_consent(data)
                    queued = True
                    prompt = f"👤 Получатель из заказа: @{username} ({name})."
            else:
                prompt = f"⚠️ Ник @{username} из заказа не удалось проверить.\n\n" + prompt
        _send_buyer_prompt(c, data, format_customer_message(CONFIG.get("payment_text") or "Заказ принят.", data)[:1500]
                           + "\n\n" + prompt)
    if queued:
        _queue_after_consent(c, data)


def payment_notice_hook(c: Cardinal, e: NewMessageEvent):
    """Fallback only for a genuine FunPay system payment message."""
    msg = getattr(e, "message", None)
    if msg is None or str(getattr(msg, "author_id", "")) != "0":
        return
    match = re.search(r"(?:оплатил|оплатила|оплачено)\s+заказ\s+#([A-Za-z0-9]+)", str(getattr(msg, "text", "") or ""), re.I)
    if not match or match.group(1).upper() in ORDERS:
        return
    submit_event(accept_paid_order, c, match.group(1).upper(), getattr(msg, "chat_id", None), "notice")


def order_hook(c: Cardinal, e: NewOrderEvent):
    """Every new order is checked off-thread; only verified AutoGift lots are handled."""
    order = getattr(e, "order", None)
    if order is None or STORAGE_ERROR:
        return
    submit_event(accept_paid_order, c, str(getattr(order, "id", "")), getattr(order, "chat_id", None), "event")


# ---------------------------------------------------------------------------
# OPERATOR REVIEW
# ---------------------------------------------------------------------------

REVIEW_STATUSES = {"needs_review", "failed"}
ITEM_LABELS = {"sent": "✅ отправлено", "pending": "⏳ ожидает", "failed": "❌ не отправлено",
               "unknown": "❓ неизвестно", "sending": "❓ неизвестно"}


def review_button(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup().add(InlineKeyboardButton("🟠 Открыть заказ", callback_data=f"ag:rv:{order_id}"))


def review_card(order_id: str) -> Tuple[str, InlineKeyboardMarkup]:
    data = ORDERS.get(order_id)
    kb = InlineKeyboardMarkup(row_width=1)
    if not data:
        return f"Заказ #{h(order_id)} не найден в журнале.", back_keyboard("ag:review")
    counts: Dict[str, int] = {}
    for item in data.get("delivery", []):
        label = ITEM_LABELS.get(item.get("status"), str(item.get("status")))
        counts[label] = counts.get(label, 0) + 1
    text = (f"<b>🟠 Заказ #{h(order_id)}</b>\n\n"
            f"Статус: <b>{h(data.get('status'))}</b>\n"
            f"Лот: {h(data.get('lot_name') or data.get('lot_key') or '—')}\n"
            f"Покупатель FunPay: {h(data.get('buyer_id') or '—')} · чат {h(data.get('chat_id') or '—')}\n"
            f"Получатель: @{h(data.get('username') or '—')} · ID {h(data.get('recipient_id') or '—')}\n"
            f"Наборов: {h(data.get('units'))} · подарков: {len(data.get('delivery', []))}\n"
            + "".join(f"{label}: {n}\n" for label, n in counts.items())
            + f"\nПричина: <code>{h(str(data.get('last_error') or '—')[:600])}</code>\n")
    if order_id in PROCESSING_ORDERS:
        text += "\n⏳ Заказ сейчас выдаётся."
        kb.add(InlineKeyboardButton("⬅️ К списку", callback_data="ag:review"))
        return text, kb
    if uncertain_count(data):
        text += ("\n❓ Оплата части подарков не подтверждена. Откройте историю подарков получателя в Telegram "
                 "(или историю Stars аккаунта) и отметьте результат.")
        kb.add(InlineKeyboardButton("✅ Неизвестные — отправлены", callback_data=f"ag:rva:unknown_sent:{order_id}"))
        kb.add(InlineKeyboardButton("❌ Неизвестные — не отправлены", callback_data=f"ag:rva:unknown_failed:{order_id}"))
    elif data.get("status") in REVIEW_STATUSES and _ledger_valid(data) and any(
            i.get("status") in {"pending", "failed"} for i in data.get("delivery", [])):
        label = "🔁 Продолжить выдачу" if data.get("recipient_id") else "👤 Запросить ник у покупателя"
        kb.add(InlineKeyboardButton(label, callback_data=f"ag:rva:retry:{order_id}"))
    if data.get("status") not in TERMINAL_STATUSES:
        kb.add(InlineKeyboardButton("✅ Выдано вручную (закрыть)", callback_data=f"ag:rva:done_ask:{order_id}"))
        kb.add(InlineKeyboardButton("↩️ Вернуть деньги", callback_data=f"ag:rva:refund_ask:{order_id}"))
    kb.add(InlineKeyboardButton("⬅️ К списку", callback_data="ag:review"))
    return text, kb


def review_confirm(action: str, order_id: str) -> Tuple[str, InlineKeyboardMarkup]:
    data = ORDERS.get(order_id) or {}
    sent = delivered_count(data)
    if action == "refund_ask":
        text = (f"↩️ Вернуть деньги покупателю по заказу <b>#{h(order_id)}</b>?\n\n"
                + (f"⚠️ Уже отправлено подарков: {sent}. Они останутся у получателя.\n" if sent else "")
                + "Возврат оформляется на FunPay сразу.")
        target = "refund"
    else:
        text = f"✅ Отметить заказ <b>#{h(order_id)}</b> как выданный вручную? AutoGift больше не будет его обрабатывать."
        target = "done"
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(InlineKeyboardButton("Да", callback_data=f"ag:rva:{target}:{order_id}"),
           InlineKeyboardButton("Нет", callback_data=f"ag:rv:{order_id}"))
    return text, kb


def review_action(action: str, order_id: str, operator_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    c = CARDINAL_INSTANCE
    note = ""
    ask_username = False
    queued = False
    with order_guard(order_id):
        data = ORDERS.get(order_id)
        if not data:
            raise ValueError("Заказ не найден.")
        if order_id in PROCESSING_ORDERS or data.get("status") in {"sending", "refunding"}:
            raise ValueError("Заказ сейчас обрабатывается. Подождите.")
        if action in {"unknown_sent", "unknown_failed"}:
            for item in data.get("delivery", []):
                if item.get("status") in {"sending", "unknown"}:
                    item["status"] = "sent" if action == "unknown_sent" else "failed"
                    item["resolved_by"] = operator_id
                    item["resolved_at"] = time.time()
            data["last_error"] = "Результат отмечен оператором."
            if action == "unknown_sent" and all(i.get("status") == "sent" for i in data["delivery"]):
                data.update(status="completed", completed_at=time.time())
            save_order(data)
            note = "✅ Результат сохранён."
        elif action == "retry":
            if uncertain_count(data):
                raise ValueError("Сначала отметьте подарки с неизвестным результатом.")
            for item in data.get("delivery", []):
                if item.get("status") == "failed":
                    item["status"] = "pending"
                    item.pop("error", None)
            data.pop("ownership", None)
            if not data.get("recipient_id") or not data.get("username"):
                data["status"] = "await_username"
                data.pop("reminder_sent_at", None)
                ask_username = True
                note = "👤 Покупателю отправлен запрос ника."
            else:
                give_consent(data, by_operator=True)
                data["retried_by"] = operator_id
                save_order(data)
                queued = True
                note = "🔁 Заказ поставлен в очередь на выдачу."
        elif action == "done":
            data.update(status="completed", completed_at=time.time(), manual_completion=operator_id,
                        last_error="Закрыт оператором вручную.")
            save_order(data)
            note = "✅ Заказ закрыт."
        elif action != "refund":
            raise ValueError("Неизвестное действие.")
    if action == "refund":
        note = "↩️ Деньги возвращены." if buyer_refund(c, order_id, None, by_operator=True) else "❌ Возврат не подтверждён FunPay — проверьте заказ."
    if ask_username:
        _send_buyer_prompt(c, data, _username_prompt(data))
    if queued:
        enqueue_delivery(order_id, str(data.get("chat_id")))
    text, kb = review_card(order_id)
    return note + "\n\n" + text, kb


def operator_reaccept(order_id: str) -> Tuple[str, InlineKeyboardMarkup]:
    """An operator confirms that a tagged purchase whose snapshot changed belongs to its lot."""
    c = CARDINAL_INSTANCE
    if order_id in ORDERS:
        return f"Заказ #{h(order_id)} уже в журнале AutoGift.", review_button(order_id)
    full = _call_with_retry(c.account.get_order, order_id)
    tagged = tagged_lot_of_order(full)
    if not tagged:
        return "В заказе нет метки AutoGift — его нельзя принять автоматически.", back_keyboard()
    key, lot = tagged
    if not lot.get("enabled") or not _binding_valid(lot):
        return ("Сначала подтвердите привязку лота («Проверить тег») и включите выдачу, затем нажмите «Принять» снова.",
                lot_card(key)[1])
    title = getattr(full, "short_description", None) or getattr(full, "title", None)
    fingerprint = _fingerprint(title, getattr(full, "full_description", None))
    found, reason = match_order_to_lot(full, fingerprint)
    if not found:
        return f"❌ Заказ не принят: {h(OWNERSHIP_TEXT.get(reason, reason))}.", back_keyboard()
    SEEN_ORDERS.pop(order_id, None)
    if not RUNNING:
        return "⏸ Выдача выключена. Включите её — заказ можно будет принять снова.", back_keyboard()
    submit_event(accept_paid_order, c, order_id, None, "operator", fingerprint)
    return (f"✅ Заказ #{h(order_id)} принят в работу: покупателю придёт запрос Telegram-ника.", back_keyboard())


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
        if not ACCOUNTS:
            BOT.send_message(chat_id, "⚠️ Невозможно включить выдачу: добавьте хотя бы один Telegram-аккаунт. Меню: /autogift")
            return True
        CONFIG["running"] = True
        _safe_json_save(CONFIG_PATH, CONFIG)
        RUNNING = True
        BALANCE_REFRESH_REQUEST.set()
        SCAN_REQUEST.set()
        resume_ready_orders()
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
    RUNTIME.ensure()
    threading.Thread(target=auth_watcher_worker, daemon=True, name="AutoGiftAuthWatcher").start()
    threading.Thread(target=events_worker, daemon=True, name="AutoGiftEvents").start()
    threading.Thread(target=delivery_worker, daemon=True, name="AutoGiftDelivery").start()
    # Balances, lot protection, paid-order backfill, reminders and archiving run
    # here without blocking Cardinal's startup.
    threading.Thread(target=background_worker, daemon=True, name="AutoGiftBackground").start()
    if RUNNING:
        resume_ready_orders()


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
