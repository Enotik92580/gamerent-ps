"""AutoSMM: автоматическая продажа SMM-услуг на FunPay через SMM-панели (API v2).

Возможности: автовыгрузка и ручное создание лотов, красивое оформление по
типу услуги и соцсети, тег #SMM для точного распознавания заказа, очередь
заказов покупателя, автоцена с конвертацией валют, защита от низкого баланса,
вотчер изменений услуг, рефилл, опросы, пауза покупателя, прокси, бэкапы.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import copy, deepcopy
from datetime import datetime
from html import escape, unescape
import json
import logging
import math
import os
import re
import secrets
import shutil
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, quote_plus, urljoin, urlparse, urlunparse

import requests
from FunPayAPI import enums
from FunPayAPI.types import LotFields
from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent
from telebot.types import InlineKeyboardButton as _TelegramButton, InlineKeyboardMarkup

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - зависит от окружения Cardinal
    BeautifulSoup = None

NAME = "AutoSMM"
VERSION = "23.2.0"
DESCRIPTION = "Автоматическая продажа SMM-услуг: автовыгрузка, ручные лоты, автоцена, рефилл"
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "c800e7e9-05ce-43eb-addc-4f5841f79726"
SETTINGS_PAGE = False

# Возможности площадки: общий код меню и заказов один для FunPay и Starvell.
PLATFORM = 'funpay'
MARKET = 'FunPay'
CAN_CREATE_LOTS = True      # создание, автовыгрузка и переоформление лотов
CAN_DELETE_LOTS = True
HAS_LOT_TAGS = True         # заказ определяется по тегу #SMM в описании
ORDER_URL = 'https://funpay.com/orders/{}/'
LOT_URL = 'https://funpay.com/lots/offer?id={}'
LOT_ID_HINT = 'число из ссылки редактирования лота (<code>offerEdit?…offer=ЧИСЛО</code>)'
MAX_MESSAGE_LEN = 2000

ROOT = os.path.join("storage", "cache")
CONFIG_PATH = os.path.join(ROOT, "auto_lots.json")
ORDERS_PATH = os.path.join(ROOT, "auto_smm_orders.json")
WAITING_PATH = os.path.join(ROOT, "auto_smm_waiting.json")
DOMAINS_PATH = os.path.join(ROOT, "auto_smm_domains.json")
STATE_PATH = os.path.join(ROOT, "auto_smm_state.json")
BACKUP_DIR = os.path.join(ROOT, "autosmm_backups")
LEGACY_ORDERS_PATH = os.path.join(ROOT, "orders_data.json")
LEGACY_WAITING_PATH = os.path.join(ROOT, "waiting_links.json")
os.makedirs(ROOT, exist_ok=True)

SCHEMA_VERSION = 14
TAG_RE = re.compile(r'#SMM:([0-9a-f]{8})', re.I)

logger = logging.getLogger("FPC.autosmm")
logger.setLevel(logging.INFO)

bot = None
cardinal_instance = None
RUNNING = False
_READY = False
_lock = threading.RLock()            # файловые операции
_cfg_lock = threading.RLock()        # транзакции настроек
_orders_lock = threading.RLock()     # история заказов
_wlock = threading.RLock()           # ожидающие заказы
_state_lock = threading.RLock()      # служебное состояние
_fp_lock = threading.Lock()          # пауза между запросами к FunPay
_order_locks: Dict[str, threading.Lock] = {}
_order_locks_guard = threading.Lock()
waiting: Dict[str, Dict[str, Any]] = {}
_service_cache: Dict[str, Tuple[float, List[Dict[str, Any]], Tuple[str, str, str, str]]] = {}
_wizards: Dict[int, Dict[str, Any]] = {}
_threads: Dict[str, threading.Thread] = {}
_jobs: Dict[str, Dict[str, Any]] = {}
_pending_admin: List[str] = []
_refill_choices: Dict[str, Dict[str, Any]] = {}
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='autosmm')
_last_fp_call = [0.0]


