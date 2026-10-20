"""AutoSMM для Starvell Cardinal: автоматическая продажа SMM-услуг на Starvell через SMM-панели (API v2).

Возможности: привязка лотов Starvell к услугам панелей, точное распознавание заказа по ID лота,
очередь заказов покупателя, запуск и слежение за заказами, автовозвраты, автоцена с конвертацией
валют, защита от низкого баланса, вотчер изменений услуг, рефилл, опросы, пауза покупателя,
готовые красивые тексты для лотов, прокси, бэкапы.

Совместим со сборками Starvell Cardinal на aiogram 3 (Hackep1551 и etheriumflipper).
Starvell не даёт создавать лоты и менять их тексты через API — лот создаётся на сайте,
а плагин готовит для него текст и затем работает с лотом по его ID.
"""
from __future__ import annotations

import asyncio
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
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, quote_plus, urljoin, urlparse, urlunparse

try:
    import requests
except ImportError:  # Starvell Cardinal ставит только aiohttp — ниже подключается обёртка над ним
    requests = None

BeautifulSoup = None
LotFields = None
enums = SimpleNamespace(OrderStatuses=SimpleNamespace(PAID=None))


class NewMessageEvent:  # совместимость общего кода с FunPay-версией
    pass


class NewOrderEvent:
    pass


class _TelegramButton:
    """Кнопка в стиле telebot; в aiogram превращается при отправке."""
    def __init__(self, text: str, callback_data: Optional[str] = None, url: Optional[str] = None):
        self.text, self.callback_data, self.url = text, callback_data, url


class InlineKeyboardMarkup:
    """Клавиатура в стиле telebot (row/add), чтобы общий код меню работал без изменений."""
    def __init__(self, row_width: int = 3, keyboard: Optional[List[List[_TelegramButton]]] = None):
        self.row_width = max(1, row_width)
        self.keyboard: List[List[_TelegramButton]] = keyboard or []

    def row(self, *buttons: _TelegramButton) -> 'InlineKeyboardMarkup':
        self.keyboard.append([b for b in buttons if b is not None])
        return self

    def add(self, *buttons: _TelegramButton) -> 'InlineKeyboardMarkup':
        items = [b for b in buttons if b is not None]
        for i in range(0, len(items), self.row_width):
            self.keyboard.append(items[i:i + self.row_width])
        return self

NAME = "AutoSMM"
VERSION = "1.0.3"
DESCRIPTION = "Автоматическая продажа SMM-услуг на Starvell: заказы через SMM-панели, автоцена, рефилл"
AUTHOR = "@LuckyFunPay_Support"
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "6f2d1c3a-8b4e-4f7a-9c2d-5e8b1a0f3c47"
SETTINGS_PAGE = False

# Возможности площадки: общий код меню и заказов один для FunPay и Starvell.
PLATFORM = 'starvell'
MARKET = 'Starvell'
CAN_CREATE_LOTS = False     # Starvell не даёт создавать лоты и менять тексты через API
CAN_DELETE_LOTS = False
HAS_LOT_TAGS = False        # заказ определяется по ID лота (offerId) из данных заказа
ORDER_URL = 'https://starvell.com/order/{}'
LOT_URL = 'https://starvell.com/offers/{}'
LOT_ID_HINT = 'число из ссылки на лот (<code>starvell.com/offers/ЧИСЛО</code>)'
MAX_MESSAGE_LEN = 1000      # лимит сообщения в чате Starvell

ROOT = os.path.join("storage", "plugins", UUID)
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

logger = logging.getLogger("starvell.autosmm")
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
_fp_lock = threading.Lock()          # пауза между запросами к Starvell
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


