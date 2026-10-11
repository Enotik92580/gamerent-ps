"""Auto VIP Roblox v16 for FunPay Cardinal, Python 3.10+.
Strict offer identity, transactional stock, durable delivery and refund queues.
Unconfirmed Roblox cleanup quarantines a server instead of selling it again.
See README.md for migration and the FunPayAPI offer-ID compatibility boundary.
"""
from __future__ import annotations

import html
import json
import logging
import math
from contextlib import contextmanager
from urllib.parse import urlsplit, quote
from types import SimpleNamespace
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import requests
from telebot.types import CallbackQuery, InlineKeyboardButton as Button
from telebot.types import InlineKeyboardMarkup as Keyboard, Message

from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent

try:  # Present in current FPC; the fallback keeps the plugin loadable on old builds.
    from FunPayAPI.common.enums import MessageTypes
except Exception:  # pragma: no cover - compatibility branch
    MessageTypes = None

try:
    from tg_bot import CBT, utils as tg_utils
    from tg_bot.static_keyboards import CLEAR_STATE_BTN
except Exception:  # pragma: no cover - only used while Cardinal itself is broken
    CBT = None
    tg_utils = None
    CLEAR_STATE_BTN = lambda: None


NAME = "Auto VIP Roblox"
VERSION = "16.2.0"
DESCRIPTION = (
    "Безопасная аренда Roblox VIP: серверы привязаны к аккаунтам, выдача "
    "атомарна, очистка повторяется до успеха, цены и лоты синхронизируются с FunPay. "
    "Админ-панель не пересекается с другими плагинами Cardinal."
)
CREDITS = "@LuckyFunPay_Support"
AUTHOR_URL = "https://t.me/LuckyFunPay_Support"
DEVELOPER_URL = "https://t.me/LuckyFunPay"
UUID = "dbd0fc30-4033-4afc-8e36-0edcaa517466"
SETTINGS_PAGE = False

PREFIX = "avr15"
STATE_PREFIX = f"{PREFIX}_state_"
# Keep the former database name deliberately: a v11 installation is upgraded in
# place, so accounts, lots and the stock pool do not disappear on the first run.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "AutoVIPRoblox_Monolith.db")
LOG = logging.getLogger("FPC.AutoVIPRoblox")

DEFAULTS = {
    "auto_refund": "1",
    "lot_management": "1",
    "event_mode": "0",
    "global_proxy": "",
    "friends_module_active": "1",
    "friends_auto_remove": "1",
    "friends_default_limit": "0",
    "legacy_commands": "1",  # disabled by default: does not interfere with other plugins
    "review_bonus_active": "1",
    "review_bonus_hours": "1",
    "msg_post_payment": (
        "🎮 <b>VIP-сервер Roblox выдан</b>\n\n"
        "🔗 Ссылка: {link}\n"
        "⏱ Аренда: {hours} ч.\n"
        "📦 Заказ: #{order_id}{bonus_line}\n\n"
        "Команды: <code>!vip_time</code>, <code>!vip_link</code>, "
        "<code>!vip_friends Ник</code>, <code>!vip_help</code>\n"
        "Для доступа пользователям до 13 лет отправьте заявку владельцу: {owner_url}"
    ),
    "msg_post_expire": "⏳ Аренда по заказу #{order_id} завершена. Сервер освобождён.",
    "msg_friend_added": "✅ {friend_name} добавлен в доступ к вашему VIP-серверу.",
    "msg_friend_not_found": "❌ Не удалось найти/принять заявку от {input}. Сначала отправьте заявку владельцу сервера.",
    "msg_friend_limit": "❌ Лимит друзей для этой аренды: {limit}.",
    "msg_review_bonus": "⭐ Спасибо за отзыв 5★! Добавлено {bonus_hours} ч. к активной аренде.",
}

DB: "PluginDB | None" = None
CARDINAL = None
STOP = threading.Event()
WORKER: threading.Thread | None = None


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def positive_float(value: Any, field: str) -> float:
    try:
        result = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        raise ValueError(f"{field}: нужно число")
    if not math.isfinite(result) or result <= 0 or result > 1_000_000:
        raise ValueError(f"{field}: нужно конечное число от 0 (не включительно) до 1000000")
    return result


def normalize_proxy(raw):
    value = (raw or '').strip()
    if not value or value == '-':
        return None
    if '://' not in value:
        value = 'http://' + value
    try:
        parts = urlsplit(value)
        if parts.scheme not in ('http', 'https', 'socks5', 'socks5h') or not parts.hostname or not parts.port or parts.path not in ('', '/') or parts.query or parts.fragment:
            raise ValueError()
    except ValueError:
        raise ValueError('Нужен HTTP/HTTPS/SOCKS5 прокси: login:pass@host:port') from None
    return {'http': value, 'https': value}


def split_ids(text: str) -> list[str]:
    return [item for item in re.split(r"[\s,;]+", text.strip()) if item]


class PluginDB:
    """Small transaction layer. Network I/O is deliberately never done under its lock."""

    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if os.path.exists(path):
            with sqlite3.connect(path) as old:
                try:
                    version = old.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
                except sqlite3.OperationalError:
                    version = None
                if not version or version[0] != '16':
                    backup_path = path + '.before-v16-' + str(time.time_ns()) + '.bak'
                    backup = sqlite3.connect(backup_path)
                    try:
                        old.backup(backup)
                    finally:
                        backup.close()
            old.close()
        self.init()

    @contextmanager
    def conn(self):
        con = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 30000")
        try:
            with con:
                yield con
        finally:
            con.close()

    def init(self) -> None:
        with self.lock, self.conn() as con:
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cookie TEXT NOT NULL, proxy TEXT NOT NULL DEFAULT '', username TEXT,
                    roblox_id TEXT, enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, game_id TEXT
                );
                CREATE TABLE IF NOT EXISTS servers (
                    server_id TEXT PRIMARY KEY, category_id INTEGER,
                    owner_account_id INTEGER, place_id TEXT, status TEXT NOT NULL DEFAULT 'free',
                    FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL,
                    FOREIGN KEY(owner_account_id) REFERENCES accounts(id) ON DELETE RESTRICT
                );
                CREATE TABLE IF NOT EXISTS lots (
                    lot_id INTEGER PRIMARY KEY, hours REAL NOT NULL DEFAULT 1,
                    category_id INTEGER, server_id TEXT, bind_type TEXT NOT NULL DEFAULT 'random',
                    price_normal REAL NOT NULL DEFAULT 0, price_event REAL NOT NULL DEFAULT 0,
                    friend_limit INTEGER NOT NULL DEFAULT -1, active INTEGER NOT NULL DEFAULT 1, name TEXT,
                    FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL,
                    FOREIGN KEY(server_id) REFERENCES servers(server_id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS wholesale_bonuses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, buy_hours REAL NOT NULL, get_hours REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rentals (
                    rental_id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                    lot_id INTEGER NOT NULL, server_id TEXT NOT NULL, owner_account_id INTEGER NOT NULL,
                    hours REAL NOT NULL, start_time REAL NOT NULL, end_time REAL NOT NULL,
                    status TEXT NOT NULL, price REAL NOT NULL DEFAULT 0, cleanup_reason TEXT,
                    cleanup_attempts INTEGER NOT NULL DEFAULT 0, next_cleanup_at REAL, last_error TEXT,
                    FOREIGN KEY(server_id) REFERENCES servers(server_id),
                    FOREIGN KEY(owner_account_id) REFERENCES accounts(id)
                );
                CREATE INDEX IF NOT EXISTS idx_rentals_active ON rentals(status, end_time);
                CREATE TABLE IF NOT EXISTS payments (
                    order_id TEXT PRIMARY KEY, rental_id TEXT, buyer_id TEXT NOT NULL, chat_id TEXT NOT NULL,
                    lot_id INTEGER, paid_hours REAL NOT NULL, bonus_hours REAL NOT NULL,
                    price REAL NOT NULL DEFAULT 0, state TEXT NOT NULL, review_bonus_given INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL, FOREIGN KEY(rental_id) REFERENCES rentals(rental_id)
                );
                CREATE TABLE IF NOT EXISTS rental_friends (
                    rental_id TEXT NOT NULL, roblox_id TEXT NOT NULL, name TEXT,
                    remove_from_server INTEGER NOT NULL DEFAULT 1, unfriend_on_cleanup INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(rental_id, roblox_id), FOREIGN KEY(rental_id) REFERENCES rentals(rental_id) ON DELETE CASCADE
                );
            """)
            self._migrate_legacy(con)
            self._upgrade_v16(con)
            self._recover_interrupted_operations(con)
            for key, value in DEFAULTS.items():
                con.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value))

    @staticmethod
    def _add_column(con, table, definition):
        columns = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if definition.split()[0] not in columns:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def _migrate_legacy(self, con: sqlite3.Connection) -> None:
        # Existing Monolith databases are retained. Servers are bound automatically only
        # when there is exactly one Roblox account; a multi-account store must bind them
        # explicitly in the new interface rather than guessing and risking an чужой server.
        self._add_column(con, "servers", "owner_account_id INTEGER")
        self._add_column(con, "servers", "place_id TEXT")
        self._add_column(con, "accounts", "enabled INTEGER NOT NULL DEFAULT 1")
        self._add_column(con, "accounts", "created_at REAL NOT NULL DEFAULT 0")
        self._add_column(con, "rentals", "next_cleanup_at REAL")
        try:
            count = con.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            if count == 1:
                account_id = con.execute("SELECT id FROM accounts LIMIT 1").fetchone()[0]
                con.execute("UPDATE servers SET owner_account_id=? WHERE owner_account_id IS NULL", (account_id,))
                con.execute("""UPDATE servers SET place_id=(SELECT game_id FROM categories c WHERE c.id=servers.category_id)
                               WHERE place_id IS NULL AND category_id IS NOT NULL""")
            # v11 had only `orders`. Migrate its active records once so an existing
            # paid rental is not treated as free stock after the upgrade. If the
            # old installation had several Roblox accounts, ownership is ambiguous;
            # the server stays locked (`cleanup_failed`) until the owner is bound in
            # the panel, rather than guessing with somebody else's cookie.
            old_table = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'").fetchone()
            has_sessions = con.execute("SELECT 1 FROM rentals LIMIT 1").fetchone()
            if old_table and not has_sessions:
                con.execute("""INSERT OR IGNORE INTO accounts(id,cookie,proxy,username,roblox_id,enabled,created_at)
                               VALUES(0,'','','UNBOUND',NULL,0,0)""")
                old_orders = con.execute("""SELECT order_id,buyer_id,chat_id,server_id,hours,start_time,end_time,status,price,friends_added
                                           FROM orders WHERE status='active'""").fetchall()
                for old in old_orders:
                    server = con.execute("SELECT owner_account_id,place_id FROM servers WHERE server_id=?", (old["server_id"],)).fetchone()
                    owner_id = server["owner_account_id"] if server else None
                    status = "active" if owner_id and server["place_id"] else "cleanup_failed"
                    rental_id = "legacy-" + str(old["order_id"])
                    con.execute("""INSERT OR IGNORE INTO rentals(rental_id,buyer_id,chat_id,lot_id,server_id,owner_account_id,hours,start_time,end_time,status,price,last_error)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (rental_id, str(old["buyer_id"]), str(old["chat_id"]), 0, str(old["server_id"]), owner_id or 0,
                                 float(old["hours"] or 0), float(old["start_time"] or time.time()), float(old["end_time"] or time.time()),
                                 status, float(old["price"] or 0), None if status == "active" else "Нужно указать владельца Roblox и Place ID"))
                    con.execute("""INSERT OR IGNORE INTO payments(order_id,rental_id,buyer_id,chat_id,lot_id,paid_hours,bonus_hours,price,state,created_at)
                                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                                (str(old["order_id"]), rental_id, str(old["buyer_id"]), str(old["chat_id"]), 0,
                                 float(old["hours"] or 0), 0, float(old["price"] or 0), "issued", float(old["start_time"] or time.time())))
                    try:
                        for friend_id in json.loads(old["friends_added"] or "[]"):
                            con.execute("INSERT OR IGNORE INTO rental_friends(rental_id,roblox_id,remove_from_server,unfriend_on_cleanup) VALUES(?,?,1,0)",
                                        (rental_id, str(friend_id)))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
        except sqlite3.OperationalError as exc:
            raise RuntimeError("Схема старой базы не поддерживается; исходная база сохранена в резервной копии") from exc

    @staticmethod
    def _recover_interrupted_operations(con):
        now = time.time()
        con.execute("UPDATE payments SET state='issue_failed' WHERE state='issuing'")
        con.execute("""UPDATE rentals SET status='cleanup_failed',
            cleanup_reason=COALESCE(cleanup_reason,'issue_failed'), next_cleanup_at=?,
            last_error='Восстановление незавершённой операции' WHERE status IN ('issuing','cleaning','refunding')""", (now,))
        con.execute("UPDATE outbox SET state='pending' WHERE state='sending'")
        con.execute("UPDATE incoming_orders SET state='pending' WHERE state='processing'")
        con.execute("""UPDATE servers SET status='cleaning' WHERE EXISTS
            (SELECT 1 FROM rentals r WHERE r.server_id=servers.server_id AND r.status='cleanup_failed')""")
        con.execute("""UPDATE servers SET status='error' WHERE status IN ('reserved','occupied') AND NOT EXISTS
            (SELECT 1 FROM rentals r WHERE r.server_id=servers.server_id
             AND r.status IN ('issuing','delivering','active','cleaning','cleanup_failed'))""")

    def _upgrade_v16(self, con):
        for table, column in (
            ('rentals', "link TEXT"), ('rentals', "source_key TEXT"),
            ('payments', "currency TEXT NOT NULL DEFAULT 'RUB'"),
            ('payments', "refund_previous_state TEXT"), ('payments', "last_error TEXT"),
            ('payments', "next_refund_at REAL NOT NULL DEFAULT 0"),
            ('servers', "verified INTEGER NOT NULL DEFAULT 0"),
            ('servers', "enabled INTEGER NOT NULL DEFAULT 1"),
            ('lots', "paused INTEGER NOT NULL DEFAULT 0"),
            ('lots', "last_error TEXT"), ('lots', "subcategory_id INTEGER"),
            ('rental_friends', "state TEXT NOT NULL DEFAULT 'added'"),
        ):
            self._add_column(con, table, column)
        con.executescript('''
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe TEXT UNIQUE NOT NULL,
                rental_id TEXT, order_id TEXT, chat_id TEXT NOT NULL, text TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'notice', state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
                last_error TEXT, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS incoming_orders (
                order_id TEXT PRIMARY KEY, amount INTEGER, price REAL, lot_id INTEGER,
                state TEXT NOT NULL DEFAULT 'pending', error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS time_credits (
                id INTEGER PRIMARY KEY AUTOINCREMENT, rental_id TEXT NOT NULL,
                order_id TEXT, hours REAL NOT NULL, start_time REAL NOT NULL,
                end_time REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS managed_friends (
                account_id INTEGER NOT NULL, roblox_id TEXT NOT NULL,
                PRIMARY KEY(account_id,roblox_id));
        ''')
        self._add_column(con, 'incoming_orders', 'event_lot_id INTEGER')
        self._add_column(con, 'incoming_orders', 'event_error TEXT')
        # Preserve v15 sessions and their timeline. Never infer a game from another pool.
        for rental in con.execute('SELECT * FROM rentals').fetchall():
            if con.execute('SELECT 1 FROM time_credits WHERE rental_id=?', (rental['rental_id'],)).fetchone():
                continue
            cursor = float(rental['start_time'])
            for p in con.execute("SELECT * FROM payments WHERE rental_id=? AND state!='refunded' ORDER BY created_at,order_id", (rental['rental_id'],)).fetchall():
                hours = float(p['paid_hours']) + float(p['bonus_hours'])
                con.execute('INSERT INTO time_credits(rental_id,order_id,hours,start_time,end_time) VALUES(?,?,?,?,?)',
                            (rental['rental_id'], p['order_id'], hours, cursor, cursor + hours * 3600))
                cursor += hours * 3600
            extra = float(rental['end_time']) - cursor
            if extra > 0:
                con.execute('INSERT INTO time_credits(rental_id,hours,start_time,end_time) VALUES(?,?,?,?)',
                            (rental['rental_id'], extra / 3600, cursor, float(rental['end_time'])))
        con.execute('''INSERT OR IGNORE INTO managed_friends(account_id,roblox_id)
            SELECT r.owner_account_id,f.roblox_id FROM rental_friends f JOIN rentals r USING(rental_id)
            WHERE f.unfriend_on_cleanup=1 AND f.state!='removed'
            AND r.status IN ('active','issuing','delivering','refunding','cleaning','cleanup_failed')''')
        for rental in con.execute("SELECT * FROM rentals WHERE source_key IS NULL").fetchall():
            lot = con.execute('SELECT * FROM lots WHERE lot_id=?', (rental['lot_id'],)).fetchone()
            server = con.execute('SELECT * FROM servers WHERE server_id=?', (rental['server_id'],)).fetchone()
            if not lot or not server:
                continue
            key = source_key(lot)
            matches = ((lot['bind_type'] == 'server' and lot['server_id'] == server['server_id']) or
                       (lot['bind_type'] == 'category' and lot['category_id'] == server['category_id']) or
                       (lot['bind_type'] == 'random' and server['category_id'] is None))
            if key and matches:
                con.execute('UPDATE rentals SET source_key=? WHERE rental_id=?', (key, rental['rental_id']))
        con.execute("INSERT INTO settings(key,value) VALUES('schema_version','16') ON CONFLICT(key) DO UPDATE SET value='16'")

    def setting(self, key: str, default: str = "") -> str:
        with self.lock, self.conn() as con:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return str(row[0]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.lock, self.conn() as con:
            con.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    def rows(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.lock, self.conn() as con:
            return con.execute(sql, params).fetchall()

    def row(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        result = self.rows(sql, params)
        return result[0] if result else None


@dataclass(frozen=True)
class RobloxAccount:
    id: int
    cookie: str
    proxy: str
    roblox_id: str


class RobloxAPI:
    def __init__(self, account):
        self.account = account
        cookie = account.cookie.strip()
        if cookie.startswith('.ROBLOSECURITY='):
            cookie = cookie.split('=', 1)[1].split(';', 1)[0]
        if '\n' in cookie or '\r' in cookie:
            raise ValueError('Cookie должен быть одной строкой')
        self.headers = {'Cookie': '.ROBLOSECURITY=' + cookie, 'User-Agent': 'Mozilla/5.0',
                        'Origin': 'https://www.roblox.com', 'Referer': 'https://www.roblox.com/',
                        'Content-Type': 'application/json'}
        self.proxies = normalize_proxy(account.proxy)
        self.session = requests.Session()
        self.session.trust_env = False
        self.csrf_token = None

    def _request(self, method, url, csrf=False, **kwargs):
        headers = dict(self.headers)
        # Receive CSRF on the real operation, never call /logout.
        if csrf and self.csrf_token:
            headers['x-csrf-token'] = self.csrf_token
        for attempt in range(3):
            try:
                response = self.session.request(method, url, headers=headers, proxies=self.proxies,
                                                timeout=(8, 20), allow_redirects=False, **kwargs)
            except requests.RequestException:
                # No exception text: proxy URLs may contain passwords.
                LOG.warning('AutoVIP Roblox: сетевая ошибка %s %s', method, urlsplit(url).hostname)
                return None
            if csrf and response.status_code == 403 and response.headers.get('x-csrf-token'):
                self.csrf_token = response.headers['x-csrf-token']
                headers['x-csrf-token'] = self.csrf_token
                continue
            if response.status_code == 429 and attempt < 2:
                time.sleep(min(5, max(1, as_int(response.headers.get('Retry-After'), 2))))
                continue
            return response
        return None

    def _json(self, method, url, **kwargs):
        response = self._request(method, url, **kwargs)
        if response is None or response.status_code not in (200, 204):
            raise RuntimeError('Roblox: HTTP ' + str(getattr(response, 'status_code', 'network')))
        if response.status_code == 204:
            return {}
        try:
            return response.json()
        except ValueError:
            raise RuntimeError('Roblox вернул некорректный JSON') from None

    def authenticated(self):
        try:
            data = self._json('GET', 'https://users.roblox.com/v1/users/authenticated')
            return (str(data['id']), str(data['name'])) if as_int(data.get('id')) > 0 else None
        except (RuntimeError, KeyError):
            return None

    def details(self, server_id):
        data = self._json('GET', f'https://games.roblox.com/v1/vip-servers/{server_id}')
        if str(data.get('id')) != str(server_id):
            raise RuntimeError('Roblox вернул другой VIP Server ID')
        return data

    def private_servers(self, place_id):
        result, cursor, seen = [], None, set()
        for _ in range(100):
            params = {'excludeFriendServers': 'true'}
            if cursor:
                params['exclusiveStartKeyCursor'] = cursor
            data = self._json('GET', f'https://games.roblox.com/v1/games/{place_id}/private-servers', params=params)
            if not isinstance(data.get('data'), list):
                raise RuntimeError('Roblox не вернул список VIP-серверов')
            result.extend(data['data'])
            cursor = data.get('nextPageCursor')
            if not cursor:
                return result
            if cursor in seen:
                break
            seen.add(cursor)
        raise RuntimeError('Roblox: не удалось дочитать страницы VIP-серверов')

    def verify_owner(self, server_id, place_id):
        details = self.details(server_id)
        game = details.get('game') or {}
        root = game.get('rootPlace') or {}
        actual_place = root.get('id') or game.get('placeId')
        if not actual_place or str(actual_place) != str(place_id):
            raise RuntimeError('Place ID не совпадает с корневым Place ID VIP-сервера')
        rows = [r for r in self.private_servers(place_id) if str(r.get('vipServerId')) == str(server_id)]
        if not rows or any(str((r.get('owner') or {}).get('id')) != str(self.account.roblox_id) for r in rows):
            raise RuntimeError('Roblox не подтвердил владельца VIP-сервера')
        return details

    def permission_ids(self, server_id):
        permissions = self.details(server_id).get('permissions')
        if not isinstance(permissions, dict) or not isinstance(permissions.get('users'), list) or 'friendsAllowed' not in permissions:
            raise RuntimeError('Roblox не подтвердил список разрешений')
        return {str(u['id']) for u in permissions['users']}, permissions['friendsAllowed']

    def set_permission(self, server_id, friend_id, add):
        self._json('PATCH', f'https://games.roblox.com/v1/vip-servers/{server_id}/permissions', csrf=True,
                   json={'usersToAdd' if add else 'usersToRemove': [int(friend_id)], 'friendsAllowed': False})
        users, broad_access = self.permission_ids(server_id)
        return (str(friend_id) in users) == add and broad_access is False

    def clear_permissions(self, server_id):
        users, _ = self.permission_ids(server_id)
        self._json('PATCH', f'https://games.roblox.com/v1/vip-servers/{server_id}/permissions', csrf=True,
                   json={'usersToRemove': [int(u) for u in users], 'friendsAllowed': False})
        users, broad_access = self.permission_ids(server_id)
        return not users and broad_access is False

    def set_active(self, server_id, active):
        self._json('PATCH', f'https://games.roblox.com/v1/vip-servers/{server_id}', csrf=True, json={'active': active})
        return self.details(server_id).get('active') is active

    def regenerate_link(self, server_id):
        old = self.details(server_id)
        self._json('PATCH', f'https://games.roblox.com/v1/vip-servers/{server_id}', csrf=True, json={'newJoinCode': True})
        data = self.details(server_id)
        code = data.get('joinCode')
        if not code or code == old.get('joinCode'):
            raise RuntimeError('Roblox не подтвердил смену кода входа')
        place = ((data.get('game') or {}).get('rootPlace') or {}).get('id')
        if not as_int(place):
            raise RuntimeError('Roblox не вернул корневой Place ID')
        return f'https://www.roblox.com/games/{place}?privateServerLinkCode={quote(str(code), safe="")}'

    def shutdown(self, server_id, place_id):
        # Deactivate first; block reconnects. Subscription/renewal is never changed.
        if not self.set_active(server_id, False):
            return False
        for attempt in range(4):
            rows = [r for r in self.private_servers(place_id) if str(r.get('vipServerId')) == str(server_id)]
            if rows and all(r.get('playing') == 0 for r in rows):
                return self.details(server_id).get('active') is False
            for row in rows:
                game_id = row.get('id')
                if row.get('playing', 0) and game_id:
                    self._request('POST', 'https://apis.roblox.com/matchmaking-api/v1/game-instances/shutdown',
                                  csrf=True, json={'placeId': int(place_id), 'gameId': str(game_id),
                                                   'privateServerId': int(server_id)})
            if attempt < 3:
                time.sleep(2)
        # An absent row is not proof of zero players. Keep quarantined and retry.
        return False

    def resolve_user(self, raw):
        match = re.fullmatch(r'https://(?:www\.)?roblox\.com/users/(\d+)(?:/profile)?/?', raw, re.I)
        user_id = raw if raw.isascii() and raw.isdigit() else (match.group(1) if match else None)
        if user_id:
            data = self._json('GET', f'https://users.roblox.com/v1/users/{user_id}')
            return str(data['id']), data['name']
        if not re.fullmatch(r'[A-Za-z0-9_]{3,20}', raw):
            return None
        data = self._json('POST', 'https://users.roblox.com/v1/usernames/users', json={'usernames': [raw], 'excludeBannedUsers': True}).get('data') or []
        return (str(data[0]['id']), data[0]['name']) if data else None

    def is_friend(self, other_id):
        data = self._json('GET', f'https://friends.roblox.com/v1/users/{self.account.roblox_id}/friends/statuses',
                          params={'userIds': str(other_id)})
        rows = [r for r in data.get('data', []) if str(r.get('id')) == str(other_id)]
        if len(rows) != 1:
            raise RuntimeError('Roblox не подтвердил статус дружбы')
        status = rows[0].get('status')
        if isinstance(status, bool):
            raise RuntimeError('Roblox вернул недопустимый статус дружбы')
        if isinstance(status, int):
            status = {0:'NotFriends', 1:'Friends', 2:'RequestSent', 3:'RequestReceived'}.get(status)
        if status not in ('Friends', 'NotFriends', 'RequestSent', 'RequestReceived'):
            raise RuntimeError('Roblox не подтвердил статус дружбы')
        return status == 'Friends'

    def accept_friend_request(self, friend_id):
        self._json('POST', f'https://friends.roblox.com/v1/users/{friend_id}/accept-friend-request', csrf=True)
        return self.is_friend(friend_id)

    def unfriend(self, friend_id):
        if not self.is_friend(friend_id):
            return True
        self._json('POST', f'https://friends.roblox.com/v1/users/{friend_id}/unfriend', csrf=True)
        return not self.is_friend(friend_id)


def database() -> PluginDB:
    if DB is None:
        raise RuntimeError("Auto VIP Roblox database has not been initialized")
    return DB


def account_for_server(server_id: str) -> tuple[sqlite3.Row, sqlite3.Row] | None:
    return database().row("""
        SELECT s.server_id,s.place_id,s.status,a.id account_id,a.cookie,a.proxy,a.roblox_id,a.username
        FROM servers s JOIN accounts a ON a.id=s.owner_account_id
        WHERE s.server_id=? AND a.enabled=1
    """, (server_id,))


def roblox_for_server(server_id: str) -> tuple[RobloxAPI, sqlite3.Row] | None:
    row = account_for_server(server_id)
    if not row:
        return None
    account = RobloxAccount(int(row["account_id"]), row["cookie"], row["proxy"], str(row["roblox_id"] or ""))
    return RobloxAPI(account), row


def active_rental(buyer_id, chat_id):
    rows = database().rows("SELECT * FROM rentals WHERE buyer_id=? AND chat_id=? AND status='active' AND end_time>?", (buyer_id, chat_id, time.time()))
    return rows[0] if len(rows) == 1 else None


def total_bonus(hours):
    for rule in database().rows('SELECT buy_hours,get_hours FROM wholesale_bonuses ORDER BY buy_hours DESC'):
        buy = positive_float(rule['buy_hours'], 'Порог бонуса')
        get = positive_float(rule['get_hours'], 'Размер бонуса')
        if hours >= buy:
            return positive_float(int(hours // buy) * get, 'Итоговый бонус')
    return 0.0


def choose_free_server(con, lot):
    key = source_key(lot)
    if not key or lot['paused']:
        return None
    base = '''SELECT s.* FROM servers s JOIN accounts a ON a.id=s.owner_account_id
        WHERE s.status='free' AND s.enabled=1 AND s.verified=1 AND a.enabled=1
        AND s.place_id IS NOT NULL AND a.roblox_id IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM rentals r WHERE r.server_id=s.server_id
          AND r.status IN ('issuing','delivering','active','refunding','cleaning','cleanup_failed'))'''
    if lot['bind_type'] == 'server':
        return con.execute(base + ' AND s.server_id=?', (lot['server_id'],)).fetchone()
    if lot['bind_type'] == 'category':
        return con.execute(base + ''' AND s.category_id=? AND EXISTS
            (SELECT 1 FROM categories c WHERE c.id=s.category_id AND c.game_id=s.place_id)
            ORDER BY s.server_id LIMIT 1''', (lot['category_id'],)).fetchone()
    return con.execute(base + ''' AND s.category_id IS NULL AND NOT EXISTS
        (SELECT 1 FROM lots l WHERE l.server_id=s.server_id)
        ORDER BY s.server_id LIMIT 1''').fetchone()


def send_fp(cardinal, chat_id, text, chat_name=None):
    try:
        # Account API bypasses Cardinal's $photo/$sleep parser; FunPay is plain text.
        text = html.unescape(re.sub(r'</?(?:b|i|code|u|s|strong|em)>', '', text))
        result = cardinal.account.send_message(int(chat_id) if str(chat_id).isdigit() else chat_id, text, chat_name)
        confirmed = bool(getattr(result, 'id', None))
        trace_command('send', 'Отправка подтверждена' if confirmed else 'FunPay не подтвердил отправку')
        return confirmed
    except Exception as exc:
        trace_command('send', 'Ошибка отправки: ' + type(exc).__name__)
        LOG.warning('AutoVIP: сообщение в чат %s не подтверждено', chat_id)
        return False


def cardinal_admin_ids(cardinal: Any) -> set[int]:
    """Read the authorization list owned by Cardinal; this plugin never self-registers admins."""
    telegram = getattr(cardinal, "telegram", None)
    raw = getattr(telegram, "authorized_users", {}) if telegram is not None else {}
    values = raw.keys() if isinstance(raw, dict) else (raw or [])
    result: set[int] = set()
    for value in values:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            pass
    # Some older Cardinal builds expose the same allowlist under admin_ids.
    for value in getattr(telegram, "admin_ids", []) if telegram is not None else []:
        try:
            result.add(int(value))
        except (TypeError, ValueError):
            pass
    return result


def is_cardinal_admin(cardinal: Any, user_id: Any) -> bool:
    try:
        return int(user_id) in cardinal_admin_ids(cardinal)
    except (TypeError, ValueError):
        return False


def notify_admins(cardinal: Any, text: str) -> None:
    bot = getattr(getattr(cardinal, "telegram", None), "bot", None)
    if bot is None:
        return
    for user_id in cardinal_admin_ids(cardinal):
        try:
            bot.send_message(user_id, text, parse_mode="HTML")
        except Exception:
            LOG.debug("AutoVIP: не удалось отправить админ-уведомление", exc_info=True)


def is_plugin_enabled(cardinal: Any) -> bool:
    plugin = getattr(cardinal, "plugins", {}).get(UUID)
    return bool(plugin and plugin.enabled)


def order_fields(cardinal, event):
    compact = event.order
    order_id = str(getattr(compact, 'id', '')).lstrip('#')
    if not re.fullmatch(r'[A-Za-z0-9-]{1,64}', order_id):
        raise ValueError('Неизвестный формат ID заказа')
    order = cardinal.account.get_order(order_id)
    if str(order.id).lstrip('#') != order_id or status_name(order) not in ('paid', 'closed'):
        raise ValueError('Заказ не оплачен, возвращён или не совпадает ID')
    if str(getattr(order, 'seller_id', '')) != str(cardinal.account.id):
        raise ValueError('Заказ принадлежит другому продавцу')
    saved = database().row('SELECT * FROM incoming_orders WHERE order_id=?', (order_id,))
    if saved and saved['event_error']:
        raise ValueError(saved['event_error'])
    ids = native_lot_ids(order) | native_lot_ids(compact)
    if saved and saved['event_lot_id']:
        ids.add(int(saved['event_lot_id']))
    if not ids and callable(getattr(cardinal.account, 'get_orders_by_ids', None)):
        response = cardinal.account.method('post', 'https://funpay.com/api/orders/get',
            headers={'Content-Type': 'application/json'}, payload=json.dumps({'order_uids': [order_id], 'include': ['details', 'users']}), raise_not_200=True)
        body = response.json()
        raw = (body.get('data') or {}).get(order_id) or {}
        if body.get('status') != 'SUCCESS' or raw.get('order_uid') != order_id:
            raise ValueError('FunPay не подтвердил исходные данные заказа')
        ids = native_lot_ids(raw) | native_lot_ids(raw.get('type_data') or {})
    if len(ids) > 1:
        raise ValueError('Противоречивые ID лота; требуется разбор')
    if saved and saved['lot_id']:
        if ids and saved['lot_id'] not in ids:
            raise ValueError('Ручная привязка противоречит ID предложения FunPay')
        ids = {int(saved['lot_id'])}
    if not ids:
        raise ValueError('FunPayAPI не передаёт ID купленного предложения. Требуется ручная привязка заказа в /vip_config → Заказы.')
    lot_id = ids.pop()
    buyer_id = str(getattr(order, 'buyer_id', '') or '')
    chat_id = str(getattr(order, 'chat_id', '') or getattr(compact, 'chat_id', '') or '')
    if not buyer_id.isdigit() or int(buyer_id) <= 0 or not chat_id:
        raise ValueError('Нет покупателя или чата в заказе')
    amount = getattr(order, 'amount', None)
    if amount is None:
        amount = getattr(compact, 'amount', None)
    if amount is None and saved:
        amount = saved['amount']
    if amount is None or not math.isfinite(float(amount)) or float(amount) != int(float(amount)) or not 1 <= int(float(amount)) <= 1_000_000:
        raise ValueError('FunPayAPI не подтвердил целое количество товара')
    price = getattr(order, 'sum', None)
    if price is None:
        price = getattr(compact, 'price', None)
    if price is None and saved:
        price = saved['price']
    if price is None or not math.isfinite(float(price)) or float(price) < 0:
        raise ValueError('FunPayAPI не подтвердил сумму платежа')
    return order, lot_id, order_id, buyer_id, chat_id, int(float(amount)), float(price)


def process_order(cardinal, event):
    order_id = str(getattr(event.order, 'id', '')).lstrip('#')
    db = database()
    # Only a reservation transaction owns stock. Per-buyer serializing also prevents
    # two simultaneous payments from creating parallel sessions for the same lot.
    with ORDER_LOCK:
        if db.row('SELECT 1 FROM payments WHERE order_id=?', (order_id,)):
            return
        try:
            order, lot_id, order_id, buyer_id, chat_id, amount, price = order_fields(cardinal, event)
        except Exception as exc:
            error = str(exc) if isinstance(exc, ValueError) else 'Не удалось получить данные заказа FunPay'
            with db.lock, db.conn() as con:
                old = con.execute('SELECT attempts FROM incoming_orders WHERE order_id=?', (order_id,)).fetchone()
                con.execute("UPDATE incoming_orders SET state='attention',error=?,attempts=attempts+1,next_at=? WHERE order_id=?",
                            (error, time.time() + 300, order_id))
            if not old or not old['attempts']:
                notify_admins(cardinal, f'⚠️ Auto VIP: заказ #{esc(order_id)} не выдан.\n{esc(error)}')
            return
        lot = db.row('SELECT * FROM lots WHERE lot_id=?', (lot_id,))
        if not lot:
            with db.lock, db.conn() as con:
                con.execute("UPDATE incoming_orders SET state='ignored',error=NULL WHERE order_id=?", (order_id,))
            return
        # A second purchase must wait for the first link to be delivered before
        # it can extend the same session. Never allocate another server solely
        # because delivery of the first payment is delayed.
        waiting = db.row("""SELECT 1 FROM rentals WHERE buyer_id=? AND chat_id=? AND lot_id=?
            AND status IN ('issuing','delivering') AND source_key=?""",
            (buyer_id, chat_id, lot_id, source_key(lot)))
        if waiting:
            with db.lock, db.conn() as con:
                con.execute("UPDATE incoming_orders SET state='pending',next_at=?,error='Ожидается доставка предыдущей покупки этого лота' WHERE order_id=?",
                            (time.time() + 10, order_id))
            return
        try:
            hours = positive_float(positive_float(lot['hours'], 'Часы лота') * amount, 'Итоговое время')
            bonus = total_bonus(hours)
            total = positive_float(hours + bonus, 'Время с бонусом')
        except ValueError:
            with db.lock, db.conn() as con:
                con.execute("UPDATE incoming_orders SET state='attention',error='Недопустимые часы или бонусы лота' WHERE order_id=?", (order_id,))
            notify_admins(cardinal, f'⚠️ Auto VIP: проверьте часы/бонусы лота {lot_id}, заказ #{esc(order_id)} не выдан.')
            return
        existing = db.row("SELECT * FROM rentals WHERE buyer_id=? AND chat_id=? AND lot_id=? AND status='active' AND end_time>? ORDER BY start_time LIMIT 1",
                          (buyer_id, chat_id, lot_id, time.time()))
        rental_id = existing['rental_id'] if existing else str(uuid.uuid4())
        with ExitStack() as operation_locks:
            operation_locks.enter_context(operation_lock('rental:' + rental_id))
            with db.lock, db.conn() as con:
                con.execute('BEGIN IMMEDIATE')
                lot = con.execute('SELECT * FROM lots WHERE lot_id=?', (lot_id,)).fetchone()
                if not lot or con.execute('SELECT 1 FROM payments WHERE order_id=?', (order_id,)).fetchone():
                    return
                previous = existing
                existing = con.execute("SELECT * FROM rentals WHERE rental_id=? AND status='active' AND end_time>?", (rental_id, time.time())).fetchone() if previous else None
                key = source_key(lot)
                if existing and (not key or existing['source_key'] != key or lot['paused']):
                    existing = None
                if previous and not existing:
                    # Cleanup or configuration may have changed while we waited.
                    # A replacement session needs both its own ID and its own lock.
                    rental_id = str(uuid.uuid4())
                    operation_locks.enter_context(operation_lock('rental:' + rental_id))
                server = None if existing else choose_free_server(con, lot)
                currency_obj = getattr(order, 'currency', None)
                currency = str(getattr(currency_obj, 'name', currency_obj) or 'UNKNOWN')
                if existing:
                    state = 'extension_pending'
                    end = add_credit(con, rental_id, order_id, total, float(existing['end_time']))
                    con.execute('UPDATE rentals SET end_time=?,hours=hours+?,price=price+? WHERE rental_id=?', (end, total, price, rental_id))
                    enqueue(con, 'extension:' + order_id, chat_id, f'✅ Аренда #{payment_label(rental_id)} продлена на {total:g} ч. Остаток: {format_left(end-time.time())}.', rental_id, order_id, 'extension')
                elif server:
                    state = 'issuing'
                    con.execute("UPDATE servers SET status='reserved' WHERE server_id=?", (server['server_id'],))
                    con.execute('''INSERT INTO rentals(rental_id,buyer_id,chat_id,lot_id,server_id,owner_account_id,hours,start_time,end_time,status,price,source_key)
                        VALUES(?,?,?,?,?,?,?,?,?,'issuing',?,?)''', (rental_id, buyer_id, chat_id, lot_id, server['server_id'], server['owner_account_id'], total, 0, 0, price, key))
                    add_credit(con, rental_id, order_id, total, 0)
                else:
                    state, rental_id = 'out_of_stock', None
                    enqueue(con, 'stock:' + order_id, chat_id, '😔 Нет доступного настроенного VIP-сервера для этого лота. Продавец уведомлён.', order_id=order_id)
                con.execute('''INSERT INTO payments(order_id,rental_id,buyer_id,chat_id,lot_id,paid_hours,bonus_hours,price,state,currency,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (order_id, rental_id, buyer_id, chat_id, lot_id, hours, bonus, price, state, currency, time.time()))
                con.execute("UPDATE incoming_orders SET state='done',error=NULL WHERE order_id=?", (order_id,))
            if existing:
                return
            if not server:
                notify_admins(cardinal, f'⚠️ Auto VIP: заказ #{esc(order_id)} — нет доступного сервера для лота {lot_id}.')
                if db.setting('auto_refund') == '1':
                    request_refund(order_id)
                return
            try:
                api, owner = roblox_for_server(server['server_id']) or (None, None)
                if not api:
                    raise RuntimeError('Нет владельца сервера')
                api.verify_owner(server['server_id'], server['place_id'])
                if not api.shutdown(server['server_id'], server['place_id']) or not api.clear_permissions(server['server_id']):
                    raise RuntimeError('Roblox не подтвердил подготовку пустого сервера')
                link = api.regenerate_link(server['server_id'])
                if not api.set_active(server['server_id'], True):
                    raise RuntimeError('Roblox не подтвердил включение сервера')
                template = db.setting('msg_post_payment')
                if '{link}' not in template:
                    template += '\n🔗 {link}'
                if '{owner_url}' not in template:
                    template += '\nЗаявку в друзья отправляйте владельцу: {owner_url}'
                replacements = {'link': link, 'hours': f'{total:g}', 'order_id': order_id,
                                'bonus_line': f'\n🎁 Бонус: +{bonus:g} ч.' if bonus else '',
                                'owner_url': f'https://www.roblox.com/users/{owner["roblox_id"]}/profile'}
                for key, value in replacements.items():
                    template = template.replace('{' + key + '}', value)
                template += f'\nДля выбора этой аренды добавляйте #{order_id} к команде. Пример: !друзья #{order_id} Ник'
                with db.lock, db.conn() as con:
                    con.execute("UPDATE rentals SET status='delivering',link=? WHERE rental_id=?", (link, rental_id))
                    con.execute("UPDATE payments SET state='delivery_pending' WHERE order_id=?", (order_id,))
                    enqueue(con, 'issue:' + order_id, chat_id, template, rental_id, order_id, 'issue')
            except Exception:
                fail_issue(cardinal, order_id, rental_id, server['server_id'], chat_id, 'Roblox не подтвердил безопасную подготовку VIP-сервера')


def fail_issue(cardinal, order_id, rental_id, server_id, chat_id, reason):
    with database().lock, database().conn() as con:
        con.execute("UPDATE rentals SET status='cleanup_failed',cleanup_reason='issue_failed',last_error=?,next_cleanup_at=0 WHERE rental_id=?", (reason, rental_id))
        con.execute("UPDATE servers SET status='cleaning' WHERE server_id=?", (server_id,))
        con.execute("UPDATE payments SET state='issue_failed' WHERE order_id=?", (order_id,))
        enqueue(con, 'issue-failed:' + order_id, chat_id, '❌ Не удалось подготовить VIP-сервер. Продавец уведомлён.', rental_id)
    if database().setting('auto_refund') == '1':
        request_refund(order_id)
    notify_admins(cardinal, f'⚠️ Auto VIP: невыдача #{esc(order_id)}. Сервер заблокирован до очистки.')


def format_left(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return f"{hours}ч {minutes}м" if hours else f"{minutes}м"


def cleanup_rental(cardinal, rental_id, reason):
    db = database()
    with operation_lock('rental:' + rental_id):
        with db.lock, db.conn() as con:
            con.execute('BEGIN IMMEDIATE')
            rental = con.execute('SELECT * FROM rentals WHERE rental_id=?', (rental_id,)).fetchone()
            if not rental or rental['status'] not in ('active', 'delivering', 'cleanup_failed', 'refunding'):
                return False
            if rental['status'] == 'active' and reason == 'expired' and rental['end_time'] > time.time():
                return False
            if rental['status'] == 'delivering' and reason == 'expired':
                return False
            reason = rental['cleanup_reason'] or reason
            con.execute("UPDATE rentals SET status='cleaning',cleanup_reason=?,cleanup_attempts=cleanup_attempts+1 WHERE rental_id=?", (reason, rental_id))
            con.execute("UPDATE servers SET status='cleaning' WHERE server_id=?", (rental['server_id'],))
            con.execute("UPDATE outbox SET state='cancelled' WHERE rental_id=? AND kind IN ('issue','extension','link') AND state='pending'", (rental_id,))
        try:
            api, server = roblox_for_server(rental['server_id']) or (None, None)
            if not api or str(server['account_id']) != str(rental['owner_account_id']):
                raise RuntimeError('Не подтверждён владелец аренды')
            # Both operations are attempted even if one fails.
            errors = []
            for operation in (lambda: api.shutdown(rental['server_id'], server['place_id']),
                              lambda: api.clear_permissions(rental['server_id']),
                              lambda: bool(api.regenerate_link(rental['server_id']))):
                try:
                    if not operation():
                        errors.append('Нет подтверждения Roblox')
                except Exception:
                    errors.append('Ошибка операции Roblox')
            if errors:
                raise RuntimeError('; '.join(errors))
            with db.lock, db.conn() as con:
                con.execute("UPDATE rental_friends SET state='removed' WHERE rental_id=?", (rental_id,))
            # Do not delete a friendship still used by another rental on this owner.
            cleanup_managed_friends(api)
            with db.lock, db.conn() as con:
                pending = con.execute("SELECT 1 FROM payments WHERE rental_id=? AND state!='refunded'", (rental_id,)).fetchone()
                final = 'completed' if reason in ('expired', 'manual') else 'cancelled'
                if reason == 'refund' and not pending:
                    final = 'refunded'
                if reason == 'issue_failed':
                    final = 'issue_failed'
                con.execute('UPDATE rentals SET status=?,last_error=NULL,next_cleanup_at=NULL,link=NULL WHERE rental_id=?', (final, rental_id))
                con.execute("UPDATE servers SET status='free' WHERE server_id=?", (rental['server_id'],))
                undelivered = 'cancelled_unissued' if reason in ('manual','cancelled') else 'delivery_failed'
                con.execute("UPDATE payments SET state=? WHERE rental_id=? AND state='delivery_pending'", (undelivered, rental_id))
                con.execute("UPDATE payments SET state='extension_unnotified' WHERE rental_id=? AND state='extension_pending'", (rental_id,))
                if rental['chat_id']:
                    enqueue(con, 'finished:' + rental_id, rental['chat_id'], db.setting('msg_post_expire').replace('{order_id}', payment_label(rental_id)), rental_id)
            return True
        except Exception:
            cleanup_error(rental_id, 'Roblox не подтвердил полную очистку. Сервер остаётся заблокирован; будет повтор.')
            return False


def cleanup_error(rental_id: str, error: str) -> None:
    with database().lock, database().conn() as con:
        row = con.execute("SELECT cleanup_attempts,server_id FROM rentals WHERE rental_id=?", (rental_id,)).fetchone()
        attempts = int(row["cleanup_attempts"] or 1) if row else 1
        # 30s, 60s, 120s ... then at most once per 15 minutes for a bad proxy/API outage.
        delay = min(900, 30 * (2 ** min(attempts - 1, 5)))
        con.execute("UPDATE rentals SET status='cleanup_failed',last_error=?,next_cleanup_at=? WHERE rental_id=?",
                    (error, time.time() + delay, rental_id))
        if row:
            con.execute("UPDATE servers SET status='cleaning' WHERE server_id=?", (row["server_id"],))
    LOG.warning("AutoVIP cleanup retry required: rental=%s: %s", rental_id, error)
    if CARDINAL is not None:
        notify_admins(CARDINAL, f"⚠️ <b>Auto VIP: очистка требует повтора</b>\nАренда: <code>{esc(rental_id[:8])}</code>\nПричина: {esc(error)}")


def payment_label(rental_id):
    row = database().row("""SELECT order_id FROM payments WHERE rental_id=?
        ORDER BY CASE WHEN state IN ('issued','extended','extension_pending','delivery_pending') THEN 0 ELSE 1 END,
        created_at,order_id LIMIT 1""", (rental_id,))
    return str(row['order_id']) if row else rental_id[:8]


def worker_loop(cardinal):
    last_sync = 0
    while not STOP.wait(3):
        try:
            db, now = database(), time.time()
            recover_idle_operations()
            for row in db.rows("SELECT rental_id,cleanup_reason FROM rentals WHERE (status='active' AND end_time<=?) OR (status='cleanup_failed' AND COALESCE(next_cleanup_at,0)<=?)", (now, now)):
                run_once('rental:' + row['rental_id'], cleanup_rental, cardinal, row['rental_id'], row['cleanup_reason'] or 'expired')
            for row in db.rows("SELECT id FROM outbox WHERE state='pending' AND next_at<=? ORDER BY id LIMIT 20", (now,)):
                run_once('outbox:' + str(row['id']), deliver_outbox, cardinal, row['id'])
            if db.setting('auto_refund') == '1':
                for row in db.rows("SELECT order_id FROM payments WHERE state IN ('issue_failed','delivery_failed','out_of_stock')"):
                    request_refund(row['order_id'])
            for row in db.rows("SELECT order_id FROM payments WHERE state='refund_pending' AND next_refund_at<=?", (now,)):
                run_once('refund:' + row['order_id'], perform_refund, cardinal, row['order_id'])
            if is_plugin_enabled(cardinal):
                for row in db.rows("SELECT order_id FROM incoming_orders WHERE state='pending' AND next_at<=? ORDER BY created_at LIMIT 10", (now,)):
                    run_once('order:' + row['order_id'], process_order, cardinal, SimpleNamespace(order=SimpleNamespace(id=row['order_id'])))
                if now-last_sync >= 60:
                    last_sync = now
                    run_once('lots', reconcile_lots, cardinal)
        except Exception:
            LOG.error('AutoVIP: ошибка фонового цикла; следующая попытка через 3 секунды')


def reconcile_lots(cardinal):
    if database().setting('lot_management') != '1':
        return
    with FP_LOT_LOCK:
        for lot in database().rows('SELECT * FROM lots'):
            try:
                with database().lock, database().conn() as con:
                    desired = choose_free_server(con, lot) is not None
                fields = cardinal.account.get_lot_fields(int(lot['lot_id']))
                if bool(fields.active) != desired:
                    fields.active = desired
                    cardinal.account.save_lot(fields.renew_fields())
                with database().lock, database().conn() as con:
                    con.execute('UPDATE lots SET active=?,last_error=NULL WHERE lot_id=?', (int(desired), lot['lot_id']))
            except Exception:
                with database().lock, database().conn() as con:
                    con.execute("UPDATE lots SET last_error='FunPay не подтвердил синхронизацию' WHERE lot_id=?", (lot['lot_id'],))
            if STOP.wait(1):
                return


def apply_prices(cardinal):
    failed = []
    with FP_LOT_LOCK:
        target = 'price_event' if database().setting('event_mode') == '1' else 'price_normal'
        for lot in database().rows(f'SELECT lot_id,{target} price FROM lots WHERE {target}>0'):
            try:
                fields = cardinal.account.get_lot_fields(int(lot['lot_id']))
                fields.price = round(positive_float(lot['price'], 'Цена'), 2)
                cardinal.account.save_lot(fields.renew_fields())
            except Exception:
                failed.append(str(lot['lot_id']))
            if STOP.wait(1):
                return
    notify_admins(cardinal, 'Auto VIP: цены обновлены.' if not failed else 'Auto VIP: не удалось обновить цены лотов: ' + ', '.join(failed))


def command_names(text: str) -> str:
    if database().setting("legacy_commands", "0") == "1":
        return text.replace("!vip_time", "!время").replace("!vip_link", "!ссылка").replace("!vip_friends", "!друзья").replace("!vip_help", "!команды").replace("!vip_stock", "!бронь").replace("!vip_review", "!отзыв")
    return text


def process_chat_message(cardinal, event):
    obj = getattr(event, 'message', None) or getattr(event, 'chat', None)
    text = command_text(getattr(obj, 'text', '') or getattr(obj, 'last_message_text', ''))
    buyer = str(getattr(obj, 'author_id', None) or getattr(obj, 'last_message_author_id', '') or '')
    chat = str(getattr(obj, 'chat_id', None) or getattr(obj, 'id', '') or '')
    parsed = parse_chat_command(text)
    if parsed is None:
        return
    if not buyer.isdigit() or buyer == '0':
        trace_command('process', 'Не подтверждён автор-покупатель')
        return
    if buyer == str(cardinal.account.id) or getattr(obj, 'by_bot', False):
        trace_command('process', 'Пропущено сообщение продавца / бота')
        return
    action, parts = parsed
    trace_command('process', 'Команда принята: ' + action)
    if action == 'help':
        text = ('🎮 Команды VIP в этом чате:\n!vip_time — остаток времени\n!vip_link — обновить ссылку\n'
                '!vip_friends Ник — принять заявку и дать доступ\n!vip_stock — свободные серверы общего пула\n'
                '!vip_review — проверить отзыв 5★\n!vip_help — эта справка\n\n'
                'При нескольких арендах: !vip_link #ЗАКАЗ или !vip_friends #ЗАКАЗ Ник.')
        text += '\nРусские команды: !время, !ссылка, !друзья Ник, !бронь, !отзыв, !команды.\nСправка также: /com, !com, !command, !commands.\nСовместимость первой версии: !time, !link, !friend Ник, !вип.'
        send_fp(cardinal, chat, text)
        return
    if action == 'stock':
        with database().lock, database().conn() as con:
            count = con.execute("""SELECT COUNT(*) FROM servers s JOIN accounts a ON a.id=s.owner_account_id
                WHERE s.status='free' AND s.enabled=1 AND s.verified=1 AND a.enabled=1 AND s.category_id IS NULL
                AND s.place_id IS NOT NULL AND a.roblox_id IS NOT NULL
                AND NOT EXISTS(SELECT 1 FROM rentals r WHERE r.server_id=s.server_id AND r.status IN
                    ('issuing','delivering','active','refunding','cleaning','cleanup_failed'))
                AND NOT EXISTS(SELECT 1 FROM lots l WHERE l.server_id=s.server_id)""").fetchone()[0]
        send_fp(cardinal, chat, f'Свободных общих серверов: {count}.')
        return
    rentals = database().rows("SELECT * FROM rentals WHERE buyer_id=? AND chat_id=? AND status='active' AND end_time>? ORDER BY start_time", (buyer, chat, time.time()))
    selectors = [part.lstrip('#') for part in parts if part.startswith('#')]
    if len(selectors) > 1:
        send_fp(cardinal, chat, 'Укажите один номер заказа. Пример: !vip_friends #ЗАКАЗ Ник')
        return
    selector = selectors[0] if selectors else None
    parts = [part for part in parts if not part.startswith('#')]
    rental = None
    if selector:
        p = database().row("SELECT rental_id FROM payments WHERE order_id=? COLLATE NOCASE AND buyer_id=? AND chat_id=? AND state IN ('issued','extended','extension_pending')", (selector, buyer, chat))
        rental = next((r for r in rentals if p and r['rental_id'] == p['rental_id']), None)
    elif len(rentals) == 1:
        rental = rentals[0]
    if not rental:
        trace_command('process', 'Нужен выбор заказа' if rentals else 'Активной аренды покупателя в этом чате нет')
        labels = '\n'.join(f'#{payment_label(r["rental_id"])} · лот {r["lot_id"]} · {format_left(r["end_time"]-time.time())}' for r in rentals)
        send_fp(cardinal, chat, ('Укажите заказ после команды:\n' + labels) if rentals else 'Активных VIP-аренд нет.')
        return
    if action == 'friends':
        if len(parts) != 1:
            send_fp(cardinal, chat, f'Формат: !друзья #{payment_label(rental["rental_id"])} Ник')
        else:
            add_friend(cardinal, rental, parts[0])
        return
    with ExitStack() as operations:
        lock = operation_lock('rental:' + rental['rental_id'])
        if not lock.acquire(blocking=False):
            send_fp(cardinal, chat, '⏳ С этой арендой уже выполняется операция. Повторите команду чуть позже.')
            return
        operations.callback(lock.release)
        rental = database().row("SELECT * FROM rentals WHERE rental_id=? AND status='active' AND end_time>?", (rental['rental_id'], time.time()))
        if not rental:
            send_fp(cardinal, chat, 'Аренда уже завершена или её состояние изменилось. Проверьте !vip_time.')
            return
        if action == 'time':
            send_fp(cardinal, chat, f'⏱ #{payment_label(rental["rental_id"])}: {format_left(rental["end_time"]-time.time())}.')
        elif action == 'link':
            try:
                send_fp(cardinal, chat, '⏳ Обновляю ссылку VIP-сервера. Дождитесь результата.')
                api, _ = roblox_for_server(rental['server_id'])
                link = api.regenerate_link(rental['server_id'])
                if rental['end_time'] <= time.time():
                    send_fp(cardinal, chat, 'Время аренды закончилось во время обновления ссылки.')
                    return
                with database().lock, database().conn() as con:
                    con.execute('UPDATE rentals SET link=? WHERE rental_id=?', (link, rental['rental_id']))
                    con.execute("UPDATE outbox SET state='cancelled' WHERE rental_id=? AND kind='link' AND state='pending'", (rental['rental_id'],))
                    enqueue(con, 'link:' + str(uuid.uuid4()), chat, '🔗 Новая ссылка: ' + link, rental['rental_id'], kind='link')
                deliver_command_replies(cardinal, rental['rental_id'], kind='link')
            except Exception:
                send_fp(cardinal, chat, 'Roblox не подтвердил обновление ссылки. Попробуйте позднее.')
        elif action == 'review':
            send_fp(cardinal, chat, '⏳ Проверяю отзыв 5★ по вашему заказу.')
            rows = database().rows("SELECT order_id FROM payments WHERE rental_id=? AND state IN ('issued','extended','extension_pending') AND review_bonus_given=0", (rental['rental_id'],))
            added = False
            for p in rows:
                if selector and p['order_id'].lower() != selector.lower():
                    continue
                try:
                    granted = grant_review(cardinal, cardinal.account.get_order(p['order_id']))
                    added = granted or added
                    if granted:
                        deliver_command_replies(cardinal, rental['rental_id'], dedupe='review:' + p['order_id'])
                except Exception:
                    pass
            if not added:
                send_fp(cardinal, chat, 'Новый отзыв 5★ не найден, бонус уже выдан или выключен продавцом.')


def add_friend(cardinal, rental, raw):
    db = database()
    with GUARD:
        if 'rental:' + rental['rental_id'] in RUNNING:
            send_fp(cardinal, rental['chat_id'], 'Операция с этой арендой уже выполняется. Повторите позже.')
            return
    lock = operation_lock('rental:' + rental['rental_id'])
    if not lock.acquire(blocking=False):
        send_fp(cardinal, rental['chat_id'], 'Операция с этой арендой уже выполняется. Повторите позже.')
        return
    try:
        rental = db.row("SELECT * FROM rentals WHERE rental_id=? AND status='active' AND end_time>?", (rental['rental_id'], time.time()))
        if not rental:
            return
        if db.setting('friends_module_active') != '1':
            send_fp(cardinal, rental['chat_id'], 'Автодобавление друзей выключено.')
            return
        api, _ = roblox_for_server(rental['server_id'])
        with operation_lock('owner:' + str(rental['owner_account_id'])):
            send_fp(cardinal, rental['chat_id'], '⏳ Проверяю заявку Roblox и доступ к вашему серверу.')
            target = api.resolve_user(raw)
            if not target:
                raise ValueError('Пользователь Roblox не найден')
            friend_id, friend_name = target
            if friend_id == str(api.account.roblox_id):
                raise ValueError('Владелец уже имеет доступ к серверу')
            row = db.row('SELECT state FROM rental_friends WHERE rental_id=? AND roblox_id=?', (rental['rental_id'], friend_id))
            if row and row['state'] == 'added':
                send_fp(cardinal, rental['chat_id'], 'Этот пользователь уже добавлен.')
                return
            lot = db.row('SELECT friend_limit FROM lots WHERE lot_id=?', (rental['lot_id'],))
            limit = lot['friend_limit'] if lot else -1
            limit = as_int(db.setting('friends_default_limit'), 0) if limit < 0 else limit
            used = db.row("SELECT COUNT(*) n FROM rental_friends WHERE rental_id=? AND state!='removed'", (rental['rental_id'],))['n']
            if (not row or row['state'] == 'removed') and limit and used >= limit:
                send_fp(cardinal, rental['chat_id'], db.setting('msg_friend_limit').replace('{limit}', str(limit)))
                return
            prior = api.is_friend(friend_id)
            # Persist intent BEFORE accepting/granting, so crashes cannot orphan access.
            with db.lock, db.conn() as con:
                con.execute("INSERT INTO rental_friends(rental_id,roblox_id,name,unfriend_on_cleanup,state) VALUES(?,?,?,?,'pending') ON CONFLICT(rental_id,roblox_id) DO UPDATE SET state='pending'", (rental['rental_id'], friend_id, friend_name, int(not prior)))
                if not prior:
                    con.execute('INSERT OR IGNORE INTO managed_friends VALUES(?,?)', (rental['owner_account_id'], friend_id))
            try:
                if not prior and not api.accept_friend_request(friend_id):
                    raise ValueError('Сначала отправьте заявку владельцу Roblox')
                if rental['end_time'] <= time.time() or not api.set_permission(rental['server_id'], friend_id, True):
                    raise ValueError('Roblox не подтвердил доступ или время аренды закончилось')
                if rental['end_time'] <= time.time():
                    raise ValueError('Время аренды закончилось')
                with db.lock, db.conn() as con:
                    con.execute("UPDATE rental_friends SET state='added' WHERE rental_id=? AND roblox_id=?", (rental['rental_id'], friend_id))
                send_fp(cardinal, rental['chat_id'], db.setting('msg_friend_added').replace('{friend_name}', friend_name))
            except Exception:
                # Unknown network outcome must be compensated, even if no reply was received.
                try:
                    if api.set_permission(rental['server_id'], friend_id, False):
                        with db.lock, db.conn() as con:
                            con.execute("UPDATE rental_friends SET state='removed' WHERE rental_id=? AND roblox_id=?", (rental['rental_id'], friend_id))
                        cleanup_managed_friends(api)
                except Exception:
                    pass
                send_fp(cardinal, rental['chat_id'], db.setting('msg_friend_not_found').replace('{input}', raw))
    except Exception:
        if rental:
            send_fp(cardinal, rental['chat_id'], 'Не удалось добавить пользователя. Проверьте заявку владельцу и повторите команду.')
    finally:
        lock.release()


def process_review(cardinal, event):
    if MessageTypes is None:
        return
    message = event.message
    allowed = (getattr(MessageTypes, 'NEW_FEEDBACK', None), getattr(MessageTypes, 'FEEDBACK_CHANGED', None))
    if str(getattr(message, 'author_id', '')) != '0' or getattr(message, 'type', None) not in allowed:
        return
    order = cardinal.get_order_from_object(message)
    if order:
        grant_review(cardinal, cardinal.account.get_order(order.id))


# ----- Telegram panel -------------------------------------------------------

def edit_or_send(bot, chat_id, text, kb, message_id=None):
    footer = (f'\n\n<i>v{VERSION}</i>\n'
              f'Автор: <a href="{AUTHOR_URL}">{CREDITS}</a> · '
              f'<a href="{DEVELOPER_URL}">Канал</a>')
    text += footer
    options = {'reply_markup':kb, 'parse_mode':'HTML', 'disable_web_page_preview':True}
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, **options)
            return
        except Exception as exc:
            if 'message is not modified' in str(exc).lower():
                return
    bot.send_message(chat_id, text, **options)


def main_menu(bot, chat_id, message_id=None):
    db = database()
    counts = db.row("""SELECT
        (SELECT COUNT(*) FROM accounts WHERE id>0 AND enabled=1 AND enabled=1) accounts,
        (SELECT COUNT(*) FROM categories) categories,
        (SELECT COUNT(*) FROM servers WHERE enabled=1) servers,
        (SELECT COUNT(DISTINCT server_id) FROM rentals WHERE status IN
            ('active','issuing','delivering','refunding','cleaning','cleanup_failed')) occupied,
        (SELECT COUNT(*) FROM rentals WHERE status='active') active,
        (SELECT COUNT(*) FROM rentals WHERE status='cleanup_failed') failed,
        (SELECT COUNT(*) FROM incoming_orders WHERE state='attention') attention,
        (SELECT COUNT(*) FROM payments WHERE state IN ('refund_pending','partial_refund')) refunds,
        (SELECT COUNT(*) FROM outbox WHERE state IN ('pending','sending')) messages""")
    stock = db.row("""SELECT COUNT(*) free, COALESCE(SUM(CASE WHEN s.category_id IS NULL
        AND NOT EXISTS(SELECT 1 FROM lots l WHERE l.server_id=s.server_id) THEN 1 ELSE 0 END),0) general
        FROM servers s JOIN accounts a ON a.id=s.owner_account_id
        WHERE s.enabled=1 AND s.verified=1 AND s.status='free' AND a.enabled=1
        AND s.place_id IS NOT NULL AND a.roblox_id IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM rentals r WHERE r.server_id=s.server_id AND r.status IN
            ('issuing','delivering','active','refunding','cleaning','cleanup_failed'))""")
    earned = db.rows("SELECT currency,SUM(price) total FROM payments WHERE state IN ('issued','extended') GROUP BY currency ORDER BY currency")
    amounts = [money_text(row['total'], row['currency']) for row in earned]
    income = ' · '.join(amounts[:3]) if amounts else 'пока нет выдач'
    if len(amounts)>3:
        income += ' · подробнее в статистике'
    event_mode = db.setting('event_mode') == '1'
    mode = '🎉 Евент' if event_mode else '📋 Обычный'
    proxy = 'настроен' if db.setting('global_proxy') else 'напрямую'
    text = ('💼 <b>Auto VIP Roblox</b>\n'
            f'👥 Аккаунтов: <b>{counts["accounts"]}</b>\n'
            f'🖥 Серверов: <b>{counts["servers"]}</b> | 📁 Категорий: <b>{counts["categories"]}</b>\n\n'
            f'🌐 Глобальный прокси: <b>{proxy}</b>\n\n'
            f'🟢 Свободно: <b>{stock["free"]}</b> (общих: {stock["general"]}) | 🔴 Занято: <b>{counts["occupied"]}</b>\n'
            f'💰 Выдано: <b>{income}</b>\n\n⚙️ Режим: <b>{mode}</b>')
    alerts = []
    if counts['failed']:
        alerts.append(f'очистка — {counts["failed"]}')
    if counts['attention']:
        alerts.append(f'заказы — {counts["attention"]}')
    if counts['refunds']:
        alerts.append(f'возвраты — {counts["refunds"]}')
    if alerts:
        text += '\n\n⚠️ <b>Требуют внимания:</b> ' + ', '.join(alerts)
    if counts['messages']:
        text += f'\n✉️ Ожидают отправки: <b>{counts["messages"]}</b>'
    if not counts['accounts']:
        text += '\n\n💡 Начните с добавления аккаунта Roblox.'
    kb = Keyboard(row_width=2)
    kb.row(panel_button('Переключить режим', 'mode'))
    kb.row(panel_button('🌐 Настроить прокси', 'proxy'))
    order_title = '📥 Заказы' + (f' · {counts["attention"]}' if counts['attention'] else '')
    kb.row(panel_button('🖥 Сервера','servers'), panel_button('👥 Аккаунты','accounts'))
    kb.row(panel_button('📁 Категории серверов','cats'))
    kb.row(panel_button('⚙️ Лоты','lots'), panel_button('🕹 Управление арендой','rentals'))
    kb.row(panel_button('🎁 Бонусы','bonuses'), panel_button('📊 Статистика','stats'))
    kb.row(panel_button('📜 История заказов','c_history:0'), panel_button('📥 Разбор заказов','orders'))
    kb.row(panel_button('⭐ Отзывы','reviews'))
    kb.row(panel_button('🤝 Заявки в друзья','friends'))
    kb.row(panel_button('✉️ Шаблоны сообщений','templates'))
    kb.row(panel_button('⚙️ Настройки','settings'), panel_button('📖 Инструкция','guide'))
    kb.row(Button('👨‍💻 Автор', url=AUTHOR_URL), Button('📣 Канал автора', url=DEVELOPER_URL))
    kb.row(panel_button('🔄 Обновить панель', 'main'))
    edit_or_send(bot, chat_id, text, kb, message_id)


def simple_list_menu(bot, chat_id, message_id, kind, page=0):
    db = database()
    definitions = {
        'accounts': ('accounts', 'id>0 AND enabled=1', 'id', '👥 Управление аккаунтами', 'add_account'),
        'servers': ('servers', 'enabled=1 AND category_id IS NULL', 'server_id', '🖥 Общие серверы', 'add_server'),
        'cats': ('categories', '1=1', 'id', '📁 Категории серверов', 'add_category'),
        'lots': ('lots', '1=1', 'lot_id', '⚙️ Настройка лотов', 'add_lot'),
    }
    table, where, key, title, add_action = definitions[kind]
    total = db.row(f'SELECT COUNT(*) n FROM {table} WHERE {where}')['n']
    page, pages = classic_page(total, page)
    rows = db.rows(f'SELECT * FROM {table} WHERE {where} ORDER BY {key} LIMIT 8 OFFSET ?', (page*8,))
    text = f'<b>{title}</b>\n\nВсего: {total} · Страница {page+1}/{pages}\n\n'
    if kind == 'servers':
        text += 'Серверы вне категорий. Для разных игр откройте «Категории серверов».\n\n'
    elif kind == 'cats':
        text += 'Одна игра — одна категория. Выберите категорию, чтобы добавить в неё серверы.\n\n'
    elif kind == 'accounts':
        text += 'Выберите аккаунт для обновления cookie или удаления.\n\n'
    if not rows:
        text += 'Пока ничего не добавлено. Нажмите кнопку ниже.'
    kb = Keyboard(row_width=2)
    for row in rows:
        identity = row[key]
        if kind == 'accounts':
            label = '👤 ' + str(row['username'] or identity)
            action = f'c_account:{identity}'
        elif kind == 'servers':
            label = ('🟢 ' if row['status']=='free' else '🟠 ') + str(identity)
            action = f'c_server:{identity}'
        elif kind == 'cats':
            count = db.row('SELECT COUNT(*) n FROM servers WHERE enabled=1 AND category_id=?', (identity,))['n']
            label, action = f'📁 {row["name"]} · {count} серв.', f'c_cat:{identity}:0'
        else:
            label = ('⏸ ' if row['paused'] else '📦 ') + str(row['name'] or identity)
            action = f'lot:{identity}'
        kb.row(classic_button(label, action))
    classic_nav(kb, page, pages, 'listpage:' + kind)
    label = {'accounts':'аккаунт','servers':'общий сервер','cats':'категорию','lots':'лот'}[kind]
    kb.row(panel_button('➕ Добавить ' + label, 'c_wizard:' + add_action))
    kb.row(panel_button('⬅️ Главное меню', 'main'))
    edit_or_send(bot, chat_id, text, kb, message_id)


def rentals_menu(bot, chat_id, message_id, page=0, history=False):
    where = "status NOT IN ('issuing','delivering','active','refunding','cleaning','cleanup_failed')" if history else "status IN ('issuing','delivering','active','refunding','cleaning','cleanup_failed')"
    total = database().row('SELECT COUNT(*) n FROM rentals WHERE ' + where)['n']
    page, pages = classic_page(total, page, 7)
    rows = database().rows('SELECT * FROM rentals WHERE ' + where + ' ORDER BY end_time DESC LIMIT 7 OFFSET ?', (page*7,))
    text = f'{"📜 История заказов" if history else "🕹 Управление арендой"}\n\nВсего: {total} · Страница {page+1}/{pages}\n\n'
    kb = Keyboard(row_width=2)
    labels = {'active':'Активна','delivering':'Отправка ссылки','issuing':'Подготовка','refunding':'Возврат','cleaning':'Очистка','cleanup_failed':'Нужен повтор очистки','completed':'Завершена','cancelled':'Отменена','refunded':'Возвращена','issue_failed':'Не выдана'}
    for index, rental in enumerate(rows, page*7+1):
        order = payment_label(rental['rental_id'])
        status = labels.get(rental['status'], rental['status'])
        text += f'{index}. #{esc(order)} · {esc(status)}\nСервер: {esc(rental["server_id"])}'
        if rental['status']=='active':
            text += ' · ' + format_left(rental['end_time']-time.time())
        text += '\n\n'
        kb.row(classic_button(f'#{order} · сервер {rental["server_id"]}', f'rental:{rental["rental_id"]}'))
    if not rows:
        text += 'Пока нет записей.'
    classic_nav(kb, page, pages, 'c_history' if history else 'rentpage')
    kb.row(panel_button('⬅️ Главное меню', 'main'))
    edit_or_send(bot, chat_id, text, kb, message_id)


def rental_details(bot, call, rental_id):
    r = database().row('SELECT * FROM rentals WHERE rental_id=?', (rental_id,))
    if not r:
        raise ValueError('Аренда не найдена')
    payments = database().rows('SELECT order_id,state,price,currency FROM payments WHERE rental_id=? ORDER BY created_at', (rental_id,))
    lines = '\n'.join(f'#{esc(p["order_id"])}: {esc(p["state"])} · {p["price"]:g} {esc(p["currency"])}' for p in payments[-10:])
    text = (f'🕹 <b>Аренда #{esc(payment_label(rental_id))}</b>\nЛот: {r["lot_id"]}\n'
        f'Сервер: <code>{esc(r["server_id"])}</code>\nПокупатель: {esc(r["buyer_id"])}\n'
        f'Осталось: {format_left(r["end_time"]-time.time())}\nСтатус: <code>{esc(r["status"])}</code>\n'
        f'Причина завершения: {esc(r["cleanup_reason"] or "—")}\nОшибка: {esc(r["last_error"] or "нет")}\n\n'
        f'Платежи (последние 10 из {len(payments)}):\n{lines}\n\nВозврат возвращает все платежи этой аренды. Отменить — без возврата.')
    kb = Keyboard(row_width=2)
    with GUARD:
        busy = 'rental:' + rental_id in RUNNING
    if busy or r['status'] in ('issuing','cleaning','refunding'):
        kb.add(Button('⏳ Выполняется операция', callback_data=f'{PREFIX}:busy'))
    else:
        if r['status'] == 'active' and r['end_time'] > time.time():
            kb.add(Button('➕ Продлить', callback_data=f'{PREFIX}:input:extend:{rental_id}'))
        if r['status'] in ('active','delivering'):
            kb.add(Button('✅ Завершить', callback_data=f'{PREFIX}:stop:{rental_id}'), Button('❌ Отменить', callback_data=f'{PREFIX}:cancelrent:{rental_id}'))
        if r['status'] == 'cleanup_failed':
            kb.add(Button('🔁 Повторить очистку', callback_data=f'{PREFIX}:retry:{rental_id}'))
        if any(p['state'] not in ('refunded','partial_refund') for p in payments):
            kb.add(Button('💸 Возврат всех платежей', callback_data=f'{PREFIX}:refund:{rental_id}'))
    kb.add(Button('🔄 Обновить', callback_data=f'{PREFIX}:rental:{rental_id}'), Button('◀️ Аренды', callback_data=f'{PREFIX}:rentals'))
    edit_or_send(bot, call.message.chat.id, text, kb, call.message.message_id)


def stats_menu(bot, chat_id, message_id):
    rows = database().rows('SELECT state,currency,COUNT(*) n,SUM(price) total FROM payments GROUP BY state,currency ORDER BY currency,state')
    text = '📊 <b>Платежи по валютам и состояниям</b>\n\n'
    for r in rows:
        text += f'{esc(r["currency"])} · {esc(r["state"])}: {r["n"]} шт. / {r["total"]:.2f}\n'
    text += '\nissued/extended — доставлено; delivery_pending/extension_pending — ожидает сообщения; refund_pending — возврат не подтверждён; refunded — возвращено. Частичные возвраты требуют сверки суммы.'
    edit_or_send(bot, chat_id, text, Keyboard().add(Button('◀️ В меню', callback_data=f'{PREFIX}:main')), message_id)


def bonuses_menu(bot, chat_id, message_id, page=0):
    total = database().row('SELECT COUNT(*) n FROM wholesale_bonuses')['n']
    page, pages = classic_page(total, page)
    rules = database().rows('SELECT * FROM wholesale_bonuses ORDER BY buy_hours,id LIMIT 8 OFFSET ?', (page*8,))
    text = f'🎁 <b>Оптовые бонусы</b>\n\nСтраница {page+1}/{pages}\nЗа каждый полный набор часов из выбранного правила добавляется подарок. Применяется одно правило с наибольшим достигнутым порогом.\n'
    kb = Keyboard(row_width=2)
    for rule in rules:
        text += f'\nКупи {rule["buy_hours"]:g} ч. → +{rule["get_hours"]:g} ч.'
        kb.row(panel_button(f'✏️ {rule["buy_hours"]:g} → +{rule["get_hours"]:g}', f'c_wizard:edit_bonus:{rule["id"]}'),
               panel_button('🗑 Удалить', f'c_confirm:bonus:{rule["id"]}'))
    if not rules:
        text += '\nПравил пока нет.'
    classic_nav(kb, page, pages, 'c_bonuses')
    kb.row(panel_button('➕ Добавить правило', 'c_wizard:bonus'))
    kb.row(panel_button('⭐ Бонус за отзыв', 'reviews'), panel_button('⬅️ Главное меню','main'))
    edit_or_send(bot, chat_id, text, kb, message_id)


def settings_menu(bot, chat_id, message_id):
    text = ('⚙️ <b>Настройки магазина</b>\n\n'
            '🟢 Включено  ·  ⚪ Выключено\nНажмите переключатель, чтобы изменить настройку.\n\n'
            '<b>Автолоты</b> — скрывать лоты без свободных серверов.\n'
            '<b>Автовозврат</b> — возвращать платёж при невыдаче.\n'
            '<b>Команды покупателя</b> — !команды или !command. Русские VIP-команды работают без отдельного переключателя.')
    kb = Keyboard(row_width=2)
    kb.row(switch_button('lot_management','Автолоты','settings'), switch_button('auto_refund','Автовозврат','settings'))
    kb.row(panel_button('🤝 Друзья','friends'), panel_button('⭐ Отзывы','reviews'))
    kb.row(panel_button('💬 Сообщения','templates'), panel_button('🌐 Прокси','proxy'))
    kb.row(panel_button('🔎 Диагностика команд','command_diagnostics'))
    kb.row(panel_button('◀️ Главное меню','main'))
    edit_or_send(bot, chat_id, text, kb, message_id)


def begin_input(tg, bot, call, action, *extra):
    if action not in INPUTS:
        raise ValueError('Неизвестное действие')
    if action == 'extend':
        r = database().row("SELECT 1 FROM rentals WHERE rental_id=? AND status='active' AND end_time>?", (extra[0] if extra else '', time.time()))
        with GUARD:
            busy = 'rental:' + (extra[0] if extra else '') in RUNNING
        if not r or busy:
            raise ValueError('Аренда уже завершена или занята операцией')
    prompt = INPUTS[action]
    if extra and action != 'extend':
        prompt += '\nВыбранный ID: ' + extra[0]
    message = bot.send_message(call.message.chat.id, esc(prompt), parse_mode='HTML', reply_markup=Keyboard().add(Button('❌ Отмена ввода', callback_data=f'{PREFIX}:main')))
    tg.set_state(call.message.chat.id, message.message_id, call.from_user.id, STATE_PREFIX + action, {'extra': list(extra), 'at': time.time()})


def current_state(tg, message):
    state = tg.get_state(message.chat.id, message.from_user.id) or {}
    if state.get('state') == STATE_PREFIX + 'classic':
        return 'classic', state.get('data') or {}
    for name in INPUTS:
        if tg.check_state(message.chat.id, message.from_user.id, STATE_PREFIX + name):
            return name, state.get('data') or {}
    return None


def handle_input(tg, bot, message):
    if CARDINAL is None or not is_cardinal_admin(CARDINAL, message.from_user.id) or not is_plugin_enabled(CARDINAL):
        return
    found = current_state(tg, message)
    if not found or not message.text:
        return
    if found[0] == 'classic':
        classic_input(tg, bot, message, found[1])
        return
    action, data = found
    if message.text.startswith('/'):
        tg.clear_state(message.chat.id, message.from_user.id, True)
        return
    if data.get('at', time.time()) + 1800 < time.time():
        tg.clear_state(message.chat.id, message.from_user.id, True)
        bot.reply_to(message, 'Время ожидания ввода истекло. Откройте действие снова.')
        return
    try:
        reply = update_configuration(action, message.text.strip(), data.get('extra') or [])
        tg.clear_state(message.chat.id, message.from_user.id, True)
        bot.reply_to(message, reply, parse_mode='HTML')
        if action in ('add_account','update_account','proxy'):
            try:
                bot.delete_message(message.chat.id, message.message_id)
            except Exception:
                pass
    except (ValueError, IndexError, TypeError, sqlite3.IntegrityError) as exc:
        # Never echo credentials or SQLite input values in an error.
        detail = str(exc) if action not in ('add_account','update_account','proxy') and isinstance(exc, ValueError) else 'Проверьте формат, данные и подключение.'
        bot.reply_to(message, '❌ ' + esc(detail), parse_mode='HTML')
    except Exception:
        LOG.error('AutoVIP: не удалось выполнить настройку %s', action)
        bot.reply_to(message, '❌ Не удалось выполнить действие. Настройки не подтверждены; повторите после проверки подключения.')


def init_telegram(cardinal):
    if not cardinal.telegram:
        return
    tg, bot = cardinal.telegram, cardinal.telegram.bot
    def clear_own(chat, user):
        state = tg.get_state(chat, user) or {}
        if str(state.get('state', '')).startswith(STATE_PREFIX):
            tg.clear_state(chat, user, True)
    def open_panel(message):
        if not is_cardinal_admin(cardinal, message.from_user.id):
            return
        clear_own(message.chat.id, message.from_user.id)
        main_menu(bot, message.chat.id)
    def callback(call):
        try:
            if not is_cardinal_admin(cardinal, call.from_user.id):
                bot.answer_callback_query(call.id, 'Нет доступа', show_alert=True)
                return
            if not is_plugin_enabled(cardinal):
                bot.answer_callback_query(call.id, 'Плагин выключен. Включите его в Cardinal.', show_alert=True)
                return
            bot.answer_callback_query(call.id)
            chat, mid = call.message.chat.id, call.message.message_id
            parts = call.data.split(':')
            action = parts[1]
            if action.startswith('c_'):
                with operation_lock(f'admin-input:{chat}:{call.from_user.id}'):
                    classic_callback(tg, bot, call, parts[1:])
                return
            clear_own(chat, call.from_user.id)
            if action == 'main':
                main_menu(bot, chat, mid)
            elif action in ('accounts','servers','cats','lots'):
                simple_list_menu(bot, chat, mid, action)
            elif action == 'listpage':
                if parts[2] not in ('accounts','servers','cats','lots'):
                    raise ValueError('Неизвестный список')
                simple_list_menu(bot, chat, mid, parts[2], int(parts[3]))
            elif action in ('rentals','rentpage'):
                rentals_menu(bot, chat, mid, int(parts[2]) if len(parts)>2 else 0)
            elif action == 'rental':
                rental_details(bot, call, parts[2])
            elif action == 'lot':
                lot_details(bot, chat, mid, int(parts[2]))
            elif action == 'pause':
                with ORDER_LOCK, database().lock, database().conn() as con:
                    if con.execute('UPDATE lots SET paused=1-paused WHERE lot_id=?', (int(parts[2]),)).rowcount != 1:
                        raise ValueError('Лот не найден')
                lot_details(bot, chat, mid, int(parts[2]))
            elif action in ('orders','orderpage'):
                orders_menu(bot, chat, mid, int(parts[2]) if len(parts)>2 else 0)
            elif action == 'guidepage':
                guide_menu(bot, chat, mid, parts[2])
            elif action == 'command_diagnostics':
                command_diagnostics_menu(bot, chat, mid)
            elif action in ('stats','bonuses','settings','guide','friends','reviews','templates'):
                {'stats': stats_menu, 'bonuses': bonuses_menu, 'settings': settings_menu, 'guide': guide_menu,
                 'friends':friends_menu, 'reviews':reviews_menu, 'templates':templates_menu}[action](bot, chat, mid)
            elif action == 'input':
                begin_input(tg, bot, call, parts[2], *parts[3:])
            elif action == 'proxy':
                begin_input(tg, bot, call, 'proxy')
            elif action == 'reset_templates':
                for key, value in DEFAULTS.items():
                    if key.startswith('msg_'):
                        database().set_setting(key, value)
                templates_menu(bot, chat, mid)
            elif action == 'toggle':
                key = parts[2]
                if key == 'legacy_commands':
                    settings_menu(bot, chat, mid)
                    return
                if key not in ('lot_management','auto_refund','friends_auto_remove','friends_module_active','legacy_commands','review_bonus_active'):
                    raise ValueError('Неизвестная настройка')
                destination = parts[3] if len(parts)>3 else 'settings'
                if destination not in ('settings','friends','reviews'):
                    raise ValueError('Неизвестный раздел настроек')
                database().set_setting(key, '0' if database().setting(key) == '1' else '1')
                {'settings':settings_menu,'friends':friends_menu,'reviews':reviews_menu}[destination](bot, chat, mid)
            elif action == 'mode':
                with GUARD:
                    if 'prices' in RUNNING:
                        raise ValueError('Цены уже обновляются')
                    database().set_setting('event_mode', '0' if database().setting('event_mode') == '1' else '1')
                    run_once('prices', apply_prices, cardinal)
                main_menu(bot, chat, mid)
            elif action in ('stop','cancelrent','retry','refund'):
                rid = parts[2]
                r = database().row('SELECT * FROM rentals WHERE rental_id=?', (rid,))
                if not r or r['status'] in ('issuing','cleaning','refunding'):
                    raise ValueError('Аренда не найдена или занята операцией')
                if action in ('stop','cancelrent') and r['status'] not in ('active','delivering'):
                    raise ValueError('Аренда уже завершена')
                if action == 'retry' and r['status'] != 'cleanup_failed':
                    raise ValueError('Повтор очистки сейчас не нужен')
                if action == 'refund':
                    started = run_once('rental:' + rid, refund_rental, cardinal, rid)
                else:
                    reason = {'stop':'manual','cancelrent':'cancelled','retry':r['cleanup_reason'] or 'expired'}[action]
                    started = run_once('rental:' + rid, cleanup_rental, cardinal, rid, reason)
                if not started:
                    raise ValueError('Операция уже выполняется')
                rental_details(bot, call, rid)
            elif action != 'busy':
                raise ValueError('Неизвестное действие')
        except ValueError as exc:
            bot.send_message(call.message.chat.id, '❌ ' + esc(exc), parse_mode='HTML')
        except Exception:
            LOG.error('AutoVIP: ошибка Telegram-действия')
            bot.send_message(call.message.chat.id, '❌ Не удалось выполнить действие. Обновите карточку.')
    def cancel(message):
        if is_cardinal_admin(cardinal, message.from_user.id):
            clear_own(message.chat.id, message.from_user.id)
    tg.msg_handler(open_panel, commands=['vip','vip_config'])
    tg.msg_handler(cancel, commands=['cancel'], func=lambda m: current_state(tg, m) is not None)
    tg.cbq_handler(callback, lambda c: bool(c.data) and c.data.startswith(PREFIX + ':'))
    tg.msg_handler(lambda m: handle_input(tg, bot, m), func=lambda m: current_state(tg, m) is not None)
    cardinal.add_telegram_commands(UUID, [('vip_config','панель Auto VIP Roblox',True), ('vip','панель Auto VIP Roblox',True)])


def init_plugin(cardinal):
    global DB, CARDINAL, INSTANCE_LOCK
    if INSTANCE_LOCK is not None:
        raise RuntimeError('Auto VIP уже запущен в этом процессе')
    handle = open(DB_PATH + '.lock', 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            if os.path.getsize(DB_PATH + '.lock') == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        handle.close()
        raise RuntimeError('Эта база Auto VIP уже используется другим процессом. Остановите второй экземпляр.') from None
    try:
        DB = PluginDB(DB_PATH)
        CARDINAL = cardinal
        INSTANCE_LOCK = handle
        init_telegram(cardinal)
    except Exception:
        INSTANCE_LOCK = None
        handle.close()
        raise
    LOG.info('Auto VIP Roblox v%s initialized', VERSION)


def start_worker(cardinal: Any) -> None:
    global WORKER
    if WORKER and WORKER.is_alive():
        return
    STOP.clear()
    WORKER = threading.Thread(target=worker_loop, args=(cardinal,), daemon=True, name="AutoVIP-cleanup")
    WORKER.start()


def on_new_order(cardinal, event):
    if not is_plugin_enabled(cardinal):
        return
    compact = event.order
    order_id = str(getattr(compact, 'id', '')).lstrip('#')
    if not re.fullmatch(r'[A-Za-z0-9-]{1,64}', order_id):
        return
    amount = getattr(compact, 'amount', None)
    # Preserve invalid values for strict validation; never silently truncate or
    # convert NaN to SQLite NULL and accidentally lose the original evidence.
    amount = str(amount) if amount is not None else None
    price = getattr(compact, 'price', None)
    price = str(price) if price is not None else None
    event_lot_id, event_error = None, None
    try:
        ids = native_lot_ids(compact)
        if len(ids) > 1:
            raise ValueError('Противоречивые ID предложения в событии FunPay')
        event_lot_id = next(iter(ids), None)
    except ValueError as exc:
        event_error = str(exc)
    with database().lock, database().conn() as con:
        con.execute('INSERT OR IGNORE INTO incoming_orders(order_id,amount,price,created_at) VALUES(?,?,?,?)', (order_id, amount, price, time.time()))
        saved = con.execute('SELECT event_lot_id FROM incoming_orders WHERE order_id=?', (order_id,)).fetchone()
        if saved['event_lot_id'] and event_lot_id and int(saved['event_lot_id']) != event_lot_id:
            event_error = 'Повторное событие FunPay содержит другой ID предложения'
        con.execute('''UPDATE incoming_orders SET event_lot_id=COALESCE(event_lot_id,?),
            event_error=COALESCE(event_error,?),amount=COALESCE(amount,?),price=COALESCE(price,?)
            WHERE order_id=?''', (event_lot_id, event_error, amount, price, order_id))
    run_once('order:' + order_id, process_order, cardinal, event)


def on_new_message(cardinal, event):
    if is_plugin_enabled(cardinal):
        message = event.message
        if not (parse_chat_command(getattr(message, 'text', '')) is not None or
                (str(getattr(message, 'author_id', '')) == '0' and MessageTypes is not None and
                 getattr(message, 'type', None) in (getattr(MessageTypes, 'NEW_FEEDBACK', None), getattr(MessageTypes, 'FEEDBACK_CHANGED', None)))):
            return
        trace_command('receive', 'Получено полное сообщение NEW_MESSAGE')
        started = run_once('message:' + str(getattr(message, 'chat_id', '')) + ':' + str(getattr(message, 'id', uuid.uuid4())), process_message_safe, cardinal, event)
        if not started:
            trace_command('process', 'Задание уже выполняется или очередь операций занята')


def on_legacy_message(cardinal, event):
    if not is_plugin_enabled(cardinal):
        return
    obj = event.chat
    if parse_chat_command(getattr(obj, 'last_message_text', '')) is None or getattr(obj, 'last_by_bot', False):
        return
    trace_command('receive', 'Получено превью LAST_CHAT_MESSAGE_CHANGED')
    started = run_once('legacy:' + str(getattr(obj, 'id', '')) + ':' + str(getattr(obj, 'node_msg_id', '')),
                       process_legacy_message, cardinal, event)
    if not started:
        trace_command('process', 'Чтение истории уже выполняется или очередь операций занята')


def on_order_status_changed(cardinal, event):
    order = getattr(event, 'order', None)
    order_id = str(getattr(order, 'id', '')).lstrip('#')
    if not database().row('SELECT 1 FROM payments WHERE order_id=?', (order_id,)):
        return
    if status_name(order) == 'refunded':
        run_once('refund-event:' + order_id, reconcile_refund_event, cardinal, order_id)
    elif 'partial' in status_name(order):
        with database().lock, database().conn() as con:
            con.execute("UPDATE payments SET state='partial_refund',last_error='Нужна сверка суммы частичного возврата' WHERE order_id=? AND state!='refunded'", (order_id,))
        notify_admins(cardinal, f'⚠️ Частичный возврат #{esc(order_id)}: проверьте сумму и оставшееся время. Полная аренда автоматически не отменяется.')


def stop_plugin(cardinal, *args):
    global INSTANCE_LOCK
    if DB is not None and (database().row("SELECT 1 FROM rentals WHERE status IN ('issuing','delivering','active','refunding','cleaning','cleanup_failed')") or database().row("SELECT 1 FROM payments WHERE state='refund_pending'")):
        raise RuntimeError('Сначала завершите аренды, очистку и возвраты Auto VIP, затем удалите плагин.')
    with GUARD:
        if RUNNING:
            raise RuntimeError('Дождитесь завершения операций Auto VIP перед удалением.')
    STOP.set()
    if WORKER and WORKER.is_alive():
        WORKER.join(timeout=5)
    if INSTANCE_LOCK is not None:
        INSTANCE_LOCK.close()
        INSTANCE_LOCK = None


BIND_TO_PRE_INIT = [init_plugin]
BIND_TO_POST_START = [start_worker]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_LAST_CHAT_MESSAGE_CHANGED = [on_legacy_message]
BIND_TO_ORDER_STATUS_CHANGED = [on_order_status_changed]
BIND_TO_DELETE = stop_plugin

GUARD = threading.RLock()

LOCKS = {}

ORDER_LOCK = threading.RLock()

FP_LOT_LOCK = threading.Lock()

RUNNING = set()

HANDLED_MESSAGES = {}

COMMAND_TRACE = {}

BUSY_STATUSES = ('issuing', 'delivering', 'active', 'refunding', 'cleaning', 'cleanup_failed')

INSTANCE_LOCK = None

from concurrent.futures import ThreadPoolExecutor

from contextlib import ExitStack

EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix='AutoVIP')


def operation_lock(key):
    with GUARD:
        return LOCKS.setdefault(str(key), threading.RLock())


def run_once(key, function, *args):
    with GUARD:
        if key in RUNNING or len(RUNNING) >= 128:
            return False
        RUNNING.add(key)
    def run():
        try:
            function(*args)
        except Exception:
            LOG.error('AutoVIP: операция %s завершилась ошибкой', key)
            if key.startswith('order:') and DB is not None:
                with database().lock, database().conn() as con:
                    con.execute("UPDATE incoming_orders SET state='attention',error='Ошибка обработки; требуется повтор из панели' WHERE order_id=? AND state='pending'", (key[6:],))
        finally:
            with GUARD:
                RUNNING.discard(key)
    try:
        EXECUTOR.submit(run)
    except RuntimeError:
        with GUARD:
            RUNNING.discard(key)
        return False
    return True


def source_key(lot):
    kind = lot['bind_type']
    if kind == 'category' and lot['category_id'] and not lot['server_id']:
        return 'category:' + str(lot['category_id'])
    if kind == 'server' and lot['server_id'] and not lot['category_id']:
        return 'server:' + str(lot['server_id'])
    if kind == 'random' and not lot['server_id'] and not lot['category_id']:
        return 'random'
    return None


def enqueue(con, key, chat_id, text, rental_id=None, order_id=None, kind='notice'):
    con.execute('''INSERT OR IGNORE INTO outbox(dedupe,chat_id,text,rental_id,order_id,kind,created_at)
        VALUES(?,?,?,?,?,?,?)''', (key, str(chat_id), text, rental_id, order_id, kind, time.time()))


def notice(key, chat_id, text, rental_id=None):
    with database().lock, database().conn() as con:
        enqueue(con, key, chat_id, text, rental_id)


def status_name(order):
    status = getattr(order, 'status', '')
    return str(getattr(status, 'name', status)).split('.')[-1].lower()


def native_lot_ids(obj):
    # Never scan free text/HTML, buyer fields, links or node_id (subcategory).
    result = set()
    for name in ('lot_id', 'offer_id'):
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None:
            if not re.fullmatch(r'[1-9][0-9]*', str(value)):
                raise ValueError('Неверный ID предложения в заказе')
            result.add(int(value))
    return result


def add_credit(con, rental_id, order_id, hours, start):
    end = start + hours * 3600
    con.execute('INSERT INTO time_credits(rental_id,order_id,hours,start_time,end_time) VALUES(?,?,?,?,?)',
                (rental_id, order_id, hours, start, end))
    return end


def deliver_outbox(cardinal, outbox_id):
    db = database()
    item = db.row('SELECT * FROM outbox WHERE id=?', (outbox_id,))
    if not item:
        return
    lock = operation_lock('rental:' + item['rental_id']) if item['rental_id'] else operation_lock('outbox:' + str(outbox_id))
    with lock:
        with db.lock, db.conn() as con:
            item = con.execute("SELECT * FROM outbox WHERE id=? AND state='pending'", (outbox_id,)).fetchone()
            if not item:
                return
            rental = con.execute('SELECT * FROM rentals WHERE rental_id=?', (item['rental_id'],)).fetchone()
            if item['kind'] in ('issue', 'extension'):
                expected = 'delivery_pending' if item['kind'] == 'issue' else 'extension_pending'
                payment = con.execute('SELECT state FROM payments WHERE order_id=?', (item['order_id'],)).fetchone()
                if not payment or payment['state'] != expected:
                    con.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (outbox_id,))
                    return
            if item['kind'] in ('issue', 'extension', 'link') and (not rental or rental['status'] not in ('active', 'delivering') or (rental['status'] == 'active' and rental['end_time'] <= time.time())):
                con.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (outbox_id,))
                return
            con.execute("UPDATE outbox SET state='sending',attempts=attempts+1 WHERE id=?", (outbox_id,))
        ok = send_fp(cardinal, item['chat_id'], item['text'])
        with db.lock, db.conn() as con:
            if not ok:
                con.execute("UPDATE outbox SET state='pending',next_at=?,last_error='Нет подтверждения отправки' WHERE id=?", (time.time() + min(300, 10 * (item['attempts'] + 1)), outbox_id))
            else:
                con.execute("UPDATE outbox SET state='sent',last_error=NULL WHERE id=?", (outbox_id,))
                if item['kind'] == 'issue':
                    now = time.time()
                    if rental['status'] == 'delivering':
                        con.execute("UPDATE rentals SET status='active',start_time=?,end_time=?+hours*3600 WHERE rental_id=?", (now, now, rental['rental_id']))
                        con.execute('UPDATE time_credits SET start_time=start_time+?,end_time=end_time+? WHERE rental_id=? AND revoked=0', (now, now, rental['rental_id']))
                        con.execute("UPDATE servers SET status='occupied' WHERE server_id=?", (rental['server_id'],))
                    con.execute("UPDATE payments SET state='issued' WHERE order_id=? AND state='delivery_pending'", (item['order_id'],))
                elif item['kind'] == 'extension':
                    con.execute("UPDATE payments SET state='extended' WHERE order_id=? AND state='extension_pending'", (item['order_id'],))
        if not ok and item['attempts'] in (0, 9):
            notify_admins(cardinal, f'⚠️ Auto VIP: сообщение #{esc(item["order_id"] or "")} не доставлено. Сохранён повтор отправки.')


def cleanup_managed_friends(api):
    db = database()
    if db.setting('friends_auto_remove') != '1':
        return
    with operation_lock('owner:' + str(api.account.id)):
        for friend in db.rows('SELECT roblox_id FROM managed_friends WHERE account_id=?', (api.account.id,)):
            used = db.row("""SELECT 1 FROM rental_friends f JOIN rentals r USING(rental_id)
                WHERE r.owner_account_id=? AND f.roblox_id=? AND f.state!='removed'
                AND r.status IN ('issuing','delivering','active','cleaning','cleanup_failed','refunding')""", (api.account.id, friend['roblox_id']))
            if not used:
                if not api.unfriend(friend['roblox_id']):
                    raise RuntimeError('Roblox не подтвердил удаление дружбы')
                with db.lock, db.conn() as con:
                    con.execute('DELETE FROM managed_friends WHERE account_id=? AND roblox_id=?', (api.account.id, friend['roblox_id']))


def request_refund(order_id):
    db = database()
    payment = db.row('SELECT rental_id FROM payments WHERE order_id=?', (order_id,))
    if not payment:
        return
    with operation_lock('rental:' + str(payment['rental_id'])):
        with db.lock, db.conn() as con:
            con.execute("""UPDATE payments SET refund_previous_state=state,state='refund_pending',next_refund_at=0
                WHERE order_id=? AND state NOT IN ('refunded','refund_pending','partial_refund')""", (order_id,))


def mark_refunded(order_id):
    db = database()
    payment = db.row('SELECT * FROM payments WHERE order_id=?', (order_id,))
    if not payment:
        return
    with operation_lock('rental:' + str(payment['rental_id'])):
        with db.lock, db.conn() as con:
            con.execute('BEGIN IMMEDIATE')
            payment = con.execute('SELECT * FROM payments WHERE order_id=?', (order_id,)).fetchone()
            if payment['state'] == 'refunded':
                return
            con.execute("UPDATE payments SET state='refunded',last_error=NULL WHERE order_id=?", (order_id,))
            con.execute("UPDATE outbox SET state='cancelled' WHERE order_id=? AND state='pending' AND kind IN ('issue','extension')", (order_id,))
            rental = con.execute('SELECT * FROM rentals WHERE rental_id=?', (payment['rental_id'],)).fetchone()
            if rental:
                now = time.time() if rental['start_time'] else 0
                shift = 0.0
                for credit in con.execute('SELECT * FROM time_credits WHERE rental_id=? AND revoked=0 ORDER BY start_time,id', (rental['rental_id'],)).fetchall():
                    start, end = float(credit['start_time']), float(credit['end_time'])
                    if credit['order_id'] == order_id:
                        shift += max(0, end - max(now, start))
                        con.execute('UPDATE time_credits SET revoked=1 WHERE id=?', (credit['id'],))
                    elif shift:
                        con.execute('UPDATE time_credits SET start_time=?,end_time=? WHERE id=?', (max(now, start-shift), max(now, end-shift), credit['id']))
                new_end = max(now, float(rental['end_time']) - shift) if rental['start_time'] else 0
                con.execute('UPDATE rentals SET end_time=?,hours=MAX(0,hours-?),price=MAX(0,price-?) WHERE rental_id=?', (new_end, shift/3600, payment['price'], rental['rental_id']))
                remaining = con.execute("SELECT 1 FROM payments WHERE rental_id=? AND state!='refunded'", (rental['rental_id'],)).fetchone()
                if rental['status'] in ('active', 'delivering') and (not remaining or (rental['start_time'] and new_end <= now)):
                    con.execute("UPDATE rentals SET status='cleanup_failed',cleanup_reason='refund',next_cleanup_at=0 WHERE rental_id=?", (rental['rental_id'],))
                elif not remaining and rental['status'] not in BUSY_STATUSES:
                    con.execute("UPDATE rentals SET status='refunded' WHERE rental_id=?", (rental['rental_id'],))
            enqueue(con, 'refunded:' + order_id, payment['chat_id'], f'💸 Возврат заказа #{order_id} подтверждён FunPay. Неиспользованное время этого заказа снято.', payment['rental_id'])


def perform_refund(cardinal, order_id):
    payment = database().row('SELECT rental_id FROM payments WHERE order_id=?', (order_id,))
    if not payment:
        return
    # Keep delivery/renewal from racing the financial operation and reconciliation.
    with operation_lock('rental:' + str(payment['rental_id'])):
        return perform_refund_locked(cardinal, order_id)


def perform_refund_locked(cardinal, order_id):
    db = database()
    with operation_lock('refund:' + order_id):
        payment = db.row("SELECT * FROM payments WHERE order_id=? AND state='refund_pending'", (order_id,))
        if not payment:
            return
        try:
            order = cardinal.account.get_order(order_id)
            if str(getattr(order, 'seller_id', '')) != str(cardinal.account.id):
                raise RuntimeError('Другой продавец')
            if status_name(order) == 'refunded':
                mark_refunded(order_id)
                return
            if status_name(order) not in ('paid', 'closed'):
                raise RuntimeError('Возврат требует ручной сверки статуса')
            cardinal.account.refund(order_id)
            if status_name(cardinal.account.get_order(order_id)) != 'refunded':
                raise RuntimeError('FunPay ещё не подтвердил возврат')
            mark_refunded(order_id)
        except Exception:
            with db.lock, db.conn() as con:
                con.execute("UPDATE payments SET last_error='Возврат не подтверждён FunPay',next_refund_at=? WHERE order_id=? AND state='refund_pending'", (time.time() + 120, order_id))
            if not payment['last_error']:
                notify_admins(cardinal, f'⚠️ Auto VIP: возврат #{esc(order_id)} ожидает подтверждения FunPay. Будет повторная сверка.')


def refund_rental(cardinal, rental_id):
    with operation_lock('rental:' + rental_id):
        with database().lock, database().conn() as con:
            rental = con.execute('SELECT * FROM rentals WHERE rental_id=?', (rental_id,)).fetchone()
            if not rental or rental['status'] in ('issuing', 'cleaning', 'refunding'):
                return False
            payments = con.execute("SELECT order_id FROM payments WHERE rental_id=? AND state NOT IN ('refunded','partial_refund')", (rental_id,)).fetchall()
            if not payments:
                return False
            con.execute("UPDATE payments SET refund_previous_state=state,state='refund_pending',next_refund_at=0 WHERE rental_id=? AND state NOT IN ('refunded','refund_pending','partial_refund')", (rental_id,))
            if rental['status'] in BUSY_STATUSES:
                con.execute("UPDATE rentals SET status='refunding',cleanup_reason='refund' WHERE rental_id=?", (rental_id,))
        cleanup_rental(cardinal, rental_id, 'refund')
    for payment in payments:
        perform_refund(cardinal, payment['order_id'])
    return True


def extend_rental(rental_id, hours):
    hours = positive_float(hours, 'Продление')
    with GUARD:
        if 'rental:' + rental_id in RUNNING:
            raise ValueError('С этой арендой уже выполняется операция')
    with operation_lock('rental:' + rental_id):
        with database().lock, database().conn() as con:
            con.execute('BEGIN IMMEDIATE')
            rental = con.execute("SELECT * FROM rentals WHERE rental_id=? AND status='active' AND end_time>?", (rental_id, time.time())).fetchone()
            if not rental:
                raise ValueError('Аренда уже завершена или выполняется другая операция')
            end = add_credit(con, rental_id, None, hours, float(rental['end_time']))
            con.execute('UPDATE rentals SET end_time=?,hours=hours+? WHERE rental_id=?', (end, hours, rental_id))
            enqueue(con, 'manual:' + str(uuid.uuid4()), rental['chat_id'], f'✅ Продавец продлил аренду #{payment_label(rental_id)} на {hours:g} ч.', rental_id)


def command_text(raw):
    text = str(raw or '').strip().lstrip('\ufeff\u200b\u200e\u200f').strip()
    return re.sub(r'^!\s+', '!', text)


def trace_command(stage, result):
    # Only fixed labels / exception classes. Never save message text or secrets.
    with GUARD:
        COMMAND_TRACE[stage] = (time.time(), result)


def parse_chat_command(raw):
    parts = command_text(raw).split()
    if not parts:
        return None
    token = parts.pop(0).lower()
    if token == 'получить' and parts and parts[0].lower() == 'вип':
        token = 'получить вип'
        parts.pop(0)
    aliases = {
        '!vip_time':'time', '!время':'time', '!time':'time', 'время':'time',
        '!vip_link':'link', '!ссылка':'link', '!link':'link', 'ссылка':'link',
        '!vip':'link', '!вип':'link', 'vip':'link', 'вип':'link', 'получить вип':'link',
        '!vip_friends':'friends', '!друзья':'friends', '!friend':'friends', '!friends':'friends', 'друзья':'friends',
        '!vip_help':'help', '!команды':'help', '!command':'help', '!commands':'help',
        '/com':'help', '!com':'help', 'com':'help', 'команды_vip':'help',
        '!vip_stock':'stock', '!бронь':'stock', '!vip_review':'review', '!отзыв':'review', 'отзыв':'review',
    }
    if token == '!help' and database().setting('legacy_commands') == '1':
        aliases[token] = 'help'
    action = aliases.get(token)
    if action is None:
        return None
    # Bare aliases match an entire command, not ordinary sentences containing it.
    if not token.startswith(('!', '/')) and action != 'friends' and any(not p.startswith('#') for p in parts):
        return None
    return action, parts


def deliver_command_replies(cardinal, rental_id, kind=None, dedupe=None):
    # First attempt immediately; the durable queue remains responsible for retries.
    query = "SELECT id FROM outbox WHERE rental_id=? AND state='pending'"
    values = [rental_id]
    if kind:
        query += ' AND kind=?'
        values.append(kind)
    if dedupe:
        query += ' AND dedupe=?'
        values.append(dedupe)
    for row in database().rows(query + ' ORDER BY id', tuple(values)):
        deliver_outbox(cardinal, row['id'])


def grant_review(cardinal, order):
    db = database()
    if db.setting('review_bonus_active') != '1' or status_name(order) not in ('paid', 'closed') or getattr(getattr(order, 'review', None), 'stars', 0) != 5:
        return False
    p = db.row('SELECT * FROM payments WHERE order_id=?', (str(order.id),))
    if not p or not p['rental_id']:
        return False
    with GUARD:
        if 'rental:' + p['rental_id'] in RUNNING:
            return False
    with operation_lock('rental:' + p['rental_id']):
        with db.lock, db.conn() as con:
            con.execute('BEGIN IMMEDIATE')
            p = con.execute("SELECT * FROM payments WHERE order_id=? AND state IN ('issued','extended','extension_pending') AND review_bonus_given=0", (str(order.id),)).fetchone()
            rental = con.execute("SELECT * FROM rentals WHERE rental_id=? AND status='active' AND end_time>?", (p['rental_id'], time.time())).fetchone() if p else None
            if not rental:
                return False
            bonus = positive_float(db.setting('review_bonus_hours'), 'Бонус')
            end = add_credit(con, rental['rental_id'], str(order.id), bonus, float(rental['end_time']))
            con.execute('UPDATE payments SET review_bonus_given=1 WHERE order_id=?', (str(order.id),))
            con.execute('UPDATE rentals SET hours=hours+?,end_time=? WHERE rental_id=?', (bonus, end, rental['rental_id']))
            enqueue(con, 'review:' + str(order.id), rental['chat_id'], db.setting('msg_review_bonus').replace('{bonus_hours}', f'{bonus:g}'), rental['rental_id'])
    return True


def process_message_safe(cardinal, event):
    message = event.message
    chat, mid = str(getattr(message, 'chat_id', '') or ''), str(getattr(message, 'id', '') or '')
    key = chat + ':' + mid
    # Both Cardinal hooks may describe the same message. Serialize and suppress
    # completed duplicate events so a single !link never rotates the code twice.
    with operation_lock('message-dispatch:' + key):
        if mid:
            now = time.monotonic()
            with GUARD:
                for old in [k for k, at in HANDLED_MESSAGES.items() if now-at > 600]:
                    HANDLED_MESSAGES.pop(old, None)
                if key in HANDLED_MESSAGES:
                    trace_command('process', 'Повтор уже обработанного сообщения пропущен')
                    return
        try:
            process_review(cardinal, event)
        except Exception:
            LOG.warning('AutoVIP: не удалось проверить отзыв')
        try:
            process_chat_message(cardinal, event)
        except Exception as exc:
            # Report only exception type; response bodies may contain credentials.
            LOG.error('AutoVIP: ошибка команды в чате %s (%s)', chat, type(exc).__name__)
            trace_command('process', 'Ошибка обработки: ' + type(exc).__name__)
            author = str(getattr(message, 'author_id', '') or '')
            if parse_chat_command(getattr(message, 'text', '')) is not None and author.isdigit() and author not in ('0', str(cardinal.account.id)) and not getattr(message, 'by_bot', False):
                send_fp(cardinal, chat, '❌ Не удалось выполнить VIP-команду. Повторите её позже; продавцу доступна ошибка в журнале Cardinal.')
        if mid:
            with GUARD:
                HANDLED_MESSAGES[key] = time.monotonic()
                while len(HANDLED_MESSAGES) > 4096:
                    HANDLED_MESSAGES.pop(next(iter(HANDLED_MESSAGES)))


def reconcile_refund_event(cardinal, order_id):
    if status_name(cardinal.account.get_order(order_id)) == 'refunded':
        mark_refunded(order_id)


def process_legacy_message(cardinal, event):
    obj = event.chat
    chat = getattr(obj, 'id', None)
    expected_id = getattr(obj, 'node_msg_id', None)
    try:
        # ChatShortcut has no author_id. Fetch the real message, never infer the
        # sender from chat participants or act on truncated preview text.
        history = cardinal.account.get_chat_history(chat)
        if expected_id:
            message = next((m for m in history if str(getattr(m, 'id', '')) == str(expected_id)), None)
        else:
            message = max(history, key=lambda m: as_int(getattr(m, 'id', 0)), default=None)
            if message and command_text(getattr(message, 'text', '')) != command_text(getattr(obj, 'last_message_text', '')):
                message = None
        if message is None or str(getattr(message, 'chat_id', '')) != str(chat):
            trace_command('process', 'История не подтвердила сообщение из превью')
            LOG.warning('AutoVIP: не удалось подтвердить сообщение старого режима в чате %s', chat)
            return
        process_message_safe(cardinal, SimpleNamespace(message=message))
    except Exception as exc:
        trace_command('process', 'Ошибка чтения истории: ' + type(exc).__name__)
        LOG.warning('AutoVIP: чтение команды старого режима в чате %s (%s)', chat, type(exc).__name__)


def recover_idle_operations():
    """Recover abandoned persisted claims without disturbing a running/queued job.
    This covers e.g. a transient database error inside the normal error handler.
    The instance file lock guarantees another v16 process cannot own the claim.
    """
    db = database()
    for rental in db.rows("SELECT * FROM rentals WHERE status IN ('issuing','cleaning','refunding') LIMIT 50"):
        rid = rental['rental_id']
        payments = db.rows('SELECT order_id FROM payments WHERE rental_id=?', (rid,))
        with GUARD:
            owned = ('rental:' + rid in RUNNING or any('order:' + p['order_id'] in RUNNING for p in payments))
        if owned:
            continue
        lock = operation_lock('rental:' + rid)
        if not lock.acquire(blocking=False):
            continue
        try:
            with db.lock, db.conn() as con:
                changed = con.execute("""UPDATE rentals SET status='cleanup_failed',
                    cleanup_reason=COALESCE(cleanup_reason,'issue_failed'),next_cleanup_at=0,
                    last_error='Возобновление прерванной операции'
                    WHERE rental_id=? AND status=?""", (rid, rental['status']))
                if changed.rowcount:
                    con.execute("UPDATE payments SET state='issue_failed' WHERE rental_id=? AND state='issuing'", (rid,))
                    con.execute("UPDATE servers SET status='cleaning' WHERE server_id=?", (rental['server_id'],))
        finally:
            lock.release()
    for item in db.rows("SELECT id,rental_id FROM outbox WHERE state='sending' LIMIT 50"):
        with GUARD:
            if 'outbox:' + str(item['id']) in RUNNING:
                continue
        lock = operation_lock('rental:' + item['rental_id']) if item['rental_id'] else operation_lock('outbox:' + str(item['id']))
        if not lock.acquire(blocking=False):
            continue
        try:
            with db.lock, db.conn() as con:
                con.execute("UPDATE outbox SET state='pending',next_at=0 WHERE id=? AND state='sending'", (item['id'],))
        finally:
            lock.release()


"""Guide pages embedded into the standalone plugin by rebuild.py."""

GUIDE_SECTIONS = (
    ('start', '🚀 Начало и установка', '''<b>Что подготовить</b>
Нужны работающий FunPay Cardinal, доступ администратора к его Telegram-боту, аккаунт Roblox — владелец VIP-сервера, сам сервер и предложение на FunPay.

<b>Установка</b>
1. Остановите Cardinal и сохраните копию старого плагина и его базы.
2. Поместите <code>AutoVipRoblox_LuckyFunpay.py</code> в папку <code>plugins</code>. Старую версию уберите из этой папки.
3. Существующую <code>AutoVIPRoblox_Monolith.db</code> оставьте рядом с файлом. Не запускайте две версии одновременно.
4. Запустите Cardinal, включите плагин и откройте <code>/vip_config</code> или <code>/vip</code>. Если Cardinal был запущен с выключенным плагином, после включения перезапустите Cardinal: его стартовые обработчики не выполняются повторно при нажатии переключателя.

<b>Порядок настройки</b>
Прокси при необходимости → аккаунты → серверы → категории → лоты → друзья и сообщения → проверочная аренда.

Используйте Python 3.10+ и зависимости окружения Cardinal: requests, pyTelegramBotAPI; для SOCKS нужен PySocks. FunPayAPI берётся из Cardinal.

<b>Как вводить данные</b>
Откройте нужную карточку и нажмите действие. Бот спрашивает по одному значению: выбранный аккаунт, сервер или лот уже запомнен, повторять его ID не нужно. В многошаговом вводе есть «Предыдущий шаг» и «Отмена». Дробные часы пишите через точку: <code>1.5</code>. <code>/cancel</code> или переход в меню отменяет ожидание ввода. Незавершённый ввод действует 30 минут.

При обновлении существующие настройки и аренды сохраняются в прежней базе. Меню возвращено к организации первой версии: общие серверы отдельно, серверы игр — внутри категорий.''', 'main'),
    ('ids', '🔢 Где взять ID', '''<b>Не путайте идентификаторы</b>
• <b>VIP Server ID</b> — конкретный приватный сервер Roblox, которым будет управлять плагин.
• <b>Place ID</b> — корневое место игры. Universe ID — другой номер, он сюда не подходит.
• <b>Roblox User ID</b> — номер пользователя Roblox, указанный в карточке аккаунта. Владельца сервера бот определяет сам.
• Внутренние номера аккаунтов и категорий вводить не требуется: выбирайте объект кнопкой.
• <b>LOT_ID</b> — ID купленного предложения FunPay, а не раздела магазина.
• <b>ORDER_ID</b> — номер конкретной покупки, например <code>AB12CD34</code>.

<b>VIP Server ID</b>
Откройте игру Roblox → Servers → свой приватный сервер → три точки → Configure. В адресе вида <code>https://www.roblox.com/games/configure?id=1234567890</code> возьмите цифры после <code>id=</code>.

<b>Place ID</b>
Откройте главную страницу игры. В адресе <code>https://www.roblox.com/games/109983668079237/...</code> это число после <code>/games/</code>. При добавлении сервера плагин сверяет его с корневым Place ID из Roblox.

<b>LOT_ID</b>
Откройте именно своё предложение FunPay и скопируйте его числовой ID из ссылки на предложение. Номер категории и номер заказа не подходят.

Все числа в примерах справки условные: подставляйте свои.''', 'main'),
    ('accounts', '👤 Аккаунты и cookie', '''<b>Получение cookie</b>
1. В браузере войдите на roblox.com под владельцем сервера.
2. Откройте инструменты разработчика: F12 → Application (Приложение) → Storage/Cookies → https://www.roblox.com.
3. Найдите <code>.ROBLOSECURITY</code> и скопируйте его полное значение, включая начальное предупреждение.
4. В личном чате своего бота откройте «Аккаунты» → «Добавить аккаунт» и отправьте только значение cookie.

<b>Что должно произойти</b>
После проверки появится кнопка с ником Roblox. Откройте её: в карточке показаны Roblox ID и число серверов. Добавьте каждого владельца, если серверы куплены на разных аккаунтах. При добавлении серверов владелец определяется автоматически.

<b>Обновление cookie</b>
Откройте «Аккаунты» → выберите ник → «Обновить cookie». Отправьте только новое полное значение cookie. ID аккаунта вводить не нужно.
Новый cookie должен принадлежать прежнему Roblox-пользователю. Повторное добавление того же пользователя также обновляет cookie и сохраняет привязки.

<b>Если проверка не прошла</b>
Проверьте полноту значения, правильность аккаунта и соединение с Roblox. При истечении сессии получите новый cookie.

Cookie даёт доступ к аккаунту. Не отправляйте его покупателям или в поддержку. Сообщение удаляется ботом по возможности; база и резервные копии тоже содержат cookie и должны храниться приватно.''', 'accounts'),
    ('proxy', '🌐 Прокси', '''<b>Когда нужен</b>
Если хост Cardinal не может нормально обращаться к Roblox напрямую. Сначала настройте прокси, затем добавляйте аккаунты.

<b>Главное меню → Прокси</b>
Допустимые примеры:
<code>127.0.0.1:8080</code>
<code>login:password@host:8080</code>
<code>http://login:password@host:8080</code>
<code>socks5://login:password@host:1080</code>
Поддерживается также socks5h. Для SOCKS установите PySocks в окружение Cardinal.

Протокол сохраняется. Значения в примерах не являются готовыми рабочими прокси — нужны ваши данные.

<b>Замена и удаление</b>
Отправьте новые данные в том же разделе. Настройка применяется ко всем добавленным аккаунтам Roblox. Для удаления отправьте <code>-</code>.

<b>Как проверить</b>
Надпись «настроен» означает, что настройка сохранена. Она не подтверждает доступность сети. Проверка аккаунта и добавление сервера должны успешно завершиться.

Если соединение не работает, проверьте адрес, порт, пароль, срок действия прокси и доступность Roblox с вашего хоста. Не публикуйте пароль от прокси в скриншотах.''', 'proxy'),
    ('servers', '🖥 Добавление серверов', '''<b>Главное меню → Серверы → Добавить сервер</b>
Отправьте VIP Server ID:
<code>1234567890</code>
Плагин проверит добавленные аккаунты и определит владельца и игру.

Можно отправить до 20 ID через пробел или с новой строки. Каждый сервер проверяется отдельно. Если обработка остановилась на ошибке, проверьте список: предыдущие серверы уже могли сохраниться. Повторите добавление только отсутствующих.

<b>После сохранения</b>
В списке должны быть верный сервер, владелец и Place ID. Сервер без категории и прямой привязки к лоту относится к общему пулу.

<b>После обновления со старой версии</b>
Откройте карточку сервера → «Проверить владельца». Никаких дополнительных полей вводить не нужно. Проверка владельца обязательна для новых продаж. Занятую аренду сначала дождитесь или завершите штатной кнопкой.

<b>Если владелец не определён</b>
Проверьте, что этот Roblox-аккаунт действительно владеет сервером, cookie действует, VIP Server ID взят из Configure, а Place ID относится к той же игре.

Не используйте продаваемый сервер для посторонней игры: подготовка новой аренды закрывает прежний доступ и отключает игроков. Подпиской и балансом Robux управляйте в Roblox — плагин их не продлевает.''', 'servers'),
    ('categories', '🗂 Категории и пулы', '''<b>Одна игра — одна категория</b>
Создайте отдельный пул для каждой игры, чтобы её лоты выдавали только подходящие серверы.

1. «Категории серверов» → «Добавить категорию» → отправьте только название, например <code>Моя игра</code>.
2. Откроется её карточка. Нажмите «Добавить серверы».
3. Отправьте ID VIP-сервера или несколько ID через пробел: <code>1234567890 1234567891</code>. За один ввод — до 20 ID.
4. Владельца и игру бот определит сам. Игра категории закрепляется по первому серверу; сервер другой игры добавить нельзя.

Серверы категории доступны внутри её карточки. В разделе «Сервера» главного меню показаны только серверы вне категорий.

<b>Перенос существующего свободного сервера</b>
Откройте карточку сервера → «Перенести в категорию» → нажмите название нужной категории.
Для возврата нажмите «В общий пул» в карточке сервера.
Сначала снимите прямую привязку лота, если она есть.

<b>Изменить название или игру</b>
В карточке категории нажмите «Переименовать» и отправьте новое название. Кнопка «Привязать игру» спрашивает только Place ID. Игра должна совпадать со всеми серверами категории.

Удалить можно пустую категорию без привязанных лотов. Сначала перенастройте лоты и перенесите серверы. Удаление не переводит продажи в случайный пул.''', 'cats'),
    ('lots', '⚙️ Лоты и время', '''<b>1. Добавьте своё предложение</b>
«Лоты» → «Добавить лот».
Шаг 1: отправьте только ID предложения, например <code>123456</code>. Бот проверит его на FunPay и подставит название.
Шаг 2: отправьте часы за одну штуку, например <code>1.5</code>. За 2 штуки в этом примере выдаётся 3 часа плюс применимый бонус.
Если ошиблись на первом шаге, нажмите «Предыдущий шаг». До завершения ввода лот не сохраняется.

<b>2. Выберите источник</b>
В открывшейся карточке лота нажмите один вариант:
• «Привязать категорию» → выберите её название кнопкой. Выдаётся только свободный сервер этой категории.
• «Привязать один сервер» → выберите сервер кнопкой. Выдаётся только он.
• «Общий пул» → используются только серверы без категории и прямых привязок. Для разных игр используйте категории.

Новый лот приостановлен до выбора источника. Привязка снимает паузу. Ошибка источника не разрешает выдачу из другого пула.

<b>3. Проверьте продажи</b>
В «Настройки» включите «Автолоты». Сверка наличия с FunPay выполняется примерно раз в минуту: нет свободного сервера — лот скрывается. Проверьте статус на самом FunPay.

«Пауза / включение» временно блокирует новые выдачи. При выключенных «Автолотах» видимостью предложения на FunPay управляйте вручную.

Для изменения времени: «Изменить время» → отправьте только число часов, например <code>2</code>. Новое значение действует на будущие платежи. Покупка того же лота может продлить действующую аренду при сохранённом источнике; другой лот создаёт отдельную аренду.''', 'lots'),
    ('friends', '🤝 Друзья и команды', '''<b>Настройка продавцом</b>
«Друзья» → включите модуль. Общий лимит: положительное целое, <code>0</code> или <code>-</code> — без лимита плагина.
В карточке лота → «Лимит друзей»:
<code>3</code> — до трёх пользователей;
<code>0</code> — без лимита плагина;
<code>-</code> — общий лимит. ID лота повторять не нужно.

<b>Действия покупателя</b>
1. Открыть профиль владельца из сообщения выдачи.
2. Отправить ему заявку в Roblox.
3. В чате покупки FunPay написать:
<code>!друзья #AB12CD34 НикRoblox</code>
Вместо ника можно передать User ID или ссылку на профиль. Для каждого друга повторить шаги отдельно.

<b>Команды в чате FunPay</b>
<code>!vip_help</code> — справка;
<code>!vip_time #AB12CD34</code> — остаток времени;
<code>!vip_link #AB12CD34</code> — новый код ссылки, без продления;
<code>!vip_friends #AB12CD34 Ник</code> — доступ другу;
<code>!vip_stock</code> — число свободных в общем пуле, без бронирования;
<code>!vip_review #AB12CD34</code> — проверка отзыва.
Русские варианты доступны без переключателя: !команды, !время, !ссылка, !друзья, !бронь, !отзыв. Справка также открывается командами !command и !commands.

Совместимость первой версии: /com, !com, com, команды_vip — справка; !time и время — остаток; !link и ссылка — новая ссылка; !вип, vip, вип, получить вип — VIP-ссылка; !friend Ник и друзья Ник — доступ другу; отзыв — проверка отзыва. Слова без префикса должны составлять команду целиком.

Команды отправляет покупатель из своего аккаунта FunPay. Сообщения продавца и самого бота не выполняются. Старая настройка русских команд больше не блокирует VIP-команды после обновления; !vip_help показывает доступные варианты. Перед запросом к Roblox приходит ответ о ходе операции.

При одной аренде номер заказа можно опустить; при нескольких указывайте нужный. После аренды доступ к серверу отзывается всегда. «Удалять друзей» касается только дружбы Roblox. Ограничения приватности Roblox продолжают действовать.''', 'friends'),
    ('bonuses', '🎁 Бонусы и цены', '''<b>Бонус за количество часов</b>
«Бонусы» → «Добавить правило». Сначала отправьте порог покупки: <code>5</code>. На следующем шаге — подарок: <code>1</code>.
За каждые полные 5 оплаченных часов добавляется 1 час. Покупка 10 часов получает +2 часа. При нескольких правилах применяется наибольший достигнутый порог, правила не складываются.
Для изменения нажмите кнопку правила и ответьте на два вопроса. Для удаления нажмите «Удалить» в его строке и подтвердите. В длинном списке используйте страницы.

<b>За отзыв</b>
«Отзывы» → включите бонус → «Размер бонуса», например <code>1.5</code>. После отзыва 5★ покупатель пишет <code>!отзыв #AB12CD34</code> или <code>!vip_review #AB12CD34</code>. Бонус начисляется один раз на оплаченный заказ, пока его аренда активна. Повторная команда не даёт ещё один бонус.

<b>Обычные и евент-цены</b>
В карточке каждого лота задайте обе цены:
«Обычная цена» → отправьте <code>50</code>.
«Евент-цена» → отправьте <code>75</code>.
Цена задаётся за штуку в валюте предложения. Сохранение значения не является применением на FunPay.

Нажмите переключатель режима на главной странице. Дождитесь уведомления и проверьте цены на FunPay. Лоты без заданной положительной цены выбранного режима пропускаются. Изменение цены не меняет часы за штуку и уже оплаченные аренды.''', 'bonuses'),
    ('messages', '💬 Шаблоны сообщений', '''<b>Главное меню → Сообщения</b>
Выберите нужный текст и отправьте его целиком. Не более 2500 символов. FunPay получает обычный текст; HTML-разметка здесь не нужна.

<b>Поля выдачи</b>
<code>{link}</code> — ссылка, обязательна;
<code>{hours}</code> — часы с бонусом;
<code>{order_id}</code> — номер заказа;
<code>{bonus_line}</code> — строка бонуса, если он есть;
<code>{owner_url}</code> — профиль владельца.

<b>Пример выдачи</b>
<code>🎮 Ваш VIP-сервер готов!
Ссылка: {link}
Время: {hours} ч.
Заказ: #{order_id}{bonus_line}
Владелец: {owner_url}
Отправьте ему заявку и напишите:
!vip_friends #{order_id} ВашНик</code>

<b>Поля остальных текстов</b>
Конец аренды: <code>{order_id}</code>.
Друг добавлен: <code>{friend_name}</code>.
Нет заявки: <code>{input}</code>.
Лимит друзей: <code>{limit}</code>.
Отзыв: <code>{bonus_hours}</code>.
Используйте поля только в соответствующем шаблоне.

«Сбросить шаблоны» заменяет все ваши тексты стандартными. Сохраните собственные тексты перед сбросом. После обновления старые тексты из базы сохраняются, поэтому проверьте их вручную.''', 'templates'),
    ('rentals', '🕹 Аренды и возвраты', '''<b>Главное меню → Аренды → нужный заказ</b>
В карточке проверьте покупателя, лот, сервер, статус, остаток времени и список платежей.

<b>Продлить</b> — добавить часы к действующей аренде; после нажатия отправьте, например, <code>2.5</code>. Уведомление покупателю попадёт в очередь.
<b>Завершить</b> — досрочно закрыть доступ и записать завершение.
<b>Отменить</b> — закрыть доступ со статусом отмены.
Оба действия сами по себе деньги не возвращают.
<b>Возврат всех платежей</b> — возврат первоначальной покупки и всех продлений этой аренды. Это реальное финансовое действие. Результат каждого платежа проверяется отдельно.
<b>Обновить</b> — получить актуальное состояние.

Во время операции повторные действия блокируются. Если кнопки старые, откройте карточку заново.

<b>Что происходит при окончании</b>
Плагин закрывает сервер, отзывает доступ, отключает игроков и меняет код ссылки. Пока очистка не подтверждена, сервер не продаётся заново. При ошибке появляются статус cleanup_failed и повтор очистки.

<b>Возврат отдельной покупки</b>
Возврат одного заказа на FunPay убирает только его оставшееся время и неиспользованный бонус. Другие оплаченные продления сохраняются. Частичный денежный возврат требует ручной сверки суммы и времени.

Карточка аренды не создаёт новую аренду из прямого перевода: «Продлить» работает для уже действующей.''', 'rentals'),
    ('orders', '📥 Разбор заказов', '''<b>Почему заказ здесь</b>
FunPayAPI не всегда передаёт точный ID купленного предложения. Плагин не угадывает его по названию, разделу или ссылке из сообщения покупателя.

<b>Привязать заказ и выдать</b>
1. Откройте заказ на FunPay и сверьте именно купленное предложение.
2. Убедитесь, что этот лот настроен в плагине, источник выбран и сервер доступен.
3. Нажмите «Привязать заказ и выдать» и по очереди ответьте на три вопроса: номер заказа (<code>AB12CD34</code>), ID предложения (<code>123456</code>), количество штук из заказа (<code>2</code>).
Последнее значение — штуки, а не часы. После последнего ответа запускается обработка и выдача. Проверьте количество на FunPay перед отправкой.

<b>Повторить чтение заказа</b>
Отправьте только ORDER_ID. Используйте после восстановления связи с FunPay. Если API по-прежнему не отдаёт ID лота, повтор сам по себе не решит привязку.

<b>Убрать чужой заказ</b>
Только для покупки, не относящейся к этому плагину. Она исключается из его очереди без выдачи и возврата.

<b>Ссылка ещё не пришла</b>
Откройте аренду. delivering / delivery_pending означает ожидание отправки. Сообщение сохранено в базе и повторяется; новую аренду для той же покупки вручную не создавайте. Время новой аренды начинается после подтверждения отправки в FunPay, а не после прочтения покупателем.

Неопределённый заказ не получает случайный сервер и не возвращается автоматически.''', 'orders'),
    ('check', '✅ Проверка перед продажами', '''<b>Сначала один лот и один сервер</b>
1. Проверьте владельца, Place ID и источник лота. На время настройки держите предложение скрытым на FunPay.
2. Отключите другие выдачи для этого же предложения: штатную выдачу Cardinal, встроенную выдачу FunPay и конкурирующие плагины.
3. Проверьте «Автолоты», «Автовозврат», лимиты друзей, бонусы, цены и сообщения.
4. Проведите согласованную проверочную покупку. Это реальная покупка; способ проверки и расчёты организуйте самостоятельно.
5. Убедитесь, что выдан нужный сервер и правильное число часов. Если заказ попал в разбор — сверьте предложение и привяжите вручную.
6. Проверьте вход, !vip_time и добавление друга после заявки.
7. Проверьте продление того же лота и отдельную аренду другого лота. Команды с номером заказа должны выбирать нужную аренду.
8. Завершите аренду штатной кнопкой. Проверьте отключение игроков, отказ старой ссылки и снятие разрешений.
9. Убедитесь, что сервер снова доступен и лот синхронизирован на FunPay.

<b>Если проверка не прошла</b>
Приостановите этот лот и исправьте причину из карточки. Не переводите сервер в свободные вручную через базу.

Автотесты проверяют локальную логику и сценарии с имитацией сетевых ответов. Реальный вход, ограничения игры и доступность API нужно проверять на вашем хосте.''', 'settings'),
    ('problems', '🛠 Частые вопросы', '''<b>Лот не появляется в продаже</b>
Проверьте паузу, источник, свободные проверенные серверы именно этого пула и «Автолоты». Подождите очередную сверку — примерно минуту. Ошибка FunPay отображается в карточке лота.

<b>Свободные серверы есть, но выдачи нет</b>
Свободный сервер другой категории не подходит. В random не входят серверы категорий и прямых привязок. Также проверьте «Заказы»: может отсутствовать ID предложения.

<b>Друг не добавляется</b>
Проверьте модуль, лимит, правильный заказ и ник, заявку именно владельцу выданного сервера. Дружба не отменяет ограничения приватности, возраста и родительских настроек Roblox.

<b>Старая ссылка перестала работать</b>
Она меняется при !ссылка / !vip_link и при очистке. Пользуйтесь последней ссылкой действующей аренды.

<b>cleanup_failed / сервер не освобождается</b>
Откройте карточку аренды и прочтите ошибку. Проверьте cookie владельца, соединение и сервер в Roblox; затем повторите очистку. Есть автоматические повторы с увеличением интервала. Не удаляйте базу и не обходите проверку свободным статусом.

<b>refund_pending</b>
FunPay ещё не подтвердил возврат. Проверьте заказ на FunPay и соединение. Не считайте возврат выполненным только по нажатию кнопки.

<b>Бот не принимает ввод</b>
Нажмите действие заново: ожидание длится 30 минут и снимается переходом в меню или /cancel.

<b>Команды FunPay не отвечают</b>
Из аккаунта покупателя отправьте /com. В Telegram откройте «Настройки → Диагностика команд → Обновить»: там видны версия, получение команды, результат обработки и последняя отправка. Если получение отсутствует, проверьте включение плагина и перезапуск Cardinal. Старый режим требует доступа к истории. Для !ссылка и !друзья дождитесь ответа Roblox; при ошибке проверьте cookie и прокси владельца.

Для обращения к автору приложите версию, этап, текст ошибки и ID заказа. Уберите cookie, пароли прокси и действующие приватные ссылки.''', 'main'),
    ('update', '💾 Обновление и копии', '''<b>Перед заменой файла</b>
1. Приостановите продажи и остановите Cardinal.
2. Скопируйте прежний .py и <code>AutoVIPRoblox_Monolith.db</code>. Если рядом есть файлы базы с окончаниями <code>-wal</code> и <code>-shm</code>, сохраните их тоже.
3. Замените плагин файлом <code>AutoVipRoblox_LuckyFunpay.py</code>. Старый .py уберите из plugins. Базу оставьте рядом.
4. Запустите Cardinal и проверьте аккаунты, серверы, лоты, незавершённые аренды и очередь заказов.

<b>Переход с v15</b>
Перед изменением схемы создаётся SQLite-копия <code>*.before-v16-*.bak</code>. Для каждого непроверенного сервера откройте его карточку → «Проверить владельца». Новые продажи допускаются только после проверки владельца. При обновлении с 16.1 повторно создавать аккаунты, категории и лоты не нужно.

<b>После перезапуска</b>
Очереди сообщений и возвратов сохраняются. Незавершённые операции проверяются и восстанавливаются. Если появилась ошибка очистки, исправьте доступ к Roblox и дождитесь подтверждённой очистки.

Переименование файла не требует пустой базы или повторного создания всех лотов: UUID и имя базы сохранены. Пока Cardinal остановлен, выполнять очистку по таймеру некому — учитывайте это при обслуживании активных аренд.

Для отката остановите Cardinal и восстановите согласованную резервную копию базы вместе с соответствующим старым .py. Не смешивайте несколько независимо изменённых копий базы.''', 'main'),
)



INPUTS = {
    'add_account': 'Отправьте cookie .ROBLOSECURITY. Повторное добавление того же Roblox-аккаунта обновляет cookie; серверы и аренды сохраняются.',
    'update_account': 'Формат: ACCOUNT_ID | COOKIE. Cookie должен принадлежать прежнему Roblox-пользователю.',
    'delete_account': 'Введите внутренний ID аккаунта без серверов. Аккаунт будет выключен; история останется.',
    'add_server': 'Введите VIP_SERVER_ID. Владелец и Place ID определятся по добавленным аккаунтам. Или: VIP_SERVER_ID | ACCOUNT_ID | PLACE_ID.',
    'add_server_cat': 'Формат: CATEGORY_ID | VIP_SERVER_ID [ещё ID через пробел]. Для явного владельца: CATEGORY_ID | VIP_SERVER_ID | ACCOUNT_ID | PLACE_ID.',
    'rebind_server': 'Формат: VIP_SERVER_ID | ACCOUNT_ID | PLACE_ID. Roblox проверит владельца и игру. Доступно после миграции и для свободных серверов.',
    'delete_server': 'Введите ID свободного сервера без привязанных лотов. Сервер будет выключен в пуле; история сохранится.',
    'add_category': 'Название | PLACE_ID (числа после /games/ в адресе Roblox). Можно ввести только название; игра определится по первому серверу.',
    'edit_category': 'CATEGORY_ID | название | PLACE_ID. Игра должна совпадать со всеми серверами категории.',
    'move_server': 'VIP_SERVER_ID | CATEGORY_ID. Вместо категории: - для общего пула. Только свободный сервер.',
    'delete_category': 'ID пустой категории без привязанных лотов. Сначала измените источники лотов.',
    'add_lot': 'LOT_ID | ЧАСЫ ЗА ШТУКУ | название (необязательно). По умолчанию лот приостановлен до выбора источника.',
    'delete_lot': 'ID лота без незавершённых аренд. Он будет деактивирован на FunPay и удалён из настроек плагина.',
    'bind_lot': 'LOT_ID | category | CATEGORY_ID, LOT_ID | server | SERVER_ID или LOT_ID | random. Источник сохраняется строго; привязка снимает паузу.',
    'lot_hours': 'LOT_ID | ЧАСЫ ЗА ШТУКУ. Изменение действует на будущие платежи.',
    'price_normal': 'LOT_ID | ЦЕНА', 'price_event': 'LOT_ID | ЦЕНА',
    'friend_limit': 'LOT_ID | ЛИМИТ. 0 — без лимита плагина, - или -1 — общий лимит. Лимиты Roblox сохраняются.',
    'bonus': 'КУПИЛ:БОНУС в часах, например 5:1. Применяется самое большое достигнутое правило за каждый полный порог.',
    'edit_bonus': 'ID ПРАВИЛА | КУПИЛ:БОНУС', 'delete_bonus': 'ID правила из списка бонусов.',
    'review_bonus': 'Бонус за 5★ в часах, например 1.5.',
    'global_friend_limit': '0 или - — без лимита плагина; положительное целое — максимум друзей на аренду.',
    'template': 'Новый текст. Для выдачи обязательно {link}. Поля: {link}, {hours}, {order_id}, {bonus_line}, {owner_url}. Другие шаблоны: {friend_name}, {input}, {limit}, {bonus_hours}. FunPay показывает обычный текст.',
    'proxy': 'ip:port, login:pass@host:port, http://… или socks5://… . Для удаления: -.',
    'extend': 'Количество часов продления действующей аренды.',
    'bind_order': 'Ручной разбор: ORDER_ID | LOT_ID (или ORDER_ID | LOT_ID | КОЛИЧЕСТВО, если FunPayAPI его не передаёт). Сверьте именно купленное предложение в FunPay. После сохранения начнётся выдача.',
    'retry_order': 'ID заказа из очереди для повторного чтения FunPay.',
    'ignore_order': 'ID заказа другого плагина: убрать из очереди Auto VIP без выдачи и возврата.',
}


def panel_button(label, action):
    return Button(label, callback_data=f'{PREFIX}:{action}')


def money_text(amount, currency):
    currency = str(currency or 'UNKNOWN')
    symbol = {'RUB':'₽', 'USD':'$', 'EUR':'€', 'UAH':'₴'}.get(currency, esc(currency))
    return f'{float(amount):,.2f}'.replace(',', ' ').replace('.', ',') + ' ' + symbol


def switch_button(key, title, destination):
    enabled = database().setting(key) == '1'
    return panel_button(('🟢 ' if enabled else '⚪ ') + title, f'toggle:{key}:{destination}')


def friends_menu(bot, chat_id, message_id):
    db = database()
    enabled = 'включён' if db.setting('friends_module_active') == '1' else 'выключен'
    cleanup = 'удалять добавленную дружбу' if db.setting('friends_auto_remove') == '1' else 'сохранять дружбу'
    limit = as_int(db.setting('friends_default_limit'), 0)
    text = ('🤝 <b>Друзья и доступ</b>\n\n'
            f'Модуль: <b>{enabled}</b>\nПосле аренды: <b>{cleanup}</b>\n'
            f'Общий лимит: <b>{limit if limit else "без ограничений плагина"}</b>\n\n'
            'Покупатель отправляет заявку владельцу, затем пишет:\n'
            '<code>!друзья #ЗАКАЗ Ник</code>\n\n'
            '<i>Доступ к VIP-серверу отзывается всегда. Переключатель удаления касается только дружбы Roblox.</i>')
    kb = Keyboard(row_width=2)
    kb.row(switch_button('friends_module_active','Модуль друзей','friends'),
           switch_button('friends_auto_remove','Удалять друзей','friends'))
    kb.row(panel_button('👥 Лимит друзей','input:global_friend_limit'), panel_button('💬 Тексты ответов','templates'))
    kb.row(panel_button('◀️ Главное меню','main'))
    edit_or_send(bot, chat_id, text, kb, message_id)


def reviews_menu(bot, chat_id, message_id):
    db = database()
    enabled = 'включён' if db.setting('review_bonus_active') == '1' else 'выключен'
    text = ('⭐ <b>Бонус за отзыв</b>\n\n'
            f'Статус: <b>{enabled}</b>\nЗа отзыв 5★: <b>+{esc(db.setting("review_bonus_hours"))} ч.</b>\n\n'
            'Бонус начисляется один раз на оплаченный заказ.\n'
            'Проверка — автоматически или по команде:\n<code>!отзыв #ЗАКАЗ</code>')
    kb = Keyboard(row_width=2)
    kb.row(switch_button('review_bonus_active','Бонус за отзыв','reviews'), panel_button('⏱ Размер бонуса','input:review_bonus'))
    kb.row(panel_button('💬 Сообщение за отзыв','input:template:msg_review_bonus'))
    kb.row(panel_button('◀️ Главное меню','main'))
    edit_or_send(bot, chat_id, text, kb, message_id)


def templates_menu(bot, chat_id, message_id):
    text = ('💬 <b>Сообщения покупателю</b>\n\n'
            'Выберите текст, который хотите изменить.\n'
            'Подстановки сохраняют ссылку, время и данные заказа.\n\n'
            '<i>Сброс восстановит стандартные тексты всех сообщений.</i>')
    kb = Keyboard(row_width=2)
    for left, right in (
        (('🎮 Выдача сервера','msg_post_payment'), ('⌛ Конец аренды','msg_post_expire')),
        (('🤝 Друг добавлен','msg_friend_added'), ('🔎 Нет заявки','msg_friend_not_found')),
        (('👥 Лимит друзей','msg_friend_limit'), ('⭐ Отзыв','msg_review_bonus')),
    ):
        kb.row(panel_button(left[0], 'input:template:' + left[1]), panel_button(right[0], 'input:template:' + right[1]))
    kb.row(panel_button('🔄 Сбросить шаблоны','reset_templates'))
    kb.row(panel_button('◀️ Главное меню','main'))
    edit_or_send(bot, chat_id, text, kb, message_id)


def command_diagnostics_menu(bot, chat_id, message_id):
    with GUARD:
        snapshot, pending = dict(COMMAND_TRACE), len(RUNNING)
    mode = getattr(CARDINAL, 'old_mode_enabled', None)
    mode_text = 'старый' if mode is True else ('обычный' if mode is False else 'не определён этой версией Cardinal')
    alive = bool(WORKER and WORKER.is_alive())
    text = ('🔎 <b>Диагностика команд FunPay</b>\n\n'
            f'Версия: <b>{esc(VERSION)}</b>\nФайл: <code>{esc(os.path.basename(__file__))}</code>\n'
            f'Режим чтения: {mode_text}\nФоновый обработчик: {"работает" if alive else "не запущен"}\n'
            f'Операций в очереди и работе: {pending}\n\n')
    for key, label in (('receive', 'Получение'), ('process', 'Обработка'), ('send', 'Последняя отправка в FunPay')):
        item = snapshot.get(key)
        text += f'<b>{label}</b>\n'
        text += (f'{esc(time.strftime("%H:%M:%S", time.localtime(item[0])))} · {esc(item[1])}\n\n'
                 if item else 'После запуска данных нет.\n\n')
    text += ('Из аккаунта покупателя отправьте <code>/com</code> в чат FunPay и нажмите «Обновить».\n'
             'Если получение не появилось — команда не дошла до обработчиков этой версии. '
             'Проверьте включение плагина и перезапуск Cardinal.\n\n'
             '<i>Показаны последние этапы по всем чатам; отправка может относиться к другому уведомлению. '
             'Тексты сообщений, ники и cookie не сохраняются. Диагностика сбрасывается при перезапуске.</i>')
    kb = Keyboard().row(panel_button('🔄 Обновить','command_diagnostics'), panel_button('◀️ Настройки','settings'))
    edit_or_send(bot, chat_id, text, kb, message_id)


def guide_menu(bot, chat_id, message_id, section=None):
    kb = Keyboard(row_width=2)
    if section is None:
        text = ('📖 <b>Инструкция Auto VIP Roblox</b>\n\n'
                'Пошаговая настройка, примеры ввода и помощь при ошибках.\n'
                'Начните с установки и пройдите разделы по порядку.\n\n'
                '<b>Порядок:</b> аккаунт → сервер → категория → лот → проверочная аренда.\n\n'
                'Все ID в примерах условные. После кнопки действия отправляйте полный формат с вашими данными.\n'
                '<code>/cancel</code> отменяет ожидание ввода.\n\n'
                'Выберите раздел:')
        buttons = [panel_button(title, 'guidepage:' + key) for key, title, _, _ in GUIDE_SECTIONS]
        for index in range(0, len(buttons), 2):
            kb.row(*buttons[index:index+2])
    else:
        index = next((i for i, item in enumerate(GUIDE_SECTIONS) if item[0] == section), None)
        if index is None:
            raise ValueError('Раздел инструкции не найден. Откройте оглавление заново.')
        key, title, body, destination = GUIDE_SECTIONS[index]
        text = f'📖 <b>{esc(title)}</b>\n<i>Раздел {index+1} из {len(GUIDE_SECTIONS)}</i>\n\n{body}'
        nav = []
        if index:
            nav.append(panel_button('⬅️ Назад', 'guidepage:' + GUIDE_SECTIONS[index-1][0]))
        if index+1 < len(GUIDE_SECTIONS):
            nav.append(panel_button('Далее ➡️', 'guidepage:' + GUIDE_SECTIONS[index+1][0]))
        if nav:
            kb.row(*nav)
        if destination != 'main':
            kb.row(panel_button('⚙️ Открыть настройки раздела', destination))
        kb.row(panel_button('📚 Оглавление', 'guide'))
    kb.row(panel_button('◀️ Главное меню', 'main'))
    edit_or_send(bot, chat_id, text, kb, message_id)


def lot_details(bot, chat_id, message_id, lot_id):
    lot = database().row('SELECT * FROM lots WHERE lot_id=?', (lot_id,))
    if not lot:
        raise ValueError('Лот не найден')
    source = '⚠️ Выберите источник выдачи'
    if source_key(lot):
        if lot['bind_type'] == 'category':
            cat = database().row('SELECT name FROM categories WHERE id=?', (lot['category_id'],))
            source = '📁 ' + str(cat['name']) if cat else '⚠️ Категория не найдена'
        elif lot['bind_type'] == 'server':
            source = '🖥 Сервер ' + str(lot['server_id'])
        else:
            source = '🎲 Общий пул'
    limit = 'по умолчанию' if lot['friend_limit'] < 0 else ('без лимита плагина' if not lot['friend_limit'] else str(lot['friend_limit']))
    text = (f'✏️ <b>Редактирование лота</b>\n\n📦 <b>{esc(lot["name"])}</b>\n'
            f'🆔 ID: <code>{lot_id}</code>\n⏰ За 1 шт.: {lot["hours"]:g} ч.\n'
            f'📥 Выдача: {esc(source)}\n👥 Лимит друзей: {esc(limit)}\n\n'
            f'💰 Обычная цена: {lot["price_normal"]:g}\n🎉 Евент-цена: {lot["price_event"]:g}\n'
            f'Продажи: {"⏸ на паузе" if lot["paused"] else "включены"}\n'
            f'Синхронизация: {esc(lot["last_error"] or "без ошибок")}\n\n'
            '<i>Нажмите нужную настройку. Бот попросит только новое значение.</i>')
    kb = Keyboard(row_width=2)
    kb.row(panel_button('⏰ Изменить время', f'c_wizard:lot_hours:{lot_id}'))
    kb.row(panel_button('📁 Привязать категорию', f'c_pick:{lot_id}:category:0'))
    kb.row(panel_button('🖥 Привязать один сервер', f'c_pick:{lot_id}:server:0'))
    kb.row(panel_button('🎲 Общий пул', f'c_bind:{lot_id}:random:0'))
    kb.row(panel_button('💰 Обычная цена', f'c_wizard:price_normal:{lot_id}'), panel_button('🎉 Евент-цена', f'c_wizard:price_event:{lot_id}'))
    kb.row(panel_button('👥 Лимит друзей', f'c_wizard:friend_limit:{lot_id}'))
    kb.row(panel_button('▶️ Включить' if lot['paused'] else '⏸ Приостановить', f'pause:{lot_id}'), panel_button('🗑 Удалить лот', f'c_confirm:lot:{lot_id}'))
    kb.row(panel_button('🔄 Обновить', f'lot:{lot_id}'), panel_button('⬅️ Лоты', 'lots'))
    edit_or_send(bot, chat_id, text, kb, message_id)


def orders_menu(bot, chat_id, message_id, page=0):
    total = database().row("SELECT COUNT(*) n FROM incoming_orders WHERE state IN ('attention','pending')")['n']
    pages = max(1, (total+7)//8)
    page = min(max(0, page), pages-1)
    rows = database().rows("SELECT * FROM incoming_orders WHERE state IN ('attention','pending') ORDER BY created_at DESC LIMIT 8 OFFSET ?", (page*8,))
    text = f'📥 <b>Заказы на разбор · {page+1}/{pages}</b>\n\n'
    for r in rows:
        text += f'#{esc(r["order_id"])} · {esc(r["state"])}\n{esc(r["error"] or "Ожидает обработки")}\n\n'
    kb = Keyboard(row_width=1)
    for title, action in (('🔗 Привязать заказ и выдать','bind_order'), ('🔁 Повторить чтение заказа','retry_order'), ('Убрать чужой заказ','ignore_order')):
        route = 'c_wizard' if action == 'bind_order' else 'input'
        kb.add(Button(title, callback_data=f'{PREFIX}:{route}:{action}'))
    if page:
        kb.add(Button('⬅️', callback_data=f'{PREFIX}:orderpage:{page-1}'))
    if page+1 < pages:
        kb.add(Button('➡️', callback_data=f'{PREFIX}:orderpage:{page+1}'))
    kb.add(Button('🔄 Обновить', callback_data=f'{PREFIX}:orders'), Button('◀️ В меню', callback_data=f'{PREFIX}:main'))
    edit_or_send(bot, chat_id, text, kb, message_id)


def verify_server_config(server_id, account_id=None, place_id=None):
    db = database()
    accounts = db.rows('SELECT * FROM accounts WHERE enabled=1 AND id>0' + (' AND id=?' if account_id else ''), (account_id,) if account_id else ())
    found = []
    for account in accounts:
        try:
            api = RobloxAPI(RobloxAccount(account['id'], account['cookie'], account['proxy'], str(account['roblox_id'])))
            details = api.details(server_id)
            actual_place = ((details.get('game') or {}).get('rootPlace') or {}).get('id')
            if place_id and str(actual_place) != str(place_id):
                continue
            if not as_int(actual_place):
                continue
            api.verify_owner(server_id, str(actual_place))
            found.append((account['id'], str(actual_place)))
        except Exception:
            continue
    if len(found) != 1:
        raise ValueError('Roblox не подтвердил единственного владельца и Place ID. Проверьте cookie, прокси, ID сервера и аккаунта.')
    return found[0]


def update_configuration(action, raw, extra):
    db = database()
    parts = [p.strip() for p in raw.split('|')]
    if action in ('add_account', 'update_account'):
        expected = None
        cookie = raw
        if action == 'update_account':
            account_id, cookie = raw.split('|', 1)
            expected = db.row('SELECT * FROM accounts WHERE id=? AND id>0', (int(account_id),))
            if not expected:
                raise ValueError('Аккаунт не найден')
        proxy = expected['proxy'] if expected else db.setting('global_proxy')
        cookie = cookie.strip()
        if '_|WARNING:-DO-NOT-SHARE' not in cookie:
            raise ValueError('Это не .ROBLOSECURITY cookie')
        auth = RobloxAPI(RobloxAccount(0, cookie, proxy, '')).authenticated()
        if not auth or (expected and str(expected['roblox_id']) != auth[0]):
            raise ValueError('Cookie не подтверждён или принадлежит другому аккаунту')
        uid, name = auth
        with db.lock, db.conn() as con:
            exists = con.execute('SELECT id FROM accounts WHERE roblox_id=?', (uid,)).fetchone()
            if exists:
                con.execute('UPDATE accounts SET cookie=?,username=?,enabled=1 WHERE roblox_id=?', (cookie, name, uid))
            else:
                con.execute('INSERT INTO accounts(cookie,proxy,username,roblox_id,created_at) VALUES(?,?,?,?,?)', (cookie, proxy, name, uid, time.time()))
        return '✅ Cookie проверен и сохранён для ' + esc(name)
    if action in ('add_server','add_server_cat','rebind_server'):
        category = None
        if action == 'add_server_cat':
            category, parts = int(parts[0]), parts[1:]
            if not db.row('SELECT 1 FROM categories WHERE id=?', (category,)):
                raise ValueError('Категория не найдена')
        if len(parts) == 3:
            server_ids = [parts[0]]
            account, place = int(parts[1]), parts[2]
        elif len(parts) == 1:
            server_ids, account, place = split_ids(parts[0]), None, None
        else:
            raise ValueError('Неверный формат сервера')
        if not server_ids or len(server_ids) > 20 or any(not re.fullmatch(r'[1-9][0-9]*', x) for x in server_ids):
            raise ValueError('Нужно от 1 до 20 числовых ID серверов')
        for sid in server_ids:
            owner, root_place = verify_server_config(sid, account, place)
            with operation_lock('server-config:' + sid), db.lock, db.conn() as con:
                con.execute('BEGIN IMMEDIATE')
                existing = con.execute('SELECT * FROM servers WHERE server_id=?', (sid,)).fetchone()
                if existing and existing['enabled'] and action == 'add_server':
                    raise ValueError('Сервер уже есть. Откройте его карточку → «Проверить владельца».')
                busy = con.execute("SELECT * FROM rentals WHERE server_id=? AND status IN ('issuing','delivering','active','refunding','cleaning')", (sid,)).fetchone()
                if busy:
                    raise ValueError('У сервера выполняется аренда или операция')
                old_owner = existing['owner_account_id'] if existing else None
                failed = con.execute("SELECT * FROM rentals WHERE server_id=? AND status='cleanup_failed'", (sid,)).fetchall()
                if failed and old_owner not in (None, 0, owner):
                    raise ValueError('Нельзя менять Roblox-владельца незавершённой аренды')
                cat_id = category if category is not None else (existing['category_id'] if existing else None)
                if existing and cat_id != existing['category_id'] and con.execute('SELECT 1 FROM lots WHERE server_id=?', (sid,)).fetchone():
                    raise ValueError('Сначала измените прямую привязку лота к этому серверу.')
                if cat_id:
                    cat = con.execute('SELECT * FROM categories WHERE id=?', (cat_id,)).fetchone()
                    if not cat or (cat['game_id'] and str(cat['game_id']) != root_place):
                        raise ValueError('Сервер принадлежит другой игре, чем категория')
                    con.execute('UPDATE categories SET game_id=? WHERE id=? AND game_id IS NULL', (root_place, cat_id))
                if existing:
                    if existing['status'] == 'error' and not failed:
                        recovery_id = 'recovery-' + uuid.uuid4().hex
                        con.execute('''INSERT INTO rentals(rental_id,buyer_id,chat_id,lot_id,server_id,owner_account_id,hours,start_time,end_time,status,cleanup_reason)
                            VALUES(?,'0','',0,?,?,0,0,0,'cleanup_failed','issue_failed')''', (recovery_id,sid,owner))
                    con.execute('UPDATE servers SET owner_account_id=?,place_id=?,category_id=?,verified=1,enabled=1 WHERE server_id=?', (owner, root_place, cat_id, sid))
                    con.execute("UPDATE rentals SET owner_account_id=?,next_cleanup_at=0 WHERE server_id=? AND status='cleanup_failed' AND owner_account_id IN (0,?)", (owner, sid, owner))
                else:
                    con.execute("INSERT INTO servers(server_id,owner_account_id,place_id,category_id,status,verified) VALUES(?,?,?,?,'free',1)", (sid, owner, root_place, cat_id))
        return '✅ Проверены и сохранены серверы: ' + ', '.join(server_ids)
    if action == 'extend':
        extend_rental(extra[0], raw)
        return '✅ Аренда продлена. Уведомление покупателю сохранено в очереди.'
    if action in ('bind_order','retry_order','ignore_order'):
        oid = parts[0].lstrip('#')
        with ORDER_LOCK, db.lock, db.conn() as con:
            if con.execute('SELECT 1 FROM payments WHERE order_id=?', (oid,)).fetchone():
                raise ValueError('Этот заказ уже обработан; используйте карточку аренды')
            if not con.execute('SELECT 1 FROM incoming_orders WHERE order_id=?', (oid,)).fetchone():
                raise ValueError('Заказ не найден в очереди')
            if action == 'bind_order':
                lot_id = int(parts[1])
                if not con.execute('SELECT 1 FROM lots WHERE lot_id=?', (lot_id,)).fetchone():
                    raise ValueError('Лот не настроен')
                quantity = int(parts[2]) if len(parts) == 3 else None
                if quantity is not None and not 1 <= quantity <= 1_000_000:
                    raise ValueError('Недопустимое количество')
                con.execute('UPDATE incoming_orders SET lot_id=?,amount=COALESCE(?,amount) WHERE order_id=?', (lot_id, quantity, oid))
            con.execute('UPDATE incoming_orders SET state=?,error=NULL,next_at=0 WHERE order_id=?', ('ignored' if action == 'ignore_order' else 'pending', oid))
        return '✅ Заказ убран из очереди.' if action == 'ignore_order' else '✅ Заказ поставлен в очередь обработки.'
    # Network changes are outside the DB transaction.
    if action == 'delete_lot':
        with ORDER_LOCK:
            lot_id = int(raw)
            if not db.row('SELECT 1 FROM lots WHERE lot_id=?', (lot_id,)):
                raise ValueError('Лот не найден')
            if db.row("SELECT 1 FROM rentals WHERE lot_id=? AND status IN ('issuing','delivering','active','refunding','cleaning','cleanup_failed')", (lot_id,)):
                raise ValueError('У лота есть незавершённые аренды')
            with FP_LOT_LOCK:
                fields = CARDINAL.account.get_lot_fields(lot_id)
                fields.active = False
                CARDINAL.account.save_lot(fields.renew_fields())
                with db.lock, db.conn() as con:
                    con.execute('DELETE FROM lots WHERE lot_id=?', (lot_id,))
        return '✅ Лот деактивирован на FunPay и удалён из настроек.'
    if action == 'add_lot':
        lot_id, hours = int(parts[0]), positive_float(parts[1], 'Часы')
        if lot_id <= 0:
            raise ValueError('Нужен положительный ID лота')
        fields = CARDINAL.account.get_lot_fields(lot_id)
        if str(getattr(fields, 'lot_id', '')) != str(lot_id):
            raise ValueError('FunPay вернул другой лот')
        name = parts[2] if len(parts) > 2 else (getattr(fields, 'title_ru', None) or getattr(fields, 'summary', None) or f'Лот {lot_id}')
        with ORDER_LOCK, db.lock, db.conn() as con:
            con.execute('INSERT INTO lots(lot_id,hours,name,paused,subcategory_id) VALUES(?,?,?,1,?) ON CONFLICT(lot_id) DO UPDATE SET hours=excluded.hours,name=excluded.name',
                        (lot_id, hours, str(name)[:200], getattr(fields, 'subcategory_id', None)))
        return '✅ Лот сохранён. Выберите источник выдачи в его карточке.'
    with ORDER_LOCK, db.lock, db.conn() as con:
        con.execute('BEGIN IMMEDIATE')
        if action == 'delete_account':
            aid = int(raw)
            if con.execute('SELECT 1 FROM servers WHERE owner_account_id=? AND enabled=1', (aid,)).fetchone() or con.execute("SELECT 1 FROM rentals WHERE owner_account_id=? AND status IN ('issuing','delivering','active','refunding','cleaning','cleanup_failed')", (aid,)).fetchone():
                raise ValueError('У аккаунта остались серверы или незавершённые аренды')
            changed = con.execute('UPDATE accounts SET enabled=0 WHERE id=? AND id>0', (aid,))
        elif action == 'delete_server':
            if con.execute('SELECT 1 FROM lots WHERE server_id=?', (raw,)).fetchone():
                raise ValueError('Сначала измените привязку лотов к этому серверу')
            changed = con.execute("UPDATE servers SET enabled=0 WHERE server_id=? AND status='free'", (raw,))
        elif action == 'add_category':
            name = parts[0][:100]
            place = str(int(parts[1])) if len(parts) > 1 else None
            if not name or (place and int(place) <= 0):
                raise ValueError('Нужны название и положительный Place ID')
            changed = con.execute('INSERT INTO categories(name,game_id) VALUES(?,?)', (name, place))
        elif action == 'edit_category':
            cid, name, place = int(parts[0]), parts[1][:100], str(int(parts[2]))
            if not name or int(place) <= 0 or con.execute('SELECT 1 FROM servers WHERE category_id=? AND place_id!=?', (cid, place)).fetchone():
                raise ValueError('Название пустое или игра не совпадает с серверами категории')
            changed = con.execute('UPDATE categories SET name=?,game_id=? WHERE id=?', (name, place, cid))
        elif action == 'move_server':
            sid, cid = parts[0], None if parts[1] == '-' else int(parts[1])
            server = con.execute("SELECT * FROM servers WHERE server_id=? AND status='free' AND enabled=1", (sid,)).fetchone()
            if not server:
                raise ValueError('Сервер не найден или занят')
            if con.execute('SELECT 1 FROM lots WHERE server_id=?', (sid,)).fetchone():
                raise ValueError('Сначала отвяжите лот от конкретного сервера')
            if cid:
                cat = con.execute('SELECT * FROM categories WHERE id=?', (cid,)).fetchone()
                if not cat or (cat['game_id'] and str(cat['game_id']) != str(server['place_id'])):
                    raise ValueError('Категория не найдена или относится к другой игре')
                con.execute('UPDATE categories SET game_id=? WHERE id=? AND game_id IS NULL', (server['place_id'], cid))
            changed = con.execute('UPDATE servers SET category_id=? WHERE server_id=?', (cid, sid))
        elif action == 'delete_category':
            cid = int(raw)
            if con.execute('SELECT 1 FROM servers WHERE category_id=? AND enabled=1', (cid,)).fetchone() or con.execute('SELECT 1 FROM lots WHERE category_id=?', (cid,)).fetchone():
                raise ValueError('Сначала перенесите серверы и измените привязки лотов')
            con.execute('UPDATE servers SET category_id=NULL WHERE category_id=? AND enabled=0', (cid,))
            changed = con.execute('DELETE FROM categories WHERE id=?', (cid,))
        elif action == 'bind_lot':
            lid, kind = int(parts[0]), parts[1]
            lot = con.execute('SELECT * FROM lots WHERE lot_id=?', (lid,)).fetchone()
            if not lot:
                raise ValueError('Лот не найден')
            category, server = None, None
            if kind == 'category' and len(parts) == 3:
                category = int(parts[2])
                if not con.execute('SELECT 1 FROM categories WHERE id=? AND game_id IS NOT NULL', (category,)).fetchone():
                    raise ValueError('Категория не найдена или её игра ещё не указана')
            elif kind == 'server' and len(parts) == 3:
                server = parts[2]
                if not con.execute('SELECT 1 FROM servers WHERE server_id=? AND enabled=1 AND verified=1', (server,)).fetchone():
                    raise ValueError('Сервер не найден или его владелец ещё не проверен')
            elif kind != 'random' or len(parts) != 2:
                raise ValueError('Неверный источник')
            changed = con.execute('UPDATE lots SET bind_type=?,category_id=?,server_id=?,paused=0 WHERE lot_id=?', (kind, category, server, lid))
        elif action in ('price_normal','price_event','lot_hours','friend_limit'):
            lid = int(parts[0])
            column = 'hours' if action == 'lot_hours' else action
            value = (-1 if parts[1] == '-' else int(parts[1])) if action == 'friend_limit' else positive_float(parts[1], 'Значение')
            if action == 'friend_limit' and value < -1:
                raise ValueError('Лимит должен быть -1, 0 или положительным целым')
            changed = con.execute(f'UPDATE lots SET {column}=? WHERE lot_id=?', (value, lid))
        elif action in ('bonus','edit_bonus'):
            values = raw if action == 'bonus' else parts[1]
            buy, get = [positive_float(x, 'Бонус') for x in values.split(':')]
            if action == 'bonus':
                changed = con.execute('INSERT INTO wholesale_bonuses(buy_hours,get_hours) VALUES(?,?)', (buy, get))
            else:
                changed = con.execute('UPDATE wholesale_bonuses SET buy_hours=?,get_hours=? WHERE id=?', (buy, get, int(parts[0])))
        elif action == 'delete_bonus':
            changed = con.execute('DELETE FROM wholesale_bonuses WHERE id=?', (int(raw),))
        else:
            key, value = action, raw
            if action == 'review_bonus':
                key, value = 'review_bonus_hours', positive_float(raw, 'Бонус')
            elif action == 'global_friend_limit':
                key, value = 'friends_default_limit', 0 if raw == '-' else int(raw)
                if value < 0:
                    raise ValueError('Лимит не может быть отрицательным')
            elif action == 'template':
                key = extra[0]
                if key not in DEFAULTS or not key.startswith('msg_') or len(raw) > 2500:
                    raise ValueError('Шаблон не найден или длиннее 2500 символов')
                if key == 'msg_post_payment' and '{link}' not in raw:
                    raise ValueError('Шаблон выдачи должен содержать {link}')
            elif action == 'proxy':
                key, value = 'global_proxy', '' if raw == '-' else raw
                normalize_proxy(value)
                con.execute('UPDATE accounts SET proxy=? WHERE id>0', (value,))
            else:
                raise ValueError('Неизвестное действие')
            changed = con.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))
        if changed.rowcount != 1:
            raise ValueError('Объект не найден или его состояние не позволяет изменение')
    return '✅ Изменения сохранены.'


"""v11-style cards and sequential setup over the validated v16 operations."""


CLASSIC_FIELDS = {
    'add_account': [('cookie', 'Отправьте cookie .ROBLOSECURITY аккаунта Roblox.\nБот проверит его и покажет ник владельца.')],
    'update_account': [('cookie', 'Отправьте новый cookie этого же аккаунта Roblox.')],
    'add_server': [('ids', 'Отправьте ID VIP-сервера со страницы Configure.\nМожно несколько ID через пробел (до 20). Владельца и игру бот определит сам.')],
    'add_server_cat': [('ids', 'Отправьте ID серверов для этой категории — через пробел или с новой строки (до 20).')],
    'add_category': [('name', 'Как назвать категорию? Например: Steal a Brainrot.\nИгра определится по первому добавленному серверу.')],
    'rename_category': [('name', 'Отправьте новое название категории.')],
    'category_game': [('id', 'Отправьте Place ID игры: цифры после /games/ в адресе Roblox.')],
    'add_lot': [('lot', 'Отправьте только ID вашего предложения FunPay.\nНазвание бот получит из предложения.'),
                ('hours', 'Сколько часов выдавать за 1 штуку?\nНапример: 1, 24 или 1.5.')],
    'lot_hours': [('hours', 'Сколько часов выдавать за 1 штуку? Новое время применяется к будущим покупкам.')],
    'price_normal': [('hours', 'Отправьте обычную цену за 1 штуку в валюте предложения.')],
    'price_event': [('hours', 'Отправьте евент-цену за 1 штуку в валюте предложения.')],
    'friend_limit': [('limit', 'Сколько пользователей можно добавить за аренду?\n0 — без лимита плагина. Минус (-) — общий лимит.')],
    'bonus': [('hours', 'Сколько оплаченных часов нужно для бонуса? Например: 5.'),
              ('hours', 'Сколько часов подарить за каждый такой набор? Например: 1.')],
    'edit_bonus': [('hours', 'Новый порог покупки в часах?'), ('hours', 'Сколько бонусных часов добавить?')],
    'bind_order': [('order', 'Отправьте номер заказа FunPay. Сначала проверьте на FunPay, какое предложение куплено.'),
                   ('configured_lot', 'Отправьте ID купленного предложения. Лот должен быть настроен в плагине.'),
                   ('quantity', 'Сколько штук куплено в этом заказе? Укажите количество из заказа, а не число часов. После ответа заказ попадёт в очередь выдачи.')],
}


def classic_nav(kb, page, pages, route):
    buttons = []
    if page:
        buttons.append(panel_button('⬅️ Назад', f'{route}:{page-1}'))
    if page+1 < pages:
        buttons.append(panel_button('Вперёд ➡️', f'{route}:{page+1}'))
    if buttons:
        kb.row(*buttons)


def classic_page(total, page, size=8):
    pages = max(1, (total+size-1)//size)
    return min(max(0, int(page)), pages-1), pages


def classic_button(text, action):
    return panel_button(text[:48], action)


def classic_account(bot, chat, mid, identity):
    row = database().row('SELECT * FROM accounts WHERE id=? AND enabled=1', (identity,))
    if not row:
        raise ValueError('Аккаунт уже удалён. Обновите список.')
    count = database().row('SELECT COUNT(*) n FROM servers WHERE owner_account_id=? AND enabled=1', (identity,))['n']
    text = (f'👤 <b>{esc(row["username"])}</b>\n\nRoblox ID: <code>{esc(row["roblox_id"])}</code>\n'
            f'Серверов: {count}\n\nCookie сохранён. Чтобы обновить сессию, нажмите кнопку ниже.')
    kb = Keyboard().row(panel_button('🔄 Обновить cookie', f'c_wizard:update_account:{identity}'))
    kb.row(panel_button('🗑 Удалить аккаунт', f'c_confirm:account:{identity}'))
    kb.row(panel_button('⬅️ Аккаунты', 'accounts'))
    edit_or_send(bot, chat, text, kb, mid)


def classic_server(bot, chat, mid, identity):
    row = database().row('''SELECT s.*,a.username,c.name category_name FROM servers s
        LEFT JOIN accounts a ON a.id=s.owner_account_id LEFT JOIN categories c ON c.id=s.category_id
        WHERE s.server_id=? AND s.enabled=1''', (identity,))
    if not row:
        raise ValueError('Сервер не найден. Обновите список.')
    state = {'free':'🟢 Свободен','occupied':'🔴 Занят','reserved':'⏳ Выдаётся','cleaning':'🧹 Очистка','error':'⚠️ Требует проверки'}.get(row['status'], row['status'])
    text = (f'🖥 <b>VIP-сервер {esc(identity)}</b>\n\nВладелец: {esc(row["username"] or "не проверен")}\n'
            f'Игра (Place ID): {esc(row["place_id"] or "не определена")}\n'
            f'Категория: {esc(row["category_name"] or "Общий пул")}\nСтатус: {esc(state)}')
    kb = Keyboard().row(panel_button('📁 Перенести в категорию', f'c_move_pick:{identity}:0'))
    kb.row(panel_button('🔄 Проверить владельца', f'c_recheck:{identity}'))
    if row['category_id']:
        kb.row(panel_button('↩️ В общий пул', f'c_move:{identity}:0'))
    kb.row(panel_button('🗑 Удалить сервер', f'c_confirm:server:{identity}'))
    back = f'c_cat:{row["category_id"]}:0' if row['category_id'] else 'servers'
    kb.row(panel_button('⬅️ Назад', back))
    edit_or_send(bot, chat, text, kb, mid)


def classic_category(bot, chat, mid, identity, page=0):
    row = database().row('SELECT * FROM categories WHERE id=?', (identity,))
    if not row:
        raise ValueError('Категория удалена. Обновите список.')
    total = database().row('SELECT COUNT(*) n FROM servers WHERE enabled=1 AND category_id=?', (identity,))['n']
    page, pages = classic_page(total, page)
    servers = database().rows('SELECT * FROM servers WHERE enabled=1 AND category_id=? ORDER BY server_id LIMIT 8 OFFSET ?', (identity,page*8))
    text = (f'📁 <b>{esc(row["name"])}</b>\n\n🎮 Place ID: {esc(row["game_id"] or "Определится по первому серверу")}\n'
            f'🖥 Серверов: {total} · Страница {page+1}/{pages}\n\nВыберите сервер для управления.')
    kb = Keyboard(row_width=2)
    buttons = [classic_button(('🟢 ' if s['status']=='free' else '🟠 ') + s['server_id'], 'c_server:' + s['server_id']) for s in servers]
    for offset in range(0,len(buttons),2):
        kb.row(*buttons[offset:offset+2])
    classic_nav(kb, page, pages, f'c_cat:{identity}')
    kb.row(panel_button('➕ Добавить серверы', f'c_wizard:add_server_cat:{identity}'))
    kb.row(panel_button('🎮 Привязать игру', f'c_wizard:category_game:{identity}'), panel_button('✏️ Переименовать', f'c_wizard:rename_category:{identity}'))
    kb.row(panel_button('🗑 Удалить категорию', f'c_confirm:category:{identity}'))
    kb.row(panel_button('⬅️ Категории', 'cats'))
    edit_or_send(bot, chat, text, kb, mid)


def classic_pick(bot, chat, mid, lot_id, kind, page=0, moving=None):
    if kind not in ('category','server'):
        raise ValueError('Неизвестный источник')
    if moving is None and not database().row('SELECT 1 FROM lots WHERE lot_id=?', (lot_id,)):
        raise ValueError('Лот уже удалён')
    table, where, key = ('categories','1=1','id') if kind=='category' else ('servers','enabled=1 AND verified=1','server_id')
    total = database().row(f'SELECT COUNT(*) n FROM {table} WHERE {where}')['n']
    page, pages = classic_page(total, page)
    rows = database().rows(f'SELECT * FROM {table} WHERE {where} ORDER BY {key} LIMIT 8 OFFSET ?', (page*8,))
    text = ('📁 <b>Выберите категорию</b>' if kind=='category' else '🖥 <b>Выберите сервер</b>') + '\n\n'
    text += 'Выбор сохраняется для этого лота.' if moving is None else 'Сервер будет перенесён в выбранную категорию.'
    if not rows:
        text += '\nПока ничего нет. Сначала добавьте источник через главное меню.'
    kb = Keyboard()
    for row in rows:
        label = str(row['name']) if kind=='category' else str(row['server_id'])
        route = f'c_bind:{lot_id}:{kind}:{row[key]}' if moving is None else f'c_move:{moving}:{row[key]}'
        kb.row(classic_button(label, route))
    classic_nav(kb, page, pages, f'c_pick:{lot_id}:{kind}' if moving is None else f'c_move_pick:{moving}')
    kb.row(panel_button('⬅️ Назад', f'lot:{lot_id}' if moving is None else f'c_server:{moving}'))
    edit_or_send(bot, chat, text, kb, mid)


def classic_return(bot, chat, mid, action, target):
    if action == 'bind_order':
        orders_menu(bot, chat, mid)
    elif action in ('add_account','update_account'):
        simple_list_menu(bot, chat, mid, 'accounts')
    elif action in ('add_category','rename_category','category_game','add_server_cat'):
        if target:
            classic_category(bot, chat, mid, target)
        else:
            simple_list_menu(bot, chat, mid, 'cats')
    elif action in ('add_lot','lot_hours','price_normal','price_event','friend_limit'):
        if target:
            lot_details(bot, chat, mid, int(target))
        else:
            simple_list_menu(bot, chat, mid, 'lots')
    elif action in ('bonus','edit_bonus'):
        bonuses_menu(bot, chat, mid)
    else:
        simple_list_menu(bot, chat, mid, 'servers')


def classic_start(tg, bot, call, action, target=''):
    if action not in CLASSIC_FIELDS:
        raise ValueError('Неизвестное действие')
    data = {'action':action, 'target':str(target), 'values':[], 'at':time.time()}
    classic_prompt(tg, bot, call.message.chat.id, call.from_user.id, data)


def classic_prompt(tg, bot, chat, user, data):
    data = dict(data, token=uuid.uuid4().hex[:12])
    fields = CLASSIC_FIELDS[data['action']]
    step = len(data['values'])
    title = '✏️ Настройка · шаг ' + str(step+1) + ' из ' + str(len(fields))
    text = f'<b>{title}</b>\n\n'
    if data.get('lot_name'):
        text += '📦 ' + esc(data['lot_name']) + '\n\n'
    text += esc(fields[step][1])
    kb = Keyboard()
    if step:
        kb.row(panel_button('⬅️ Предыдущий шаг', 'c_back:' + data['token']))
    kb.row(panel_button('❌ Отмена', 'c_abort:' + data['token']))
    msg = bot.send_message(chat, text, parse_mode='HTML', reply_markup=kb)
    tg.set_state(chat, msg.message_id, user, STATE_PREFIX+'classic', data)


def classic_input(tg, bot, message, unused):
    with operation_lock(f'admin-input:{message.chat.id}:{message.from_user.id}'):
        state = tg.get_state(message.chat.id, message.from_user.id) or {}
        if state.get('state') != STATE_PREFIX+'classic':
            return
        data = dict(state.get('data') or {})
        data['values'] = list(data.get('values') or [])
        if data.get('at', 0)+1800 < time.time():
            tg.clear_state(message.chat.id, message.from_user.id, True)
            bot.reply_to(message, 'Время ожидания истекло. Откройте действие снова.')
            return
        raw = message.text.strip()
        if raw.startswith('/'):
            tg.clear_state(message.chat.id, message.from_user.id, True)
            return
        action, target = data['action'], data['target']
        fields = CLASSIC_FIELDS[action]
        try:
            kind = fields[len(data['values'])][0]
            if kind in ('id','lot'):
                if not re.fullmatch(r'[1-9][0-9]*', raw):
                    raise ValueError('Отправьте только числовой ID.')
                if kind=='lot':
                    offer = CARDINAL.account.get_lot_fields(int(raw))
                    if str(getattr(offer,'lot_id','')) != raw:
                        raise ValueError('FunPay не подтвердил это предложение.')
                    if database().row('SELECT 1 FROM lots WHERE lot_id=?', (int(raw),)):
                        raise ValueError('Этот лот уже добавлен. Откройте его карточку в списке.')
                    data['lot_name'] = str(getattr(offer,'title_ru',None) or getattr(offer,'summary',None) or 'Лот '+raw)[:200]
            elif kind=='hours':
                raw = str(positive_float(raw, 'Значение'))
            elif kind=='name':
                if not raw or len(raw)>100 or '|' in raw:
                    raise ValueError('Название: от 1 до 100 символов, без |.')
            elif kind=='limit':
                limit = -1 if raw=='-' else int(raw)
                if limit < -1:
                    raise ValueError('Лимит: 0, положительное целое или минус (-).')
            elif kind=='ids':
                ids = split_ids(raw)
                if not ids or len(ids)>20 or any(not re.fullmatch(r'[1-9][0-9]*', sid) for sid in ids):
                    raise ValueError('Отправьте от 1 до 20 числовых ID серверов.')
            elif kind=='order':
                raw = raw.lstrip('#').upper()
                if not re.fullmatch(r'[A-Z0-9]+', raw) or not database().row('SELECT 1 FROM incoming_orders WHERE order_id=?', (raw,)):
                    raise ValueError('Заказ не найден в очереди разбора.')
            elif kind=='configured_lot':
                if not re.fullmatch(r'[1-9][0-9]*', raw) or not database().row('SELECT 1 FROM lots WHERE lot_id=?', (raw,)):
                    raise ValueError('Такой лот ещё не настроен в плагине.')
            elif kind=='quantity':
                if not re.fullmatch(r'[1-9][0-9]*', raw) or not 1 <= int(raw) <= 1_000_000:
                    raise ValueError('Количество должно быть целым числом от 1 до 1000000.')
            data['values'].append(raw)
            if len(data['values']) < len(fields):
                classic_prompt(tg, bot, message.chat.id, message.from_user.id, data)
                return
            values = data['values']
            if action=='add_lot':
                target = values[0]
                name = data['lot_name'].replace('|',' ')
                with ORDER_LOCK:
                    if database().row('SELECT 1 FROM lots WHERE lot_id=?', (target,)):
                        raise ValueError('Лот уже добавлен. Откройте его карточку.')
                    reply = update_configuration(action, f'{target} | {values[1]} | {name}', [])
            elif action=='bind_order':
                reply = update_configuration(action, ' | '.join(values), [])
            elif action=='add_category':
                # Create and capture the new ID in one transaction for correct navigation.
                with database().lock, database().conn() as con:
                    target = str(con.execute('INSERT INTO categories(name) VALUES(?)', (values[0],)).lastrowid)
                reply = '✅ Категория создана. Теперь добавьте в неё серверы.'
            elif action in ('rename_category','category_game'):
                with ORDER_LOCK, database().lock, database().conn() as con:
                    row = con.execute('SELECT * FROM categories WHERE id=?', (target,)).fetchone()
                    if not row:
                        raise ValueError('Категория уже удалена.')
                    if action=='category_game':
                        if con.execute('SELECT 1 FROM servers WHERE category_id=? AND place_id!=?', (target, values[0])).fetchone():
                            raise ValueError('Игра не совпадает с серверами этой категории.')
                        con.execute('UPDATE categories SET game_id=? WHERE id=?', (values[0],target))
                    else:
                        con.execute('UPDATE categories SET name=? WHERE id=?', (values[0],target))
                reply = '✅ Категория обновлена.'
            elif action in ('bonus','edit_bonus'):
                raw_value = ':'.join(values)
                reply = update_configuration(action, f'{target} | {raw_value}' if target else raw_value, [])
            else:
                raw_value = f'{target} | {values[0]}' if target else values[0]
                reply = update_configuration(action, raw_value, [])
            tg.clear_state(message.chat.id, message.from_user.id, True)
            bot.reply_to(message, reply, parse_mode='HTML')
            if action in ('add_account','update_account'):
                try:
                    bot.delete_message(message.chat.id, message.message_id)
                except Exception:
                    pass
            classic_return(bot, message.chat.id, None, action, target)
        except Exception as exc:
            detail = str(exc) if isinstance(exc, ValueError) and action not in ('add_account','update_account') else 'Проверьте данные и подключение. Результат можно проверить в списке.'
            bot.reply_to(message, '❌ ' + esc(detail) + '\nИсправьте значение и отправьте снова либо нажмите «Отмена».', parse_mode='HTML')


def classic_callback(tg, bot, call, parts):
    chat, mid, user = call.message.chat.id, call.message.message_id, call.from_user.id
    action = parts[0]
    state = tg.get_state(chat, user) or {}
    data = state.get('data') or {}
    if action in ('c_back','c_abort'):
        if state.get('state') != STATE_PREFIX+'classic' or len(parts)<2 or parts[1] != data.get('token'):
            raise ValueError('Ввод уже завершён. Откройте действие заново.')
        if action=='c_back' and data.get('values'):
            data = dict(data, values=list(data['values'])[:-1])
            classic_prompt(tg, bot, chat, user, data)
        else:
            tg.clear_state(chat, user, True)
            classic_return(bot, chat, mid, data['action'], data['target'])
        return
    if str(state.get('state','')).startswith(STATE_PREFIX):
        tg.clear_state(chat, user, True)
    if action=='c_wizard':
        classic_start(tg, bot, call, parts[1], parts[2] if len(parts)>2 else '')
    elif action=='c_account':
        classic_account(bot, chat, mid, parts[1])
    elif action=='c_server':
        classic_server(bot, chat, mid, parts[1])
    elif action=='c_cat':
        classic_category(bot, chat, mid, parts[1], int(parts[2]) if len(parts)>2 else 0)
    elif action=='c_pick':
        classic_pick(bot, chat, mid, int(parts[1]), parts[2], int(parts[3]))
    elif action=='c_bind':
        lid, kind = int(parts[1]), parts[2]
        value = f'{lid} | {kind}' + ('' if kind=='random' else ' | '+parts[3])
        update_configuration('bind_lot', value, [])
        lot_details(bot, chat, mid, lid)
    elif action=='c_move_pick':
        classic_pick(bot, chat, mid, 0, 'category', int(parts[2]), moving=parts[1])
    elif action=='c_move':
        update_configuration('move_server', parts[1]+' | '+('-' if parts[2]=='0' else parts[2]), [])
        classic_server(bot, chat, mid, parts[1])
    elif action=='c_recheck':
        owner, place = verify_server_config(parts[1])
        update_configuration('rebind_server', f'{parts[1]} | {owner} | {place}', [])
        classic_server(bot, chat, mid, parts[1])
    elif action=='c_history':
        rentals_menu(bot, chat, mid, int(parts[1]), history=True)
    elif action=='c_bonuses':
        bonuses_menu(bot, chat, mid, int(parts[1]))
    elif action in ('c_confirm','c_delete'):
        options = {'account':('delete_account','accounts'), 'server':('delete_server','servers'),
                   'category':('delete_category','cats'), 'lot':('delete_lot','lots'), 'bonus':('delete_bonus','bonuses')}
        operation, destination = options[parts[1]]
        identity = parts[2]
        if action=='c_confirm':
            kb = Keyboard().row(panel_button('🗑 Да, удалить', f'c_delete:{parts[1]}:{identity}'), panel_button('Отмена', destination))
            edit_or_send(bot, chat, f'Удалить выбранный объект <code>{esc(identity)}</code>?\nСвязанные аренды и привязки будут проверены перед удалением.', kb, mid)
        else:
            update_configuration(operation, identity, [])
            if destination=='bonuses':
                bonuses_menu(bot, chat, mid)
            else:
                simple_list_menu(bot, chat, mid, destination)
    else:
        raise ValueError('Неизвестное действие. Откройте меню заново.')
