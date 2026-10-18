"""Auto TT — single-file FunPay Cardinal plugin, Python 3.10+."""
from __future__ import annotations

import copy
import html
import ipaddress
import json
import logging
import re
import sqlite3
import string
import threading
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import requests

NAME = "Auto TT"
VERSION = "1.0.0"
DESCRIPTION = "Автовыдача TikTok-аккаунтов с LZT Market. Панель: /tt_panel"
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
CHANNEL_URL = "https://t.me/LuckyFunPay"
UUID = "bf986504-e32a-4804-aeda-c55e35805f76"
SETTINGS_PAGE = True
PREFIX = "lftt:"
DATA_PATH = Path("storage/plugins/AutoTT")
LOG = logging.getLogger("FPC.AutoTT")
D = Decimal
VARIABLES = {"login", "password", "email", "email_password", "email_login_url",
             "followers", "likes", "videos", "region", "item_id", "order_id"}
DEFAULT_TEMPLATE = """🎵 Ваш TikTok аккаунт

👤 Логин: {login}
🔑 Пароль: {password}

📧 Почта: {email}
🔐 Пароль от почты: {email_password}
🌐 Вход в почту: {email_login_url}

📊 Статистика:
👥 Подписчики: {followers}
❤️ Лайки: {likes}
📹 Видео: {videos}
🌍 Регион: {region}

Спасибо за покупку! Буду благодарен за отзыв."""
DEFAULT_WAIT = """👋 Заказ принят в работу!
🤖 Система подбирает лучший аккаунт по вашим критериям...
⏳ Ожидайте выдачу в течение 1-3 минут."""
DEFAULTS = {"enabled": False, "token": "", "proxy_mode": "bot", "proxy": "",
            "auto_refund": True, "balance_alert": True, "balance_threshold": "50",
            "low_notified": False, "template": DEFAULT_TEMPLATE, "wait_text": DEFAULT_WAIT,
            "sellers": [], "profiles": {}}
ACTIVE_STATES = {"queued", "searching", "buying", "uncertain", "purchased", "delivery_pending", "refund_pending"}
TERMINAL_STATES = {"delivered", "refunded", "cancelled", "manual", "resolved"}


class TTError(Exception):
    pass


class APIError(TTError):
    def __init__(self, kind, message="", status=0):
        super().__init__(message or kind)
        self.kind, self.status = kind, status
        self.detail = ''


def money(value, positive=False):
    raw = str(value).strip().replace(",", ".")
    if not re.fullmatch(r"\d{1,10}(?:\.\d{1,2})?", raw):
        raise TTError("Введите сумму в рублях, например 500 или 150,50.")
    try:
        number = D(raw)
    except InvalidOperation:
        raise TTError("Неверная сумма.") from None
    if positive and number <= 0:
        raise TTError("Сумма должна быть больше нуля.")
    return number


def enum(value):
    return str(getattr(value, "name", value)).upper().split(".")[-1]


def esc(value):
    return html.escape(str(value))


def tag(value):
    # Sellers often paste the whole lot tag (tt:USA); keep only the part after the prefix.
    raw = re.sub(r"^(?:tt)\s*:\s*", "", str(value).strip(), flags=re.I).strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{1,25}", raw):
        raise TTError("Тег: 1–25 латинских букв, цифр, _ или -, без пробелов. Пример: USA или tt:USA.")
    return raw


def find_tag(description):
    found = re.findall(r"(?<![\w:])tt:([A-Za-z0-9_-]{1,25})(?![\w-])", description or "", re.I)
    unique = set(t.upper() for t in found)
    if len(unique) > 1:
        raise TTError("В описании несколько разных тегов tt:. Оставьте один.")
    return next(iter(unique), None)


def search_url(value):
    raw = str(value).strip()
    parts = urlsplit(raw)
    if (len(raw) > 3500 or parts.scheme != "https" or parts.hostname != "lzt.market"
            or parts.username or parts.password or parts.port not in {None, 443} or parts.fragment
            or not re.fullmatch(r"/tiktok(?:/[a-z0-9_-]+)*/?", parts.path)):
        raise TTError("Нужна HTTPS-ссылка lzt.market из раздела /tiktok/ или его подраздела.")
    return raw


def proxy_url(value):
    raw = str(value).strip()
    if raw == "-": return ""
    if "://" not in raw: raw = "http://" + raw
    try:
        parts = urlsplit(raw)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.path not in {"", "/"} or parts.query or parts.fragment
                or not parts.port or not 1 <= parts.port <= 65535
                or ipaddress.ip_address(parts.hostname).version != 4):
            raise ValueError()
    except ValueError:
        raise TTError("Прокси: http://IPv4:PORT или http://логин:пароль@IPv4:PORT.") from None
    return raw.rstrip("/")


def validate_template(value):
    if not value.strip() or len(value) > 2500:
        raise TTError("Шаблон должен содержать 1–2500 символов.")
    try:
        for _, field, spec, conversion in string.Formatter().parse(value):
            if field is not None and (field not in VARIABLES or spec or conversion):
                raise ValueError()
        value.format(**{v: "TEST" for v in VARIABLES})
    except (ValueError, KeyError, IndexError, AttributeError):
        raise TTError("Неверные скобки или переменные шаблона. Разрешены: " + ", ".join(sorted(VARIABLES))) from None
    return value


def clean_error(value):
    text = html.unescape(re.sub(r"<[^>]*>", "", str(value)))
    text = re.sub(r"(?:https?://|www\.)\S+", "[скрыто]", text, flags=re.I)
    text = re.sub(r"(?:lzt(?:\.market)?|lolz(?:team|\.live)?|market|маркет)", "[скрыто]", text, flags=re.I)
    return text[:500]


def provider_error(data):
    errors = data.get('errors') or data.get('error') or data.get('message') or ''
    if isinstance(errors, dict): errors = list(errors.values())
    if isinstance(errors, list): errors = ' '.join(str(e) for e in errors)
    return clean_error(errors).strip()


def classify_error(data, status):
    raw = json.dumps(data.get('errors') or data.get('error') or data.get('message') or '', ensure_ascii=False).casefold()
    if status == 401: return "access"
    if any(x in raw for x in ("retry_request", "retry request")): return "retry"
    if any(x in raw for x in ("not enough balance", "not have enough money", "insufficient funds", "недостаточно средств", "недостаточно денег", "недостаточно баланса")): return "balance"
    if any(x in raw for x in ("paid access", "api access", "market scope", "доступ к api", "доступа к api", "200 sympath")): return "access"
    if status == 429 or any(x in raw for x in ("daily check limit", "daily limit of checks", "rate_limit")): return "rate"
    if any(x in raw for x in ("item is sold", "item deleted", "requested item not found", "ad was removed", "account is invalid", "account invalid", "невалид", "аккаунт продан", "аккаунт удален", "аккаунт удалён", "seller have added you", "seller has added you")): return "invalid"
    if status >= 500: return "network"
    return "unknown"


class Store:
    """SQLite journal. Every purchase intent is committed before the HTTP request."""
    def __init__(self, root=DATA_PATH):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "tt.sqlite3"
        with self.tx() as db:
            db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, state TEXT NOT NULL, value TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS reserved (item TEXT PRIMARY KEY, oid TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS blacklist (item TEXT PRIMARY KEY, stamp REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS messages (key TEXT PRIMARY KEY, stamp REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, stamp REAL, event TEXT, oid TEXT)")
            row = db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()
            if row is None:
                db.execute("INSERT INTO kv VALUES ('settings',?)", (json.dumps(DEFAULTS, ensure_ascii=False),))
            else:
                saved = json.loads(row[0])
                if not isinstance(saved, dict): raise TTError("Повреждены настройки. Восстановите резервную копию.")

    @contextmanager
    def tx(self):
        db = sqlite3.connect(str(self.path), timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def settings(self):
        with self.tx() as db:
            row = db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()
        result = copy.deepcopy(DEFAULTS)
        result.update(json.loads(row[0]))
        return result

    def change(self, fn):
        with self.tx() as db:
            data = json.loads(db.execute("SELECT value FROM kv WHERE key='settings'").fetchone()[0])
            result = fn(data)
            db.execute("UPDATE kv SET value=? WHERE key='settings'", (json.dumps(data, ensure_ascii=False),))
        return result

    def setting(self, key, value):
        if key not in DEFAULTS: raise TTError("Неизвестная настройка.")
        return self.change(lambda data: data.update({key: value}))

    def get(self, oid):
        with self.tx() as db:
            row = db.execute("SELECT value FROM orders WHERE id=?", (str(oid),)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, order):
        with self.tx() as db:
            db.execute("INSERT OR REPLACE INTO orders VALUES (?,?,?)",
                       (order['id'], order['state'], json.dumps(order, ensure_ascii=False)))

    def enqueue(self, oid):
        order = {"id": str(oid), "state": "queued", "created": time.time(), "attempts": 0}
        with self.tx() as db:
            result = db.execute("INSERT OR IGNORE INTO orders VALUES (?,?,?)",
                                (order['id'], order['state'], json.dumps(order)))
            return bool(result.rowcount)

    def orders(self):
        with self.tx() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT value FROM orders ORDER BY rowid DESC")]

    def active_orders(self):
        marks = ','.join('?'*len(ACTIVE_STATES))
        with self.tx() as db:
            return [json.loads(r[0]) for r in db.execute(
                f"SELECT value FROM orders WHERE state IN ({marks}) ORDER BY rowid ASC", tuple(ACTIVE_STATES))]

    def reserve(self, item, oid):
        with self.tx() as db:
            return bool(db.execute("INSERT OR IGNORE INTO reserved VALUES (?,?)", (str(item), oid)).rowcount)

    def reject(self, item, oid):
        with self.tx() as db:
            db.execute("DELETE FROM reserved WHERE item=? AND oid=?", (str(item), oid))
            db.execute("INSERT OR REPLACE INTO blacklist VALUES (?,?)", (str(item), time.time()))
            db.execute("DELETE FROM blacklist WHERE item NOT IN (SELECT item FROM blacklist ORDER BY stamp DESC LIMIT 1000)")

    def excluded(self):
        with self.tx() as db:
            return {r[0] for r in db.execute("SELECT item FROM blacklist UNION SELECT item FROM reserved")}

    def release(self, item, oid):
        with self.tx() as db:
            db.execute("DELETE FROM reserved WHERE item=? AND oid=?", (str(item), oid))

    def audit(self, event, oid=""):
        with self.tx() as db:
            db.execute("INSERT INTO audit(stamp,event,oid) VALUES(?,?,?)", (time.time(), event, oid))
            db.execute("DELETE FROM audit WHERE id NOT IN (SELECT id FROM audit ORDER BY id DESC LIMIT 10000)")

    def message_once(self, key):
        with self.tx() as db:
            result = bool(db.execute("INSERT OR IGNORE INTO messages VALUES (?,?)", (key, time.time())).rowcount)
            db.execute("DELETE FROM messages WHERE stamp<?", (time.time()-7*86400,))
            return result

    def blacklist_count(self):
        with self.tx() as db:
            return db.execute('SELECT COUNT(*) FROM blacklist').fetchone()[0]


class Market:
    BASE = "https://api.lzt.market"
    def __init__(self, store, cardinal, transport=None):
        self.store, self.cardinal = store, cardinal
        self.transport = transport
        self.rate_lock = threading.Lock()
        self.next_request = 0.0

    def request(self, method, path, params=None, body=None, token_override=None):
        settings = self.store.settings()
        token = settings['token'] if token_override is None else token_override
        if not token: raise APIError("access", "Не установлен токен LZT.")
        if not path.startswith('/') or '..' in path or '?' in path:
            raise TTError("Некорректный путь API.")
        proxy = settings['proxy'] if settings['proxy_mode'] == 'custom' and settings['proxy'] else getattr(self.cardinal, 'proxy', {})
        proxies = {"http": proxy, "https": proxy} if isinstance(proxy, str) else dict(proxy or {})
        if self.transport is None:
            with self.rate_lock:
                delay = self.next_request-time.monotonic()
                if delay > 0: time.sleep(delay)
                # Conservative global pace also covers category-search's 20/min limit.
                self.next_request = time.monotonic()+3.1
        try:
            with requests.Session() as session:
                session.trust_env = False
                caller = self.transport or session.request
                response = caller(method, self.BASE+path, params=params, json=body,
                                  headers={"Authorization": "Bearer "+token, "Accept": "application/json",
                                           "User-Agent": "AutoTT/"+VERSION},
                                  proxies=proxies, timeout=(15, 310 if method != 'GET' else 60),
                                  allow_redirects=False)
                try:
                    data = response.json()
                except (ValueError, TypeError):
                    raise APIError("network", "API вернул не JSON.", response.status_code) from None
        except requests.RequestException:
            raise APIError("network", "Сетевая ошибка при обращении к LZT.") from None
        if not isinstance(data, dict): raise APIError("network", "Неверная структура ответа API.")
        if response.status_code >= 300 or data.get('errors') or data.get('error') or data.get('status') == 'error':
            kind = classify_error(data, response.status_code)
            # Provider bodies can contain credentials; never persist or log them as errors.
            descriptions = {"access": "Проверьте токен, права market и доступ к API.", "balance": "Недостаточно средств на выбранном балансе.",
                            "invalid": "Аккаунт невалиден, продан, удалён или продавец запретил покупку.",
                            "retry": "Маркет просит повторить запрос.", "rate": "Лимит запросов API."}
            error = APIError(kind, descriptions.get(kind, "API не подтвердил выполнение запроса."), response.status_code)
            error.detail = provider_error(data)
            raise error
        return data

    def profile(self, token_override=None):
        data = self.request('GET', '/me', token_override=token_override)
        user = data.get('user')
        if not isinstance(user, dict) or not str(user.get('user_id', '')).isdigit():
            raise APIError('unknown', 'API не вернул профиль пользователя.')
        if str(user.get('currency', '')).lower() != 'rub':
            raise APIError('currency', 'Установите валюту RUB в настройках аккаунта LZT Market.')
        balances = [(None, money(user.get('balance', '0')), 'Основной')]
        seen = set()
        for entry in user.get('balances', []):
            bid = entry.get('balance_id')
            if bid is None or str(bid) in seen: continue
            seen.add(str(bid))
            balances.append((int(bid), money(entry.get('balance', '0')), str(entry.get('title', 'Дополнительный'))))
        return int(user['user_id']), balances

    def search(self, profile):
        parts = urlsplit(search_url(profile['url']))
        base = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                if k.split('[')[0].casefold() not in
                {'pmax', 'page', 'currency', 'fields_include', 'fields_exclude', 'cookies', 'cookie_login'}]
        base += [('pmax', profile['max_price']), ('currency', 'rub'), ('cookies', 'no'), ('cookie_login', 'no')]
        if not any(k == 'order_by' for k, _ in base): base.append(('order_by', 'price_to_up'))
        for page in range(1, 6):
            data = self.request('GET', parts.path.rstrip('/'), base+[('page', page)])
            items = data.get('items')
            if not isinstance(items, list): raise APIError('unknown', 'API не вернул список аккаунтов.')
            yield from items
            if not data.get('hasNextPage', False): break

    def item(self, item_id):
        data = self.request('GET', '/'+str(item_id))
        item = data.get('item')
        if not isinstance(item, dict) or str(item.get('item_id')) != str(item_id):
            raise APIError('unknown', 'API не вернул запрошенный аккаунт.')
        return item

    def buy(self, item_id, price, balance_id):
        body = {'price': float(money(price, True))}
        if balance_id is not None: body['balance_id'] = balance_id
        return self.request('POST', '/'+str(item_id)+'/fast-buy', body=body)


# Listing states in which nobody has bought the account yet. A purchased item is "paid" on LZT
# (the API has no "sold" state), so ownership is decided by the buyer block, not by one state name.
LISTING_STATES = {'active', 'stickied', 'awaiting', 'pre_active', 'pre_upload'}


def truthy(value):
    return value is True or str(value).strip().casefold() in {'1', 'true', 'yes'}


def owned(item, user_id):
    buyer = item.get('buyer') or {}
    if not isinstance(buyer, dict): return False
    return (truthy(buyer.get('visitorIsBuyer')) and str(buyer.get('user_id')) == str(user_id)
            and str(item.get('item_state', '')).casefold() not in LISTING_STATES)


def not_purchased(item, order):
    """Why the reserved listing is certainly not ours, or None while that is still unclear."""
    buyer = item.get('buyer')
    state = str(item.get('item_state', '')).casefold()
    if (isinstance(buyer, dict) and buyer.get('user_id') not in (None, '', 0)
            and not owned(item, order.get('market_user')) and state not in LISTING_STATES):
        return 'его купил другой покупатель'
    # Still on sale with no buyer long after our request: the purchase did not happen.
    if state in LISTING_STATES and not buyer and time.time()-order.get('buy_started', time.time()) > 600:
        return 'он по-прежнему выставлен на продажу, покупка не прошла'
    return None


def tiktok_item(item):
    return isinstance(item, dict) and str(item.get('category_id')) == '20'


def password_login(item):
    """Reject accounts the market marks as cookie-login. The search already sends cookie_login=no,
    so a response without the flag (e.g. a fast-buy body) is not treated as cookie-login."""
    values = [item[k] for k in ('tiktok_cookie_login', 'cookie_login') if k in item and item[k] is not None]
    return all(str(v).strip().casefold() in {'0', 'false', 'no', ''} for v in values)


def select_profile(profiles, selected):
    if selected in profiles: return selected, profiles[selected]
    if 'ANY' in profiles: return 'ANY', profiles['ANY']
    if len(profiles) == 1: return next(iter(profiles.items()))
    return selected, None


def credentials(item, oid):
    data = item.get('loginData') or {}
    if not isinstance(data, dict): data = {}
    def usable(value):
        return isinstance(value, str) and bool(value.strip()) and value.strip() not in {'0', '1'}
    # Per the guide a placeholder login/password is shown as an error mark; the seller is notified.
    login = data['login'] if usable(data.get('login')) else 'Ошибка (см. на LZT)'
    password = data['password'] if usable(data.get('password')) else 'Ошибка (см. на LZT)'
    email_data = item.get('emailLoginData') or {}
    if not isinstance(email_data, dict): email_data = {}
    email = email_data.get('login') or item.get('temp_email') or 'Нет'
    email_password = email_data.get('password') if usable(email_data.get('password')) else 'Нет'
    # Keep zero values. Missing statistics must not be advertised as zero.
    def statistic(key):
        value = item.get('tiktok_'+key)
        return str(value) if str(value).isdigit() else 'Неизвестно'
    region = item.get('tiktok_top_country') or item.get('tiktok_country') or item.get('country') or 'Неизвестно'
    return {'login': str(login), 'password': str(password), 'email': str(email),
            'email_password': str(email_password), 'email_login_url': str(item.get('emailLoginUrl') or 'Нет'),
            'followers': statistic('followers'), 'likes': statistic('likes'), 'videos': statistic('videos'),
            'region': str(region), 'item_id': str(item['item_id']), 'order_id': oid}


class Shop:
    def __init__(self, cardinal, store=None, market=None):
        self.cardinal = cardinal
        self.store = store or Store()
        self.market = market or Market(self.store, cardinal)
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.RLock()
        self.purchase_lock = threading.Lock()
        self.inflight = set()
        self.threads = []
        self.ui = None
        self.balance_cache = None
        self.balance_stamp = 0

    def notify(self, text):
        tg = getattr(self.cardinal, 'telegram', None)
        if tg:
            for uid in getattr(tg, 'authorized_users', {}):
                try: tg.bot.send_message(int(uid), text, parse_mode='HTML', disable_web_page_preview=True)
                except Exception: LOG.warning('Telegram notification failed')

    def say(self, order, text):
        try:
            # Use account directly: credentials must never be parsed as Cardinal directives ($photo/$sleep).
            result = self.cardinal.account.send_message(order['chat'], text, order['buyer_name'])
            return bool(result)
        except Exception:
            LOG.warning('FunPay delivery failed for order %s', order['id'])
            return False

    def blocked(self, name):
        return name.casefold() in {str(n).casefold() for n in getattr(self.cardinal, 'blacklist', [])}

    def paid_order(self, oid):
        try: order = self.cardinal.account.get_order(oid)
        except Exception: raise TTError('Не удалось проверить заказ FunPay.') from None
        if str(getattr(order, 'id', '')).lstrip('#') != str(oid):
            raise TTError('FunPay вернул другой заказ.')
        if str(getattr(order, 'seller_id', '')) != str(self.cardinal.account.id):
            raise TTError('Заказ принадлежит другому продавцу.')
        return order

    def hold(self, order, reason, uncertain=False):
        order['state'] = 'uncertain' if uncertain else 'manual'
        order['reason'] = reason
        order['check_after'] = time.time()+60
        self.store.save(order)
        self.store.audit('uncertain' if uncertain else 'manual', order['id'])
        self.notify(f"⚠️ {NAME} #{esc(order['id'])}\n{esc(reason)}\n"
                    + (f"Проверяю покупку https://lzt.market/{order.get('item_id', '')}/. Повторная закупка и возврат остановлены."
                       if uncertain else "Заказ ожидает ручного решения."))

    def refund(self, order, reason, force=False):
        if order.get('item_id') or order.get('payload'):
            self.hold(order, 'Возврат заблокирован: покупка начата или товар уже получен.')
            return
        if not force and not self.store.settings()['auto_refund']:
            self.hold(order, reason+' Автовозврат выключен.')
            return
        order['state'] = 'refund_pending'
        order['reason'] = reason
        self.store.save(order)
        try:
            fresh = self.paid_order(order['id'])
            state = enum(fresh.status)
            if state == 'PAID':
                self.cardinal.account.refund(order['id'])
                fresh = self.paid_order(order['id'])
                state = enum(fresh.status)
            if state != 'REFUNDED':
                if state != 'PAID':
                    self.hold(order, 'FunPay изменил статус заказа до подтверждения возврата. Требуется сверка.')
                    return
                order['check_after'] = time.time()+60
                self.store.save(order)
                self.notify(f"⚠️ #{esc(order['id'])}: возврат пока не подтверждён FunPay. Проверю статус.")
                return
        except Exception:
            order['check_after'] = time.time()+60
            self.store.save(order)
            self.notify(f"⚠️ #{esc(order['id'])}: ошибка возврата. Проверю статус перед следующим действием.")
            return
        order['state'] = 'refunded'
        self.store.save(order)
        self.store.audit('refunded', order['id'])
        text = 'К сожалению, подходящих аккаунтов сейчас нет. Средства возвращены автоматически.'
        if reason == 'quantity': text = 'Средства возвращены. Пожалуйста, заказывайте по 1 аккаунту за заказ.'
        self.say(order, text)
        self.notify(f"🔄 TikTok Автовозврат #{esc(order['id'])}\nПричина: {esc(reason)}")

    def process(self, oid):
        order = self.store.get(oid)
        if not order: return
        try:
            state = order['state']
            if state in {'buying', 'uncertain'}:
                self.reconcile(order)
                return
            if state in {'purchased', 'delivery_pending'}:
                self.deliver(order)
                return
            if state == 'refund_pending':
                self.refund(order, order['reason'], force=True)
                return
            if state not in {'queued', 'searching'} or not self.store.settings()['enabled']: return
            fresh = self.paid_order(oid)
            if enum(fresh.status) != 'PAID':
                order['state'] = 'cancelled'; self.store.save(order); return
            text = '\n'.join(str(getattr(fresh, k, '') or '') for k in ('full_description', 'short_description', 'description'))
            selected = find_tag(text)
            if not selected:
                order['state'] = 'cancelled'; self.store.save(order); return
            order.update({'tag': selected, 'buyer_id': str(fresh.buyer_id), 'buyer_name': str(fresh.buyer_username or ''),
                          'chat': fresh.chat_id, 'quantity': fresh.amount})
            if not order['chat'] or not order['buyer_id'].isdigit() or int(order['buyer_id']) <= 0:
                self.hold(order, 'Не определён чат или покупатель заказа.'); return
            self.store.save(order)
            if self.blocked(order['buyer_name']) and getattr(self.cardinal, 'bl_delivery_enabled', False):
                self.refund(order, 'Покупатель в чёрном списке бота.', force=True); return
            if fresh.amount is None:
                self.hold(order, 'Не определено количество товара.'); return
            if fresh.amount != 1:
                self.refund(order, 'quantity', force=True); return
            settings = self.store.settings()
            profile_key, profile = select_profile(settings['profiles'], selected)
            if not profile or not profile.get('url'):
                self.hold(order, f'Не найден настроенный TikTok-профиль для тега {selected}.'); return
            order['profile'] = profile_key
            if not settings['token']:
                self.hold(order, 'Не установлен токен LZT.'); return
            if enum(getattr(fresh, 'currency', '')) != 'RUB':
                self.hold(order, 'Версия 1.0.0 поддерживает заказы FunPay только в RUB.'); return
            order['revenue'] = str(money(fresh.sum))
            if not order.get('wait_sent') and settings['wait_text']:
                order['wait_sent'] = self.say(order, settings['wait_text'])
            order['state'] = 'searching'
            self.store.save(order)
            self.search_and_buy(order, profile)
        except APIError as error:
            if order.get('item_id'):
                self.hold(order, str(error), uncertain=order['state'] in {'buying', 'uncertain'})
            else:
                self.hold(order, str(error))
                self.say(order, 'Заказ передан продавцу для ручной обработки. Ожидайте ответа.')
        except TTError as error:
            self.hold(order, str(error), uncertain=bool(order.get('item_id') and order['state'] in {'buying', 'uncertain'}))
        except Exception:
            LOG.error('Processing error for order %s (details omitted to protect credentials)', oid)
            self.hold(order, 'Внутренняя ошибка. Проверьте заказ в панели.', uncertain=bool(order.get('item_id') and order['state'] in {'buying', 'uncertain'}))

    def search_and_buy(self, order, profile):
        candidates = []
        for item in self.market.search(profile):
            if self.stop_event.is_set() or not self.store.settings()['enabled']: return
            if not tiktok_item(item) or not password_login(item): continue
            try: price = money(item.get('price'), True)
            except TTError: continue
            if price <= money(profile['max_price'], True): candidates.append((price, item))
        # Sort the bounded five-page result, including listings returned out of order.
        candidates.sort(key=lambda pair: pair[0])
        for price, item in candidates:
            if self.stop_event.is_set() or not self.store.settings()['enabled']: return
            item_id = str(item.get('item_id', ''))
            if not re.fullmatch(r'[1-9]\d{0,15}', item_id): continue
            if item_id in self.store.excluded(): continue
            seller = str((item.get('seller') or {}).get('username', '')).casefold()
            if not seller or seller in {s.casefold() for s in self.store.settings()['sellers']}: continue
            if item.get('item_state') != 'active' or item.get('canBuyItem') is False: continue
            # Fast-buy expects price in the LZT user's currency, enforced RUB by /me.
            if str(item.get('price_currency', '')).lower() != 'rub': continue
            try: price = money(item.get('price'), True)
            except TTError: continue
            if price > money(profile['max_price'], True): continue
            with self.purchase_lock:
                if self.stop_event.is_set() or not self.store.settings()['enabled']: return
                fresh = self.paid_order(order['id'])
                if enum(fresh.status) != 'PAID':
                    order['state'] = 'cancelled'; self.store.save(order); return
                user_id, balances = self.market.profile()
                available = [(bid, amount) for bid, amount, _ in balances if amount >= price]
                if not available:
                    self.notify('💰 Не хватает средств на одном из балансов LZT для заказа #'+esc(order['id']))
                    self.refund(order, 'Недостаточно средств на LZT.', force=True); return
                if not self.store.reserve(item_id, order['id']): continue
                order.update({'item_id': item_id, 'market_user': user_id, 'cost': str(price),
                              'state': 'buying', 'buy_started': time.time(), 'candidate': item})
                self.store.save(order)  # committed before any mutating API call
                result = self.attempt_purchase(order, available)
                if result == 'next': continue
                return
        self.refund(order, 'Не найдены или не куплены подходящие аккаунты.')

    def attempt_purchase(self, order, balances):
        for balance_id, _ in balances:
            for attempt in range(10):
                if self.stop_event.is_set():
                    self.hold(order, 'Остановка во время покупки: требуется сверка.', uncertain=True); return 'stop'
                order['balance_id'] = balance_id
                self.store.save(order)
                try:
                    data = self.market.buy(order['item_id'], order['cost'], balance_id)
                    purchased = data.get('item') or {}
                    if (str(purchased.get('item_id')) == order['item_id'] and tiktok_item(purchased)
                            and owned(purchased, order['market_user'])):
                        self.accept_purchase(order, purchased); return 'done'
                    # A 200 without ownership evidence is also ambiguous.
                    self.reconcile(order); return 'stop'
                except APIError as error:
                    try: item = self.market.item(order['item_id'])
                    except APIError as lookup:
                        if lookup.kind != 'invalid' or error.kind != 'invalid':
                            self.hold(order, 'Нет подтверждения результата покупки и не удалось проверить владельца.', uncertain=True)
                            return 'stop'
                        item = {}
                    except TTError:
                        self.hold(order, 'Нет подтверждения результата покупки и не удалось проверить владельца.', uncertain=True)
                        return 'stop'
                    if owned(item, order['market_user']):
                        self.accept_purchase(order, item); return 'done'
                    if error.kind == 'retry':
                        if self.stop_event.wait(3): return 'stop'
                        continue
                    if error.kind == 'balance': break  # try another funded balance after reconciliation
                    if error.kind == 'invalid':
                        self.store.reject(order['item_id'], order['id'])
                        self.notify(f"⚠️ TikTok: пропущен аккаунт {order['item_id']} — не куплен.")
                        order.pop('item_id', None)
                        order['state'] = 'searching'; self.store.save(order)
                        return 'next'
                    if error.kind in {'access', 'rate'}:
                        # These definitive refusals did not execute the purchase.
                        self.store.release(order['item_id'], order['id'])
                        order.pop('item_id', None)
                        self.hold(order, str(error)); return 'stop'
                    self.hold(order, 'Маркет не подтвердил результат покупки. Проверяю владельца.', uncertain=True)
                    return 'stop'
            else:
                self.hold(order, 'Маркет продолжает возвращать retry_request. Проверяю покупку.', uncertain=True)
                return 'stop'
        self.store.release(order['item_id'], order['id'])
        order.pop('item_id', None)
        order['state'] = 'searching'; self.store.save(order)
        self.refund(order, 'Недостаточно средств на доступных балансах LZT.', force=True)
        return 'stop'

    def reconcile(self, order):
        item = None
        try:
            item = self.market.item(order['item_id'])
            if owned(item, order['market_user']):
                self.accept_purchase(order, item)
                return
        except TTError:
            pass
        reason = not_purchased(item, order) if isinstance(item, dict) else None
        if reason:
            # The money was not spent on this listing: skip it and continue with the next account.
            self.store.reject(order['item_id'], order['id'])
            self.notify(f"ℹ️ {NAME} #{esc(order['id'])}: аккаунт {esc(order['item_id'])} не куплен — {reason}. "
                        "Ищу другой подходящий аккаунт.")
            for key in ('item_id', 'candidate', 'uncertain_notified', 'uncertain_escalated'):
                order.pop(key, None)
            order['state'] = 'searching'
            order['check_after'] = 0
            self.store.save(order)
            self.wake.set()
            return
        order['state'] = 'uncertain'
        order['check_after'] = time.time()+60
        self.store.save(order)
        if not order.get('uncertain_notified'):
            order['uncertain_notified'] = True
            self.hold(order, 'Результат покупки неизвестен. Автоматическая сверка продолжается: обычно это решается само за 1–2 минуты.', uncertain=True)
        elif not order.get('uncertain_escalated') and time.time()-order.get('buy_started', time.time()) > 900:
            order['uncertain_escalated'] = True
            self.store.save(order)
            self.notify(f"⏰ {NAME} #{esc(order['id'])}: покупка не подтверждается уже 15 минут.\n\n"
                        f"1. Откройте https://lzt.market/{order['item_id']}/\n"
                        "2. Если аккаунт куплен вами — передайте данные покупателю вручную, "
                        f"затем /tt_panel → 📦 Заказы → #{esc(order['id'])} → «📝 Завершить вручную».\n"
                        "3. Если покупки нет и деньги не списаны — сделайте возврат на FunPay и тоже нажмите «📝 Завершить вручную».\n\n"
                        "Пока вы не нажмёте кнопку, плагин продолжит проверку раз в минуту и не купит второй аккаунт.")

    def accept_purchase(self, order, item):
        if not tiktok_item(item):
            self.hold(order, 'Купленный товар не относится к TikTok. Нужна ручная выдача.'); return
        # Generic fast-buy / item responses may omit category-specific statistics.
        # Preserve the search snapshot, while every explicit purchase value takes priority.
        item = {**order.get('candidate', {}), **item}
        # Persist credentials immediately; delivery failures cannot trigger repurchase.
        order['item'] = item
        order['state'] = 'purchased'
        order['purchased_at'] = time.time()
        self.store.save(order)
        self.store.audit('purchased', order['id'])
        if not password_login(item):
            self.hold(order, 'У купленного аккаунта обнаружен вход по cookies или не определён способ входа. Нужна ручная проверка.'); return
        try:
            confirmed_cost = money(item['price'], True)
            if str(item.get('price_currency', '')).lower() == 'rub' and confirmed_cost != money(order['cost']):
                order['cost'] = str(confirmed_cost)
                self.store.save(order)
                self.notify('⚠️ LZT вернул цену покупки, отличную от запрошенной, для #'+esc(order['id'])+'. Проверьте списание.')
        except (TTError, KeyError):
            self.notify('⚠️ Не удалось сверить цену купленного аккаунта для #'+esc(order['id'])+'. В профите использована цена запроса.')
        self.deliver(order)

    def deliver(self, order):
        fresh = self.paid_order(order['id'])
        if enum(fresh.status) not in {'PAID', 'CLOSED'}:
            self.hold(order, 'FunPay отменил заказ после начала закупки. Данные не отправлены.'); return
        if not order.get('payload'):
            if not password_login(order['item']):
                raise TTError('Купленный аккаунт требует ручной проверки способа входа.')
            fields = credentials(order['item'], order['id'])
            if 'Ошибка (см. на LZT)' in (fields['login'], fields['password']):
                self.notify(f"⚠️ {NAME} #{esc(order['id'])}: маркет вернул пустые логин/пароль. "
                            f"Передайте данные вручную: https://lzt.market/{order['item_id']}/")
            try: text = self.store.settings()['template'].format(**fields)
            except (ValueError, KeyError, IndexError, AttributeError):
                text = DEFAULT_TEMPLATE.format(**fields)
                self.notify('⚠️ Ошибка шаблона. Использован стандартный текст для #'+esc(order['id']))
            order['payload'] = text
        order['state'] = 'delivery_pending'
        order['attempts'] = order.get('attempts', 0)+1
        order['check_after'] = time.time()+60
        self.store.save(order)
        if not self.say(order, order['payload']):
            if order['attempts'] >= 5:
                self.hold(order, 'Аккаунт куплен, но данные не доставлены после 5 попыток. Нажмите «Повторить выдачу».')
                self.notify(f"⚠️ Ошибка отправки в чат для заказа #{esc(order['id'])}\n"
                            f"Отправьте покупателю вручную:\n\n{esc(order['payload'])}")
            elif order['attempts'] == 1:
                self.notify('⚠️ Данные не доставлены для #'+esc(order['id'])+'. Повторю отправку того же аккаунта.')
            return
        order['state'] = 'delivered'
        order['delivered_at'] = time.time()
        order['profit'] = str(money(order['revenue'])-money(order['cost']))
        self.store.save(order)
        self.store.audit('delivered', order['id'])
        self.notify(f"✅ Auto TT: обработан #{esc(order['id'])}\nПрофиль: {esc(order.get('profile', order['tag']))}"
                    f"\nПокупатель: {esc(order['buyer_name'])}\nПрофит без комиссии FunPay: {esc(order['profit'])} ₽"
                    f"\nАккаунт: https://lzt.market/{order['item_id']}/")

    def balances(self, force=False):
        if not force and self.balance_cache is not None and time.time()-self.balance_stamp < 60:
            return self.balance_cache
        _, balances = self.market.profile()
        self.balance_cache, self.balance_stamp = balances, time.time()
        settings = self.store.settings()
        low = not any(amount >= money(settings['balance_threshold'], True) for _, amount, _ in balances)
        if settings['balance_alert'] and low and not settings['low_notified']:
            self.notify('⚠️ Низкий баланс LZT! ['+NAME+']\n'+ '\n'.join(f'{esc(title)}: {amount} ₽' for _, amount, title in balances))
            self.store.setting('low_notified', True)
        elif not low and settings['low_notified']:
            self.store.setting('low_notified', False)
        return balances

    def admin_action(self, oid, action):
        order = self.store.get(oid)
        if not order: raise TTError('Заказ не найден.')
        with self.lock:
            if oid in self.inflight: raise TTError('Заказ уже обрабатывается.')
            if action == 'retry_delivery':
                if not order.get('item') or not order.get('market_user'):
                    raise TTError('Нет сохранённого купленного аккаунта.')
                if order['state'] == 'refunded': raise TTError('Заказ возвращён.')
                order['state'] = 'purchased'; order['attempts'] = 0
            elif action == 'reconcile':
                if not order.get('item_id') or order.get('item'):
                    raise TTError('Нет покупки с неизвестным результатом.')
                order['state'] = 'uncertain'
            elif action == 'retry_order':
                if order.get('item_id') or order['state'] not in {'manual', 'cancelled'}:
                    raise TTError('Повторная закупка этого заказа запрещена.')
                order['state'] = 'queued'
            elif action == 'resolve':
                if order['state'] not in {'manual', 'uncertain'}:
                    raise TTError('Заказ не находится на ручной проверке.')
                order['state'] = 'resolved'
                order['reason'] = 'Продавец отметил заказ разобранным вручную. Автоматическая закупка остановлена.'
            else: raise TTError('Неизвестное действие.')
            order['check_after'] = 0
            self.store.save(order)
        self.wake.set()

    def start(self):
        with self.lock:
            if any(t.is_alive() and t.name == 'AutoTTScheduler' for t in self.threads): return
            if any(t.is_alive() for t in self.threads):
                raise TTError('Предыдущие обработчики завершаются. Перезапустите Cardinal.')
            self.stop_event.clear()
            thread = threading.Thread(target=self.scheduler, name='AutoTTScheduler', daemon=True)
            self.threads = [thread]
            thread.start()

    def stop(self):
        self.stop_event.set(); self.wake.set()

    def scheduler(self):
        next_balance = 0
        while not self.stop_event.is_set():
            try:
                settings = self.store.settings()
                if settings['token'] and settings['balance_alert'] and time.time() >= next_balance:
                    next_balance = time.time()+1800
                    try: self.balances(force=True)
                    except TTError: pass
                for order in self.store.active_orders():
                    state = order['state']
                    if state not in ACTIVE_STATES or order.get('check_after', 0) > time.time(): continue
                    if state in {'queued', 'searching'} and not settings['enabled']: continue
                    with self.lock:
                        if len(self.inflight) >= 5: break
                        if order['id'] in self.inflight: continue
                        self.inflight.add(order['id'])
                    def job(oid=order['id']):
                        try: self.process(oid)
                        finally:
                            with self.lock: self.inflight.discard(oid)
                            self.wake.set()
                    thread = threading.Thread(target=job, name='AutoTTOrder', daemon=True)
                    with self.lock:
                        self.threads = [t for t in self.threads if t.is_alive()]
                        self.threads.append(thread)
                    thread.start()
            except Exception:
                LOG.error('Scheduler failure (details omitted)')
            self.wake.wait(2); self.wake.clear()


GUIDE = [
    ('🚀 Установка', 'Отправьте AutoTT_LuckyFunpay.py боту Cardinal или положите в plugins/. '
     'Включите плагин через /menu → Система и конфиги → Плагины и выполните /restart. Откройте /tt_panel. '
     'Внутренняя автовыдача сначала выключена.'),
    ('🔑 API и прокси', 'Создайте API-приложение на https://lolz.live/account/api и получите токен с правом market. '
     'Доступ к API определяется текущими правилами LZT. Установите валюту RUB на LZT. '
     'В Общих настройках токен проверяется перед сохранением; при ошибке прежний остаётся. Кнопка «Проверить API» показывает каждый баланс отдельно. '
     'Прокси бота используется по умолчанию; отдельный прокси LZT принимает HTTP/HTTPS, IPv4.'),
    ('🗂 Профили и лоты', 'Создайте профиль с тегом (латиница, цифры, _ и -, до 25 символов) и названием. '
     'Установите ссылку поиска https://lzt.market/tiktok/ с фильтрами либо ссылку подраздела TikTok и максимальную цену в RUB. '
     'В описании лота FunPay добавьте tt:ТЕГ. Не создавайте для того же лота штатную автовыдачу Cardinal или FunPay. '
     'Плагин поддерживает 1 шт. за заказ; RUB на FunPay и LZT. Профит учитывается без комиссии FunPay.'),
    ('💬 Покупатель', 'Покупатель получает логин, пароль, доступные данные почты, подписчиков, лайки, видео и регион. '
     'Почта передаётся как её вернул маркет; отдельный запрос пароля временной почты не выполняется. '
     'cookies=no и cookie_login=no добавляются к поиску. Локально проверяется tiktok_cookie_login. '
     'Подбор профиля: точное совпадение тега → ANY → единственный профиль. Без tt: заказ игнорируется.'),
    ('⚠️ Ошибки и возвраты', 'Если аккаунтов нет, действует настройка Автовозврат. Заказы не на 1 шт., '
     'заблокированные покупатели (при блокировке автовыдачи Cardinal) и нехватка средств возвращаются принудительно до покупки. '
     'Отсутствие токена/профиля, неверная валюта, ошибки API передаются продавцу. '
     'Неизвестный результат покупки сверяется раз в минуту: новый аккаунт не покупается и возврат не выполняется. '
     'В Заказах доступны проверка покупки, повтор отправки купленных данных и повтор обработки до покупки.'),
    ('📦 Хранение и пределы', 'Данные хранятся в storage/plugins/AutoTT/tt.sqlite3. '
     'Токен, прокси и данные выданных аккаунтов находятся в локальной базе: ограничьте доступ и делайте резервные копии. '
     'Поиск просматривает максимум 5 страниц. При ошибке выдачи повторяется тот же аккаунт, максимум 5 попыток. '
     'После перезапуска очередь и сверка восстанавливаются. Денежные операции требуют контрольного запуска на вашем Cardinal.'),
]


class TelegramUI:
    def __init__(self, shop):
        from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
        self.Markup, self.Button = InlineKeyboardMarkup, InlineKeyboardButton
        self.shop, self.store = shop, shop.store
        self.tg, self.bot = shop.cardinal.telegram, shop.cardinal.telegram.bot
        self.pending, self.tokens = {}, {}
        self.lock = threading.RLock()

    def allowed(self, user, chat):
        admins = getattr(self.tg, 'authorized_users', {})
        return (chat.type == 'private' and chat.id == user.id
                and (user.id in admins or str(user.id) in admins))

    def markup(self, chat, rows):
        kb = self.Markup()
        with self.lock:
            self.tokens = {k: v for k, v in self.tokens.items() if v[0] > time.time()-3600}
            for row in rows:
                buttons = []
                for label, action in row:
                    if isinstance(action, str) and action.startswith('https://'):
                        buttons.append(self.Button(label, url=action))
                    else:
                        key = uuid.uuid4().hex[:16]
                        self.tokens[key] = (time.time(), chat, action)
                        buttons.append(self.Button(label, callback_data=PREFIX+key))
                kb.row(*buttons)
        return kb

    def show(self, chat, text, rows=None, mid=None):
        rows = rows or [[('🏠 Главное', ('home',))]]
        kb = self.markup(chat, rows)
        kwargs = {'reply_markup': kb, 'parse_mode': 'HTML', 'disable_web_page_preview': True}
        if mid:
            try: return self.bot.edit_message_text(text, chat, mid, **kwargs)
            except Exception: pass
        return self.bot.send_message(chat, text, **kwargs)

    def home(self, chat, mid=None):
        s = self.store.settings()
        orders = self.store.orders()
        profit = sum((D(o.get('profit', '0')) for o in orders if o['state'] == 'delivered'), D(0))
        balance = 'не проверен — задайте токен'
        if s['token']:
            try:
                balances = self.shop.balances()
                balance = str(sum((amount for _, amount, _ in balances), D(0)))+' ₽ (кэш до 60 с)'
            except TTError:
                balance = 'ошибка API — нажмите «Проверить API»'
        text = (f"🎵 <b>{NAME} v{VERSION}</b>\n\n"
                f"{'🟢 Включена' if s['enabled'] else '🔴 Выключена'} автовыдача\n"
                f"Профилей: {len(s['profiles'])} · В очереди: {sum(o['state'] in ACTIVE_STATES for o in orders)}\n\n"
                f"💰 Прибыль без комиссии: {profit:.2f} ₽\n💳 Баланс: {esc(balance)}\n"
                f"✅ Выдано: {sum(o['state'] == 'delivered' for o in orders)} · На ручной проверке: {sum(o['state'] in {'manual', 'uncertain'} for o in orders)}\n\n"
                f"🚫 В ЧС аккаунтов: {self.store.blacklist_count()}\n\n"
                "Тег в описании FunPay: <code>tt:USA</code>\n"
                f"Автор: {CREDITS} · Канал: @LuckyFunPay")
        self.show(chat, text, [
            [('⏸ Выключить' if s['enabled'] else '▶️ Включить', ('power',)), ('🗂 Профили TikTok', ('profiles', 0))],
            [('⚙️ Общие настройки', ('settings',)), ('📦 Заказы', ('orders', 0))],
            [('🔄 Проверить API', ('balance',)), ('📖 Инструкция', ('guide', 0))],
            [('👤 Автор', AUTHOR_URL), ('📢 Канал', CHANNEL_URL)],
            [('◀️ В плагины', ('plugins',))]], mid)

    def settings(self, chat, mid=None):
        s = self.store.settings()
        self.show(chat, '⚙️ <b>Общие настройки TikTok</b>\n\n'
                  f"Токен: {'✅ сохранён' if s['token'] else '❌ не задан'}\n"
                  f"Прокси: {'свой' if s['proxy_mode']=='custom' and s['proxy'] else 'прокси бота'}\n"
                  f"Автовозврат: {'вкл' if s['auto_refund'] else 'выкл'}\n"
                  f"ЧС продавцов: {len(s['sellers'])}", [
            [('🔑 Токен LZT', ('ask', 'token', '')), ('🌍 Прокси LZT', ('proxy',))],
            [('↩️ Автовозврат '+('🟢' if s['auto_refund'] else '🔴'), ('toggle', 'auto_refund')),
             ('🔔 Баланс '+('🟢' if s['balance_alert'] else '🔴'), ('toggle', 'balance_alert'))],
            [('💰 Порог: '+s['balance_threshold']+' ₽', ('ask', 'balance_threshold', '')),
             ('🚫 ЧС продавцов', ('sellers',))],
            [('💬 Шаблон выдачи', ('ask', 'template', '')), ('⏳ Текст ожидания', ('ask', 'wait_text', ''))],
            [('🏠 Главное', ('home',)), ('🔄 Проверить API', ('balance',))]], mid)

    def profiles(self, chat, page=0, mid=None):
        profiles = sorted(self.store.settings()['profiles'].items())
        page = max(0, min(page, max(0, (len(profiles)-1)//10)))
        buttons = [(f"🏷 {key} | {value['name'][:28]}", ('profile', key)) for key, value in profiles[page*10:page*10+10]]
        rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
        rows += [[('⬅️', ('profiles', max(0, page-1))), ('➡️', ('profiles', page+1))],
                 [('➕ Создать профиль', ('ask', 'new_tag', '')), ('🏠 Главное', ('home',))]]
        self.show(chat, f"🗂 <b>Профили TikTok</b>\nСтраница {page+1}/{max(1,(len(profiles)+9)//10)}\n"
                  'Каждый тег связывает лот FunPay с фильтрами LZT.', rows, mid)

    def profile(self, chat, key, mid=None):
        p = self.store.settings()['profiles'].get(key)
        if not p: raise TTError('Профиль удалён.')
        self.show(chat, f"🏷 <b>{esc(p['name'])}</b>\n\nТег FunPay: <code>tt:{key}</code>\n"
                  f"Макс. цена: {esc(p['max_price'])} ₽\nСсылка: {esc(p['url'] or '❌ не задана')}", [
            [('🔗 Ссылка поиска', ('ask', 'url', key)), ('💰 Макс. цена', ('ask', 'max_price', key))],
            [('✏️ Название', ('ask', 'name', key)), ('🗑 Удалить', ('confirm_delete', key))],
            [('🗂 Профили', ('profiles', 0)), ('🏠 Главное', ('home',))]], mid)

    def orders(self, chat, page=0, mid=None):
        orders = [o for o in self.store.orders() if o.get('tag') or o['state'] != 'cancelled']
        page = max(0, min(page, max(0, (len(orders)-1)//10)))
        buttons = [(f"#{o['id']} · {o['state']}", ('order', o['id'])) for o in orders[page*10:page*10+10]]
        rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
        rows += [[('⬅️', ('orders', max(0, page-1))), ('➡️', ('orders', page+1))], [('🏠 Главное', ('home',))]]
        self.show(chat, f"📦 <b>Заказы TikTok</b>\nСтраница {page+1}/{max(1,(len(orders)+9)//10)}\n"
                  'uncertain — неизвестен результат покупки; manual — требуется продавец.', rows, mid)

    def order(self, chat, oid, mid=None):
        o = self.store.get(oid)
        if not o: raise TTError('Заказ не найден.')
        rows = []
        if o.get('item_id'):
            rows.append([('🛒 Аккаунт на LZT', f"https://lzt.market/{o['item_id']}/"),
                         ('🧾 Заказ FunPay', f"https://funpay.com/orders/{oid}/")])
        actions = []
        if o.get('item') and o['state'] != 'refunded': actions.append(('📨 Повторить выдачу', ('action', oid, 'retry_delivery')))
        if o.get('item_id') and not o.get('item'): actions.append(('🔎 Проверить покупку', ('action', oid, 'reconcile')))
        if not o.get('item_id') and o['state'] == 'manual': actions.append(('🔁 Повторить обработку', ('action', oid, 'retry_order')))
        if actions: rows.append(actions)
        if o.get('payload'): rows.append([('📄 Данные выдачи', ('payload', oid))])
        if o['state'] in {'manual', 'uncertain'}:
            rows.append([('📝 Завершить вручную', ('confirm_resolve', oid))])
        rows += [[('🔄 Обновить', ('order', oid)), ('📦 Заказы', ('orders', 0))]]
        self.show(chat, f"📦 <b>Заказ #{esc(oid)}</b>\n\nСтатус: {esc(o['state'])}\n"
                  f"Тег лота: {esc(o.get('tag', '—'))} · Профиль: {esc(o.get('profile', '—'))}\nПокупатель: {esc(o.get('buyer_name', '—'))}\n"
                  f"Покупка: {esc(o.get('cost', '—'))} ₽ · Профит: {esc(o.get('profit', '—'))} ₽\n"
                  f"{esc(o.get('reason', ''))}", rows, mid)

    def ask(self, chat, field, key):
        s = self.store.settings()
        prompts = {
            'token': 'Пришлите API-токен LZT с правом market. Сообщение с токеном будет удалено, если Telegram разрешит.',
            'proxy': 'Пришлите HTTP/HTTPS IPv4-прокси: http://логин:пароль@IP:PORT. Символ - очищает прокси.',
            'balance_threshold': 'Введите положительный порог низкого баланса в рублях.',
            'new_tag': 'Введите тег профиля: латиница, цифры, _ и -, до 25 символов. Например USA — можно прислать и целиком tt:USA.',
            'new_name': 'Введите название нового профиля (до 80 символов).',
            'name': 'Введите новое название профиля (до 80 символов).',
            'url': 'Пришлите ссылку поиска HTTPS с lzt.market из раздела /tiktok/ или подраздела. Фильтры сохранятся.',
            'max_price': 'Введите максимальную цену покупки в рублях.',
            'seller_add': 'Пришлите точный ник продавца LZT, без ссылки.',
            'seller_remove': 'Пришлите ник продавца для удаления из ЧС.',
            'template': 'Пришлите шаблон выдачи (до 2500 символов). Переменные: '+', '.join('{'+v+'}' for v in sorted(VARIABLES))+'\n\n'+s['template'],
            'wait_text': 'Пришлите текст ожидания (до 1000 символов). Символ - отключает сообщение.\n\n'+s['wait_text'],
        }
        if field not in prompts: raise TTError('Неизвестное поле.')
        sent = self.show(chat, esc(prompts[field])+'\n\nОтмена: /tt_cancel', [[('✖️ Отмена', ('cancel',))]])
        with self.lock: self.pending[chat] = (time.time(), field, key, sent.message_id)

    def has_input(self, message):
        if not self.allowed(message.from_user, message.chat): return False
        with self.lock: pending = self.pending.get(message.chat.id)
        return bool(pending and time.time()-pending[0] < 600 and getattr(message, 'text', None)
                    and (not message.text.startswith('/') or message.text == '/tt_cancel'))

    def check_access_editable(self):
        if (self.store.settings()['enabled'] or self.shop.inflight
                or any(o['state'] in {'buying', 'uncertain', 'purchased', 'delivery_pending'}
                       for o in self.store.orders())):
            raise TTError('Выключите автовыдачу и завершите активные покупки/сверку перед изменением API-доступа.')

    def input(self, message):
        if not self.has_input(message): return
        chat = message.chat.id
        with self.lock: pending = self.pending.pop(chat)
        _, field, key, _ = pending
        raw = message.text.strip()
        if raw == '/tt_cancel': self.home(chat); return
        try:
            if field in {'token', 'proxy'}:
                try: self.bot.delete_message(chat, message.message_id)
                except Exception: pass
                self.check_access_editable()
            if field == 'token':
                if len(raw) > 4096 or not raw or any(c.isspace() for c in raw): raise TTError('Токен должен быть одной строкой без пробелов.')
                self.show(chat, '⏳ Проверяю токен LZT…')
                try: self.shop.market.profile(token_override=raw)
                except TTError as error:
                    raise TTError(f'Токен не сохранён. {error}' + (f' HTTP {error.status}.' if isinstance(error, APIError) and error.status else '')) from None
                self.check_access_editable()
                self.store.setting('token', raw)
                self.shop.balance_cache = None
                self.show(chat, '✅ TT LOLZ Токен сохранен и проверен.', [[('🔄 Проверить API', ('balance',)), ('⚙️ Настройки', ('settings',))]])
            elif field == 'proxy':
                value = proxy_url(raw)
                self.store.change(lambda s: s.update({'proxy': value, 'proxy_mode': 'custom' if value else 'bot'}))
                self.shop.balance_cache = None
                self.dispatch(chat, None, ('proxy',))
            elif field == 'new_tag':
                key = tag(raw)
                if key in self.store.settings()['profiles']: raise TTError('Тег уже существует.')
                self.ask(chat, 'new_name', key)
            elif field == 'new_name':
                if not 1 <= len(raw) <= 80: raise TTError('Название: 1–80 символов.')
                def create(s):
                    if key in s['profiles']: raise TTError('Тег уже существует.')
                    s['profiles'][key] = {'name': raw, 'max_price': '500', 'url': ''}
                self.store.change(create); self.profile(chat, key)
            elif field in {'url', 'max_price', 'name'}:
                value = search_url(raw) if field == 'url' else str(money(raw, True)) if field == 'max_price' else raw
                if field == 'name' and not 1 <= len(raw) <= 80: raise TTError('Название: 1–80 символов.')
                def update(s):
                    if key not in s['profiles']: raise TTError('Профиль удалён.')
                    s['profiles'][key][field] = value
                self.store.change(update); self.profile(chat, key)
            elif field in {'seller_add', 'seller_remove'}:
                if not raw or len(raw) > 80 or '/' in raw or ':' in raw or any(c.isspace() for c in raw):
                    raise TTError('Нужен ник продавца, без ссылки и пробелов.')
                def sellers(s):
                    existing = next((n for n in s['sellers'] if n.casefold() == raw.casefold()), None)
                    if field == 'seller_add':
                        if existing: raise TTError('Продавец уже в ЧС.')
                        s['sellers'].append(raw)
                    else:
                        if not existing: raise TTError('Продавец не найден в ЧС.')
                        s['sellers'].remove(existing)
                self.store.change(sellers); self.dispatch(chat, None, ('sellers',))
            elif field == 'balance_threshold':
                self.store.change(lambda s: s.update({'balance_threshold': str(money(raw, True)), 'low_notified': False}))
                self.settings(chat)
            elif field == 'template':
                self.store.setting('template', validate_template(message.text)); self.settings(chat)
            elif field == 'wait_text':
                if len(message.text) > 1000: raise TTError('Текст ожидания: до 1000 символов.')
                self.store.setting('wait_text', '' if raw == '-' else message.text); self.settings(chat)
        except (TTError, ValueError) as error:
            text = str(error) if isinstance(error, TTError) else 'Неверный формат значения.'
            self.show(chat, '⚠️ '+esc(text), [[('✏️ Ввести заново', ('ask', field, key)), ('⚙️ Настройки', ('settings',))]])

    def callback(self, call):
        if not call.message or not self.allowed(call.from_user, call.message.chat): return
        chat, mid = call.message.chat.id, call.message.message_id
        self.bot.answer_callback_query(call.id)
        with self.lock:
            token = self.tokens.pop(call.data[len(PREFIX):], None)
        if not token or token[1] != chat or time.time()-token[0] >= 3600:
            self.show(chat, 'Кнопка устарела. Откройте /tt_panel.', mid=mid); return
        try: self.dispatch(chat, mid, token[2])
        except TTError as error: self.show(chat, '⚠️ '+esc(error), mid=mid)

    def dispatch(self, chat, mid, action):
        op, *args = action
        if op == 'home': self.home(chat, mid)
        elif op == 'settings': self.settings(chat, mid)
        elif op == 'profiles': self.profiles(chat, args[0], mid)
        elif op == 'profile': self.profile(chat, args[0], mid)
        elif op == 'orders': self.orders(chat, args[0], mid)
        elif op == 'order': self.order(chat, args[0], mid)
        elif op == 'payload':
            order = self.store.get(args[0])
            if not order or not order.get('payload'): raise TTError('Сохранённых данных выдачи нет.')
            payload = order['payload']
            for offset in range(0, len(payload), 3500):
                self.bot.send_message(chat, payload[offset:offset+3500], parse_mode=None, disable_web_page_preview=True)
        elif op == 'ask': self.ask(chat, *args)
        elif op == 'cancel':
            with self.lock: self.pending.pop(chat, None)
            self.home(chat, mid)
        elif op == 'toggle':
            if args[0] not in {'auto_refund', 'balance_alert'}: raise TTError('Неверная настройка.')
            self.store.change(lambda s: s.update({args[0]: not s[args[0]], 'low_notified': False}))
            self.settings(chat, mid)
        elif op == 'power':
            s = self.store.settings()
            if not s['enabled']:
                if not s['token'] or not any(p['url'] for p in s['profiles'].values()):
                    raise TTError('Сначала задайте токен и хотя бы один профиль со ссылкой поиска.')
                self.shop.balances(force=True)  # check access and RUB currency before enable
            self.store.setting('enabled', not s['enabled'])
            self.shop.wake.set(); self.home(chat, mid)
        elif op == 'balance':
            try:
                balances = self.shop.balances(force=True)
                text = '✅ API работает · Валюта RUB\n\n'+ '\n'.join(f'{esc(title)}: {amount} ₽' for _, amount, title in balances)
                text += '\n\nОплата возможна с одного баланса; суммы не объединяются.'
            except TTError as error: text = '❌ Проверка API не пройдена.\n'+esc(error)
            self.show(chat, text, [[('🔄 Проверить снова', ('balance',)), ('🏠 Главное', ('home',))]], mid)
        elif op == 'proxy':
            s = self.store.settings()
            self.show(chat, '🌍 <b>Прокси LZT</b>\nСвой прокси: '+('✅ задан' if s['proxy'] else '❌ не задан')+'\n'
                      'Настройка влияет только на запросы LZT.', [
                [('✅ Прокси бота' if s['proxy_mode']=='bot' else 'Прокси бота', ('proxy_mode', 'bot')),
                 ('✅ Свой прокси' if s['proxy_mode']=='custom' else 'Свой прокси', ('proxy_mode', 'custom'))],
                [('✏️ Задать', ('ask', 'proxy', '')), ('🗑 Очистить', ('clear_proxy',))],
                [('⚙️ Настройки', ('settings',)), ('🏠 Главное', ('home',))]], mid)
        elif op in {'proxy_mode', 'clear_proxy'}:
            self.check_access_editable()
            if op == 'proxy_mode':
                if args[0] == 'custom' and not self.store.settings()['proxy']: raise TTError('Сначала задайте свой прокси.')
                self.store.setting('proxy_mode', args[0])
            else: self.store.change(lambda s: s.update({'proxy': '', 'proxy_mode': 'bot'}))
            self.shop.balance_cache = None
            self.dispatch(chat, mid, ('proxy',))
        elif op == 'sellers':
            sellers = self.store.settings()['sellers']
            self.show(chat, f"🚫 <b>ЧС продавцов ({len(sellers)})</b>\n"+esc(', '.join(sellers)[:2500] or 'Список пуст.'), [
                [('➕ Добавить', ('ask', 'seller_add', '')), ('➖ Удалить', ('ask', 'seller_remove', ''))],
                [('⚙️ Настройки', ('settings',)), ('🏠 Главное', ('home',))]], mid)
        elif op == 'confirm_delete':
            self.show(chat, 'Удалить профиль '+esc(args[0])+'?', [[('🗑 Удалить', ('delete', args[0])), ('↩️ Отмена', ('profile', args[0]))]], mid)
        elif op == 'delete':
            self.store.change(lambda s: s['profiles'].pop(args[0], None)); self.profiles(chat, 0, mid)
        elif op == 'action':
            self.shop.admin_action(*args); self.order(chat, args[0], mid)
        elif op == 'confirm_resolve':
            self.show(chat, 'Вы проверили покупку на LZT и вручную решили заказ с покупателем? '
                      'После подтверждения автоматическая сверка этого заказа прекратится. '
                      'Деньги и данные эта кнопка не отправляет.', [
                [('✅ Уже разобрано', ('action', args[0], 'resolve')), ('↩️ Назад', ('order', args[0]))]], mid)
        elif op == 'guide':
            page = max(0, min(args[0], len(GUIDE)-1))
            title, text = GUIDE[page]
            self.show(chat, '<b>'+title+'</b>\n\n'+esc(text), [
                [('⬅️', ('guide', max(0, page-1))), ('➡️', ('guide', min(len(GUIDE)-1, page+1)))],
                [('👤 Автор', AUTHOR_URL), ('📢 Канал', CHANNEL_URL)], [('🏠 Главное', ('home',))]], mid)
        elif op == 'plugins':
            from tg_bot import CBT
            kb = self.Markup()
            kb.row(self.Button('🧩 Открыть плагины', callback_data=f'{CBT.PLUGINS_LIST}:0'))
            self.bot.send_message(chat, 'Плагины Cardinal', reply_markup=kb)

    def command(self, message):
        if self.allowed(message.from_user, message.chat):
            with self.lock: self.pending.pop(message.chat.id, None)
            self.home(message.chat.id)

    def register(self):
        self.tg.msg_handler(self.command, commands=['tt_panel'])
        self.tg.msg_handler(self.input, func=self.has_input, content_types=['text'])
        self.tg.cbq_handler(self.callback, lambda call: bool(call.data) and call.data.startswith(PREFIX))
        from tg_bot import CBT
        def settings_call(call):
            if call.message and self.allowed(call.from_user, call.message.chat):
                self.bot.answer_callback_query(call.id)
                self.home(call.message.chat.id, call.message.message_id)
        self.tg.cbq_handler(settings_call, lambda call: bool(call.data) and
                            (call.data == f'{CBT.PLUGIN_SETTINGS}:{UUID}' or call.data.startswith(f'{CBT.PLUGIN_SETTINGS}:{UUID}:')))
        self.shop.cardinal.add_telegram_commands(UUID, [('tt_panel', '🎵 Auto TT — панель', True)])


SHOP = None


def init(cardinal, *args):
    global SHOP
    if SHOP is not None: return
    SHOP = Shop(cardinal)
    if getattr(cardinal, 'telegram', None):
        SHOP.ui = TelegramUI(SHOP); SHOP.ui.register()
    LOG.info('%s v%s initialized, author %s', NAME, VERSION, CREDITS)


def start(cardinal, *args):
    if SHOP is None: init(cardinal)
    SHOP.start()


def stop(cardinal=None, *args):
    if SHOP: SHOP.stop()


def new_order(cardinal, event, *args):
    if not SHOP or not SHOP.store.settings()['enabled']: return
    order = getattr(event, 'order', None)
    if order is None or enum(getattr(order, 'status', '')) != 'PAID': return
    oid = str(getattr(order, 'id', '')).lstrip('#')
    if re.fullmatch(r'[A-Za-z0-9-]{1,64}', oid):
        SHOP.store.enqueue(oid); SHOP.wake.set()


BIND_TO_PRE_INIT = [init]
BIND_TO_POST_START = [start]
BIND_TO_PRE_STOP = [stop]
BIND_TO_NEW_ORDER = [new_order]
BIND_TO_DELETE = stop
