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
VERSION = "1.0.2"
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


# ---------------------------------------------------------------------------
# Каталог соцсетей и типов услуг
# ---------------------------------------------------------------------------

PLATFORMS: Dict[str, Dict[str, Any]] = {
    'telegram': {'name': 'Telegram', 'short': 'TELEGRAM', 'emoji': '✈️',
                 'pattern': r'telegram|телеграм|\btg\b|\bтг\b|t\.me', 'domains': ['t.me', 'telegram.me'],
                 'links': {'profile': 'https://t.me/channel', 'group': 'https://t.me/chat', 'post': 'https://t.me/channel/123',
                           'story': 'https://t.me/channel/s/1', 'poll': 'https://t.me/channel/123', 'bot': 'https://t.me/your_bot',
                           'channel': 'https://t.me/channel', 'comment': 'https://t.me/channel/123?comment=1'}},
    'instagram': {'name': 'Instagram', 'short': 'INSTAGRAM', 'emoji': '📸',
                  'pattern': r'instagram|инстаграм|инста\b|\binsta\b|\big\b', 'domains': ['instagram.com'],
                  'links': {'profile': 'https://instagram.com/username', 'post': 'https://instagram.com/p/XXXXXXX',
                            'video': 'https://instagram.com/reel/XXXXXXX', 'story': 'https://instagram.com/stories/username/123',
                            'stream': 'https://instagram.com/username/live'}},
    'tiktok': {'name': 'TikTok', 'short': 'TIKTOK', 'emoji': '🎵',
               'pattern': r'tik\s?tok|тик\s?ток', 'domains': ['tiktok.com', 'vt.tiktok.com', 'vm.tiktok.com'],
               'links': {'profile': 'https://tiktok.com/@username', 'post': 'https://tiktok.com/@username/video/123',
                         'video': 'https://tiktok.com/@username/video/123', 'stream': 'https://tiktok.com/@username/live'}},
    'youtube': {'name': 'YouTube', 'short': 'YOUTUBE', 'emoji': '▶️',
                'pattern': r'youtube|ютуб|\byt\b|shorts', 'domains': ['youtube.com', 'youtu.be'],
                'links': {'profile': 'https://youtube.com/@channel', 'post': 'https://youtube.com/watch?v=XXXXXXX',
                          'video': 'https://youtube.com/watch?v=XXXXXXX', 'stream': 'https://youtube.com/live/XXXXXXX',
                          'comment': 'https://youtube.com/watch?v=XXXXXXX&lc=XXXX'}},
    'vk': {'name': 'ВКонтакте', 'short': 'VK', 'emoji': '🔵',
           'pattern': r'\bvk\b|\bвк\b|вконтакт|vkontakte|vk\.com', 'domains': ['vk.com', 'vk.ru', 'm.vk.com'],
           'links': {'profile': 'https://vk.com/club123', 'group': 'https://vk.com/club123', 'post': 'https://vk.com/wall-123_456',
                     'poll': 'https://vk.com/wall-123_456', 'video': 'https://vk.com/video-123_456',
                     'stream': 'https://vk.com/video-123_456', 'story': 'https://vk.com/story-123_456'}},
    'twitch': {'name': 'Twitch', 'short': 'TWITCH', 'emoji': '🟣', 'pattern': r'twitch|твич', 'domains': ['twitch.tv'],
               'links': {'profile': 'https://twitch.tv/channel', 'stream': 'https://twitch.tv/channel',
                         'video': 'https://twitch.tv/videos/123'}},
    'kick': {'name': 'Kick', 'short': 'KICK', 'emoji': '🟢', 'pattern': r'\bkick\b', 'domains': ['kick.com'],
             'links': {'profile': 'https://kick.com/channel', 'stream': 'https://kick.com/channel'}},
    'twitter': {'name': 'X (Twitter)', 'short': 'X/TWITTER', 'emoji': '✖️',
                'pattern': r'twitter|твиттер|\bx\.com\b|\(x\)|\bx\s*/\s*twitter', 'domains': ['twitter.com', 'x.com'],
                'links': {'profile': 'https://x.com/username', 'post': 'https://x.com/username/status/123',
                          'poll': 'https://x.com/username/status/123', 'stream': 'https://x.com/i/broadcasts/XXXX'}},
    'discord': {'name': 'Discord', 'short': 'DISCORD', 'emoji': '🎮', 'pattern': r'discord|дискорд',
                'domains': ['discord.com', 'discord.gg'],
                'links': {'profile': 'https://discord.gg/invite', 'group': 'https://discord.gg/invite',
                          'post': 'https://discord.com/channels/123/456/789'}},
    'facebook': {'name': 'Facebook', 'short': 'FACEBOOK', 'emoji': '📘', 'pattern': r'facebook|фейсбук|\bfb\b',
                 'domains': ['facebook.com', 'fb.com', 'fb.watch'],
                 'links': {'profile': 'https://facebook.com/page', 'group': 'https://facebook.com/groups/123',
                           'post': 'https://facebook.com/page/posts/123', 'video': 'https://facebook.com/watch/?v=123',
                           'stream': 'https://facebook.com/watch/?v=123'}},
    'threads': {'name': 'Threads', 'short': 'THREADS', 'emoji': '🧵', 'pattern': r'threads', 'domains': ['threads.net', 'threads.com'],
                'links': {'profile': 'https://threads.net/@username', 'post': 'https://threads.net/@username/post/XXXX'}},
    'ok': {'name': 'Одноклассники', 'short': 'OK.RU', 'emoji': '🟠', 'pattern': r'одноклассник|odnoklassniki|ok\.ru',
           'domains': ['ok.ru'], 'links': {'profile': 'https://ok.ru/group/123', 'group': 'https://ok.ru/group/123',
                                           'post': 'https://ok.ru/group/123/topic/456', 'video': 'https://ok.ru/video/123'}},
    'rutube': {'name': 'Rutube', 'short': 'RUTUBE', 'emoji': '📺', 'pattern': r'rutube|рутуб', 'domains': ['rutube.ru'],
               'links': {'profile': 'https://rutube.ru/channel/123', 'post': 'https://rutube.ru/video/XXXX',
                         'video': 'https://rutube.ru/video/XXXX', 'stream': 'https://rutube.ru/video/XXXX'}},
    'dzen': {'name': 'Дзен', 'short': 'ДЗЕН', 'emoji': '🟡', 'pattern': r'дзен|dzen|zen\.yandex', 'domains': ['dzen.ru', 'zen.yandex.ru'],
             'links': {'profile': 'https://dzen.ru/id/XXXX', 'post': 'https://dzen.ru/a/XXXX', 'video': 'https://dzen.ru/video/watch/XXXX'}},
    'spotify': {'name': 'Spotify', 'short': 'SPOTIFY', 'emoji': '🎧', 'pattern': r'spotify|спотифай', 'domains': ['spotify.com', 'open.spotify.com'],
                'links': {'profile': 'https://open.spotify.com/artist/XXXX', 'track': 'https://open.spotify.com/track/XXXX',
                          'post': 'https://open.spotify.com/track/XXXX'}},
    'yamusic': {'name': 'Яндекс Музыка', 'short': 'ЯНДЕКС МУЗЫКА', 'emoji': '🎶', 'pattern': r'яндекс\s*музык|yandex\s*music',
                'domains': ['music.yandex.ru'], 'links': {'profile': 'https://music.yandex.ru/artist/123',
                                                          'track': 'https://music.yandex.ru/album/1/track/2',
                                                          'post': 'https://music.yandex.ru/album/1/track/2'}},
    'soundcloud': {'name': 'SoundCloud', 'short': 'SOUNDCLOUD', 'emoji': '☁️', 'pattern': r'soundcloud|саундклауд',
                   'domains': ['soundcloud.com'], 'links': {'profile': 'https://soundcloud.com/artist',
                                                            'track': 'https://soundcloud.com/artist/track',
                                                            'post': 'https://soundcloud.com/artist/track'}},
    'likee': {'name': 'Likee', 'short': 'LIKEE', 'emoji': '💗', 'pattern': r'likee|лайки\s*\(likee\)', 'domains': ['likee.video', 'l.likee.video'],
              'links': {'profile': 'https://likee.video/@username', 'post': 'https://likee.video/@username/video/123'}},
    'pinterest': {'name': 'Pinterest', 'short': 'PINTEREST', 'emoji': '📌', 'pattern': r'pinterest|пинтерест',
                  'domains': ['pinterest.com', 'pin.it'], 'links': {'profile': 'https://pinterest.com/username',
                                                                    'post': 'https://pinterest.com/pin/123'}},
    'reddit': {'name': 'Reddit', 'short': 'REDDIT', 'emoji': '👽', 'pattern': r'reddit|реддит', 'domains': ['reddit.com'],
               'links': {'profile': 'https://reddit.com/user/name', 'group': 'https://reddit.com/r/community',
                         'post': 'https://reddit.com/r/community/comments/XXXX'}},
    'linkedin': {'name': 'LinkedIn', 'short': 'LINKEDIN', 'emoji': '💼', 'pattern': r'linkedin|линкедин', 'domains': ['linkedin.com'],
                 'links': {'profile': 'https://linkedin.com/in/name', 'post': 'https://linkedin.com/posts/XXXX'}},
    'website': {'name': 'Сайт', 'short': 'САЙТ', 'emoji': '🌐', 'pattern': r'traffic|трафик|website|сайт\b|визит',
                'domains': ['*'], 'links': {'page': 'https://example.com/page', 'post': 'https://example.com/page',
                                            'profile': 'https://example.com'}},
}
PLATFORM_ORDER = ['telegram', 'instagram', 'tiktok', 'youtube', 'vk', 'twitch', 'kick', 'twitter', 'discord', 'facebook',
                  'threads', 'ok', 'rutube', 'dzen', 'spotify', 'yamusic', 'soundcloud', 'likee', 'pinterest', 'reddit',
                  'linkedin', 'website']
OTHER_PLATFORM = {'name': 'SMM', 'short': '', 'emoji': '⚡', 'domains': [], 'links': {'post': 'https://example.com/page',
                                                                                    'profile': 'https://example.com/page'}}

KINDS: Dict[str, Dict[str, Any]] = {
    'followers': {'ru': 'ПОДПИСЧИКИ', 'en': 'FOLLOWERS', 'emoji': '👥', 'unit': ('подписчик', 'подписчика', 'подписчиков'),
                  'unit_en': ('follower', 'followers'), 'target': 'profile',
                  'select': ['подписчик', 'фолловер', 'follower', 'subscriber', 'участник']},
    'premium_followers': {'ru': 'PREMIUM-ПОДПИСЧИКИ', 'en': 'PREMIUM FOLLOWERS', 'emoji': '💎',
                          'unit': ('premium-подписчик', 'premium-подписчика', 'premium-подписчиков'),
                          'unit_en': ('premium follower', 'premium followers'), 'target': 'profile',
                          'select': ['премиум', 'premium', 'подписчик']},
    'members': {'ru': 'УЧАСТНИКИ', 'en': 'MEMBERS', 'emoji': '👨‍👩‍👧', 'unit': ('участник', 'участника', 'участников'),
                'unit_en': ('member', 'members'), 'target': 'group', 'select': ['участник', 'подписчик', 'member']},
    'friends': {'ru': 'ДРУЗЬЯ', 'en': 'FRIENDS', 'emoji': '🤝', 'unit': ('друг', 'друга', 'друзей'),
                'unit_en': ('friend', 'friends'), 'target': 'profile', 'select': ['друз', 'заявк', 'friend']},
    'views': {'ru': 'ПРОСМОТРЫ', 'en': 'VIEWS', 'emoji': '👁', 'unit': ('просмотр', 'просмотра', 'просмотров'),
              'unit_en': ('view', 'views'), 'target': 'post', 'select': ['просмотр', 'view']},
    'story_views': {'ru': 'ПРОСМОТРЫ СТОРИС', 'en': 'STORY VIEWS', 'emoji': '📲', 'unit': ('просмотр', 'просмотра', 'просмотров'),
                    'unit_en': ('view', 'views'), 'target': 'story', 'select': ['сторис', 'истори', 'stories', 'просмотр']},
    'reels_views': {'ru': 'ПРОСМОТРЫ REELS/SHORTS', 'en': 'REELS/SHORTS VIEWS', 'emoji': '🎬',
                    'unit': ('просмотр', 'просмотра', 'просмотров'), 'unit_en': ('view', 'views'), 'target': 'video',
                    'select': ['просмотр', 'reels', 'shorts', 'view']},
    'stream_viewers': {'ru': 'ЗРИТЕЛИ НА СТРИМ', 'en': 'STREAM VIEWERS', 'emoji': '🎥', 'unit': ('зритель', 'зрителя', 'зрителей'),
                       'unit_en': ('viewer', 'viewers'), 'target': 'stream',
                       'select': ['зрител', 'стрим', 'трансляц', 'эфир', 'viewer', 'просмотр']},
    'watchtime': {'ru': 'ЧАСЫ ПРОСМОТРА', 'en': 'WATCH TIME', 'emoji': '⏱', 'unit': ('час', 'часа', 'часов'),
                  'unit_en': ('hour', 'hours'), 'target': 'video', 'select': ['врем', 'час', 'watch', 'просмотр']},
    'likes': {'ru': 'ЛАЙКИ', 'en': 'LIKES', 'emoji': '❤️', 'unit': ('лайк', 'лайка', 'лайков'), 'unit_en': ('like', 'likes'),
              'target': 'post', 'select': ['лайк', 'like', 'нрав', 'оценк']},
    'dislikes': {'ru': 'ДИЗЛАЙКИ', 'en': 'DISLIKES', 'emoji': '👎', 'unit': ('дизлайк', 'дизлайка', 'дизлайков'),
                 'unit_en': ('dislike', 'dislikes'), 'target': 'post', 'select': ['дизлайк', 'dislike', 'лайк']},
    'reactions': {'ru': 'РЕАКЦИИ', 'en': 'REACTIONS', 'emoji': '🔥', 'unit': ('реакция', 'реакции', 'реакций'),
                  'unit_en': ('reaction', 'reactions'), 'target': 'post', 'select': ['реакц', 'reaction', 'лайк']},
    'comments': {'ru': 'КОММЕНТАРИИ', 'en': 'COMMENTS', 'emoji': '💬', 'unit': ('комментарий', 'комментария', 'комментариев'),
                 'unit_en': ('comment', 'comments'), 'target': 'post', 'select': ['коммент', 'comment']},
    'comment_likes': {'ru': 'ЛАЙКИ НА КОММЕНТАРИИ', 'en': 'COMMENT LIKES', 'emoji': '👍', 'unit': ('лайк', 'лайка', 'лайков'),
                      'unit_en': ('like', 'likes'), 'target': 'comment', 'select': ['лайк', 'коммент']},
    'reposts': {'ru': 'РЕПОСТЫ', 'en': 'SHARES', 'emoji': '🔁', 'unit': ('репост', 'репоста', 'репостов'),
                'unit_en': ('share', 'shares'), 'target': 'post', 'select': ['репост', 'подел', 'share', 'repost']},
    'saves': {'ru': 'СОХРАНЕНИЯ', 'en': 'SAVES', 'emoji': '🔖', 'unit': ('сохранение', 'сохранения', 'сохранений'),
              'unit_en': ('save', 'saves'), 'target': 'post', 'select': ['сохран', 'save']},
    'votes': {'ru': 'ГОЛОСА В ОПРОС', 'en': 'POLL VOTES', 'emoji': '🗳', 'unit': ('голос', 'голоса', 'голосов'),
              'unit_en': ('vote', 'votes'), 'target': 'poll', 'select': ['голос', 'опрос', 'vote', 'poll']},
    'boosts': {'ru': 'БУСТЫ КАНАЛА', 'en': 'CHANNEL BOOSTS', 'emoji': '🚀', 'unit': ('буст', 'буста', 'бустов'),
               'unit_en': ('boost', 'boosts'), 'target': 'channel', 'select': ['буст', 'boost', 'голос']},
    'bot_starts': {'ru': 'СТАРТЫ БОТА', 'en': 'BOT STARTS', 'emoji': '🤖', 'unit': ('старт', 'старта', 'стартов'),
                   'unit_en': ('start', 'starts'), 'target': 'bot', 'select': ['старт', 'бот', 'запуск', 'bot']},
    'plays': {'ru': 'ПРОСЛУШИВАНИЯ', 'en': 'PLAYS', 'emoji': '🎧', 'unit': ('прослушивание', 'прослушивания', 'прослушиваний'),
              'unit_en': ('play', 'plays'), 'target': 'track', 'select': ['прослуш', 'воспроизвед', 'play', 'stream']},
    'impressions': {'ru': 'ОХВАТЫ И ПОКАЗЫ', 'en': 'IMPRESSIONS', 'emoji': '📣', 'unit': ('показ', 'показа', 'показов'),
                    'unit_en': ('impression', 'impressions'), 'target': 'post', 'select': ['охват', 'показ', 'impression', 'reach']},
    'traffic': {'ru': 'ТРАФИК НА САЙТ', 'en': 'WEBSITE TRAFFIC', 'emoji': '🌐', 'unit': ('посещение', 'посещения', 'посещений'),
                'unit_en': ('visit', 'visits'), 'target': 'page', 'select': ['трафик', 'посещ', 'traffic', 'визит']},
    'generic': {'ru': 'SMM-УСЛУГА', 'en': 'SMM SERVICE', 'emoji': '⚡', 'unit': ('единица', 'единицы', 'единиц'),
                'unit_en': ('unit', 'units'), 'target': 'post', 'select': []},
}
KIND_ORDER = ['followers', 'premium_followers', 'members', 'friends', 'views', 'story_views', 'reels_views', 'stream_viewers',
              'watchtime', 'likes', 'dislikes', 'reactions', 'comments', 'comment_likes', 'reposts', 'saves', 'votes',
              'boosts', 'bot_starts', 'plays', 'impressions', 'traffic', 'generic']

# Составные типы проверяются первыми: они точнее простых слов.
_COMPOUND_KINDS = [
    ('bot_starts', r'bot\s*start|старт\w*\s+бот|запуск\w*\s+бот|bot\s*users?|пользовател\w*\s+(в\s+)?бот'),
    ('boosts', r'boost|буст|голос\w*\s+(для|за|на)\s+канал'),
    ('watchtime', r'watch\s*-?time|время\s+просмотр|час\w*\s+просмотр'),
    ('stream_viewers', r'live\s*stream|stream\s*view|зрител|прям\w*\s+эфир|трансляц|\blive\b[^|\]]*?(view|просмотр)'),
    ('story_views', r'(stor(y|ies)|сторис|истори)[^|\]]*?(view|просмотр)|(view|просмотр)[^|\]]*?(stor(y|ies)|сторис|истори)'),
    ('reels_views', r'(reels?|shorts|клип|igtv)[^|\]]*?(view|просмотр)|(view|просмотр)[^|\]]*?(reels?|shorts|клип|igtv)'),
    ('comment_likes', r'(like|лайк)\w*\s+(on\s+|на\s+|for\s+|к\s+)?(comment|коммент)|(comment|коммент)\w*\s+(like|лайк)'),
    ('premium_followers', r'(premium|премиум)[^|\]]*?(подписч|subscriber|member|follower|участник)|'
                          r'(подписч|subscriber|member|follower|участник)[^|\]]*?(premium|премиум)'),
    ('traffic', r'traffic|трафик|website|визит\w*\s+на\s+сайт|посещен'),
    ('votes', r'\bpoll|\bопрос|голосован|\bvotes?\b'),
]
# Простые типы: выбирается слово, которое встречается в названии раньше всех.
_SIMPLE_KINDS = [
    ('dislikes', r'dislike|дизлайк'),
    ('reactions', r'reaction|реакци|emoji|эмодзи'),
    ('comments', r'comment|коммент'),
    ('members', r'member|участник|\bjoin|вступлен'),
    ('followers', r'follower|subscriber|подписч|фоллов|\bsubs\b'),
    ('friends', r'friend|друз|заявк\w*\s+в\s+друз'),
    ('saves', r'\bsaves?\b|сохранен|закладк|bookmark'),
    ('reposts', r'repost|репост|\bshares?\b|подел|retweet|ретвит'),
    ('plays', r'\bplays?\b|прослушив|\bstreams?\b|listen|слушател'),
    ('impressions', r'impression|охват|показ|\breach\b'),
    ('views', r'view|просмотр'),
    ('likes', r'like|лайк|heart|сердеч'),
    ('votes', r'голос'),
]

TARGET_TEXT = {
    'profile': ('ссылку на открытый профиль, канал или страницу', 'a link to a public profile, channel or page'),
    'group': ('ссылку на открытую группу, чат или канал', 'a link to a public group, chat or channel'),
    'post': ('ссылку на конкретный пост, видео или публикацию', 'a link to the exact post, video or publication'),
    'story': ('ссылку на активную сторис', 'a link to an active story'),
    'video': ('ссылку на видео (Reels, Shorts, ролик)', 'a link to the video (Reels, Shorts, clip)'),
    'stream': ('ссылку на трансляцию или канал со стримом', 'a link to the live stream or streaming channel'),
    'poll': ('ссылку на пост с опросом', 'a link to the post with the poll'),
    'bot': ('ссылку на бота', 'a link to the bot'),
    'channel': ('ссылку на публичный канал', 'a link to the public channel'),
    'comment': ('ссылку на комментарий', 'a link to the comment'),
    'track': ('ссылку на трек, альбом или артиста', 'a link to the track, album or artist'),
    'page': ('полную ссылку на страницу сайта', 'the full link to the website page'),
}
TARGET_NOTES = {
    'profile': (['Профиль или канал должен быть открытым (публичным)', 'Не меняйте ссылку и юзернейм до завершения заказа'],
                ['The profile or channel must be public', 'Do not change the link or username until completion']),
    'group': (['Группа или чат должны быть открытыми, вступление — без одобрения', 'Не меняйте ссылку до завершения заказа'],
              ['The group or chat must be public with open joining', 'Do not change the link until completion']),
    'post': (['Публикация должна быть доступна без авторизации', 'Не удаляйте и не скрывайте публикацию до завершения'],
             ['The publication must be visible without login', 'Do not delete or hide the publication until completion']),
    'story': (['Сторис должна быть активна до завершения заказа', 'Аккаунт должен быть открытым'],
              ['The story must stay active until completion', 'The account must be public']),
    'video': (['Видео должно быть доступно без авторизации', 'Не удаляйте видео до завершения заказа'],
              ['The video must be public', 'Do not delete the video until completion']),
    'stream': (['Трансляция должна идти в момент запуска', 'Не завершайте стрим раньше заказанного времени'],
               ['The stream must be live when the order starts', 'Do not end the stream early']),
    'poll': (['Опрос должен быть открытым', 'Бот спросит номер варианта ответа, если это нужно'],
             ['The poll must be open', 'The bot will ask for the answer number if required']),
    'bot': (['Бот должен быть доступен и отвечать на /start'], ['The bot must be available and respond to /start']),
    'channel': (['Канал должен быть публичным'], ['The channel must be public']),
    'comment': (['Комментарий должен быть виден без авторизации'], ['The comment must be visible without login']),
    'track': (['Трек должен быть доступен для прослушивания'], ['The track must be publicly playable']),
    'page': (['Страница должна открываться без пароля и гео-блокировок'], ['The page must open without passwords or geo-blocks']),
}
KIND_EXTRA_NOTES = {
    'comments': (['Комментарии под публикацией должны быть открыты'], ['Comments must be enabled under the publication']),
    'votes': (['Проверьте, что голосование ещё не закрыто'], ['Make sure the poll is still open']),
    'friends': (['Профиль должен принимать заявки в друзья'], ['The profile must accept friend requests']),
}

# ---------------------------------------------------------------------------
# Темы оформления лотов
# ---------------------------------------------------------------------------
# FunPay показывает обычный текст пропорциональным шрифтом: рамки вида ╔═╗
# «едут», поэтому используются только горизонтальные линии и маркеры слева.
THEMES: Dict[str, Dict[str, Any]] = {
    'premium': {'title': '⭐ Премиум', 'summary': '⭐ {title} ⭐ {badges}', 'joiner': ' • ',
                'sep': '━━━━━━━━━━━━━━━━━━━━', 'bullet': '▸', 'steps': ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣'],
                'get': ('📦 ЧТО ВЫ ПОЛУЧАЕТЕ', '📦 WHAT YOU GET'), 'how': ('📝 КАК ЗАКАЗАТЬ', '📝 HOW TO ORDER'),
                'warn': ('⚠️ ВАЖНО', '⚠️ IMPORTANT'),
                'intro': ('⚡ Автовыдача 24/7 — бот сам примет заказ и запустит услугу',
                          '⚡ 24/7 auto delivery — the bot accepts and starts your order automatically'),
                'footer': ('⭐ Спасибо за выбор! Будем рады вашему отзыву.', '⭐ Thank you for choosing us! Your review is appreciated.')},
    'neon': {'title': '⚡ Неон', 'summary': '⚡ {title} ⚡ {badges}', 'joiner': ' | ',
             'sep': '⚡━━━━━━━━━━━━━━━━━━⚡', 'bullet': '💠', 'steps': ['➊', '➋', '➌', '➍', '➎'],
             'get': ('💠 ЧТО ВХОДИТ', '💠 WHAT IS INCLUDED'), 'how': ('🧭 КАК ЗАКАЗАТЬ', '🧭 HOW TO ORDER'),
             'warn': ('🚨 ОБЯЗАТЕЛЬНО', '🚨 REQUIRED'),
             'intro': ('⚡ МОМЕНТАЛЬНЫЙ АВТОЗАПУСК 24/7', '⚡ INSTANT 24/7 AUTO START'),
             'footer': ('💠 Работаем круглосуточно — заказывайте в любое время!', '💠 We work 24/7 — order any time!')},
    'fire': {'title': '🔥 Яркий', 'summary': '🔥 {title} 🔥 {badges}', 'joiner': ' ✦ ',
             'sep': '🔥━━━━━━━━━━━━━━━━━━🔥', 'bullet': '🔸', 'steps': ['①', '②', '③', '④', '⑤'],
             'get': ('🎁 ВЫ ПОЛУЧИТЕ', '🎁 YOU GET'), 'how': ('🛒 КАК КУПИТЬ', '🛒 HOW TO BUY'), 'warn': ('❗ ВНИМАНИЕ', '❗ ATTENTION'),
             'intro': ('🔥 ГОРЯЧЕЕ ПРЕДЛОЖЕНИЕ • АВТОВЫДАЧА 24/7', '🔥 HOT DEAL • 24/7 AUTO DELIVERY'),
             'footer': ('🔥 Оставьте отзыв — это очень помогает!', '🔥 Leave a review — it helps a lot!')},
    'minimal': {'title': '💎 Минимал', 'summary': '{title} • {badges}', 'joiner': ' • ',
                'sep': '────────────────', 'bullet': '•', 'steps': ['1.', '2.', '3.', '4.', '5.'],
                'get': ('Что входит', 'What is included'), 'how': ('Как заказать', 'How to order'), 'warn': ('Важно', 'Important'),
                'intro': ('Автоматический запуск 24/7.', 'Automatic start 24/7.'),
                'footer': ('Спасибо за заказ!', 'Thank you for your order!')},
    'classic': {'title': '📋 Классика FunPay', 'summary': '【{title}】 {badges}', 'joiner': ' ✔ ',
                'sep': '═══════════════════', 'bullet': '✔️', 'steps': ['1)', '2)', '3)', '4)', '5)'],
                'get': ('🛍 ОПИСАНИЕ ТОВАРА', '🛍 PRODUCT'), 'how': ('📌 ПОРЯДОК ЗАКАЗА', '📌 ORDER STEPS'),
                'warn': ('⛔ ПРАВИЛА', '⛔ RULES'),
                'intro': ('✅ Автоматическая выдача — без ожидания продавца', '✅ Automatic delivery — no waiting for the seller'),
                'footer': ('🤝 Честная работа. Отзыв — лучшая благодарность!', '🤝 Honest service. A review is the best thanks!')},
    'info': {'title': '🎯 Информативный', 'summary': '🎯 {title} • {badges}', 'joiner': ' • ',
             'sep': '▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬', 'bullet': '▪️', 'steps': ['1️⃣', '2️⃣', '3️⃣', '4️⃣', '5️⃣'],
             'get': ('📊 ПАРАМЕТРЫ УСЛУГИ', '📊 SERVICE DETAILS'), 'how': ('🧾 ИНСТРУКЦИЯ', '🧾 INSTRUCTIONS'),
             'warn': ('📌 УСЛОВИЯ', '📌 TERMS'),
             'intro': ('🤖 Заказ обрабатывается ботом автоматически 24/7', '🤖 Orders are processed by the bot 24/7'),
             'footer': ('📈 Статус заказа можно проверить в любой момент командой «чек».',
                        '📈 You can check the order status any time with the "check" command.'), 'details': True},
}
THEME_ORDER = ['premium', 'neon', 'fire', 'minimal', 'classic', 'info']
LEGACY_STYLE_THEME = {'s1': 'premium', 's2': 'neon', 's3': 'classic', 's4': 'info', 's5': 'fire'}

CUSTOM_TEMPLATE_VARS = (
    '{title} {title_en} {platform} {platform_en} {kind} {kind_en} {emoji} {service_name} {clean_name} '
    '{per_piece} {unit} {min_pcs} {max_pcs} {min} {max} {speed} {start} {guarantee} {quality} '
    '{example_link} {link_hint} {requirements} {price} {service_id} {category} {badges} {variant}'
)

DEFAULT_DOMAINS = [
    "t.me", "telegram.me", "vk.com", "instagram.com", "tiktok.com",
    "youtube.com", "youtu.be", "twitch.tv", "twitter.com", "x.com",
    "facebook.com", "threads.net", "kick.com", "discord.com", "discord.gg",
    "ok.ru", "rutube.ru", "spotify.com", "soundcloud.com", "pinterest.com",
    "linkedin.com", "reddit.com", "dzen.ru", "music.yandex.ru", "likee.video",
]

DEFAULT_MESSAGES = {
    "after_payment": "✅ Оплата получена!\n\nЧтобы запустить услугу, отправьте в этот чат корректную ссылку.\n\n📎 Пример: {example_link}\n\n⚠️ Не запускайте эту же услугу параллельно у другого продавца.",
    "after_confirmation": "🚀 Заказ передан в работу!\n\n🔢 Номер заказа в панели: {panel_order_id}\n🔗 Ссылка: {link}\n\n📊 Проверить статус: чек {panel_order_id}",
    "after_completion": "🎉 НАКРУТКА ЗАВЕРШЕНА УСПЕШНО!\n\n✅ Ваш заказ полностью выполнен.\n\n🔢 Номер заказа в панели: {panel_order_id}\n📎 Ссылка: {link}\n\n⭐ Спасибо за заказ! Будем рады вашему отзыву.",
    "refill_info": "♻️ Для этого заказа доступно восстановление списаний (рефилл).\n\nЕсли часть накрутки спишется, отправьте в чат: {command} {order_id}\n🔁 Доступно запусков: {refill_limit}\n⏳ Действует: {refill_period} (до {refill_deadline})",
    "poll_request": "🔢 Пришлите номер варианта ответа в опросе (цифрой, например: 1).",
    "queued": "📥 Заказ #{funpay_id} принят и поставлен в очередь (позиция {position}).\n\nСначала завершим предыдущий заказ — затем бот сам попросит ссылку для этого.",
    "next_in_queue": "➡️ Переходим к следующему заказу #{funpay_order_id}.\n\nОтправьте ссылку для него.\n📎 Пример: {example_link}\n\n📥 Осталось в очереди: {remaining}",
    "order_error": "⚠️ Не удалось запустить заказ в SMM-сервисе. Продавец уже получил уведомление — средства будут возвращены или заказ запустят вручную.",
    "link_reminder": "⏰ Напоминаем: для запуска заказа #{funpay_order_id} отправьте в этот чат ссылку.\n📎 Пример: {example_link}",
}
MESSAGE_INFO = {
    'after_payment': ('После оплаты', '{buyer_username} {funpay_order_id} {example_link} {service_title}'),
    'after_confirmation': ('После запуска', '{panel_order_id} {order_id} {funpay_order_id} {link}'),
    'after_completion': ('После выполнения', '{panel_order_id} {order_id} {funpay_order_id} {link}'),
    'refill_info': ('Рефилл доступен', '{command} {order_id} {funpay_order_id} {link} {refill_limit} {refill_period} {refill_deadline}'),
    'poll_request': ('Запрос номера ответа (опрос)', 'нет'),
    'queued': ('Постановка в очередь', '{funpay_id} {funpay_order_id} {position}'),
    'next_in_queue': ('Следующий заказ из очереди', '{funpay_order_id} {remaining} {example_link}'),
    'order_error': ('Ошибка запуска заказа', 'нет'),
    'link_reminder': ('Напоминание о ссылке', '{funpay_order_id} {example_link}'),
}
_PREVIOUS_DEFAULT_MESSAGES = {
    "after_payment": "❤️ Спасибо за оплату!\n\nПришлите ссылку для запуска услуги.\nПример: {example_link}",
    "after_confirmation": "🎉 Заказ передан в работу!\n\n🔢 Номер: {panel_order_id}\n🔗 Ссылка: {link}\n\nПроверка: <code>чек {panel_order_id}</code>",
}
_PREVIOUS_CONFIRMATION_TEXTS = {
    _PREVIOUS_DEFAULT_MESSAGES['after_confirmation'],
    "🚀 Заказ передан в работу!\n\n🔢 Номер заказа в панели: {panel_order_id}\n🔗 Ссылка: {link}\n\nДля проверки отправьте: чек {panel_order_id}",
    "🚀 Заказ передан в работу!\n\n🔢 Номер заказа в панели: {panel_order_id}\n🔗 Ссылка: {link}\n\n📊 Проверить статус: чек {panel_order_id}\n🔄 Запросить refill: рефилл {panel_order_id}",
}

GOOD_STATUSES = {'completed', 'complete', 'done', 'success'}
BAD_STATUSES = {'failed', 'error', 'canceled', 'cancelled', 'fail', 'refunded_by_panel'}
REVIEW_STATUSES = {'submission_unknown', 'awaiting_refund', 'refund_unknown', 'partial'}
YES_WORDS = {'+', 'да', 'yes', 'y', 'ok', 'ок', 'okay', 'go', 'start', 'старт', 'подтверждаю', 'верно', 'запуск'}
NO_WORDS = {'-', 'нет', 'no', 'n', 'stop', 'стоп', 'cancel', 'отмена'}
REFILL_RE = re.compile(r'^(?:!|/)?(?:рефилл|refill)(?:\s+#?(\d+))?$', re.I)


# ---------------------------------------------------------------------------
# Общие утилиты
# ---------------------------------------------------------------------------

class PanelError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = False, status_code: Optional[int] = None):
        super().__init__(message)
        self.uncertain = uncertain
        self.status_code = status_code


class LotPublishError(RuntimeError):
    def __init__(self, message: str, *, stop_batch: bool = False):
        super().__init__(message)
        self.stop_batch = stop_batch


class ConfigGuardError(RuntimeError):
    """Запись отменена: она стёрла бы все панели или лоты."""


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(str(value).replace(",", ".").replace(' ', ''))
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        try:
            number = float(str(value).replace(',', '.'))
            return int(number) if math.isfinite(number) else default
        except (ValueError, TypeError):
            return default


def _markup(value: Any) -> float:
    """Валидирует наценку в процентах до сохранения."""
    number = _float(value, float("nan"))
    if not math.isfinite(number) or number < -99 or number > 10000:
        raise ValueError('Наценка должна быть числом от -99 до 10 000.')
    return number


def _money(value: float) -> str:
    if value >= 1 or value == 0:
        return f'{value:.2f}'
    return f'{value:.6f}'.rstrip('0').rstrip('.')


def _plural(number: Any, forms: Tuple[str, str, str]) -> str:
    n = abs(_int(number)) % 100
    if 11 <= n <= 19:
        return forms[2]
    n %= 10
    return forms[0] if n == 1 else forms[1] if 2 <= n <= 4 else forms[2]


def _plural_en(number: Any, forms: Tuple[str, str]) -> str:
    return forms[0] if _int(number) == 1 else forms[1]


def _human_number(value: Any) -> str:
    number = _int(value)
    return f'{number:,}'.replace(',', ' ') if number else str(value)


def _clip_units(text: str, limit: int) -> str:
    """Обрезает по UTF-16 единицам, как считает браузер FunPay."""
    return text.encode('utf-16-le')[:max(0, limit) * 2].decode('utf-16-le', errors='ignore')


def _utf16_len(text: str) -> int:
    return len(text.encode('utf-16-le')) // 2


def _summary_field(text: str, limit: int) -> str:
    text = re.sub(r'\s+', ' ', text).strip()
    if _utf16_len(text) <= limit:
        return text
    clipped = _clip_units(text, limit - 1)
    if ' ' in clipped:
        clipped = clipped.rsplit(' ', 1)[0]
    return clipped.rstrip('.,;:•|✦ ') + '…'


def _short(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    if len(text) <= limit:
        return text
    cut = text[:max(1, limit - 1)].rsplit(" ", 1)[0].strip()
    return (cut or text[:limit - 1]).rstrip(".,;: ") + "…"


def _plain(text: Any) -> str:
    """FunPay показывает обычный текст: HTML-теги покупателю не отправляем."""
    return unescape(re.sub(r'<[^>]*>', '', str(text or ''))).strip()


def _format(template: str, values: Dict[str, Any]) -> str:
    class Safe(dict):
        def __missing__(self, key):
            return '{' + key + '}'
    try:
        return str(template).format_map(Safe({k: '' if v is None else str(v) for k, v in values.items()}))
    except (ValueError, AttributeError, IndexError, KeyError):
        result = str(template)
        for key, value in values.items():
            result = result.replace('{' + key + '}', '' if value is None else str(value))
        return result


def _translit(text: str) -> str:
    pairs = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
             "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
             "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"}
    pairs.update({key.upper(): value.title() for key, value in list(pairs.items())})
    return re.sub(r"[ \t]+", " ", "".join(pairs.get(char, char) for char in text)).strip() or "SMM service"


def _api_url(value: Any) -> str:
    """Проверяет адрес API без попыток угадать путь панели."""
    value = str(value or '').strip().rstrip('?')
    try:
        parsed = urlparse(value)
        valid = (parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and
                 not parsed.password and not parsed.fragment and not re.search(r'\s', value))
        parsed.port
        if any(key.lower() in {'key', 'api_key', 'token', 'action'} for key, _ in parse_qsl(parsed.query)):
            raise ValueError('Укажите адрес API без ключа и action в URL. Ключ вводится отдельно.')
    except (ValueError, TypeError) as exc:
        if str(exc).startswith('Укажите адрес API'):
            raise
        valid = False
    if not valid:
        raise ValueError('Укажите полный API URL из документации панели, например https://example.com/api/v2.')
    return value


def _api_url_warning(value: str) -> str:
    path = urlparse(value).path.strip('/')
    if not path:
        return ('⚠️ В адресе нет пути до API. Обычно он выглядит так: https://example.com/api/v2. '
                'Если у вашей панели другой формат — проигнорируйте предупреждение.')
    return ''


def _display_url(value: Any) -> str:
    try:
        parsed = urlparse(str(value))
        host = parsed.hostname or ''
        if ':' in host:
            host = '[' + host + ']'
        if parsed.port:
            host += ':' + str(parsed.port)
        return urlunparse((parsed.scheme, host, parsed.path, '', '', ''))
    except ValueError:
        return '[неверный URL]'


def _secret_values(cfg: Dict[str, Any]) -> List[str]:
    values = []
    for panel in cfg.get('services', {}).values():
        if not isinstance(panel, dict):
            continue
        values.append(str(panel.get('api_key') or ''))
        values.append(str((panel.get('proxy') or {}).get('url') or ''))
    values.append(str((cfg.get('proxy') or {}).get('url') or ''))
    result = []
    for value in values:
        if not value:
            continue
        result.append(value)
        try:
            parsed = urlparse(value if '://' in value else 'http://' + value)
            result.extend([parsed.username or '', parsed.password or ''])
        except ValueError:
            pass
    return [x for x in result if len(x) >= 3]


def _safe_detail(value: Any, cfg: Optional[Dict[str, Any]] = None, limit: int = 240) -> str:
    text = unescape(str(value))
    cfg = cfg if cfg is not None else load_config()
    for secret in sorted(set(_secret_values(cfg)), key=len, reverse=True):
        for variant in {secret, quote(secret, safe=''), quote_plus(secret)}:
            text = text.replace(variant, '[скрыто]')
    text = re.sub(r'https?://[^\s<>"\']+', lambda match: _display_url(match.group(0)), text)
    text = re.sub(r'(?i)\b(key|api_key|token|password)\s*[=:]\s*[^\s,;]+', r'\1=[скрыто]', text)
    text = re.sub(r'<[^>]*>', '', text)
    return re.sub(r'[ \t]+', ' ', text).strip()[:limit]


def _domains(values: Any) -> List[str]:
    if isinstance(values, str):
        values = re.split(r"[,\s]+", values)
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        item = str(value).lower().strip().replace("https://", "").replace("http://", "").split("/")[0].split('?')[0]
        if item == '*':
            return ['*']
        item = item.split(':')[0]
        if item.startswith("www."):
            item = item[4:]
        if item and '.' in item and re.fullmatch(r"[a-z0-9.-]+", item) and item not in result:
            result.append(item)
    return result[:40]


def _order_lock(funpay_id: Any) -> threading.Lock:
    with _order_locks_guard:
        key = str(funpay_id)
        if key not in _order_locks:
            _order_locks[key] = threading.Lock()
        return _order_locks[key]


def _submit(func: Callable, *args: Any, **kwargs: Any) -> None:
    """Сетевые операции выполняются вне потока событий FunPay и Telegram."""
    def run():
        try:
            func(*args, **kwargs)
        except Exception:
            logger.exception('[AutoSMM] Background task %s failed', getattr(func, '__name__', func))
    try:
        _executor.submit(run)
    except RuntimeError:
        threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# Хранилище: атомарная запись, бэкапы, восстановление
# ---------------------------------------------------------------------------

_last_backup: Dict[str, float] = {}


def _json_load(path: str) -> Tuple[str, Any]:
    if not os.path.exists(path):
        return 'missing', None
    with _lock:
        try:
            with open(path, encoding='utf-8') as file:
                return 'ok', json.load(file)
        except (OSError, ValueError, TypeError, UnicodeDecodeError):
            return 'corrupt', None


def _atomic_write(path: str, data: Any) -> None:
    with _lock:
        temporary = path + '.tmp'
        with open(temporary, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            try:
                os.fsync(file.fileno())
            except OSError:
                pass
        os.replace(temporary, path)


def _backup(path: str, force: bool = False) -> None:
    """Хранит до 15 копий каждого файла не чаще раза в 10 минут."""
    if not os.path.exists(path):
        return
    now = time.time()
    if not force and now - _last_backup.get(path, 0) < 600:
        return
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        base = os.path.splitext(os.path.basename(path))[0]
        target = os.path.join(BACKUP_DIR, f'{base}-{datetime.now().strftime("%Y%m%d-%H%M%S")}.json')
        shutil.copy2(path, target)
        _last_backup[path] = now
        copies = sorted(x for x in os.listdir(BACKUP_DIR) if x.startswith(base + '-') and x.endswith('.json'))
        for name in copies[:-15]:
            os.remove(os.path.join(BACKUP_DIR, name))
    except OSError as exc:
        logger.warning('[AutoSMM] Backup of %s failed: %s', path, exc)


def _backups(path: str) -> List[str]:
    if not os.path.isdir(BACKUP_DIR):
        return []
    base = os.path.splitext(os.path.basename(path))[0]
    names = sorted((x for x in os.listdir(BACKUP_DIR) if x.startswith(base + '-') and x.endswith('.json')), reverse=True)
    return [os.path.join(BACKUP_DIR, x) for x in names]


def _load_recovering(path: str, default: Any, expected: type, label: str) -> Any:
    """Повреждённый файл не перезаписывается дефолтом: он сохраняется рядом,
    а данные восстанавливаются из последнего рабочего бэкапа."""
    status, data = _json_load(path)
    if status == 'ok' and isinstance(data, expected):
        return data
    if status == 'missing':
        return deepcopy(default)
    corrupt = f'{path}.corrupt-{datetime.now().strftime("%Y%m%d-%H%M%S")}'
    try:
        shutil.copy2(path, corrupt)
    except OSError:
        corrupt = path
    for candidate in _backups(path):
        state, value = _json_load(candidate)
        if state == 'ok' and isinstance(value, expected):
            _atomic_write(path, value)
            _admin_later(f'♻️ AutoSMM: {label} повреждён и восстановлен из бэкапа '
                         f'{os.path.basename(candidate)}. Повреждённый файл сохранён: {os.path.basename(corrupt)}.')
            return value
    _admin_later(f'🛑 AutoSMM: {label} повреждён, а рабочего бэкапа нет. Файл сохранён как '
                 f'{os.path.basename(corrupt)} — данные из него можно восстановить вручную.')
    return deepcopy(default)


# ---------------------------------------------------------------------------
# Настройки: схема 14
# ---------------------------------------------------------------------------

def _default_pricing() -> Dict[str, Any]:
    return {'enabled': False, 'mode': 'percent', 'percent': 30.0, 'fixed': 10.0, 'interval': 60,
            'usd_mode': 'auto', 'usd_rate': 95.0, 'notify': True, 'min_change': 0.5}


def _default_config() -> Dict[str, Any]:
    return {
        'schema_version': SCHEMA_VERSION, 'services': {}, 'lot_mapping': {},
        'auto_refunds': True, 'confirm_link': True, 'domain_check': True, 'anti_dumping': True, 'min_margin': 0.0,
        'api_timeout': 45, 'poll_interval': 120, 'notification_chat_id': None, 'auto_start': True,
        'notify_new_order': False, 'watcher_enabled': True, 'respect_blacklist': True,
        'watcher_notify': 'important', 'watcher_threshold': 5.0, 'watcher_autoprice': True,
        'early_complete_minutes': 5, 'link_hint_minutes': 10, 'remind_hours': 6, 'stale_hours': 24,
        'summary_limit': 80, 'description_limit': 5000,
        'proxy': {'mode': 'bot', 'url': ''},
        'pricing': _default_pricing(),
        'refill': {'send_moment': 'after_start', 'notify': True, 'auto_enable': False},
        'messages': deepcopy(DEFAULT_MESSAGES), 'message_template_version': 6,
        'paused_buyers': [],
        'design': {'theme': 'premium', 'custom': {}, 'assign': {}, 'sections': {}, 'english': True, 'title_source': 'smart'},
        'batches': {},
    }


def _new_tag(existing: Iterable[str] = ()) -> str:
    used = set(existing)
    while True:
        tag = secrets.token_hex(4)
        if tag not in used:
            return tag


def _panel(panel_id: str, value: Any) -> Dict[str, Any]:
    """Нормализует панель, сохраняя все дополнительные поля."""
    value = dict(value) if isinstance(value, dict) else {}
    method = str(value.get("request_method", "POST")).upper()
    proxy = value.get('proxy') if isinstance(value.get('proxy'), dict) else {}
    mode = str(proxy.get('mode') or 'plugin')
    value.update({
        "name": str(value.get("name") or f"Панель #{panel_id}")[:48],
        "api_url": str(value.get("api_url", "")).strip().rstrip("?"),
        "api_key": str(value.get("api_key", "")).strip(),
        "discount": max(-100.0, min(100.0, _float(value.get("discount")))),
        "request_method": method if method in ("GET", "POST") else "POST",
        "enabled": bool(value.get("enabled", True)),
        "min_balance": max(0.0, _float(value.get('min_balance'))),
        "proxy": {'mode': mode if mode in ('plugin', 'bot', 'custom', 'direct') else 'plugin',
                  'url': str(proxy.get('url') or '').strip()},
    })
    return value


def _refill_settings(value: Any) -> Dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    return {'enabled': bool(value.get('enabled', False)), 'limit_enabled': bool(value.get('limit_enabled', True)),
            'limit': max(1, _int(value.get('limit'), 1)), 'period_enabled': bool(value.get('period_enabled', True)),
            'hours': max(1, _int(value.get('hours'), 168))}


def _lot(key: str, value: Any) -> Dict[str, Any]:
    """Нормализует лот, сохраняя все дополнительные поля."""
    value = dict(value) if isinstance(value, dict) else {}
    markup_mode = str(value.get('markup_mode') or 'global')
    value.update({
        "name": str(value.get("name") or key)[:200],
        "funpay_lot_id": _int(value.get("funpay_lot_id", value.get("lot_id"))) or None,
        "service_id": str(value.get("service_id", "")).strip(),
        "quantity": max(1, _int(value.get("quantity"), 1)),
        "panel_id": str(value.get("panel_id", value.get("service_number", "1"))),
        "domains": _domains(value.get("domains", value.get("allowed_domains", []))),
        "created_at": _float(value.get("created_at")) or 0.0,
        "source": str(value.get('source') or 'legacy'),
        "subcategory": _int(value.get('subcategory')) or None,
        "batch_id": str(value.get('batch_id') or ''),
        "tag": str(value.get('tag') or '').lower(),
        "tag_written": bool(value.get('tag_written', False)),
        "poll": bool(value.get('poll', False)),
        "autoprice": bool(value.get('autoprice', True)),
        "markup_mode": markup_mode if markup_mode in ('global', 'percent', 'fixed') else 'global',
        "markup_value": _float(value.get('markup_value')),
        "refill": _refill_settings(value.get('refill')),
        "template": str(value.get('template') or ''),
        "platform": str(value.get('platform') or ''),
        "kind": str(value.get('kind') or ''),
        "last_price": _float(value.get('last_price')) or None,
        "stock_target": max(0, _int(value.get('stock_target'))),
    })
    return value


def _normalize_config(cfg: Any) -> Tuple[Dict[str, Any], bool]:
    """Миграция со всех прошлых версий. Возвращает (настройки, изменились ли)."""
    if not isinstance(cfg, dict):
        cfg = {}
    original = deepcopy(cfg)
    defaults = _default_config()
    old_version = _int(cfg.get('schema_version'))
    message_version = _int(cfg.get('message_template_version'))
    for key, value in defaults.items():
        if key not in cfg:
            cfg[key] = deepcopy(value)
    if old_version < 13 and _int(cfg.get('summary_limit')) in (80, 120):
        cfg['summary_limit'] = 80
    # proxies {enabled, url} -> proxy {mode, url}: прежнее «выключено» значило «напрямую».
    old_proxy = cfg.pop('proxies', None)
    if isinstance(old_proxy, dict) and old_version < 14:
        url = str(old_proxy.get('url') or '').strip()
        cfg['proxy'] = {'mode': 'custom' if old_proxy.get('enabled') and url else 'direct', 'url': url}
    if old_version and old_version < 14 and 'poll_interval' in original:
        cfg['poll_interval'] = original['poll_interval']
    proxy = cfg.get('proxy') if isinstance(cfg.get('proxy'), dict) else {}
    mode = str(proxy.get('mode') or 'bot')
    cfg['proxy'] = {'mode': mode if mode in ('bot', 'custom', 'direct') else 'bot', 'url': str(proxy.get('url') or '').strip()}
    pricing = cfg.get('pricing') if isinstance(cfg.get('pricing'), dict) else {}
    cfg['pricing'] = {**_default_pricing(), **pricing}
    cfg['pricing']['interval'] = max(15, _int(cfg['pricing'].get('interval'), 60))
    refill = cfg.get('refill') if isinstance(cfg.get('refill'), dict) else {}
    cfg['refill'] = {**defaults['refill'], **refill}
    design = cfg.get('design') if isinstance(cfg.get('design'), dict) else {}
    cfg['design'] = {**defaults['design'], **design}
    for key in ('custom', 'assign', 'sections'):
        if not isinstance(cfg['design'].get(key), dict):
            cfg['design'][key] = {}
    if cfg['design'].get('theme') not in THEMES and not str(cfg['design'].get('theme', '')).startswith('custom:'):
        cfg['design']['theme'] = 'premium'
    if not isinstance(cfg.get('services'), dict):
        cfg['services'] = {}
    if not isinstance(cfg.get('lot_mapping'), dict):
        cfg['lot_mapping'] = {}
    if not isinstance(cfg.get('batches'), dict):
        cfg['batches'] = {}
    if not isinstance(cfg.get('paused_buyers'), list):
        cfg['paused_buyers'] = []
    cfg['services'] = {str(k): _panel(str(k), v) for k, v in cfg['services'].items()}
    lots = {str(k): _lot(str(k), v) for k, v in cfg['lot_mapping'].items()}
    used_tags = {x['tag'] for x in lots.values() if re.fullmatch(r'[0-9a-f]{8}', x['tag'])}
    for item in lots.values():
        if not re.fullmatch(r'[0-9a-f]{8}', item['tag']):
            item['tag'] = _new_tag(used_tags)
            item['tag_written'] = False
            used_tags.add(item['tag'])
        if not item['created_at']:
            item['created_at'] = time.time()
    cfg['lot_mapping'] = lots
    if not isinstance(cfg.get('messages'), dict):
        cfg['messages'] = deepcopy(DEFAULT_MESSAGES)
    for key, value in DEFAULT_MESSAGES.items():
        if not isinstance(cfg['messages'].get(key), str) or not cfg['messages'][key].strip():
            cfg['messages'][key] = value
    if message_version < 2:
        for key, old_value in _PREVIOUS_DEFAULT_MESSAGES.items():
            if cfg['messages'].get(key) == old_value:
                cfg['messages'][key] = DEFAULT_MESSAGES[key]
    if message_version < 6 and cfg['messages'].get('after_confirmation') in _PREVIOUS_CONFIRMATION_TEXTS:
        cfg['messages']['after_confirmation'] = DEFAULT_MESSAGES['after_confirmation']
    cfg['message_template_version'] = 6
    for key in ('auto_refunds', 'confirm_link', 'domain_check', 'anti_dumping', 'auto_start', 'notify_new_order',
                'watcher_enabled', 'respect_blacklist', 'watcher_autoprice'):
        cfg[key] = bool(cfg.get(key))
    if cfg.get('watcher_notify') not in ('important', 'all', 'off'):
        cfg['watcher_notify'] = 'important'
    cfg['watcher_threshold'] = max(0.0, min(1000.0, _float(cfg.get('watcher_threshold'), 5.0)))
    cfg['poll_interval'] = max(20, min(3600, _int(cfg.get('poll_interval'), 120)))
    cfg['api_timeout'] = max(5, min(180, _int(cfg.get('api_timeout'), 45)))
    cfg['early_complete_minutes'] = max(0, min(1440, _int(cfg.get('early_complete_minutes'), 5)))
    cfg['schema_version'] = SCHEMA_VERSION
    return cfg, cfg != original


_cfg_cache: Optional[Dict[str, Any]] = None


def load_config() -> Dict[str, Any]:
    """Возвращает копию настроек. Изменять настройки — только через config_tx()."""
    global _cfg_cache
    with _cfg_lock:
        if _cfg_cache is None:
            raw = _load_recovering(CONFIG_PATH, {}, dict, 'Файл настроек AutoSMM')
            cfg, changed = _normalize_config(raw)
            if changed or not os.path.exists(CONFIG_PATH):
                if os.path.exists(CONFIG_PATH):
                    _backup(CONFIG_PATH, force=True)
                _atomic_write(CONFIG_PATH, cfg)
            _cfg_cache = cfg
        return deepcopy(_cfg_cache)


def _commit_config(cfg: Dict[str, Any], allow_shrink: bool = False) -> None:
    global _cfg_cache
    with _cfg_lock:
        cfg, _ = _normalize_config(cfg)
        old = _cfg_cache or {}
        if not allow_shrink:
            for key, label in (('lot_mapping', 'лоты'), ('services', 'панели')):
                if old.get(key) and not cfg.get(key):
                    _notify_admins(f'🛑 AutoSMM: предотвращена потеря настроек — запись удалила бы все {label}. '
                                   'Изменение отменено, ничего не потеряно.')
                    raise ConfigGuardError(f'Запись удалила бы все {label}.')
        _backup(CONFIG_PATH)
        _atomic_write(CONFIG_PATH, cfg)
        _cfg_cache = cfg


@contextmanager
def config_tx(allow_shrink: bool = False):
    """Атомарная транзакция «прочитать → изменить → записать»."""
    with _cfg_lock:
        cfg = load_config()
        yield cfg
        _commit_config(cfg, allow_shrink)


def _save_config(cfg: Dict[str, Any], allow_shrink: bool = False) -> None:
    _commit_config(cfg, allow_shrink)


def _lot_by_key(key: str) -> Optional[Dict[str, Any]]:
    item = load_config()['lot_mapping'].get(key)
    return item


def _lot_key_by_funpay_id(cfg: Dict[str, Any], funpay_id: Any) -> Optional[str]:
    return next((k for k, v in cfg['lot_mapping'].items() if v.get('funpay_lot_id') and _int(v['funpay_lot_id']) == _int(funpay_id)), None)


# ---------------------------------------------------------------------------
# История заказов, ожидающие заказы и служебное состояние
# ---------------------------------------------------------------------------

_orders_cache: Optional[List[Dict[str, Any]]] = None


def _orders_raw() -> List[Dict[str, Any]]:
    global _orders_cache
    with _orders_lock:
        if _orders_cache is None:
            value = _load_recovering(ORDERS_PATH, [], list, 'История заказов AutoSMM')
            if not os.path.exists(ORDERS_PATH) and os.path.exists(LEGACY_ORDERS_PATH):
                status, legacy = _json_load(LEGACY_ORDERS_PATH)
                if status == 'ok' and isinstance(legacy, list):
                    value = [{'funpay_order_id': str(x.get('order_id', '')), 'chat_id': x.get('chat_id'),
                              'panel_id': str(x.get('service_number', 1)), 'service_id': str(x.get('service_id', '')),
                              'panel_order_id': str(x.get('id_zakaz', '')), 'status': x.get('status', 'pending'),
                              'price': _float(x.get('summa')), 'cost': _float(x.get('spent')),
                              'quantity': _int(x.get('quantity'), 1), 'link': x.get('customer_url', ''),
                              'created_at': _float(x.get('created_at'), time.time()),
                              'is_refunded': bool(x.get('is_refunded', False))}
                             for x in legacy if isinstance(x, dict) and x.get('order_id')]
                    _atomic_write(ORDERS_PATH, value)
                    logger.info('[AutoSMM] Migrated %s legacy orders.', len(value))
            _orders_cache = [x for x in value if isinstance(x, dict)]
        return _orders_cache


def _persist_orders() -> None:
    with _orders_lock:
        _backup(ORDERS_PATH)
        _atomic_write(ORDERS_PATH, _orders_raw())


def _orders() -> List[Dict[str, Any]]:
    with _orders_lock:
        return deepcopy(_orders_raw())


def _get_order(funpay_id: Any) -> Optional[Dict[str, Any]]:
    with _orders_lock:
        item = next((x for x in _orders_raw() if str(x.get('funpay_order_id')) == str(funpay_id)), None)
        return deepcopy(item) if item else None


def _add_order(order: Dict[str, Any]) -> bool:
    with _orders_lock:
        if any(str(x.get('funpay_order_id')) == str(order['funpay_order_id']) for x in _orders_raw()):
            return False
        _orders_raw().append(deepcopy(order))
        _persist_orders()
        return True


def _update_order(funpay_id: Any, **updates: Any) -> Optional[Dict[str, Any]]:
    with _orders_lock:
        item = next((x for x in _orders_raw() if str(x.get('funpay_order_id')) == str(funpay_id)), None)
        if item is None:
            return None
        changed = any(item.get(k) != v for k, v in updates.items())
        if changed:
            item.update(deepcopy(updates))
            _persist_orders()
        return deepcopy(item)


def _save_waiting() -> None:
    with _wlock:
        snapshot = deepcopy(waiting)
    _atomic_write(WAITING_PATH, snapshot)


def _load_waiting() -> Dict[str, Dict[str, Any]]:
    value = _load_recovering(WAITING_PATH, {}, dict, 'Список ожидающих заказов AutoSMM')
    if not os.path.exists(WAITING_PATH) and os.path.exists(LEGACY_WAITING_PATH):
        status, legacy = _json_load(LEGACY_WAITING_PATH)
        migrated: Dict[str, Dict[str, Any]] = {}
        if status == 'ok' and isinstance(legacy, dict):
            for key, item in legacy.items():
                if not isinstance(item, dict):
                    continue
                step = {'await_link': 'link', 'await_confirm': 'confirm'}.get(str(item.get('step')), str(item.get('step') or 'link'))
                migrated[str(key)] = {'buyer_id': item.get('buyer_id'), 'chat_id': item.get('chat_id'),
                                      'funpay_order_id': str(item.get('order_id_funpay', key)),
                                      'panel_id': str(item.get('service_number', 1)), 'service_id': str(item.get('service_id', '')),
                                      'quantity': _int(item.get('real_amount'), 1), 'price': _float(item.get('price')),
                                      'domains': _domains(item.get('allowed_domains', [])), 'step': step,
                                      **({'link': item['link']} if item.get('link') else {})}
        value = migrated
        _atomic_write(WAITING_PATH, value)
    now = time.time()
    for item in value.values():
        if isinstance(item, dict):
            item.setdefault('created_at', now)
            item.setdefault('queued', False)
    return {str(k): v for k, v in value.items() if isinstance(v, dict)}


_state_cache: Optional[Dict[str, Any]] = None


def _state_data() -> Dict[str, Any]:
    global _state_cache
    with _state_lock:
        if _state_cache is None:
            value = _load_recovering(STATE_PATH, {}, dict, 'Служебное состояние AutoSMM')
            for key, default in (('sales_off', []), ('balance_off', {}), ('balance_low', {}), ('watch', {}),
                                 ('watch_missing', {}), ('fx', {}), ('last_full_sync', 0), ('balances', {})):
                value.setdefault(key, deepcopy(default))
            _state_cache = value
        return _state_cache


def _state_get(key: str, default: Any = None) -> Any:
    with _state_lock:
        return deepcopy(_state_data().get(key, default))


def _state_set(key: str, value: Any) -> None:
    with _state_lock:
        _state_data()[key] = deepcopy(value)
        _atomic_write(STATE_PATH, _state_data())


def _global_domains() -> List[str]:
    status, value = _json_load(DOMAINS_PATH)
    result = _domains(value) if status == 'ok' else []
    return result or list(DEFAULT_DOMAINS)


def _save_global_domains(values: List[str]) -> None:
    _atomic_write(DOMAINS_PATH, _domains(values) or list(DEFAULT_DOMAINS))


# ---------------------------------------------------------------------------
# Прокси
# ---------------------------------------------------------------------------

def _normalize_proxy(value: str) -> str:
    """Принимает login:pass@ip:port, ip:port, ip:port:login:pass и URL со схемой."""
    value = str(value or '').strip()
    if not value:
        raise ValueError('Прокси не указан.')
    if '://' not in value:
        parts = value.split(':')
        if '@' not in value and len(parts) == 4 and parts[1].isdigit():
            value = f'{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}'
        value = 'http://' + value
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https', 'socks5', 'socks5h', 'socks4'):
        raise ValueError('Поддерживаются схемы http, https, socks5, socks4.')
    try:
        port = parsed.port
    except ValueError:
        raise ValueError('Неверный порт прокси.') from None
    if not parsed.hostname or not port:
        raise ValueError('Укажите прокси в формате логин:пароль@адрес:порт или адрес:порт.')
    return value


def _mask_proxy(value: str) -> str:
    if not value:
        return 'не задан'
    try:
        parsed = urlparse(value)
        auth = f'{parsed.username}:***@' if parsed.username else ''
        return f'{parsed.scheme}://{auth}{parsed.hostname}:{parsed.port}'
    except ValueError:
        return '[неверный прокси]'


def _bot_proxy() -> Optional[str]:
    c = cardinal_instance
    for source in (getattr(getattr(c, 'account', None), 'proxy', None), getattr(c, 'proxy', None)):
        if isinstance(source, dict) and (source.get('https') or source.get('http')):
            return str(source.get('https') or source.get('http'))
    return None


def _resolve_proxy(cfg: Dict[str, Any], panel: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], str]:
    """Возвращает (url или None, описание способа подключения)."""
    mode, url, origin = 'plugin', '', ''
    if panel is not None:
        mode, url = panel.get('proxy', {}).get('mode', 'plugin'), panel.get('proxy', {}).get('url', '')
    if mode == 'plugin':
        mode, url, origin = cfg['proxy']['mode'], cfg['proxy']['url'], ' (как у плагина)'
    if mode == 'bot':
        proxy = _bot_proxy()
        return proxy, (f'прокси бота {_mask_proxy(proxy)}' if proxy else 'напрямую (у бота нет прокси)') + origin
    if mode == 'custom':
        if not url:
            return None, 'напрямую (свой прокси не задан)' + origin
        return url, f'свой прокси {_mask_proxy(url)}' + origin
    return None, 'напрямую' + origin


def _proxy_kwargs(proxy: Optional[str]) -> Dict[str, Any]:
    return {'proxies': {'http': proxy, 'https': proxy}} if proxy else {}


def _test_proxy(proxy: Optional[str]) -> str:
    try:
        response = requests.get('https://api.ipify.org?format=json', timeout=(8, 12), **_proxy_kwargs(proxy))
        ip = response.json().get('ip')
        return f'✅ Работает. Внешний IP: {ip}'
    except requests.exceptions.ProxyError:
        return '❌ Прокси не отвечает или отклонил авторизацию.'
    except requests.exceptions.InvalidSchema:
        return '❌ Для SOCKS-прокси на сервере нужен пакет PySocks (pip install pysocks).'
    except requests.Timeout:
        return '❌ Таймаут: прокси или сеть не ответили вовремя.'
    except Exception as exc:
        return f'❌ Ошибка проверки: {type(exc).__name__}'


# ---------------------------------------------------------------------------
# Курсы валют
# ---------------------------------------------------------------------------

def _cbr_rates() -> Dict[str, float]:
    fx = _state_get('fx', {}) or {}
    if fx.get('rates') and time.time() - _float(fx.get('ts')) < 4 * 3600:
        return fx['rates']
    cfg = load_config()
    try:
        response = requests.get('https://www.cbr-xml-daily.ru/daily_json.js', timeout=(8, 15),
                                **_proxy_kwargs(_resolve_proxy(cfg)[0]))
        valutes = response.json().get('Valute', {})
        rates = {code: _float(item.get('Value')) / max(1, _int(item.get('Nominal'), 1))
                 for code, item in valutes.items() if _float(item.get('Value')) > 0}
        if rates:
            _state_set('fx', {'ts': time.time(), 'rates': rates})
            return rates
    except Exception as exc:
        logger.warning('[AutoSMM] CBR rates unavailable: %s', type(exc).__name__)
    return fx.get('rates') or {}


def _fx_to_rub(currency: str, cfg: Optional[Dict[str, Any]] = None) -> float:
    currency = str(currency or 'RUB').upper().strip()
    if currency in ('', 'RUB', 'RUR', '₽'):
        return 1.0
    cfg = cfg or load_config()
    if currency == 'USD' and cfg['pricing'].get('usd_mode') == 'manual':
        rate = _float(cfg['pricing'].get('usd_rate'))
        if rate > 0:
            return rate
    rate = _cbr_rates().get(currency)
    if not rate:
        if currency == 'USD' and _float(cfg['pricing'].get('usd_rate')) > 0:
            return _float(cfg['pricing']['usd_rate'])
        raise PanelError(f'Нет курса для валюты {currency}. Проверьте доступ к cbr-xml-daily.ru или задайте ручной курс USD.')
    return rate


def _usd_rate_text(cfg: Dict[str, Any]) -> str:
    try:
        rate = _fx_to_rub('USD', cfg)
        return f'{rate:.2f} ₽' + (' (ручной)' if cfg['pricing'].get('usd_mode') == 'manual' else ' (ЦБ РФ)')
    except PanelError:
        return 'нет данных'


# ---------------------------------------------------------------------------
# SMM Panel API v2
# ---------------------------------------------------------------------------

def _panel_quirks(panel: Dict[str, Any]) -> Dict[str, Any]:
    host = (urlparse(str(panel.get('api_url') or '')).hostname or '').lower()
    if 'smmbind' in host:
        return {'early_minutes': 15, 'confirmations': 2, 'force_method': 'POST'}
    return {'early_minutes': 0, 'confirmations': 1, 'force_method': ''}


class PanelAPI:
    def __init__(self, cfg: Dict[str, Any], panel_id: Any):
        self.id, self.cfg = str(panel_id), cfg
        self.data = cfg['services'].get(self.id)
        if not self.data:
            raise PanelError(f'Панель #{self.id} не найдена. Проверьте сервис в карточке лота.')
        if not self.data.get('enabled', True):
            raise PanelError(f'Панель «{self.data.get("name")}» выключена.')
        try:
            self.url = _api_url(self.data.get('api_url'))
        except ValueError as exc:
            raise PanelError(str(exc)) from None
        if not self.data['api_key'] or self.data['api_key'] == 'YOUR_API_KEY':
            raise PanelError('Для панели не задан API-ключ.')
        self.proxy = _resolve_proxy(cfg, self.data)[0]
        self.quirks = _panel_quirks(self.data)

    def _error(self, message: str, action: str, *, uncertain: bool = False, status_code: Optional[int] = None) -> PanelError:
        message = _safe_detail(message, self.cfg, 650)
        logger.warning('[AutoSMM] Panel #%s action=%s: %s', self.id, action, message)
        return PanelError(message, uncertain=uncertain, status_code=status_code)

    def _api_error(self, value: Any) -> str:
        detail = _safe_detail(value, self.cfg)
        hints = {
            'user_inactive': 'Проверьте API-ключ, активность аккаунта и доступ к API в кабинете панели.',
            'invalid_api_key': 'Проверьте API-ключ в кабинете панели.', 'invalid key': 'Проверьте API-ключ в кабинете панели.',
            'incorrect api key': 'Проверьте API-ключ в кабинете панели.',
            'not enough funds': 'Пополните баланс SMM-панели.', 'insufficient funds': 'Пополните баланс SMM-панели.',
        }
        return detail + (' — ' + hints[detail.lower()] if detail.lower() in hints else '')

    def _request(self, method: str, data: Dict[str, Any], options: Dict[str, Any], read_only: bool) -> requests.Response:
        url = self.url
        for redirect in range(4):
            response = (requests.get(url, params=data, **options) if method == 'GET' else requests.post(url, data=data, **options))
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get('Location', '')
            target = urljoin(url, location)
            old, new = urlparse(url), urlparse(target)
            try:
                target = _api_url(target)
                same_origin = (old.hostname == new.hostname and
                               (old.scheme, old.port or (443 if old.scheme == 'https' else 80)) ==
                               (new.scheme, new.port or (443 if new.scheme == 'https' else 80)))
                upgrade = (old.scheme == 'http' and new.scheme == 'https' and old.hostname == new.hostname
                           and old.port in (None, 80) and new.port in (None, 443))
            except ValueError:
                same_origin = upgrade = False
            if not read_only or not location or response.status_code == 303 or not (same_origin or upgrade) or redirect == 3:
                response.close()
                raise self._error(f'HTTP {response.status_code}: API перенаправляет запрос. '
                                  f'Укажите конечный адрес API из документации панели: {_display_url(target)}.',
                                  str(data['action']), uncertain=not read_only, status_code=response.status_code)
            response.close()
            url = target
        raise PanelError('Слишком много перенаправлений API.')

    def call(self, action: str, **kwargs: Any) -> Any:
        read_only = action in {'services', 'status', 'balance', 'refill_status'}
        data = {**{k: v for k, v in kwargs.items() if v is not None}, 'key': self.data['api_key'], 'action': action}
        timeout = max(5, min(180, _int(self.cfg.get('api_timeout'), 45)))
        options: Dict[str, Any] = {'timeout': (min(10, timeout), timeout), 'allow_redirects': False,
                                   'headers': {'Accept': 'application/json', 'User-Agent': f'AutoSMM/{VERSION}'},
                                   **_proxy_kwargs(self.proxy)}
        method = self.quirks.get('force_method') or str(self.data.get('request_method', 'POST')).upper()
        attempts = 3 if read_only else 1
        for attempt in range(attempts):
            try:
                response = self._request(method, data, options, read_only)
            except requests.RequestException as exc:
                if isinstance(exc, requests.exceptions.SSLError):
                    message, retry = 'Ошибка SSL: проверьте сертификат панели и дату на сервере Cardinal.', False
                elif isinstance(exc, requests.exceptions.ProxyError):
                    message, retry = 'Ошибка прокси: проверьте его адрес, авторизацию и доступность.', False
                elif isinstance(exc, (requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema, requests.exceptions.MissingSchema)):
                    message, retry = 'Неверный URL API или прокси (для SOCKS нужен пакет PySocks).', False
                elif isinstance(exc, requests.Timeout):
                    message, retry = 'Панель не ответила вовремя. Проверьте доступность API с сервера Cardinal.', True
                else:
                    message, retry = 'Не удалось связаться с панелью: проверьте DNS, сеть и доступ с IP сервера.', True
                if read_only and retry and attempt < attempts - 1:
                    time.sleep(1 + attempt * 2)
                    continue
                raise self._error(message, action, uncertain=not read_only) from None
            try:
                code = response.status_code
                try:
                    result = response.json()
                except ValueError:
                    result = None
                api_error = result.get('error') if isinstance(result, dict) else None
                if not 200 <= code < 300:
                    hints = {400: 'Неверные параметры или отказ API. Проверьте ключ и настройки панели.',
                             401: 'Панель не авторизовала запрос. Проверьте API-ключ.',
                             403: 'Доступ запрещён. Проверьте доступ к API, ограничения IP и защиту панели (прокси).',
                             404: 'Адрес API не найден. Скопируйте точный API URL из кабинета панели.',
                             405: f'Метод {method} не поддерживается. Переключите GET / POST в карточке панели.',
                             429: 'Превышен лимит запросов. Подождите и увеличьте интервал проверки.'}
                    message = f'HTTP {code}: ' + hints.get(code, 'Ошибка сервера панели.' if code >= 500 else 'Панель отклонила HTTP-запрос.')
                    if api_error:
                        message += ' Ответ API: ' + self._api_error(api_error)
                    if read_only and (code == 429 or code >= 500) and attempt < attempts - 1:
                        delay = 1 + attempt * 2
                        retry_after = response.headers.get('Retry-After')
                        if retry_after:
                            delay = _float(retry_after, 11)
                        if 0 <= delay <= 10:
                            time.sleep(delay)
                            continue
                    raise self._error(message, action, uncertain=not read_only and code >= 500, status_code=code)
                if api_error:
                    raise self._error('Панель отклонила запрос: ' + self._api_error(api_error), action)
                if result is None:
                    content_type = str(response.headers.get('Content-Type', '')).lower()
                    body = response.text[:1000].lower()
                    kind = ('HTML-страницу (защита, вход в кабинет или неверный URL)'
                            if 'html' in content_type or '<html' in body or '<!doctype' in body else 'неверный JSON или пустой ответ')
                    raise self._error(f'HTTP {code}: панель вернула {kind}. Проверьте API URL и доступ с сервера.',
                                      action, uncertain=not read_only, status_code=code)
                return result
            finally:
                response.close()
        raise PanelError('Панель не ответила.')

    def services(self, refresh: bool = False) -> List[Dict[str, Any]]:
        cached = _service_cache.get(self.id)
        identity = (self.url, self.data['api_key'], self.data.get('request_method', 'POST'), str(self.proxy))
        if cached and not refresh and time.time() - cached[0] < 900 and cached[2] == identity:
            return cached[1]
        result = self.call('services')
        if isinstance(result, dict) and isinstance(result.get('services'), list):
            result = result['services']
        if not isinstance(result, list):
            raise PanelError('API вернул не список услуг.')
        result = [x for x in result if isinstance(x, dict) and str(x.get('service', '')).strip()]
        _service_cache[self.id] = (time.time(), result, identity)
        return result

    def service(self, service_id: Any) -> Dict[str, Any]:
        found = next((x for x in self.services() if str(x.get('service')) == str(service_id).strip()), None)
        if not found:
            found = next((x for x in self.services(refresh=True) if str(x.get('service')) == str(service_id).strip()), None)
        if not found:
            raise PanelError(f'Услуга #{service_id} не найдена в панели «{self.data.get("name")}».')
        return found

    def balance(self) -> Dict[str, Any]:
        result = self.call('balance')
        if not isinstance(result, dict) or 'balance' not in result:
            raise PanelError('API вернул неверный баланс.')
        currency = str(result.get('currency') or '').upper().strip() or 'RUB'
        balances = _state_get('balances', {}) or {}
        balances[self.id] = {'balance': _float(result.get('balance')), 'currency': currency, 'ts': time.time()}
        _state_set('balances', balances)
        return {**result, 'currency': currency}

    def currency(self) -> str:
        info = (_state_get('balances', {}) or {}).get(self.id) or {}
        if info.get('currency') and time.time() - _float(info.get('ts')) < 24 * 3600:
            return str(info['currency'])
        return self.balance()['currency']

    def add(self, service_id: Any, link: str, quantity: int, **extra: Any) -> str:
        result = self.call('add', service=str(service_id), link=link, quantity=quantity, **extra)
        if not isinstance(result, dict) or not str(result.get('order') or '').isdigit() or _int(result.get('order')) <= 0:
            raise PanelError('Панель не вернула корректный номер заказа. Проверьте историю панели.', uncertain=True)
        return str(result['order'])

    def status(self, order_id: Any) -> Dict[str, Any]:
        result = self.call('status', order=str(order_id))
        if not isinstance(result, dict) or not str(result.get('status') or '').strip():
            raise PanelError('API вернул неверный статус.')
        return result

    def statuses(self, order_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Пакетная проверка (orders=1,2,3); при неподдержке — по одному."""
        result: Dict[str, Dict[str, Any]] = {}
        ids = [str(x) for x in order_ids if str(x).isdigit()]
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            data = None
            if len(chunk) > 1:
                try:
                    data = self.call('status', orders=','.join(chunk))
                except PanelError:
                    data = None
            if isinstance(data, dict) and any(isinstance(data.get(x), dict) for x in chunk):
                for order_id in chunk:
                    item = data.get(order_id)
                    if isinstance(item, dict) and str(item.get('status') or '').strip():
                        result[order_id] = item
                continue
            for order_id in chunk:
                try:
                    result[order_id] = self.status(order_id)
                except PanelError:
                    continue
        return result

    def refill(self, order_id: Any) -> str:
        result = self.call('refill', order=str(order_id))
        refill_id = result.get('refill') if isinstance(result, dict) else None
        if isinstance(refill_id, dict):
            refill_id = refill_id.get('refill') or refill_id.get('id')
        if refill_id in (None, '', 0, '0', False):
            raise PanelError('Панель не подтвердила запуск рефилла.')
        return str(refill_id)


def _service_cost(panel: Dict[str, Any], service: Dict[str, Any], quantity: int) -> float:
    """Себестоимость в валюте панели."""
    return max(0.0, _float(service.get('rate')) * (1 - max(-100, min(100, _float(panel.get('discount')))) / 100) * quantity / 1000)


def _cost_rub(cfg: Dict[str, Any], panel_id: str, service: Dict[str, Any], quantity: int,
              api: Optional[PanelAPI] = None) -> Tuple[float, float, str]:
    """(себестоимость в рублях, себестоимость в валюте панели, валюта панели)."""
    panel = cfg['services'][str(panel_id)]
    native = _service_cost(panel, service, quantity)
    currency = (api or PanelAPI(cfg, panel_id)).currency()
    return native * _fx_to_rub(currency, cfg), native, currency


def _effective_markup(cfg: Dict[str, Any], lot: Optional[Dict[str, Any]]) -> Tuple[str, float]:
    if lot and lot.get('markup_mode') in ('percent', 'fixed'):
        return lot['markup_mode'], _float(lot.get('markup_value'))
    pricing = cfg['pricing']
    return ('fixed', _float(pricing['fixed'])) if pricing.get('mode') == 'fixed' else ('percent', _float(pricing['percent']))


def _price_from_cost(cost_rub: float, mode: str, value: float) -> float:
    price = cost_rub + value if mode == 'fixed' else cost_rub * (1 + value / 100)
    if price >= 1:
        return math.ceil(price * 100 - 1e-9) / 100
    return max(0.000001, math.ceil(price * 1_000_000 - 1e-9) / 1_000_000)


def _markup_text(mode: str, value: float) -> str:
    return f'+{value:g} ₽' if mode == 'fixed' else f'{value:+g}%'


# ---------------------------------------------------------------------------
# Разбор услуги панели: соцсеть, тип, характеристики
# ---------------------------------------------------------------------------

_EMOJI_RE = re.compile('[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍←-⇿⌀-⏿]+')


def _service_text(service: Dict[str, Any]) -> Tuple[str, str]:
    return str(service.get('name') or '').lower(), str(service.get('category') or '').lower()


def _detect_platform(service: Dict[str, Any]) -> str:
    name, category = _service_text(service)
    for text in (category, name):
        for key in PLATFORM_ORDER:
            if key == 'website':
                continue
            if re.search(PLATFORMS[key]['pattern'], text):
                return key
    if re.search(r'traffic|трафик|website|визит|посещен', name + ' ' + category):
        return 'website'
    return 'other'


def _detect_kind(service: Dict[str, Any]) -> str:
    name, category = _service_text(service)
    service_type = str(service.get('type') or '').lower()
    if 'poll' in service_type:
        return 'votes'
    if 'comment' in service_type:
        return 'comments'
    for text in (name, category, name + ' ' + category):
        for kind, pattern in _COMPOUND_KINDS:
            if re.search(pattern, text):
                return kind
        best: Optional[Tuple[int, int, str]] = None
        for order, (kind, pattern) in enumerate(_SIMPLE_KINDS):
            match = re.search(pattern, text)
            if match and (best is None or (match.start(), order) < best[:2]):
                best = (match.start(), order, kind)
        if best:
            return best[2]
    return 'generic'


def _parse_service(service: Dict[str, Any]) -> Dict[str, Any]:
    """Извлекает из «TG Members | Max 50K | R30 | 1-2K/Day [ID 4512]» понятные поля."""
    raw = str(service.get('name') or '')
    text = raw
    info: Dict[str, Any] = {'speed': '', 'start': '', 'guarantee': '', 'guarantee_days': 0, 'quality': [], 'max_hint': ''}
    low = text.lower()

    match = re.search(r'(\d+(?:[.,]\d+)?\s*[kкmм]?\s*[-–]\s*)?\d+(?:[.,]\d+)?\s*[kкmм]?\s*(?:/|per|в|за)\s*(?:day|d|сут\w*|день|дн|h|hour|час)\b', low)
    if match:
        info['speed'] = match.group(0).replace(' ', '').upper().replace('/DAY', '/сутки').replace('/D', '/сутки') \
            .replace('/H', '/час').replace('/HOUR', '/час')
        text = text.replace(raw[match.start():match.end()], ' ')
    match = re.search(r'(?:start|старт)\s*:?\s*([0-9]+\s*[-–]\s*[0-9]+\s*(?:h|hr|hours?|ч|час\w*|min|мин\w*|m)|'
                      r'[0-9]+\s*(?:h|hr|hours?|ч|час\w*|min|мин\w*|m)|instant\w*|моментальн\w*|мгновенн\w*)', low)
    if match:
        value = match.group(1)
        value = re.sub(r'(?:hours?|hr|h)\b', 'ч', value)
        value = re.sub(r'(?:min|m)\b', 'мин', value)
        info['start'] = 'моментально' if re.match(r'instant|момент|мгновен', value) else value.replace(' ', '')
        text = re.sub(re.escape(raw[match.start():match.end()]), ' ', text, flags=re.I)
    elif re.search(r'\binstant\b|моментальн|мгновенн', low):
        info['start'] = 'моментально'
    match = re.search(r'(?:\br(\d{1,3})\b|refill\s*:?\s*(\d{1,3})\s*(?:d|days?|дн\w*)|(\d{1,3})\s*(?:d|days?|дн\w*)\s*refill|'
                      r'(?:гарант|рефил)\w*\s*:?\s*(\d{1,3})\s*(?:д|дн\w*)|(\d{1,3})\s*(?:д|дн\w*)\s*(?:гарант|рефил)\w*)', low)
    if match:
        days = _int(next(x for x in match.groups() if x))
        if 0 < days <= 3650:
            info['guarantee_days'] = days
            info['guarantee'] = f'{days} {_plural(days, ("день", "дня", "дней"))}'
        text = re.sub(re.escape(raw[match.start():match.end()]), ' ', text, flags=re.I)
    elif re.search(r'lifetime|навсегда|пожизнен', low):
        info['guarantee'] = 'пожизненная'
        info['guarantee_days'] = 3650
    elif service.get('refill') in (True, 1, '1', 'true', 'True'):
        info['guarantee'] = 'есть (рефилл)'
    info['no_refill'] = bool(re.search(r'no\s*refill|без\s*гарант|без\s*рефил|\bnr\b', low))
    if info['no_refill'] and not match:
        info['guarantee'], info['guarantee_days'] = '', 0
    match = re.search(r'max\s*:?\s*(\d+(?:[.,]\d+)?\s*[kкmм]?)', low)
    if match:
        info['max_hint'] = match.group(1).upper()
        text = re.sub(re.escape(raw[match.start():match.end()]), ' ', text, flags=re.I)
    for pattern, label in ((r'\breal\b|живы|реальн', 'ЖИВЫЕ'), (r'premium|премиум', 'PREMIUM'), (r'\bhq\b|high\s*quality|качествен', 'HQ'),
                           (r'\bru\b|рус|россия|рф\b', 'RU'), (r'снг|\bcis\b', 'СНГ'), (r'\bmix\b|микс', 'MIX'),
                           (r'\busa?\b|сша', 'USA'), (r'non[\s-]?drop|без\s+списан', 'БЕЗ СПИСАНИЙ'), (r'\bbots?\b|бот(ы|ов)\b', 'БОТЫ')):
        found = re.search(pattern, low)
        if found and label not in info['quality']:
            info['quality'].append(label)
            info.setdefault('quality_words', {})[label] = found.group(0)
    text = re.sub(r'\[[^\]]*\bid\b[^\]]*\]|\bid\s*[:#]?\s*\d+|^\s*\d+\s*[-–.|:]\s*', ' ', text, flags=re.I)
    text = _EMOJI_RE.sub(' ', text)
    info['clean_name'] = ' • '.join(_name_chunks(text)) or raw.strip() or 'SMM-услуга'
    return info


def _name_chunks(text: str) -> List[str]:
    """«Telegram Подписчики [Без гарантии] [Резервные]» → ['Telegram Подписчики', 'Без гарантии', 'Резервные']."""
    result: List[str] = []
    for chunk in re.split(r'[\[\]\(\)\|•{}]+|\s[-–—]\s', text):
        chunk = re.sub(r'\s+', ' ', chunk).strip(' .,;:-–—/\\*#')
        if len(chunk) >= 2 and not chunk.isdigit() and chunk.lower() not in (x.lower() for x in result):
            result.append(chunk)
    return result


_STRUCTURAL_RE = [
    r'(\d+(?:[.,]\d+)?\s*[kкmм]?\s*[-–]\s*)?\d+(?:[.,]\d+)?\s*[kкmм]?\s*(?:/|per|в|за)\s*(?:day|d|сут\w*|день|дн|h|hour|час)\b',
    r'(?:start|старт)\s*:?\s*(?:[0-9]+\s*[-–]\s*[0-9]+|[0-9]+)?\s*(?:h|hr|hours?|ч|час\w*|min|мин\w*|m)?\b',
    r'\b(?:max|min|макс|мин)\s*:?\s*\d+(?:[.,]\d+)?\s*[kкmм]?\b', r'\br\d{1,3}\b', r'\bid\s*[:#]?\s*\d+',
    r'\binstant\b|моментальн\w*|мгновенн\w*', r'\bnr\b',
]
_FILLER_RE = r'^(?:на|в|для|for|to|on|услуг\w*|services?|накрутк\w*|smm|new|новинка|best|топ|top)$'
_LONE_FILLER_RE = r'^(?:post|posts|пост|посты)$'


def _variant(service: Dict[str, Any], profile: Dict[str, Any]) -> str:
    """Чем услуга отличается от других того же типа: «Без гарантии • Резервные».
    Соцсеть, тип, скорость, старт и лимиты отбрасываются — они уже показаны отдельно."""
    text = _EMOJI_RE.sub(' ', str(service.get('name') or ''))
    for pattern in _STRUCTURAL_RE:
        text = re.sub(pattern, ' ', text, flags=re.I)
    words = [PLATFORMS.get(profile['platform'], {}).get('pattern', '')]
    words += [p for k, p in _COMPOUND_KINDS + _SIMPLE_KINDS if k == profile['kind']]
    words += [re.escape(x) for x in KINDS.get(profile['kind'], KINDS['generic'])['select'] if len(x) >= 4]
    # Слова из названия типа («бусты канала», «старты бота») не повторяем в варианте.
    kinfo = KINDS.get(profile['kind'], KINDS['generic'])
    title_stems = [w[:4] for w in re.split(r'[\s/-]+', (kinfo['ru'] + ' ' + kinfo['en']).lower()) if len(w) >= 4]
    parts = []
    for chunk in _name_chunks(text):
        tokens = []
        for token in chunk.split():
            low = token.lower()
            if any(w and re.search(w, low) for w in words) or re.fullmatch(_FILLER_RE, low) or \
                    any(low.startswith(stem) for stem in title_stems):
                continue
            tokens.append(token)
        if len(tokens) == 1 and re.fullmatch(_LONE_FILLER_RE, tokens[0].lower()):
            tokens = []
        cleaned = ' '.join(tokens).strip(' .,;:-–—/')
        if len(cleaned) >= 2 and not cleaned.isdigit() and cleaned.lower() not in (x.lower() for x in parts):
            parts.append(cleaned)
    if profile['kind'] == 'reactions':
        # Для реакций эмодзи — это и есть суть услуги (👍❤️🔥), их не выбрасываем.
        emojis = ''.join(dict.fromkeys(re.findall('[\U0001F300-\U0001FAFF\u2764\u2B50]', str(service.get('name') or ''))))
        if emojis:
            parts.insert(0, emojis[:12])
    return ' • '.join(parts[:4])


_EN_PHRASES = [
    (r'без\s+гарант\w*', 'no refill'), (r'без\s+рефил\w*', 'no refill'), (r'без\s+списан\w*', 'no drop'),
    (r'гарант\w*', 'refill'), (r'рефил\w*', 'refill'), (r'резервн\w*', 'reserve'), (r'живы\w*', 'real'),
    (r'реальн\w*', 'real'), (r'быстр\w*', 'fast'), (r'медленн\w*', 'slow'), (r'дешев\w*|дешёв\w*', 'cheap'),
    (r'премиум\w*', 'premium'), (r'русск\w*|россия\w*|\bрф\b', 'russian'), (r'бот(ы|ов)?\b', 'bots'),
    (r'смешанн\w*|микс\w*', 'mix'), (r'качествен\w*', 'HQ'), (r'стабильн\w*', 'stable'), (r'нов(ые|ый|ая)\b', 'new'),
    (r'активн\w*', 'active'), (r'целев\w*', 'targeted'), (r'мужск\w*', 'male'), (r'женск\w*', 'female'),
    (r'офферн\w*|оффер\w*', 'offer'), (r'медиа\w*', 'media'), (r'аудитори\w*', 'audience'), (r'снг', 'CIS'),
    (r'последн\w*', 'latest'), (r'будущ\w*', 'future'), (r'пост\b', 'post'), (r'пост(ов|а|ы)\b', 'posts'), (r'публикаци\w*', 'post'),
    (r'позитивн\w*', 'positive'), (r'негативн\w*', 'negative'), (r'аккаунт\w*', 'accounts'), (r'просмотр\w*', 'views'),
    (r'подписчик\w*', 'followers'), (r'реакци\w*', 'reactions'), (r'лайк\w*', 'likes'), (r'комментари\w*', 'comments'),
    (r'участник\w*', 'members'), (r'репост\w*', 'shares'), (r'канал\w*', 'channel'), (r'чат\w*', 'chat'),
    (r'групп\w*', 'group'), (r'видео', 'video'), (r'сторис', 'stories'), (r'эфир\w*', 'live'), (r'автопросмотр\w*', 'auto views'),
    (r'удержани\w*', 'retention'), (r'миров\w*', 'worldwide'), (r'онлайн\w*', 'online'), (r'час(ы|ов)\b', 'hours'),
    (r'бот(а|у)\b', 'bot'), (r'зарубежн\w*', 'foreign'), (r'иностранн\w*', 'foreign'), (r'мгновенн\w*', 'instant'),
    (r'моментальн\w*', 'instant'), (r'ручн\w*', 'manual'), (r'авто\w*', 'auto'), (r'дешёв\w*', 'cheap'),
    (r'\bдн(я|ей|и)?\b|\bдень\b', 'days'), (r'\bчас(а|ов)?\b|\bч\b', 'h'), (r'\bмин\w*', 'min'),
    (r'\bи\b', '&'), (r'\bс\b', 'with'), (r'\bот\b', 'from'), (r'\bдо\b', 'up to'),
]


def _en_phrase(text: str) -> str:
    result = str(text or '')
    for pattern, en in _EN_PHRASES:
        result = re.sub(pattern, en, result, flags=re.I)
    return _translit(result) if re.search('[а-яё]', result, re.I) else result


def _service_profile(service: Dict[str, Any]) -> Dict[str, Any]:
    platform = _detect_platform(service)
    kind = _detect_kind(service)
    if kind == 'traffic':
        platform = 'website'
    return {'platform': platform, 'kind': kind, **_parse_service(service)}


def _platform_info(platform: str) -> Dict[str, Any]:
    return PLATFORMS.get(platform, OTHER_PLATFORM)


def _infer_domains(service: Dict[str, Any]) -> List[str]:
    profile = _service_profile(service)
    return list(_platform_info(profile['platform']).get('domains') or [])


def _example_link(platform: str, kind: str) -> str:
    links = _platform_info(platform).get('links', {})
    target = KINDS.get(kind, KINDS['generic'])['target']
    return links.get(target) or links.get('post') or links.get('profile') or 'https://example.com/page'


def _example(domains: List[str], platform: str = '', kind: str = '') -> str:
    if platform:
        return _example_link(platform, kind or 'generic')
    joined = ' '.join(domains)
    for key in PLATFORM_ORDER:
        if any(domain in joined for domain in PLATFORMS[key]['domains'] if domain != '*'):
            return _example_link(key, kind or 'followers')
    return 'https://example.com/page'


# ---------------------------------------------------------------------------
# Сборка текстов лота
# ---------------------------------------------------------------------------

def _pieces_range(service: Dict[str, Any], quantity: int) -> Tuple[int, int]:
    quantity = max(1, quantity)
    smin, smax = max(1, _int(service.get('min'), 1)), _int(service.get('max'))
    min_pcs = max(1, math.ceil(smin / quantity))
    max_pcs = max(min_pcs, smax // quantity) if smax > 0 else 99999
    return min_pcs, min(99999, max_pcs)


def _design_context(service: Dict[str, Any], quantity: int, price: float = 0.0, cfg: Optional[Dict[str, Any]] = None,
                    profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg or load_config()
    profile = profile or _service_profile(service)
    platform, kind = profile['platform'], profile['kind']
    pinfo, kinfo = _platform_info(platform), KINDS.get(kind, KINDS['generic'])
    min_pcs, max_pcs = _pieces_range(service, quantity)
    target = kinfo['target']
    notes_ru, notes_en = list(TARGET_NOTES.get(target, TARGET_NOTES['post'])[0]), list(TARGET_NOTES.get(target, TARGET_NOTES['post'])[1])
    extra = KIND_EXTRA_NOTES.get(kind)
    if extra:
        notes_ru += extra[0]
        notes_en += extra[1]
    notes_ru.append('Не запускайте эту же услугу параллельно у других продавцов')
    notes_en.append('Do not run the same service with other sellers at the same time')
    if kind == 'generic':
        title_ru = _short(profile['clean_name'].upper(), 60)
        title_en = _short(_translit(profile['clean_name']).upper(), 60)
    else:
        title_ru = f"{kinfo['ru']} {pinfo['short']}".strip()
        title_en = f"{pinfo['short'] if platform not in ('vk', 'ok', 'dzen', 'yamusic', 'website') else _translit(pinfo['short'])} {kinfo['en']}".strip()
    if platform == 'website':
        title_ru, title_en = kinfo['ru'] if kind == 'traffic' else title_ru, 'WEBSITE TRAFFIC' if kind == 'traffic' else title_en
    variant = _variant(service, profile)
    if cfg['design'].get('title_source') == 'panel':
        title_ru = _short(profile['clean_name'], 70)
        title_en = _short(_en_phrase(profile['clean_name']), 70)
        variant = ''
    unit = _plural(quantity, kinfo['unit'])
    unit_en = _plural_en(quantity, kinfo['unit_en'])
    return {
        'title': title_ru, 'title_en': title_en, 'platform': pinfo['name'], 'platform_en': _translit(pinfo['name']),
        'platform_key': platform, 'kind_key': kind, 'kind': kinfo['ru'].capitalize(), 'kind_en': kinfo['en'].capitalize(),
        'emoji': kinfo['emoji'], 'platform_emoji': pinfo['emoji'],
        'service_name': str(service.get('name') or ''), 'clean_name': profile['clean_name'],
        'per_piece': _human_number(quantity), 'per_piece_raw': quantity, 'unit': unit, 'unit_en': unit_en,
        'min_pcs': min_pcs, 'max_pcs': max_pcs, 'min': _human_number(service.get('min', '1')), 'max': _human_number(service.get('max', '—')),
        'speed': profile['speed'], 'start': profile['start'], 'guarantee': profile['guarantee'],
        'guarantee_days': profile['guarantee_days'], 'quality': ', '.join(profile['quality']),
        'quality_list': profile['quality'], 'quality_words': profile.get('quality_words', {}),
        'no_refill': profile.get('no_refill', False), 'variant': variant, 'variant_en': _en_phrase(variant),
        'example_link': _example_link(platform, kind), 'link_hint': TARGET_TEXT.get(target, TARGET_TEXT['post'])[0],
        'link_hint_en': TARGET_TEXT.get(target, TARGET_TEXT['post'])[1],
        'requirements': '\n'.join('• ' + x for x in notes_ru), 'requirements_en': '\n'.join('• ' + x for x in notes_en),
        'notes_ru': notes_ru, 'notes_en': notes_en,
        'price': _money(price) if price else '', 'service_id': str(service.get('service', '')),
        'category': str(service.get('category') or ''), 'confirm': bool(cfg.get('confirm_link', True)),
        'poll': kind == 'votes' and 'poll' in str(service.get('type', '')).lower(),
    }


def _badges(ctx: Dict[str, Any], english: bool = False) -> List[Tuple[str, int]]:
    """(текст, приоритет). Чем больше приоритет, тем дольше значок остаётся в названии."""
    result: List[Tuple[str, int]] = []
    variant = ctx.get('variant') or ''
    low = variant.lower()
    if variant:
        result.append(((ctx['variant_en'] if english else variant).upper(), 7))
    quality = [x for x in ctx['quality_list'] if x != 'БОТЫ' and x.lower() not in ctx['title'].lower() and
               not (low and str(ctx['quality_words'].get(x, '')).lower() in low)][:2]
    if quality:
        text = ' '.join(quality)
        if english:
            text = _en_text(text)
        result.append((text, 3))
    shown = bool(re.search(r'гарант|рефил|refill|guarantee|warranty', low))
    if ctx.get('no_refill') and not shown and not ctx['guarantee_days']:
        result.append(('⛔ NO REFILL' if english else '⛔ БЕЗ ГАРАНТИИ', 4))
    elif shown:
        pass
    elif ctx['guarantee_days']:
        days = ctx['guarantee_days']
        text = (f'♻️ {"LIFETIME" if days >= 3650 else str(days) + "D"} REFILL' if english else
                f'♻️ ГАРАНТИЯ {"НАВСЕГДА" if days >= 3650 else str(days) + " ДН"}')
        result.append((text, 4))
    elif ctx['guarantee']:
        result.append(('♻️ REFILL' if english else '♻️ ГАРАНТИЯ', 4))
    if ctx['speed']:
        result.append(((f'🚀 {_en_text(ctx["speed"]).upper()}' if english else f'🚀 {ctx["speed"].upper()}'), 1))
    elif ctx['start'] == 'моментально':
        result.append(('⚡ INSTANT' if english else '⚡ МОМЕНТАЛЬНО', 1))
    if ctx['per_piece_raw'] > 1:
        result.append(((f'{ctx["per_piece"]} PCS PACK' if english else f'ПАКЕТ {ctx["per_piece"]} ШТ'), 6))
    elif ctx['min_pcs'] > 1:
        result.append(((f'MIN {ctx["min_pcs"]} PCS' if english else f'ОТ {ctx["min_pcs"]} ШТ'), 6))
    result.append(('AUTO 24/7' if english else 'АВТОВЫДАЧА 24/7', 5))
    return result


def _compose_summary(theme: Dict[str, Any], title: str, badges: List[Tuple[str, int]], limit: int) -> str:
    items = list(badges)
    while True:
        badge_text = theme['joiner'].join(text for text, _ in items)
        text = _format(theme['summary'], {'title': title, 'badges': badge_text})
        text = re.sub(r'\s+', ' ', text).strip(' •|✦✔')
        if _utf16_len(text) <= limit or not items:
            break
        if len(items) == 1 and items[0][1] >= 7:
            # Остался только вариант услуги — укорачиваем его, но не убираем.
            base = _format(theme['summary'], {'title': title, 'badges': ''})
            room = limit - _utf16_len(re.sub(r'\s+', ' ', base).strip(' •|✦✔')) - 1
            if room >= 6:
                short = _summary_field(items[0][0], room)
                text = re.sub(r'\s+', ' ', _format(theme['summary'], {'title': title, 'badges': short})).strip(' •|✦✔')
                if _utf16_len(text) <= limit:
                    return text
        items.remove(min(items, key=lambda x: x[1]))
    if _utf16_len(text) > limit:
        text = _summary_field(_format(theme['summary'], {'title': title, 'badges': ''}).strip(' •|✦✔'), limit)
    return text


_QUALITY_EN = {'ЖИВЫЕ': 'REAL', 'БЕЗ СПИСАНИЙ': 'NO DROP', 'СНГ': 'CIS', 'БОТЫ': 'BOTS'}


def _en_text(value: str) -> str:
    value = str(value or '')
    for ru, en in _QUALITY_EN.items():
        value = value.replace(ru, en)
    value = (value.replace('моментально', 'instant').replace('сутки', 'day').replace('час', 'hour')
             .replace('мин', 'min').replace('ч', 'h'))
    return _translit(value) if re.search('[а-яё]', value, re.I) else value


def _theme_description(theme: Dict[str, Any], ctx: Dict[str, Any], english: bool) -> str:
    i = 1 if english else 0
    b = theme['bullet']
    lines = [theme['sep'], f"{ctx['emoji']} {ctx['title_en'] if english else ctx['title']}", theme['sep'], '', theme['intro'][i], '',
             theme['get'][i]]
    if english:
        lines.append(f"{b} 1 pc = {ctx['per_piece']} {ctx['unit_en']}")
        if ctx['clean_name']:
            lines.append(f"{b} 🧾 Service: {_en_phrase(ctx['clean_name'])}")

        if ctx['start']:
            lines.append(f"{b} 🚀 Start: {_en_text(ctx['start'])}")
        if ctx['speed']:
            lines.append(f"{b} 📈 Speed: {_en_text(ctx['speed'])}")
        if ctx['guarantee']:
            lines.append(f"{b} ♻️ Refill guarantee: " + (f"{ctx['guarantee_days']} days" if ctx['guarantee_days'] and ctx['guarantee_days'] < 3650
                                                         else 'lifetime' if ctx['guarantee_days'] else 'yes'))
        if ctx['quality']:
            lines.append(f"{b} 🌍 Audience: {_en_text(ctx['quality'])}")
        if theme.get('details'):
            lines.append(f"{b} 📦 Service limits: {ctx['min']} – {ctx['max']} {ctx['unit_en']}")
    else:
        lines.append(f"{b} 1 шт. = {ctx['per_piece']} {ctx['unit']}")
        if ctx['clean_name']:
            lines.append(f"{b} 🧾 Услуга: {ctx['clean_name']}")

        if ctx['start']:
            lines.append(f"{b} 🚀 Старт: {ctx['start']}")
        if ctx['speed']:
            lines.append(f"{b} 📈 Скорость: {ctx['speed']}")
        if ctx['guarantee']:
            lines.append(f"{b} ♻️ Гарантия от списаний: {ctx['guarantee']}")
        if ctx['quality']:
            lines.append(f"{b} 🌍 Аудитория: {ctx['quality']}")
        if theme.get('details'):
            lines.append(f"{b} 📦 Лимиты услуги: {ctx['min']} – {ctx['max']} {_plural(_int(ctx['max'].replace(' ', '')), KINDS[ctx['kind_key']]['unit'])}")
    lines += ['', theme['how'][i]]
    steps = theme['steps']
    if english:
        flow = [f"Pay for the quantity you need — from {ctx['min_pcs']} to {ctx['max_pcs']} pcs",
                f"Send {ctx['link_hint_en']} to the chat: {ctx['example_link']}"]
        if ctx['poll']:
            flow.append('Send the answer option number when the bot asks')
        if ctx['confirm']:
            flow.append('Confirm the start with "+"')
        flow.append('Check the status any time: check ORDER_NUMBER')
    else:
        flow = [f"Оплатите нужное количество — от {ctx['min_pcs']} до {ctx['max_pcs']} шт.",
                f"Отправьте в чат {ctx['link_hint']}: {ctx['example_link']}"]
        if ctx['poll']:
            flow.append('Укажите номер варианта ответа, когда бот спросит')
        if ctx['confirm']:
            flow.append('Подтвердите запуск знаком «+»')
        flow.append('Проверяйте статус в любой момент: чек НОМЕР_ЗАКАЗА')
    for index, step in enumerate(flow):
        lines.append(f"{steps[min(index, len(steps) - 1)]} {step}")
    lines += ['', theme['warn'][i]]
    for note in (ctx['notes_en'] if english else ctx['notes_ru']):
        lines.append(f'{b} {note}')
    lines += ['', theme['footer'][i]]
    return '\n'.join(lines)


def _template_choice(cfg: Dict[str, Any], profile: Dict[str, Any], override: str = '') -> str:
    """Порядок: шаблон лота/мастера → назначение соцсеть:тип → соцсеть:* → *:тип → тема по умолчанию."""
    if override and override != 'auto':
        return override
    assign = cfg['design'].get('assign', {})
    for key in (f"{profile['platform']}:{profile['kind']}", f"{profile['platform']}:*", f"*:{profile['kind']}"):
        if assign.get(key):
            return assign[key]
    return cfg['design'].get('theme') or 'premium'


def _template_name(cfg: Dict[str, Any], choice: str) -> str:
    if choice.startswith('custom:'):
        item = cfg['design']['custom'].get(choice[7:])
        return '🧩 ' + (item.get('name') if item else 'удалённый шаблон')
    if choice == 'auto':
        return '🎯 по настройкам оформления'
    return THEMES.get(choice, THEMES['premium'])['title']


def render_lot_texts(service: Dict[str, Any], quantity: int, price: float, choice: str, limits: Dict[str, int],
                     cfg: Optional[Dict[str, Any]] = None, tag: str = '') -> Dict[str, str]:
    """Готовит краткое и подробное описание RU/EN с учётом лимитов формы FunPay."""
    cfg = cfg or load_config()
    profile = _service_profile(service)
    ctx = _design_context(service, quantity, price, cfg, profile)
    choice = _template_choice(cfg, profile, choice)
    s_ru, s_en = limits.get('summary_ru', 80), limits.get('summary_en', 80)
    d_ru, d_en = limits.get('desc_ru', 5000), limits.get('desc_en', 5000)
    tag_line = f'\n\n#SMM:{tag}' if tag else ''
    custom = cfg['design']['custom'].get(choice[7:]) if choice.startswith('custom:') else None
    theme = THEMES.get(choice if not custom else 'premium', THEMES['premium'])
    if custom:
        values = {**ctx, 'badges': ' • '.join(text for text, _ in _badges(ctx))}
        summary_ru = _summary_field(_format(custom.get('summary_ru') or '{title}', values), s_ru)
        desc_ru = _format(custom.get('desc_ru') or '', values).strip() or _theme_description(theme, ctx, False)
        values_en = {**values, 'badges': ' • '.join(text for text, _ in _badges(ctx, True)), 'variant': ctx['variant_en'],
                     'requirements': ctx['requirements_en'], 'link_hint': ctx['link_hint_en']}
        summary_en = _summary_field(_format(custom.get('summary_en') or '', values_en), s_en) if custom.get('summary_en') else \
            _compose_summary(theme, ctx['title_en'], _badges(ctx, True), s_en)
        desc_en = _format(custom.get('desc_en') or '', values_en).strip() if custom.get('desc_en') else _theme_description(theme, ctx, True)
    else:
        summary_ru = _compose_summary(theme, ctx['title'], _badges(ctx), s_ru)
        summary_en = _compose_summary(theme, ctx['title_en'], _badges(ctx, True), s_en)
        desc_ru = _theme_description(theme, ctx, False)
        desc_en = _theme_description(theme, ctx, True)
    if not cfg['design'].get('english', True):
        summary_en = _summary_field(_translit(summary_ru), s_en)
        desc_en = _translit(desc_ru)
    summary_en = _summary_field(_latin_only(summary_en), s_en)
    desc_en = _latin_only(desc_en)
    desc_ru = _clip_units(desc_ru, max(1, d_ru - _utf16_len(tag_line))).rstrip() + tag_line
    desc_en = _clip_units(desc_en, max(1, d_en - _utf16_len(tag_line))).rstrip() + tag_line
    return {'summary_ru': summary_ru, 'summary_en': summary_en, 'desc_ru': desc_ru, 'desc_en': desc_en,
            'platform': profile['platform'], 'kind': profile['kind'], 'template': choice}


def _latin_only(text: str) -> str:
    """FunPay отклоняет английские поля с кириллицей («некорректное английское описание»)."""
    text = str(text or '')
    return _translit(text) if re.search('[а-яё]', text, re.I) else text


def _plain_english(text: str) -> str:
    """Запасной вариант EN-текста: только латиница, цифры и пунктуация, без эмодзи."""
    lines = []
    text = _latin_only(text)
    for ru, en in (('—', '-'), ('–', '-'), ('«', '"'), ('»', '"'), ('…', '...'), ('•', '-'), ('×', 'x')):
        text = text.replace(ru, en)
    for line in text.splitlines():
        line = re.sub(r'[^\x20-\x7E]', ' ', line)
        line = re.sub(r'\s+', ' ', line).strip(' -•|')
        if line or (lines and lines[-1]):
            lines.append(line)
    return '\n'.join(lines).strip()


def _with_tag(text: str, tag: str, limit: int = 5000) -> str:
    text = _clip_units(TAG_RE.sub('', str(text or '')).rstrip(), max(0, limit - 16)).rstrip()
    return (text + '\n\n' if text else '') + f'#SMM:{tag}'


def _preview_sample(platform: str, kind: str) -> Dict[str, Any]:
    pinfo, kinfo = _platform_info(platform), KINDS.get(kind, KINDS['generic'])
    return {'service': '1001', 'name': f"{pinfo['name']} {kinfo['en'].title()} | Real | R30 | 5K/Day | Start 0-1h",
            'category': f"{pinfo['name']} {kinfo['en'].title()}", 'rate': '45', 'min': '10', 'max': '50000',
            'type': 'Default', 'refill': True}


# ---------------------------------------------------------------------------
# Starvell: мост между потоками плагина и асинхронным ядром
# ---------------------------------------------------------------------------
# Логика плагина (заказы, панели, меню) выполняется в рабочих потоках, а Starvell
# Cardinal живёт в asyncio. Корутины ядра запускаются в его цикле событий и
# дожидаются из потока — так общий код остаётся одинаковым для FunPay и Starvell.

_SV: Dict[str, Any] = {'service': None, 'loop': None, 'loop_thread': None, 'bot': None, 'me': None, 'me_name': ''}


def _run(coro: Any, timeout: float = 90) -> Any:
    """Выполняет корутину в цикле событий Starvell Cardinal и ждёт результат из рабочего потока."""
    loop = _SV.get('loop')
    if loop is None or loop.is_closed():
        if asyncio.iscoroutine(coro):
            coro.close()
        raise RuntimeError('Starvell Cardinal ещё не запущен.')
    if threading.get_ident() == _SV.get('loop_thread'):
        if asyncio.iscoroutine(coro):
            coro.close()
        raise RuntimeError('Синхронный вызов из потока событий запрещён.')
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


# ---------------------------------------------------------------------------
# HTTP без requests: обёртка над aiohttp (его ставит сам Starvell Cardinal)
# ---------------------------------------------------------------------------

class _HttpExceptions:
    class RequestException(IOError):
        pass

    class Timeout(RequestException):
        pass

    class ConnectionError(RequestException):
        pass

    class SSLError(ConnectionError):
        pass

    class ProxyError(ConnectionError):
        pass

    class InvalidURL(RequestException):
        pass

    class InvalidSchema(RequestException):
        pass

    class MissingSchema(RequestException):
        pass


class _HttpResponse:
    def __init__(self, status: int, headers: Any, body: bytes, charset: Optional[str]):
        self.status_code = status
        self.headers = headers
        self.content = body
        self.text = body.decode(charset or 'utf-8', 'replace')

    def json(self) -> Any:
        return json.loads(self.text)

    def close(self) -> None:
        pass


class _AioRequests:
    """Синхронный интерфейс requests поверх aiohttp: запрос выполняется в цикле событий бота."""
    exceptions = _HttpExceptions
    RequestException = _HttpExceptions.RequestException
    Timeout = _HttpExceptions.Timeout

    def get(self, url: str, params: Any = None, **kwargs: Any) -> _HttpResponse:
        return self._call('GET', url, params=params, **kwargs)

    def post(self, url: str, data: Any = None, **kwargs: Any) -> _HttpResponse:
        return self._call('POST', url, data=data, **kwargs)

    def _call(self, method: str, url: str, params: Any = None, data: Any = None, headers: Optional[Dict[str, str]] = None,
              timeout: Any = 30, proxies: Optional[Dict[str, str]] = None, allow_redirects: bool = True) -> _HttpResponse:
        connect, read = timeout if isinstance(timeout, (tuple, list)) else (timeout, timeout)
        proxy = (proxies or {}).get('https') or (proxies or {}).get('http')
        return _run(self._request(method, url, params, data, headers, float(connect), float(read), proxy, allow_redirects),
                    timeout=float(connect) + float(read) + 15)

    async def _request(self, method: str, url: str, params: Any, data: Any, headers: Any, connect: float, read: float,
                       proxy: Optional[str], allow_redirects: bool) -> _HttpResponse:
        import aiohttp
        from multidict import CIMultiDict
        errors = _HttpExceptions
        if not re.match(r'https?://', str(url), re.I):
            raise errors.MissingSchema(f'Нет схемы в адресе: {url}')
        connector, proxy_url = None, None
        if proxy:
            if proxy.lower().startswith('socks'):
                try:
                    from aiohttp_socks import ProxyConnector
                except ImportError:
                    raise errors.InvalidSchema('Для SOCKS-прокси нужен пакет aiohttp_socks.') from None
                connector = ProxyConnector.from_url(proxy)
            else:
                proxy_url = proxy
        timeout = aiohttp.ClientTimeout(total=connect + read, sock_connect=connect, sock_read=read)
        try:
            async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
                async with session.request(method, url, params=params, data=data, headers=headers, proxy=proxy_url,
                                           allow_redirects=allow_redirects) as response:
                    body = await response.read()
                    return _HttpResponse(response.status, CIMultiDict(response.headers), body, response.charset)
        except asyncio.TimeoutError:
            raise errors.Timeout('Таймаут запроса.') from None
        except (aiohttp.ClientProxyConnectionError, aiohttp.ClientHttpProxyError):
            raise errors.ProxyError('Ошибка прокси.') from None
        except aiohttp.ClientSSLError:
            raise errors.SSLError('Ошибка SSL.') from None
        except aiohttp.InvalidURL:
            raise errors.InvalidURL('Неверный адрес.') from None
        except aiohttp.ClientError as exc:
            if 'proxy' in type(exc).__name__.lower():
                raise errors.ProxyError('Ошибка прокси.') from None
            raise errors.ConnectionError(type(exc).__name__) from None
        except Exception as exc:
            if 'proxy' in type(exc).__name__.lower():  # ошибки aiohttp_socks
                raise errors.ProxyError(str(exc)) from None
            raise


if requests is None:
    requests = _AioRequests()


def _service() -> Any:
    service = _SV.get('service')
    if service is None:
        raise RuntimeError('Сервис Starvell ещё не подключён.')
    return service


def _sv_api_url() -> str:
    api = getattr(_service(), 'api', None)
    return str(getattr(getattr(api, 'config', None), 'API_URL', '') or 'https://starvell.com/api').rstrip('/')


def _session() -> Any:
    api = getattr(_service(), 'api', None)
    session = getattr(api, 'session', None)
    if session is None:
        raise RuntimeError('API Starvell не инициализирован (проверьте session_cookie).')
    return session


def _is_rate_limited(exc: Exception) -> bool:
    text = str(exc).lower()
    return '429' in text or 'too many' in text or 'rate limit' in text or 'слишком много' in text


FP_MIN_INTERVAL = 0.7


def _fp_call(func: Callable, *args: Any, attempts: int = 4, **kwargs: Any) -> Any:
    """Запрос к Starvell с паузой между вызовами и отступлением при лимите запросов."""
    for attempt in range(attempts):
        with _fp_lock:
            delay = FP_MIN_INTERVAL - (time.time() - _last_fp_call[0])
            if delay > 0:
                time.sleep(delay)
            _last_fp_call[0] = time.time()
        try:
            result = func(*args, **kwargs)
            if asyncio.iscoroutine(result):
                result = _run(result)
            return result
        except Exception as exc:
            if _is_rate_limited(exc) and attempt < attempts - 1:
                wait = 4 * (2 ** attempt)
                logger.warning('[AutoSMM] Starvell rate limit, retry in %ss', wait)
                time.sleep(wait)
                continue
            raise


def _account_ready() -> bool:
    return _SV.get('service') is not None and _SV.get('loop') is not None and bool(_SV.get('me'))


def _api_error(result: Any) -> str:
    """Starvell отвечает {message, data:{code}} при ошибке, success: false — в обёртках ядра."""
    if isinstance(result, dict):
        if result.get('success') is False or result.get('error'):
            return str(result.get('error') or result.get('message') or 'ошибка Starvell')
        if result.get('statusCode') and _int(result.get('statusCode')) >= 400:
            message = result.get('message')
            return '; '.join(map(str, message)) if isinstance(message, list) else str(message or result.get('statusCode'))
    return ''


def _funpay_rejection(exc: Exception) -> Dict[str, Any]:
    return {}


def _rejection_text(exc: Exception, cfg: Optional[Dict[str, Any]] = None) -> str:
    return _safe_detail(str(exc) or type(exc).__name__, cfg, 400)


# ---------------------------------------------------------------------------
# Лоты Starvell (offers)
# ---------------------------------------------------------------------------

def _dig(value: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _unwrap(value: Any) -> Any:
    while isinstance(value, dict) and isinstance(value.get('data'), dict) and len(value) <= 3:
        value = value['data']
    return value


def _offer_info(offer_id: Any) -> Dict[str, Any]:
    """Данные лота: цена, количество, активность, описания."""
    offer_id = str(offer_id).strip()
    getter = getattr(_service(), 'get_offer_info', None) or getattr(getattr(_service(), 'api', None), 'get_offer_info', None)
    if callable(getter):
        info = _fp_call(getter, offer_id)
    else:
        info = _fp_call(_session().get_json, f'{_sv_api_url()}/offers/{offer_id}',
                        referer='https://starvell.com/account/sells', include_sid=True)
    info = _unwrap(info)
    if isinstance(info, dict) and isinstance(info.get('offer'), dict):
        info = info['offer']
    if not isinstance(info, dict) or not info or _api_error(info):
        raise RuntimeError(f'Лот {offer_id} не найден на вашем аккаунте Starvell. {_api_error(info)}'.strip())
    return info


def _offer_title(info: Dict[str, Any]) -> str:
    for path in (('descriptions', 'rus', 'briefDescription'), ('descriptions', 'rus', 'description'),
                 ('descriptions', 'eng', 'briefDescription'), ('briefDescription',), ('title',), ('name',)):
        value = _dig(info, *path)
        if value:
            return re.sub(r'\s+', ' ', str(value)).strip()
    return ''


class _OfferFields:
    """Обёртка лота Starvell с интерфейсом FunPay LotFields, нужным общему коду."""

    def __init__(self, offer_id: Any, info: Dict[str, Any]):
        self.lot_id = _int(offer_id) or offer_id
        self.offer_id = str(offer_id)
        self.info = info
        self.raw_price = info.get('price')
        self.price = _float(self.raw_price)
        self.amount = _int(info.get('availability', info.get('amount')), 0)
        self.active = bool(info.get('isActive', info.get('active', True)))
        self.price_changed = False
        category = info.get('categoryId') or _dig(info, 'category', 'id') or info.get('subCategoryId')
        self.subcategory = SimpleNamespace(id=_int(category))
        title = _offer_title(info)
        self.fields = {'fields[summary][ru]': title, 'fields[name][ru]': title, 'price': str(self.raw_price or ''),
                       'amount': str(self.amount), 'active': 'on' if self.active else '', 'node_id': str(_int(category) or ''),
                       'fields[desc][ru]': str(_dig(info, 'descriptions', 'rus', 'description') or ''),
                       'fields[desc][en]': str(_dig(info, 'descriptions', 'eng', 'description') or '')}


def _lot_fields(lot_id: Any) -> _OfferFields:
    return _OfferFields(lot_id, _offer_info(lot_id))


def _apply_lot_updates(lot_fields: _OfferFields, updates: Dict[str, Any]) -> _OfferFields:
    if 'price' in updates:
        lot_fields.price = _float(updates['price'])
        lot_fields.price_changed = True
    if 'amount' in updates:
        lot_fields.amount = max(0, _int(updates['amount']))
    if 'active' in updates:
        lot_fields.active = updates['active'] in ('on', True, 1, '1')
    return lot_fields


def _price_unit() -> str:
    return (_state_get('starvell', {}) or {}).get('price_unit', 'rub')


def _format_price(value: float, unit: str) -> str:
    if unit == 'kop':
        return str(int(round(value * 100)))
    return f'{value:.2f}' if value >= 1 else _money(round(value, 6))


def _post_offer(offer_id: str, price: str, availability: int, active: bool) -> Any:
    payload = {'price': price, 'availability': int(availability), 'isActive': bool(active)}
    result = _fp_call(_session().post_json, f'{_sv_api_url()}/offers/{offer_id}/partial-update', payload,
                      referer='https://starvell.com/account/sells', include_sid=True)
    error = _api_error(result)
    if error:
        raise RuntimeError(f'Starvell не сохранил лот: {error}')
    return result


def _save_lot_fields(lot_fields: _OfferFields) -> None:
    """partial-update требует цену, количество и активность вместе."""
    if not lot_fields.price_changed:
        raw = lot_fields.raw_price
        price = str(raw) if raw not in (None, '') else _format_price(lot_fields.price, _price_unit())
        _post_offer(lot_fields.offer_id, price, lot_fields.amount, lot_fields.active)
        return
    unit = _price_unit()
    target = lot_fields.price
    _post_offer(lot_fields.offer_id, _format_price(target, unit), lot_fields.amount, lot_fields.active)
    # Сверка: Starvell отдаёт цену лота строкой в рублях. Если записалось в 100 раз
    # больше или меньше — меняем единицы, исправляем и запоминаем.
    try:
        actual = _float(_offer_info(lot_fields.offer_id).get('price'))
    except Exception:
        return
    if target <= 0 or abs(actual - target) <= max(0.011, target * 0.02):
        return
    other = 'kop' if unit == 'rub' else 'rub'
    if abs(actual - target * 100) <= target * 2 or abs(actual - target / 100) <= max(0.011, target * 0.02 / 100):
        _post_offer(lot_fields.offer_id, _format_price(target, other), lot_fields.amount, lot_fields.active)
        state = _state_get('starvell', {}) or {}
        state['price_unit'] = other
        _state_set('starvell', state)
        logger.warning('[AutoSMM] Starvell price unit switched to %s for offer %s', other, lot_fields.offer_id)
        return
    raise RuntimeError(f'Starvell записал цену {actual}, ожидалось {target}. Проверьте лот вручную.')


def _edit_lot(lot_id: Any, updates: Dict[str, Any]) -> _OfferFields:
    lot_fields = _lot_fields(lot_id)
    _apply_lot_updates(lot_fields, updates)
    _save_lot_fields(lot_fields)
    return lot_fields


def _lot_is_active(lot_fields: _OfferFields) -> bool:
    return bool(lot_fields.active)


def _set_lot_active(lot_id: Any, active: bool) -> bool:
    lot_fields = _lot_fields(lot_id)
    if lot_fields.active == active:
        return False
    _apply_lot_updates(lot_fields, {'active': 'on' if active else ''})
    _save_lot_fields(lot_fields)
    return True


def _delete_funpay_lot(lot_id: Any) -> None:
    raise RuntimeError('Starvell не даёт удалять лоты через API — удалите лот на сайте.')


def _write_tag(lot_key: str) -> Tuple[bool, str]:
    """На Starvell лот заказа известен по ID (offerId) — тег в описании не нужен."""
    with config_tx() as tx:
        if lot_key in tx['lot_mapping']:
            tx['lot_mapping'][lot_key]['tag_written'] = True
    return True, 'Starvell сообщает ID лота в заказе — заказы будут распознаваться точно.'


def _node_form(subcategory: int) -> Dict[str, Any]:
    raise RuntimeError('Starvell не даёт создавать лоты через API.')


def _form_limits(form: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, int]:
    return {'summary_ru': 100, 'summary_en': 100, 'desc_ru': 5000, 'desc_en': 5000}


def _create_lot(*args: Any, **kwargs: Any) -> Tuple[int, str, List[str]]:
    raise LotPublishError('Starvell не даёт создавать лоты через API. Создайте лот на сайте и привяжите его.')
def _funpay_lot_id(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, dict):
        for key in ('id', 'lot_id', 'offer_id', 'offerId'):
            if _int(value.get(key)):
                return _int(value.get(key))
        return 0
    if isinstance(value, (int, str)):
        return _int(value)
    for name in ('id', 'lot_id', 'offer_id', 'offerId'):
        if _int(getattr(value, name, 0)):
            return _int(getattr(value, name, 0))
    return 0


def _lot_text(value: Any) -> str:
    for name in ('description', 'summary', 'name', 'title'):
        text = getattr(value, name, None)
        if text:
            return re.sub(r'\s+', ' ', str(text)).strip().lower()
    return ''


def _register_lot(name: str, funpay_id: int, panel_id: str, service_id: Any, quantity: int, domains: List[str], *,
                  source: str, subcategory: Optional[int] = None, batch_id: str = '', tag: str = '', platform: str = '',
                  kind: str = '', template: str = '', price: float = 0.0, markup: Optional[Tuple[str, float]] = None,
                  poll: bool = False, stock: int = 0, tag_written: bool = False, refill: Optional[Dict[str, Any]] = None) -> str:
    with config_tx() as cfg:
        key = _lot_key_by_funpay_id(cfg, funpay_id) if funpay_id else None
        if not key:
            number = 1
            while f'lot_{number}' in cfg['lot_mapping']:
                number += 1
            key = f'lot_{number}'
        old = cfg['lot_mapping'].get(key, {})
        item = {**old, 'name': name, 'funpay_lot_id': funpay_id or None, 'panel_id': str(panel_id), 'service_id': str(service_id),
                'quantity': quantity, 'domains': domains, 'source': old.get('source') or source,
                'subcategory': subcategory or old.get('subcategory'), 'batch_id': batch_id or old.get('batch_id', ''),
                'tag': tag or old.get('tag') or _new_tag(x.get('tag') for x in cfg['lot_mapping'].values()),
                'tag_written': tag_written or old.get('tag_written', False), 'platform': platform, 'kind': kind,
                'template': template or old.get('template', ''), 'poll': poll or old.get('poll', False),
                'created_at': old.get('created_at') or time.time(), 'stock_target': stock or old.get('stock_target', 0)}
        if price:
            item['last_price'] = price
        if markup:
            item['markup_mode'], item['markup_value'] = markup
        if refill:
            item['refill'] = {**_refill_settings(old.get('refill')), **refill}
        cfg['lot_mapping'][key] = item
    return key


# ---------------------------------------------------------------------------
# Уведомления продавцу
# ---------------------------------------------------------------------------

def _admin_ids() -> List[int]:
    raw = getattr(getattr(cardinal_instance, 'telegram', None), 'authorized_users', None) or {}
    values = raw.keys() if isinstance(raw, dict) else raw if isinstance(raw, (list, tuple, set)) else []
    return [_int(value) for value in values if _int(value)]


def _is_admin(event: Any) -> bool:
    return _int(getattr(getattr(event, 'from_user', None), 'id', None)) in _admin_ids()


def _admin_later(text: str) -> None:
    if bot and _READY:
        _notify_admins(text)
    else:
        _pending_admin.append(text)


def _notify_admins(text: str, kb: Optional[InlineKeyboardMarkup] = None, html: bool = False) -> None:
    if not bot:
        _pending_admin.append(text)
        return
    try:
        cfg = load_config()
    except Exception:
        cfg = _default_config()
    targets = [cfg['notification_chat_id']] if cfg.get('notification_chat_id') else _admin_ids()
    body = text if html else _safe_detail(text, cfg, 3500)
    for target in targets:
        try:
            bot.send_message(target, body, reply_markup=kb, parse_mode='HTML' if html else None,
                             disable_web_page_preview=True)
        except Exception as exc:
            logger.warning('[AutoSMM] Admin notification failed: %s', type(exc).__name__)


def _send(c: Any, chat_id: Any, text: str) -> bool:
    """Cardinal.send_message возвращает None, если сообщение не доставлено."""
    if not chat_id or not text:
        return False
    try:
        result = c.send_message(chat_id, _plain(text))
        return result is not None and result is not False
    except Exception as exc:
        logger.warning('[AutoSMM] Starvell message to %s failed: %s', chat_id, type(exc).__name__)
        return False


def _order_url(funpay_id: Any) -> str:
    return ORDER_URL.format(funpay_id)


# ---------------------------------------------------------------------------
# Сопоставление собеседника и заказа
# ---------------------------------------------------------------------------

def _message_values(message: Any, field: str) -> List[str]:
    values: List[str] = []
    value = message.get(field) if isinstance(message, dict) else getattr(message, field, None)
    if value not in (None, '') and not isinstance(value, (dict, list, tuple, set)):
        values.append(str(value))
    chat = message.get('chat') if isinstance(message, dict) else getattr(message, 'chat', None)
    if field == 'chat_id' and chat is not None:
        value = chat.get('id') if isinstance(chat, dict) else getattr(chat, 'id', None)
        if value not in (None, ''):
            values.append(str(value))
    return list(dict.fromkeys(values))


def _chat_key(value: Any) -> Optional[Tuple[str, str]]:
    if value in (None, ''):
        return None
    value = str(value).strip()
    if value.isdigit():
        return ('cid', str(int(value)))
    alias = re.fullmatch(r'users-(\d+)-(\d+)', value)
    if alias:
        return ('users', '-'.join(str(x) for x in sorted(map(int, alias.groups()))))
    return ('opaque', value)


def _sender_matches_buyer(message: Any, data: Dict[str, Any]) -> bool:
    author_ids = _message_values(message, 'author_id') + _message_values(message, 'user_id')
    if data.get('buyer_id') not in (None, '') and str(data['buyer_id']) in author_ids:
        return True
    expected = str(data.get('buyer_username') or '').strip().lstrip('@').lower()
    names = _message_values(message, 'author') + _message_values(message, 'author_username') + _message_values(message, 'username')
    return bool(expected and any(name.strip().lstrip('@').lower() == expected for name in names))


def _message_matches_waiting(message: Any, data: Dict[str, Any]) -> bool:
    author_ids = _message_values(message, 'author_id') + _message_values(message, 'user_id')
    if '0' in author_ids:
        return False
    actual = [key for key in (_chat_key(x) for x in _message_values(message, 'chat_id')) if key]
    saved = [_chat_key(data.get('chat_id'))]
    aliases = data.get('chat_aliases', [])
    if isinstance(aliases, list):
        saved.extend(_chat_key(x) for x in aliases)
    saved = [key for key in saved if key]
    if actual and saved:
        if set(actual) & set(saved):
            return True
        if {key[0] for key in actual} & {key[0] for key in saved}:
            return False
        if any(key[0] == 'opaque' for key in actual + saved):
            return False
    return _sender_matches_buyer(message, data)


def _remember_message_chat(message: Any, data: Dict[str, Any], *, saved_order: bool = False) -> Any:
    values = _message_values(message, 'chat_id')
    if not values:
        return data.get('chat_id')
    current, previous = values[0], str(data.get('chat_id') or '')
    if current == previous:
        return data.get('chat_id')
    aliases = list(data.get('chat_aliases') or []) if isinstance(data.get('chat_aliases'), list) else []
    if previous and previous not in aliases:
        aliases.append(previous)
    data.update(chat_id=int(current) if current.isdigit() else current, chat_aliases=aliases)
    if saved_order:
        _update_order(data['funpay_order_id'], chat_id=data['chat_id'], chat_aliases=aliases)
    else:
        with _wlock:
            if str(data.get('funpay_order_id')) in waiting:
                waiting[str(data['funpay_order_id'])].update(chat_id=data['chat_id'], chat_aliases=aliases)
        _save_waiting()
    return data['chat_id']


# ---------------------------------------------------------------------------
# Определение лота по заказу: тег → точное название → название + параметры
# ---------------------------------------------------------------------------

def _norm_title(value: Any) -> str:
    text = unescape(str(value or '')).replace('\xa0', ' ').lower()
    text = re.sub(r'\s+', ' ', text).strip()
    return re.sub(r',\s*\d+\s*(шт|pcs|ед)\.?$', '', text).strip(' ,')


def _order_texts(order: Any) -> List[str]:
    values = []
    for field in ('description', 'short_description', 'title', 'summary', 'offer_name', 'lot_name'):
        try:
            value = order.get(field) if isinstance(order, dict) else getattr(order, field, None)
        except Exception:
            value = None
        if value:
            values.append(_norm_title(value))
    return list(dict.fromkeys(x for x in values if x))


def _match_by_name(order: Any, lots: Dict[str, Dict[str, Any]]) -> Tuple[Optional[str], int]:
    """(ключ лота, количество кандидатов). Только полное название или название + «, параметр»."""
    texts = _order_texts(order)
    exact, prefix = [], []
    for key, item in lots.items():
        name = _norm_title(item.get('name'))
        if not name:
            continue
        if any(text == name for text in texts):
            exact.append(key)
        elif any(text.startswith(name + ',') for text in texts):
            prefix.append(key)
    candidates = exact or prefix
    return (candidates[0] if len(candidates) == 1 else None), len(candidates)


def _match_by_tag(full_description: str, lots: Dict[str, Dict[str, Any]]) -> Tuple[Optional[str], List[str]]:
    tags = [x.lower() for x in TAG_RE.findall(str(full_description or ''))]
    for tag in tags:
        key = next((k for k, v in lots.items() if v.get('tag') == tag), None)
        if key:
            return key, tags
    return None, tags


def _find_lot(order: Any, c: Any) -> Tuple[Optional[str], str]:
    """Возвращает (ключ лота, причина при неудаче)."""
    lots = load_config()['lot_mapping']
    if not lots:
        return None, ''
    lot_id = _int(getattr(order, 'lot_id', 0))
    if lot_id:
        # Площадка сообщила ID лота (Starvell) — это точное совпадение.
        exact = next((k for k, v in lots.items() if v.get('funpay_lot_id') and _int(v['funpay_lot_id']) == lot_id), None)
        if exact:
            return exact, ''
    key, count = _match_by_name(order, lots)
    if key and count == 1:
        return key, ''
    full_description, error = None, ''
    fetch = getattr(c.account, 'get_order', None)
    if callable(fetch):
        for attempt in range(3):
            try:
                full = _fp_call(fetch, str(order.id), attempts=2)
                full_description = getattr(full, 'full_description', None) or ''
                break
            except Exception as exc:
                error = type(exc).__name__
                time.sleep(2 + attempt * 3)
    if full_description is not None:
        tagged, tags = _match_by_tag(full_description, lots)
        if tagged:
            return tagged, ''
        if tags:
            return None, f'в заказе есть тег #SMM:{tags[0]}, но такого лота нет в плагине'
    if count > 1:
        return None, f'название подходит к {count} лотам — запишите теги в карточках этих лотов'
    if error and not full_description:
        # Без кандидатов по названию это, скорее всего, чужой заказ: не шумим.
        logger.warning('[AutoSMM] Order %s: Starvell order details unavailable (%s), no name match.', getattr(order, 'id', '?'), error)
    return None, ''


# ---------------------------------------------------------------------------
# Возвраты и ручная проверка
# ---------------------------------------------------------------------------

def _refund(c: Any, funpay_id: Any, chat_id: Any, reason: str, *, notify_reason: str = '') -> None:
    funpay_id = str(funpay_id)
    with _orders_lock:
        item = _get_order(funpay_id)
        if item and (item.get('is_refunded') or item.get('refund_attempted')):
            return
        if not item:
            data = waiting.get(funpay_id, {})
            _add_order({**data, 'funpay_order_id': funpay_id, 'status': 'awaiting_refund', 'panel_order_id': '',
                        'created_at': time.time(), 'is_refunded': False, 'cost': 0, 'chat_id': chat_id})
        _update_order(funpay_id, status='awaiting_refund', refund_reason=reason)
    cfg = load_config()
    if not cfg['auto_refunds']:
        _remove_waiting(funpay_id)
        _notify_admins(f'⚠️ Требуется ручной возврат по заказу #{funpay_id}\nПричина: {notify_reason or reason}\n{_order_url(funpay_id)}')
        _advance_queue(c, funpay_id)
        return
    with _orders_lock:
        current = _get_order(funpay_id) or {}
        if current.get('refund_attempted') or current.get('is_refunded'):
            return
        _update_order(funpay_id, refund_attempted=True, status='refunding')
    try:
        _fp_call(c.account.refund, funpay_id, attempts=2)
    except Exception as exc:
        _update_order(funpay_id, status='refund_unknown', refund_error=type(exc).__name__)
        with _wlock:
            if funpay_id in waiting:
                waiting[funpay_id]['step'] = 'review'
        _save_waiting()
        _notify_admins(f'🛑 Не удалось выполнить автовозврат #{funpay_id} ({type(exc).__name__}).\n'
                       f'Причина возврата: {notify_reason or reason}\nВерните деньги вручную: {_order_url(funpay_id)}')
        return
    _update_order(funpay_id, is_refunded=True, status='refunded', cost=0)
    _remove_waiting(funpay_id)
    _notify_admins(f'⚠️ Авто-возврат по заказу #{funpay_id}\nПричина: {notify_reason or reason}')
    _send(c, chat_id, f'❌ Ваши средства возвращены по причине: {reason}')
    _advance_queue(c, funpay_id)


def _manual_review(c: Any, data: Dict[str, Any], reason: str) -> None:
    funpay_id = str(data['funpay_order_id'])
    _update_order(funpay_id, status='submission_unknown', error=reason)
    with _wlock:
        if funpay_id in waiting:
            waiting[funpay_id]['step'] = 'review'
    _save_waiting()
    _notify_admins(f'⚠️ Заказ Starvell #{funpay_id}: запуск в панели #{data["panel_id"]} не подтверждён. {reason}\n'
                   f'Проверьте историю панели по услуге {data["service_id"]}, ссылке {data.get("link", "")} и количеству '
                   f'{data["quantity"]}. Откройте «⚠️ Проверить заказы» в AutoSMM.')
    _send(c, data['chat_id'], '⏳ Ответ SMM-сервиса задержался. Продавец проверит запуск заказа. Повторно отправлять ссылку не нужно.')
    _advance_queue(c, funpay_id, data)


def _recover_submissions() -> int:
    """Перезапуск не может безопасно повторить прерванный платный запрос."""
    count = 0
    with _wlock:
        for data in list(waiting.values()):
            if data.get('step') == 'starting':
                _add_order({**data, 'status': 'submission_unknown', 'panel_order_id': '', 'created_at': time.time(),
                            'is_refunded': False, 'cost': 0})
                data['step'] = 'review'
                count += 1
    with _orders_lock:
        for order in _orders_raw():
            if order.get('status') in {'submitting', 'retry_ready'}:
                order.update(status='submission_unknown', error='Перезапуск во время передачи заказа. Проверьте историю панели.')
                count += 1
            if order.get('status') == 'refunding':
                order.update(status='refund_unknown', refund_error='Перезапуск во время возврата.')
                count += 1
            if order.get('panel_order_id') and str(order.get('funpay_order_id')) in waiting:
                with _wlock:
                    waiting.pop(str(order['funpay_order_id']), None)
        _persist_orders()
    _save_waiting()
    return count


# ---------------------------------------------------------------------------
# Очередь заказов покупателя
# ---------------------------------------------------------------------------

def _buyer_key(data: Dict[str, Any]) -> str:
    if data.get('buyer_id') not in (None, ''):
        return 'id:' + str(data['buyer_id'])
    return 'name:' + str(data.get('buyer_username') or '').lower()


def _buyer_entries(key: str) -> List[Dict[str, Any]]:
    with _wlock:
        items = [v for v in waiting.values() if _buyer_key(v) == key]
    return sorted(items, key=lambda x: (_float(x.get('created_at')), str(x.get('funpay_order_id'))))


def _remove_waiting(funpay_id: Any) -> Optional[Dict[str, Any]]:
    with _wlock:
        data = waiting.pop(str(funpay_id), None)
    _save_waiting()
    return data


def _advance_queue(c: Any, finished_id: Any, finished: Optional[Dict[str, Any]] = None) -> None:
    """Когда активный заказ ушёл в работу или отменён, просим ссылку для следующего."""
    finished = finished or _get_order(finished_id) or {}
    if not finished:
        return
    key = _buyer_key(finished)
    entries = [x for x in _buyer_entries(key) if x.get('step') in ('link', 'poll', 'confirm')]
    if not entries:
        return
    nxt = entries[0]
    if not nxt.get('queued'):
        return
    with _wlock:
        if str(nxt['funpay_order_id']) in waiting:
            waiting[str(nxt['funpay_order_id'])]['queued'] = False
            waiting[str(nxt['funpay_order_id'])]['prompted_at'] = time.time()
    _save_waiting()
    cfg = load_config()
    _send(c, nxt['chat_id'], _format(cfg['messages']['next_in_queue'], {
        'funpay_order_id': nxt['funpay_order_id'], 'remaining': len(entries) - 1,
        'example_link': _example(nxt.get('domains', []), nxt.get('platform', ''), nxt.get('kind', ''))}))


# ---------------------------------------------------------------------------
# Пауза покупателей (/stop, /start) и чёрный список
# ---------------------------------------------------------------------------

def _is_paused(cfg: Dict[str, Any], buyer_id: Any, username: Any) -> bool:
    username = str(username or '').lower()
    for item in cfg.get('paused_buyers', []):
        if buyer_id not in (None, '') and str(item.get('id') or '') == str(buyer_id):
            return True
        if username and str(item.get('username') or '').lower() == username:
            return True
    return False


def _is_blacklisted(c: Any, username: Any) -> bool:
    check = getattr(c, 'is_blacklisted', None)
    if callable(check):
        try:
            return bool(check(username))
        except Exception:
            return False
    names = getattr(c, 'blacklist', None) or []
    return bool(username) and str(username).lower() in {str(x).lower() for x in names}


def _pause_buyer(c: Any, buyer_id: Any, username: str, chat_id: Any) -> None:
    with config_tx() as cfg:
        if not _is_paused(cfg, buyer_id, username):
            cfg['paused_buyers'].append({'id': str(buyer_id or ''), 'username': username, 'since': time.time()})
    key_id, key_name = 'id:' + str(buyer_id or ''), 'name:' + str(username or '').lower()
    dropped = []
    with _wlock:
        for funpay_id, data in list(waiting.items()):
            if _buyer_key(data) in (key_id, key_name) and data.get('step') in ('link', 'poll', 'confirm'):
                waiting.pop(funpay_id, None)
                dropped.append(funpay_id)
    _save_waiting()
    manual = []
    with _orders_lock:
        for order in _orders_raw():
            if _buyer_key(order) in (key_id, key_name) and order.get('panel_order_id') and not order.get('is_refunded') \
                    and str(order.get('status', '')).lower() not in GOOD_STATUSES | BAD_STATUSES | {'manual', 'refunded'}:
                order.update(status='manual', manual_since=time.time(), completion_notified=True)
                manual.append(str(order['funpay_order_id']))
        if manual:
            _persist_orders()
    _notify_admins(f'⏸ Автообработка приостановлена для покупателя {username or "?"} (ID {buyer_id or "?"}).\n'
                   f'Снято ожидание ссылки: {", ".join("#" + x for x in dropped) or "нет"}\n'
                   f'Переведено в ручной режим: {", ".join("#" + x for x in manual) or "нет"}\n'
                   'Чтобы вернуть — отправьте /start в этот чат или нажмите кнопку в «⚙️ Настройки → ⏸ На паузе».')


def _resume_buyer(buyer_id: Any, username: str) -> bool:
    with config_tx() as cfg:
        before = len(cfg['paused_buyers'])
        cfg['paused_buyers'] = [x for x in cfg['paused_buyers'] if not (
            (buyer_id not in (None, '') and str(x.get('id') or '') == str(buyer_id)) or
            (username and str(x.get('username') or '').lower() == str(username).lower()))]
        return len(cfg['paused_buyers']) != before


def _seller_command(c: Any, msg: Any, command: str) -> None:
    chat_id = getattr(msg, 'chat_id', None)
    buyer_id = getattr(msg, 'interlocutor_id', None)
    username = ''
    with _wlock:
        known = [x for x in waiting.values() if _message_matches_waiting({'chat_id': chat_id}, x)]
    known += [x for x in _orders() if _chat_key(x.get('chat_id')) == _chat_key(chat_id)]
    if known:
        buyer_id = buyer_id or known[-1].get('buyer_id')
        username = str(known[-1].get('buyer_username') or '')
    if not username:
        username = str(getattr(msg, 'chat_name', '') or '')
    if not username and not buyer_id:
        try:
            chat = _fp_call(c.account.get_chat, int(chat_id), False)
            username = str(getattr(chat, 'name', '') or '')
        except Exception:
            pass
    if not username and not buyer_id:
        _notify_admins(f'⚠️ Не удалось определить покупателя для команды {command} в чате {chat_id}. Повторите позже.')
        return
    if command == '/stop':
        _pause_buyer(c, buyer_id, username, chat_id)
    elif _resume_buyer(buyer_id, username):
        _notify_admins(f'▶️ Автообработка возобновлена для покупателя {username or "?"} (ID {buyer_id or "?"}).')
    else:
        _notify_admins(f'ℹ️ Покупатель {username or "?"} не был на паузе.')


# ---------------------------------------------------------------------------
# Новый заказ
# ---------------------------------------------------------------------------

def _resolve_chat(c: Any, order: Any) -> Any:
    buyer = getattr(order, 'buyer_id', None)
    username = str(getattr(order, 'buyer_username', '') or '')
    chat = getattr(order, 'chat_id', None)
    lookup = getattr(c.account, 'get_chat_by_name', None)
    if username and callable(lookup):
        try:
            cached = lookup(username, False)
            if getattr(cached, 'id', None) and str(getattr(cached, 'name', '') or '').lower() == username.lower():
                chat = cached.id
        except Exception:
            pass
    if not chat and buyer and getattr(c.account, 'id', None):
        chat = f'users-{buyer}-{c.account.id}'
    return chat


def _handle_new_order(c: Any, order: Any) -> None:
    funpay_id = str(order.id)
    with _order_lock(funpay_id):
        with _wlock:
            if funpay_id in waiting:
                return
        if _get_order(funpay_id):
            return
        cfg = load_config()
        if not cfg['lot_mapping']:
            return
        key, reason = _find_lot(order, c)
        if not key:
            if reason:
                _notify_admins(f'⚠️ AutoSMM не определил лот заказа #{funpay_id}: {reason}.\n'
                               f'Обработайте заказ вручную: {_order_url(funpay_id)}')
            else:
                logger.info('[AutoSMM] Order %s is not an AutoSMM lot.', funpay_id)
            return
        cfg = load_config()
        lot = cfg['lot_mapping'][key]
        buyer = getattr(order, 'buyer_id', None)
        username = str(getattr(order, 'buyer_username', '') or '')
        if cfg.get('respect_blacklist') and _is_blacklisted(c, username):
            _notify_admins(f'🚫 Заказ #{funpay_id} от {username} из чёрного списка — автонакрутка не запущена.\n{_order_url(funpay_id)}')
            return
        if _is_paused(cfg, buyer, username):
            _notify_admins(f'⏸ Новый заказ #{funpay_id} от покупателя {username}, который сейчас на паузе. '
                           f'Накрутка не запущена — обработайте вручную.\n{_order_url(funpay_id)}')
            return
        chat = _resolve_chat(c, order)
        if not chat or not buyer:
            _notify_admins(f'⚠️ Заказ #{funpay_id}: не удалось определить чат покупателя. Обработайте вручную.\n{_order_url(funpay_id)}')
            return
        amount = max(1, _int(getattr(order, 'amount', 1), 1))
        price = _float(getattr(order, 'price', 0))
        refill = lot['refill']
        now = time.time()
        try:
            paid_at = getattr(order, 'date', None).timestamp()
            paid_at = paid_at if abs(paid_at - now) < 86400 else now
        except Exception:
            paid_at = now
        entry = {'buyer_id': buyer, 'buyer_username': username, 'chat_id': chat, 'funpay_order_id': funpay_id,
                 'lot_key': key, 'lot_name': lot['name'], 'panel_id': lot['panel_id'], 'service_id': lot['service_id'],
                 'quantity': amount * lot['quantity'], 'amount': amount, 'price': price, 'domains': lot['domains'],
                 'platform': lot.get('platform', ''), 'kind': lot.get('kind', ''), 'poll': bool(lot.get('poll')),
                 'step': 'link', 'created_at': paid_at, 'queued': False, 'prompted_at': now,
                 'refill_terms': {'enabled': refill['enabled'], 'left': refill['limit'] if refill['limit_enabled'] else None,
                                  'hours': refill['hours'] if refill['period_enabled'] else None}}
        with _wlock:
            # Проверка очереди и вставка — атомарно: два одновременных заказа
            # одного покупателя не станут активными оба.
            if funpay_id in waiting:
                return
            active = [x for x in _buyer_entries(_buyer_key(entry)) if x.get('step') in ('link', 'poll', 'confirm', 'starting')]
            entry['queued'] = bool(active)
            waiting[funpay_id] = entry
        _save_waiting()
    if entry['queued']:
        _send(c, chat, _format(cfg['messages']['queued'], {'funpay_id': funpay_id, 'funpay_order_id': funpay_id,
                                                         'position': len(active) + 1}))
    else:
        _send(c, chat, _format(cfg['messages']['after_payment'], {
            'example_link': _example(lot['domains'], lot.get('platform', ''), lot.get('kind', '')), 'buyer_username': username,
            'funpay_order_id': funpay_id, 'service_title': lot['name']}))
    if refill['enabled'] and cfg['refill'].get('send_moment') == 'after_payment':
        _send_refill_info(c, entry, None)
    if cfg.get('notify_new_order'):
        _notify_admins(f'🛍 Новый SMM заказ #{funpay_id}\nЛот: {lot["name"]}\nПокупатель: {username}\n'
                       f'Количество: {amount} шт. → {entry["quantity"]} ед.\nСумма: {price:.2f} ₽'
                       + ('\n📥 Поставлен в очередь' if entry['queued'] else ''))
    logger.info('[AutoSMM] Order %s accepted (lot %s), waiting for a link.', funpay_id, key)


# ---------------------------------------------------------------------------
# Сообщения покупателя
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r'(?:https?://[^\s<>"\']+|(?:www\.)?(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s<>"\']*)?|@[a-z0-9_]{4,32})', re.I)


def _extract_link(text: str, data: Dict[str, Any]) -> str:
    for match in _LINK_RE.finditer(text):
        value = match.group(0).rstrip('.,;!?)]»"\'')
        if value.startswith('@'):
            if data.get('platform') == 'telegram' or any(d in ('t.me', 'telegram.me') for d in data.get('domains', [])):
                return 'https://t.me/' + value[1:]
            continue
        if not re.match(r'https?://', value, re.I):
            host = value.split('/')[0].lower().removeprefix('www.')
            known = set(data.get('domains') or []) | set(DEFAULT_DOMAINS)
            if '*' in (data.get('domains') or []) or host in known or any(host.endswith('.' + x) for x in known):
                value = 'https://' + value
            else:
                continue
        return value
    return ''


def _valid_link(link: str, domains: List[str], check: bool = True) -> Tuple[bool, str]:
    parsed = urlparse(link.strip())
    if parsed.scheme not in ('https', 'http') or not parsed.netloc:
        return False, '❌ Нужна полная ссылка, например https://...'
    if not check:
        return True, ''
    host = (parsed.hostname or '').lower().removeprefix('www.')
    allowed = _domains(domains) or _global_domains()
    if '*' in allowed or host in allowed or any(host.endswith('.' + x) for x in allowed):
        return True, ''
    return False, f'❌ Домен {host} не подходит для этого заказа. Разрешены: {", ".join(allowed[:8])}.'


def _confirm_word(text: str) -> Optional[bool]:
    value = text.strip().lower().rstrip('!.')
    if value in YES_WORDS or value.startswith('+'):
        return True
    if value in NO_WORDS or value.startswith('-'):
        return False
    return None


def _set_waiting(funpay_id: str, **updates: Any) -> Dict[str, Any]:
    with _wlock:
        if funpay_id in waiting:
            waiting[funpay_id].update(updates)
            data = deepcopy(waiting[funpay_id])
        else:
            data = {}
    _save_waiting()
    return data


def _ask_confirmation(c: Any, chat: Any, data: Dict[str, Any]) -> None:
    lines = [f'✅ Ссылка для заказа #{data["funpay_order_id"]} принята: {data["link"]}']
    if data.get('answer_number'):
        lines.append(f'🔢 Вариант ответа: {data["answer_number"]}')
    lines.append(f'📦 Количество: {data["quantity"]}')
    lines.append('\nОтправьте «+», чтобы запустить, или «-», чтобы ввести ссылку заново.')
    _send(c, chat, '\n'.join(lines))


def _process_buyer_input(c: Any, msg: Any, text: str) -> bool:
    """Возвращает True, если сообщение относилось к ожидающему заказу."""
    with _wlock:
        matches = [deepcopy(d) for d in waiting.values() if _message_matches_waiting(msg, d)]
    if not matches:
        return False
    addressed = re.fullmatch(r'!smm\s+#?([A-Za-z0-9]+)\s+(.+)', text, re.I | re.S)
    if addressed:
        requested, text = addressed.group(1).upper(), addressed.group(2).strip()
        matches = [d for d in matches if str(d['funpay_order_id']).upper() == requested]
        if not matches:
            _send(c, getattr(msg, 'chat_id', None), '❌ Не найден ожидающий заказ с таким номером.')
            return True
    matches.sort(key=lambda x: (_float(x.get('created_at')), str(x.get('funpay_order_id'))))
    actionable = [d for d in matches if d.get('step') in ('link', 'poll', 'confirm') and (addressed or not d.get('queued'))]
    starting = [d for d in matches if d.get('step') == 'starting']
    if not actionable and not starting:
        queued = [d for d in matches if d.get('step') in ('link', 'poll', 'confirm')]
        if queued:
            # Самовосстановление очереди: активного заказа нет — берём первый из очереди.
            queued[0] = _set_waiting(str(queued[0]['funpay_order_id']), queued=False) or queued[0]
            actionable = [queued[0]]
    if not actionable:
        chat = _remember_message_chat(msg, matches[0])
        if any(d.get('step') == 'starting' for d in matches):
            if _extract_link(text, matches[0]) or _confirm_word(text) is not None:
                _send(c, chat, '⏳ Заказ уже передаётся в SMM-сервис. Пожалуйста, подождите.')
        elif any(d.get('step') == 'review' for d in matches):
            if _extract_link(text, matches[0]) or _confirm_word(text) is not None:
                _send(c, chat, '⏳ Продавец проверяет запуск заказа. Повторный запуск не нужен.')
        return True
    data = actionable[0]
    funpay_id = str(data['funpay_order_id'])
    chat = _remember_message_chat(msg, data)
    cfg = load_config()
    if data['step'] == 'link' or (data['step'] == 'confirm' and _extract_link(text, data)):
        link = _extract_link(text, data)
        if not link:
            if time.time() - _float(data.get('hint_at')) >= max(1, cfg.get('link_hint_minutes', 10)) * 60:
                _set_waiting(funpay_id, hint_at=time.time())
                example = _example(data.get('domains', []), data.get('platform', ''), data.get('kind', ''))
                _send(c, chat, f'🔗 Не вижу ссылки. Отправьте полную ссылку для заказа #{funpay_id}.\n📎 Пример: {example}')
            return True
        ok, reason = _valid_link(link, data.get('domains', []), cfg.get('domain_check', True))
        if not ok:
            _send(c, chat, reason)
            return True
        if data.get('poll') and not data.get('answer_number'):
            _set_waiting(funpay_id, link=link, step='poll')
            _send(c, chat, cfg['messages']['poll_request'])
            return True
        data = _set_waiting(funpay_id, link=link, step='confirm' if cfg['confirm_link'] else 'starting')
        if cfg['confirm_link']:
            _ask_confirmation(c, chat, data)
        else:
            _send(c, chat, '⏳ Ссылка принята. Запускаю заказ…')
            _submit(_start_order, c, funpay_id)
        return True
    if data['step'] == 'poll':
        number = re.fullmatch(r'#?\s*(\d{1,3})', text.strip())
        if not number or _int(number.group(1)) <= 0:
            _send(c, chat, '🔢 Нужно число — номер варианта ответа (например: 1).')
            return True
        data = _set_waiting(funpay_id, answer_number=_int(number.group(1)), step='confirm' if cfg['confirm_link'] else 'starting')
        if cfg['confirm_link']:
            _ask_confirmation(c, chat, data)
        else:
            _send(c, chat, '⏳ Принято. Запускаю заказ…')
            _submit(_start_order, c, funpay_id)
        return True
    if data['step'] == 'confirm':
        answer = _confirm_word(text)
        if answer is True:
            _set_waiting(funpay_id, step='starting')
            _send(c, chat, '⏳ Подтверждено. Запускаю заказ в SMM-сервисе…')
            _submit(_start_order, c, funpay_id)
        elif answer is False:
            with _wlock:
                if funpay_id in waiting:
                    waiting[funpay_id].pop('link', None)
                    waiting[funpay_id].pop('answer_number', None)
                    waiting[funpay_id]['step'] = 'link'
            _save_waiting()
            _send(c, chat, '✏️ Хорошо, отправьте другую ссылку.')
        elif time.time() - _float(data.get('hint_at')) >= 60:
            _set_waiting(funpay_id, hint_at=time.time())
            _send(c, chat, 'Отправьте «+», чтобы запустить заказ, или «-», чтобы изменить ссылку.')
        return True
    return True


def _status_message(result: Dict[str, Any]) -> str:
    lines = ['📊 Статус заказа', f"• Состояние: {_plain_status(result.get('status'))}"]
    for key, label in (('start_count', 'Стартовое значение'), ('remains', 'Осталось')):
        value = result.get(key)
        if value not in (None, ''):
            lines.append(f'• {label}: {_plain(value)}')
    return '\n'.join(lines)


def _plain_status(value: Any) -> str:
    text = _plain(value)
    labels = {'in progress': 'В процессе', 'processing': 'В процессе', 'pending': 'В очереди', 'completed': 'Выполнен',
              'complete': 'Выполнен', 'partial': 'Частично выполнен', 'canceled': 'Отменён', 'cancelled': 'Отменён',
              'failed': 'Ошибка выполнения', 'manual': 'Ведёт продавец'}
    return labels.get(text.lower(), text or 'Неизвестно')


def _check_command(c: Any, msg: Any, panel_order_id: str) -> None:
    chat = getattr(msg, 'chat_id', None)
    owned = [x for x in _orders() if str(x.get('panel_order_id')) == panel_order_id and _message_matches_waiting(msg, x)]
    if len(owned) != 1:
        _send(c, chat, '❌ Заказ не найден в базе бота. Проверьте номер из сообщения о запуске.')
        return
    order = owned[0]
    chat = _remember_message_chat(msg, order, saved_order=True)
    if str(order.get('status', '')).lower() == 'manual':
        _send(c, chat, 'ℹ️ Этот заказ ведёт продавец вручную.')
        return
    try:
        result = PanelAPI(load_config(), order['panel_id']).status(order['panel_order_id'])
        _send(c, chat, _status_message(result))
    except PanelError:
        _send(c, chat, '❌ SMM-сервис сейчас не ответил. Попробуйте позже.')


def auto_smm_handler(c: Any, event: Any, *args: Any) -> None:
    try:
        if isinstance(event, NewMessageEvent):
            _on_message(c, event.message)
        elif isinstance(event, NewOrderEvent):
            order = event.order
            status = getattr(order, 'status', None)
            paid = getattr(getattr(enums, 'OrderStatuses', None), 'PAID', None)
            if status is not None and paid is not None and status != paid:
                return
            _submit(_handle_new_order, c, order)
    except Exception:
        logger.exception('[AutoSMM] Event handler failed')


def _on_message(c: Any, msg: Any) -> None:
    author = str(getattr(msg, 'author_id', None))
    text = str(getattr(msg, 'text', '') or '').strip()
    if not text or author == '0':
        return
    if author == str(getattr(c.account, 'id', '')):
        if text.lower() in ('/stop', '/start'):
            _submit(_seller_command, c, msg, text.lower())
        return
    cfg = load_config()
    username = str(getattr(msg, 'author', '') or getattr(msg, 'chat_name', '') or '')
    if _is_paused(cfg, getattr(msg, 'author_id', None), username):
        return
    if cfg.get('respect_blacklist') and _is_blacklisted(c, username):
        return
    command = re.fullmatch(r'(?:чек|check|статус|status)\s+#?(\d+)', text.lower())
    if command:
        _submit(_check_command, c, msg, command.group(1))
        return
    if _handle_refill_input(c, msg, text):
        return
    _process_buyer_input(c, msg, text)


# ---------------------------------------------------------------------------
# Запуск заказа в панели
# ---------------------------------------------------------------------------

def _start_order(c: Any, funpay_id: str, retry: bool = False) -> None:
    funpay_id = str(funpay_id)
    with _order_lock(funpay_id):
        _start_order_locked(c, funpay_id, retry)


def _start_order_locked(c: Any, funpay_id: str, retry: bool) -> None:
    with _wlock:
        data = deepcopy(waiting.get(funpay_id))
    existing = _get_order(funpay_id)
    if data is None:
        if not (retry and existing):
            return
        data = deepcopy(existing)
    if existing and (existing.get('is_refunded') or existing.get('status') not in {'retry_ready', 'preflight_failed'}):
        return
    record = {**data, 'funpay_order_id': funpay_id, 'panel_order_id': '', 'status': 'submitting', 'cost': 0,
              'created_at': _float(data.get('created_at')) or time.time(), 'submitted_at': time.time(),
              'is_refunded': False, 'completion_notified': False}
    record.pop('step', None)
    if existing:
        _update_order(funpay_id, **record)
    else:
        _add_order(record)
    _set_waiting(funpay_id, step='starting')
    chat = data['chat_id']
    cfg = load_config()
    # Покупатель мог получить возврат вне плагина: проверяем до платного запроса.
    fetch = getattr(c.account, 'get_order', None)
    if callable(fetch):
        try:
            actual = _fp_call(fetch, funpay_id, attempts=3)
            status = getattr(actual, 'status', None)
            status_name = str(getattr(status, 'name', status or '')).lower().split('.')[-1]
            if status_name not in {'paid', 'closed', 'refunded', 'partially_refunded'}:
                raise RuntimeError('status ' + status_name)
        except Exception as exc:
            _update_order(funpay_id, status='preflight_failed', error='Не удалось подтвердить оплату на Starvell.')
            _set_waiting(funpay_id, step='confirm')
            _notify_admins(f'⚠️ Заказ #{funpay_id}: не удалось проверить оплату на Starvell ({type(exc).__name__}). '
                           'Запрос в панель не отправлялся.')
            _send(c, chat, '⏳ Не удалось проверить оплату на Starvell. Заказ пока не отправлен. Повторите «+» через минуту.')
            return
        if status_name in ('refunded', 'partially_refunded'):
            _update_order(funpay_id, status='refunded', is_refunded=True, cost=0)
            _remove_waiting(funpay_id)
            _send(c, chat, 'ℹ️ Этот заказ отменён/возвращён на Starvell — накрутка не запускается.')
            _advance_queue(c, funpay_id)
            return
    request_started = False
    try:
        api = PanelAPI(cfg, data['panel_id'])
        service = api.service(data['service_id'])
        quantity = _int(data['quantity'])
        if quantity < max(1, _int(service.get('min'), 1)) or (_int(service.get('max')) > 0 and quantity > _int(service.get('max'))):
            raise PanelError(f'Количество {quantity} вне лимитов услуги ({service.get("min")}–{service.get("max")}).')
        if str(service.get('type', 'Default')).strip().lower() not in ('default', 'poll'):
            raise PanelError('Автозапуск поддерживает только услуги типа Default и Poll.')
        cost_rub, cost_native, currency = _cost_rub(cfg, data['panel_id'], service, quantity, api)
        if not math.isfinite(cost_rub) or cost_rub <= 0:
            raise PanelError('Панель вернула некорректную стоимость услуги.')
        balance = api.balance()
        funds = _float(balance.get('balance'), float('nan'))
        if not math.isfinite(funds) or funds < cost_native:
            raise PanelError(f'На балансе панели недостаточно средств ({funds:g} {currency}, нужно {cost_native:.4f}).')
        price = _float(data.get('price'))
        if cfg['anti_dumping'] and price > 0 and price - cost_rub < _float(cfg['min_margin']):
            _update_order(funpay_id, cost=0, error='Антидемпинг')
            _refund(c, funpay_id, chat, 'стоимость услуги изменилась, заказ не может быть выполнен по этой цене.',
                    notify_reason=f'антидемпинг: цена {price:.2f} ₽, себестоимость сейчас {cost_rub:.2f} ₽, '
                                  f'мин. прибыль {cfg["min_margin"]} ₽. Обновите цену лота.')
            return
        if cfg['anti_dumping'] and price <= 0:
            _notify_admins(f'⚠️ Заказ #{funpay_id}: Starvell не передал цену заказа — антидемпинг пропущен.')
        _update_order(funpay_id, cost=round(cost_rub, 6), cost_native=cost_native, currency=currency)
        if (_get_order(funpay_id) or {}).get('is_refunded'):
            return
        extra = {'answer_number': data.get('answer_number')} if data.get('answer_number') else {}
        request_started = True
        panel_order = api.add(data['service_id'], data['link'], quantity, **extra)
    except Exception as exc:
        reason = _safe_detail(str(exc) if isinstance(exc, PanelError) else type(exc).__name__, cfg, 650)
        logger.error('[AutoSMM] Start order %s: %s', funpay_id, reason)
        if request_started and (not isinstance(exc, PanelError) or exc.uncertain):
            _manual_review(c, data, reason)
        else:
            _update_order(funpay_id, cost=0, error=reason)
            critical = 'не найдена' in reason and 'Панель' in reason
            _notify_admins(('❌ Критическая ошибка AutoSMM' if critical else '❌ Ошибка провайдера накрутки') +
                           f'\nЗаказ #{funpay_id} не запущен: {reason}')
            _send(c, chat, cfg['messages']['order_error'])
            _refund(c, funpay_id, chat, 'не удалось запустить услугу.', notify_reason=reason)
        return
    current = _get_order(funpay_id) or {}
    refunded_during_send = bool(current.get('is_refunded'))
    terms = data.get('refill_terms') or {}
    refill = {'enabled': bool(terms.get('enabled')), 'left': terms.get('left'), 'used': 0,
              'deadline': (time.time() + _int(terms['hours']) * 3600) if terms.get('hours') else None}
    _update_order(funpay_id, panel_order_id=panel_order, status='refunded' if refunded_during_send else 'pending',
                  started_at=time.time(), refill=refill)
    _remove_waiting(funpay_id)
    if refunded_during_send:
        _notify_admins(f'⚠️ Заказ #{funpay_id} возвращён на Starvell во время запуска, но панель создала #{panel_order}.')
        return
    values = {'panel_order_id': panel_order, 'order_id': panel_order, 'funpay_order_id': funpay_id, 'link': data['link']}
    if not _send(c, chat, _format(cfg['messages']['after_confirmation'], values)):
        _notify_admins(f'⚠️ Заказ #{funpay_id} запущен в панели (#{panel_order}), но сообщение покупателю не доставлено.')
    if refill['enabled'] and cfg['refill'].get('send_moment') == 'after_start':
        _send_refill_info(c, {**data, 'panel_order_id': panel_order}, refill)
    _advance_queue(c, funpay_id, {**data, 'funpay_order_id': funpay_id})
    _submit(_refresh_balance_quiet, data['panel_id'])


def _refresh_balance_quiet(panel_id: str) -> None:
    try:
        PanelAPI(load_config(), panel_id).balance()
    except PanelError:
        pass


def on_funpay_order_status_changed(c: Any, event: Any, *args: Any) -> None:
    order = getattr(event, 'order', None)
    status = getattr(order, 'status', None)
    name = str(getattr(status, 'name', status or '')).lower().split('.')[-1]
    if name not in ('refunded', 'partially_refunded'):
        return
    funpay_id = str(getattr(order, 'id', ''))
    data = _remove_waiting(funpay_id) if funpay_id in waiting else None
    current = _get_order(funpay_id)
    if data and not current:
        _add_order({**data, 'status': 'refunded', 'is_refunded': True, 'panel_order_id': '', 'cost': 0,
                    'created_at': time.time(), 'refund_reason': 'Возврат подтверждён событием Starvell.'})
    elif current and not current.get('is_refunded'):
        status_now = str(current.get('status') or '').lower()
        _update_order(funpay_id, status='refunded' if not current.get('panel_order_id') or status_now in BAD_STATUSES else status_now,
                      is_refunded=True)
    if data:
        _send(c, data.get('chat_id'), 'ℹ️ Заказ отменён/возвращён — накрутка по нему не запускается.')
        _advance_queue(c, funpay_id, data)


# ---------------------------------------------------------------------------
# Рефилл
# ---------------------------------------------------------------------------

def _fmt_deadline(ts: Any) -> str:
    return datetime.fromtimestamp(_float(ts)).strftime('%d.%m.%Y %H:%M') if ts else 'бессрочно'


def _fmt_hours(hours: Any) -> str:
    hours = _int(hours)
    if not hours:
        return 'бессрочно'
    return f'{hours} ч' + (f' ({hours // 24} {_plural(hours // 24, ("день", "дня", "дней"))})' if hours % 24 == 0 and hours >= 24 else '')


def _send_refill_info(c: Any, data: Dict[str, Any], refill: Optional[Dict[str, Any]]) -> None:
    cfg = load_config()
    terms = data.get('refill_terms') or {}
    left = refill.get('left') if refill else terms.get('left')
    deadline = refill.get('deadline') if refill else ((time.time() + _int(terms['hours']) * 3600) if terms.get('hours') else None)
    _send(c, data.get('chat_id'), _format(cfg['messages']['refill_info'], {
        'command': '!рефилл', 'order_id': data.get('panel_order_id', ''), 'funpay_order_id': data.get('funpay_order_id', ''),
        'link': data.get('link', ''), 'refill_limit': 'без ограничений' if left is None else left,
        'refill_period': _fmt_hours(terms.get('hours')), 'refill_deadline': _fmt_deadline(deadline)}))


def _refill_eligible(order: Dict[str, Any]) -> bool:
    refill = order.get('refill') or {}
    if not refill.get('enabled') or not order.get('panel_order_id') or order.get('is_refunded'):
        return False
    if str(order.get('status', '')).lower() in BAD_STATUSES | {'manual', 'refunded', 'submission_unknown'}:
        return False
    if refill.get('left') is not None and _int(refill['left']) <= 0:
        return False
    if refill.get('deadline') and _float(refill['deadline']) < time.time():
        return False
    return True


def _refill_key(msg: Any) -> str:
    return '|'.join(_message_values(msg, 'chat_id')) + '|' + '|'.join(_message_values(msg, 'author_id'))


def _handle_refill_input(c: Any, msg: Any, text: str) -> bool:
    chat = getattr(msg, 'chat_id', None)
    key = _refill_key(msg)
    pending = _refill_choices.get(key)
    if pending and pending['expires'] < time.time():
        _refill_choices.pop(key, None)
        pending = None
    if pending:
        value = text.strip().lower()
        if value in NO_WORDS:
            _refill_choices.pop(key, None)
            _send(c, chat, 'Хорошо, рефилл не запускаем.')
            return True
        if value.isdigit() and 1 <= _int(value) <= len(pending['orders']):
            _refill_choices.pop(key, None)
            _submit(_run_refill, c, msg, pending['orders'][_int(value) - 1])
            return True
    match = REFILL_RE.fullmatch(text.strip())
    if not match:
        return False
    owned = [x for x in _orders() if _message_matches_waiting(msg, x) and _refill_eligible(x)]
    if match.group(1):
        owned = [x for x in owned if str(x.get('panel_order_id')) == match.group(1) or str(x.get('funpay_order_id')) == match.group(1)]
    if not owned:
        _send(c, chat, '♻️ Сейчас нет заказов, для которых доступен рефилл (не включён, истёк срок или исчерпан лимит).')
        return True
    owned.sort(key=lambda x: _float(x.get('started_at') or x.get('created_at')), reverse=True)
    owned = owned[:10]
    if len(owned) == 1:
        _submit(_run_refill, c, msg, str(owned[0]['funpay_order_id']))
        return True
    lines = ['♻️ Выберите заказ для рефилла — отправьте его номер в списке:']
    for index, order in enumerate(owned, 1):
        refill = order.get('refill') or {}
        left = 'без лимита' if refill.get('left') is None else f'осталось {refill["left"]}'
        lines.append(f'{index}. Заказ {order["panel_order_id"]} · {left} · до {_fmt_deadline(refill.get("deadline"))}\n   {order.get("link", "")}')
    lines.append('\nДля отмены отправьте «-».')
    _refill_choices[key] = {'orders': [str(x['funpay_order_id']) for x in owned], 'expires': time.time() + 900}
    _send(c, chat, '\n'.join(lines))
    return True


def _run_refill(c: Any, msg: Any, funpay_id: str) -> None:
    chat = getattr(msg, 'chat_id', None)
    with _order_lock('refill:' + funpay_id):
        order = _get_order(funpay_id)
        if not order or not _refill_eligible(order):
            _send(c, chat, '♻️ Рефилл для этого заказа сейчас недоступен.')
            return
        if time.time() - _float(order.get('last_refill_at')) < 120:
            _send(c, chat, '⏳ Рефилл по этому заказу уже запущен. Подождите.')
            return
        refill = dict(order.get('refill') or {})
        if refill.get('left') is not None:
            refill['left'] = _int(refill['left']) - 1
        refill['used'] = _int(refill.get('used')) + 1
        _update_order(funpay_id, refill=refill, last_refill_at=time.time())
        try:
            refill_id = PanelAPI(load_config(), order['panel_id']).refill(order['panel_order_id'])
        except PanelError as exc:
            restored = dict(refill)
            if restored.get('left') is not None:
                restored['left'] = _int(restored['left']) + 1
            restored['used'] = max(0, _int(restored.get('used')) - 1)
            _update_order(funpay_id, refill=restored, last_refill_at=0)
            _send(c, chat, '⚠️ Сервис сейчас не принял запрос на рефилл. Попытка не списана — продавец уже уведомлён.')
            _notify_admins(f'⚠️ Рефилл не создан\nЗаказ #{funpay_id} (панель {order["panel_order_id"]})\nОтвет: {exc}')
            return
    left = '' if refill.get('left') is None else f'\n🔁 Осталось запусков: {refill["left"]}'
    _send(c, chat, f'♻️ Рефилл запущен (№ {refill_id}). Восстановление занимает от нескольких часов до суток.{left}')
    if load_config()['refill'].get('notify', True):
        _notify_admins(f'♻️ Запущен рефилл по заказу #{funpay_id} (панель {order["panel_order_id"]}, рефилл {refill_id}).')


# ---------------------------------------------------------------------------
# Слежение за заказами
# ---------------------------------------------------------------------------

def _monitor_pass(c: Any) -> None:
    cfg = load_config()
    by_panel: Dict[str, List[Dict[str, Any]]] = {}
    for order in _orders():
        status = str(order.get('status', '')).strip().lower()
        if order.get('is_refunded') or not order.get('panel_order_id'):
            continue
        if status in {'partial', 'partial_resolved', 'awaiting_refund', 'refunding', 'refund_unknown', 'submission_unknown',
                      'manual', 'refunded'} | BAD_STATUSES:
            continue
        if status in GOOD_STATUSES and order.get('completion_notified') is not False:
            continue
        by_panel.setdefault(str(order['panel_id']), []).append(order)
    for panel_id, orders in by_panel.items():
        try:
            api = PanelAPI(cfg, panel_id)
            results = api.statuses([str(x['panel_order_id']) for x in orders])
        except PanelError:
            continue
        for order in orders:
            result = results.get(str(order['panel_order_id']))
            if result:
                try:
                    _apply_status(c, cfg, api, order, result)
                except Exception:
                    logger.exception('[AutoSMM] Status update %s failed', order.get('funpay_order_id'))


def _apply_status(c: Any, cfg: Dict[str, Any], api: PanelAPI, order: Dict[str, Any], result: Dict[str, Any]) -> None:
    funpay_id = order['funpay_order_id']
    previous = str(order.get('status', '')).strip().lower()
    status = str(result.get('status', 'pending')).strip().lower()
    charge = _float(result.get('charge'), -1)
    if status in BAD_STATUSES:
        _update_order(funpay_id, status=status, cost=0, panel_charge=charge)
        _refund(c, funpay_id, order['chat_id'], 'SMM-сервис не смог выполнить заказ.',
                notify_reason=f'панель вернула статус «{status}» по заказу {order["panel_order_id"]}')
        return
    if status == 'partial':
        updates: Dict[str, Any] = {'status': 'partial', 'remains': result.get('remains')}
        if charge >= 0:
            try:
                updates['cost'] = round(charge * _fx_to_rub(order.get('currency') or api.currency(), cfg), 6)
            except PanelError:
                pass
        _update_order(funpay_id, **updates)
        _send(c, order['chat_id'], f'ℹ️ Заказ {order["panel_order_id"]} выполнен частично (осталось {result.get("remains")}). '
                                   'Продавец свяжется с вами по оставшейся части.')
        _notify_admins(f'⚠️ Заказ #{funpay_id} выполнен частично. Остаток: {result.get("remains")}. '
                       f'Решите вопрос с покупателем вручную.\n{_order_url(funpay_id)}')
        return
    if status in GOOD_STATUSES:
        started = _float(order.get('started_at') or order.get('submitted_at') or order.get('created_at'))
        guard = max(_int(cfg.get('early_complete_minutes'), 5), api.quirks['early_minutes']) * 60
        if previous not in GOOD_STATUSES and time.time() - started < guard:
            return
        seen = _int(order.get('complete_seen')) + 1
        if previous not in GOOD_STATUSES and seen < api.quirks['confirmations']:
            _update_order(funpay_id, complete_seen=seen)
            return
        if previous not in GOOD_STATUSES:
            updates = {'status': 'completed', 'completion_notified': False, 'complete_seen': seen, 'completed_at': time.time()}
            if charge >= 0:
                try:
                    updates['cost'] = round(charge * _fx_to_rub(order.get('currency') or api.currency(), cfg), 6)
                except PanelError:
                    pass
            _update_order(funpay_id, **updates)
        if order.get('completion_notified') is not True:
            values = {'panel_order_id': order['panel_order_id'], 'order_id': order['panel_order_id'],
                      'funpay_order_id': funpay_id, 'link': order.get('link', '')}
            if not _send(c, order['chat_id'], _format(cfg['messages']['after_completion'], values)):
                return
            _update_order(funpay_id, completion_notified=True)
            refill = order.get('refill') or {}
            if refill.get('enabled') and cfg['refill'].get('send_moment') == 'after_complete':
                _send_refill_info(c, order, refill)
        return
    _update_order(funpay_id, status=status, remains=result.get('remains'), complete_seen=0)


def _reminder_pass(c: Any) -> None:
    cfg = load_config()
    now = time.time()
    with _wlock:
        items = [deepcopy(x) for x in waiting.values()]
    for data in items:
        if data.get('step') != 'link' or data.get('queued'):
            continue
        age = now - _float(data.get('prompted_at') or data.get('created_at'))
        funpay_id = str(data['funpay_order_id'])
        if cfg.get('remind_hours') and age >= cfg['remind_hours'] * 3600 and not data.get('reminded'):
            _set_waiting(funpay_id, reminded=True)
            _send(c, data['chat_id'], _format(cfg['messages']['link_reminder'], {
                'funpay_order_id': funpay_id,
                'example_link': _example(data.get('domains', []), data.get('platform', ''), data.get('kind', ''))}))
        if cfg.get('stale_hours') and age >= cfg['stale_hours'] * 3600 and not data.get('stale_notified'):
            _set_waiting(funpay_id, stale_notified=True)
            _notify_admins(f'⏰ Заказ #{funpay_id} уже {int(age // 3600)} ч ждёт ссылку от покупателя '
                           f'{data.get("buyer_username") or "?"}.\n{_order_url(funpay_id)}')


# ---------------------------------------------------------------------------
# Автоцена и синхронизация остатков
# ---------------------------------------------------------------------------

def _lot_price_info(cfg: Dict[str, Any], lot: Dict[str, Any], api: Optional[PanelAPI] = None) -> Dict[str, Any]:
    api = api or PanelAPI(cfg, lot['panel_id'])
    service = api.service(lot['service_id'])
    cost_rub, native, currency = _cost_rub(cfg, lot['panel_id'], service, lot['quantity'], api)
    mode, value = _effective_markup(cfg, lot)
    price = _price_from_cost(cost_rub, mode, value)
    return {'service': service, 'rate': _float(service.get('rate')), 'currency': currency, 'cost_rub': cost_rub,
            'cost_native': native, 'mode': mode, 'value': value, 'price': price}


def _pricing_pass(force: bool = False, manual: bool = False, only_keys: Optional[set] = None,
                  force_price: bool = False) -> Dict[str, Any]:
    cfg = load_config()
    report = {'updated': [], 'same': 0, 'skipped': [], 'errors': [], 'stock': 0}
    if not _account_ready():
        report['errors'].append('Аккаунт Starvell ещё не загружен.')
        return report
    full = force or time.time() - _float(_state_get('last_full_sync', 0)) > 6 * 3600
    apis: Dict[str, PanelAPI] = {}
    for key, lot in cfg['lot_mapping'].items():
        if only_keys is not None and key not in only_keys:
            continue
        label = _short(lot['name'], 40)
        if not lot.get('funpay_lot_id'):
            if lot.get('autoprice') and cfg['pricing']['enabled']:
                report['skipped'].append(f'{label}: не привязан Starvell ID')
            continue
        want_price = (cfg['pricing']['enabled'] or force_price) and lot.get('autoprice')
        want_stock = full and lot.get('stock_target') and only_keys is None
        if not want_price and not want_stock:
            continue
        try:
            api = apis.get(lot['panel_id']) or PanelAPI(cfg, lot['panel_id'])
            apis[lot['panel_id']] = api
            info = _lot_price_info(cfg, lot, api) if want_price else None
        except PanelError as exc:
            report['errors'].append(f'{label}: {exc}')
            continue
        new_price = info['price'] if info else None
        if new_price and not full and lot.get('last_price'):
            change = abs(new_price - lot['last_price']) / max(lot['last_price'], 1e-9) * 100
            if change < _float(cfg['pricing'].get('min_change'), 0.5):
                report['same'] += 1
                continue
        try:
            fields = _lot_fields(lot['funpay_lot_id'])
            updates: Dict[str, Any] = {}
            old_price = _float(getattr(fields, 'price', None) or fields.fields.get('price'))
            if new_price and abs(new_price - old_price) / max(old_price, 1e-9) * 100 >= _float(cfg['pricing'].get('min_change'), 0.5):
                updates['price'] = _money(new_price)
            if want_stock:
                amount = _int(getattr(fields, 'amount', None) if getattr(fields, 'amount', None) is not None else fields.fields.get('amount'))
                if amount < lot['stock_target'] // 2:
                    updates['amount'] = str(lot['stock_target'])
            if updates:
                _apply_lot_updates(fields, updates)
                _save_lot_fields(fields)
                if 'amount' in updates:
                    report['stock'] += 1
            if 'price' in updates:
                report['updated'].append(f'{label}: {_money(old_price)} → {_money(new_price)} ₽ (себест. {_money(info["cost_rub"])} ₽)')
            elif new_price:
                report['same'] += 1
            if new_price:
                with config_tx() as tx:
                    if key in tx['lot_mapping']:
                        tx['lot_mapping'][key]['last_price'] = new_price
        except Exception as exc:
            report['errors'].append(f'{label}: {_rejection_text(exc, cfg)}')
    if full and only_keys is None:
        _state_set('last_full_sync', time.time())
    if not manual and cfg['pricing'].get('notify') and (report['updated'] or report['errors']):
        _notify_admins(_pricing_report_text(report))
    return report


def _pricing_report_text(report: Dict[str, Any]) -> str:
    lines = ['💰 AutoSMM: пересчёт цен', f'Обновлено: {len(report["updated"])} · Без изменений: {report["same"]} · '
             f'Пропущено: {len(report["skipped"])} · Ошибок: {len(report["errors"])}']
    if report.get('stock'):
        lines.append(f'Пополнен остаток у лотов: {report["stock"]}')
    for title, items in (('Изменения', report['updated']), ('Пропущено', report['skipped']), ('Ошибки', report['errors'])):
        if items:
            lines.append(f'\n{title}:')
            lines += ['• ' + x for x in items[:25]]
            if len(items) > 25:
                lines.append(f'… и ещё {len(items) - 25}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Защита от низкого баланса
# ---------------------------------------------------------------------------

def _balance_pass() -> None:
    cfg = load_config()
    if not _account_ready():
        return
    off = _state_get('balance_off', {}) or {}
    low = _state_get('balance_low', {}) or {}
    for panel_id, panel in cfg['services'].items():
        threshold = _float(panel.get('min_balance'))
        if threshold <= 0 or not panel.get('enabled', True):
            if off.get(panel_id) and threshold <= 0:
                _restore_lots(off.pop(panel_id), cfg)
                low.pop(panel_id, None)
            continue
        try:
            api = PanelAPI(cfg, panel_id)
            balance = api.balance()
            rub = _float(balance.get('balance')) * _fx_to_rub(balance['currency'], cfg)
        except PanelError:
            continue
        lots = [x for x in cfg['lot_mapping'].values() if str(x['panel_id']) == panel_id and x.get('funpay_lot_id')]
        if rub < threshold and not low.get(panel_id):
            disabled = []
            for lot in lots:
                try:
                    if _set_lot_active(lot['funpay_lot_id'], False):
                        disabled.append(lot['funpay_lot_id'])
                except Exception as exc:
                    logger.warning('[AutoSMM] Cannot disable lot %s: %s', lot['funpay_lot_id'], type(exc).__name__)
            off[panel_id] = sorted(set(off.get(panel_id, []) + disabled))
            low[panel_id] = True
            _notify_admins(f'🔴 AutoSMM: низкий баланс\nПанель «{panel["name"]}»: {rub:.2f} ₽ (порог {threshold:g} ₽).\n'
                           f'Выключено лотов на Starvell: {len(disabled)}. Пополните баланс — лоты включатся автоматически.')
        elif rub >= threshold and low.get(panel_id):
            restored = _restore_lots(off.pop(panel_id, []), cfg)
            low.pop(panel_id, None)
            _notify_admins(f'🟢 AutoSMM: баланс восстановлен\nПанель «{panel["name"]}»: {rub:.2f} ₽. Включено лотов: {restored}.')
    _state_set('balance_off', off)
    _state_set('balance_low', low)


def _restore_lots(lot_ids: List[int], cfg: Dict[str, Any]) -> int:
    restored = 0
    sales_off = set(_state_get('sales_off', []) or [])
    for lot_id in lot_ids:
        if lot_id in sales_off:
            continue
        try:
            if _set_lot_active(lot_id, True):
                restored += 1
        except Exception as exc:
            logger.warning('[AutoSMM] Cannot enable lot %s: %s', lot_id, type(exc).__name__)
    return restored


# ---------------------------------------------------------------------------
# Вотчер изменений услуг у провайдера
# ---------------------------------------------------------------------------

WATCH_MODES = ['important', 'all', 'off']
WATCH_MODE_LABELS = {'important': 'только важные', 'all': 'все', 'off': 'выключены'}


def _watcher_pass() -> None:
    """Сверяет используемые услуги с каталогом провайдера.
    Цена изменилась — цены лотов пересчитываются сразу; уведомление — одно на проверку."""
    cfg = load_config()
    if not cfg.get('watcher_enabled'):
        return
    used: Dict[str, set] = {}
    for lot in cfg['lot_mapping'].values():
        used.setdefault(str(lot['panel_id']), set()).add(str(lot['service_id']))
    snapshots = _state_get('watch', {}) or {}
    missing = _state_get('watch_missing', {}) or {}
    threshold = max(0.0, _float(cfg.get('watcher_threshold'), 5.0))
    events: List[Dict[str, Any]] = []
    reprice: set = set()
    for panel_id, service_ids in used.items():
        try:
            api = PanelAPI(cfg, panel_id)
            catalog = {str(x.get('service')): x for x in api.services(refresh=True)}
        except PanelError:
            continue
        panel_snap = snapshots.setdefault(panel_id, {})
        for service_id in service_ids:
            key = f'{panel_id}:{service_id}'
            lots = [(k, x) for k, x in cfg['lot_mapping'].items() if str(x['panel_id']) == panel_id and str(x['service_id']) == service_id]
            service = catalog.get(service_id)
            if not service:
                if service_id in panel_snap and not missing.get(key):
                    missing[key] = time.time()
                    events.append({'panel': panel_id, 'service': service_id, 'lots': lots, 'important': True,
                                   'changes': ['Услуга пропала из каталога провайдера']})
                continue
            missing.pop(key, None)
            current = {'rate': _float(service.get('rate')), 'name': str(service.get('name') or ''),
                       'min': _int(service.get('min')), 'max': _int(service.get('max'))}
            old = panel_snap.get(service_id)
            panel_snap[service_id] = current
            if not old:
                continue
            changes, important = [], False
            if abs(old['rate'] - current['rate']) > 1e-9:
                pct = (current['rate'] - old['rate']) / max(old['rate'], 1e-9) * 100
                changes.append(f'Цена за 1000: {old["rate"]:g} → {current["rate"]:g} ({pct:+.2f}%)')
                important = important or abs(pct) >= threshold
                if cfg.get('watcher_autoprice', True):
                    reprice.update(k for k, x in lots if x.get('autoprice') and x.get('funpay_lot_id'))
            if old['name'] != current['name']:
                changes.append(f'Название: «{_short(old["name"], 60)}» → «{_short(current["name"], 60)}»')
                important = True
            if old['min'] != current['min']:
                changes.append(f'Минимум: {old["min"]} → {current["min"]}')
                important = True
            if old['max'] != current['max']:
                changes.append(f'Максимум: {old["max"]} → {current["max"]}')
                important = True
            if changes:
                events.append({'panel': panel_id, 'service': service_id, 'lots': lots, 'important': important, 'changes': changes})
    _state_set('watch', snapshots)
    _state_set('watch_missing', missing)
    report = None
    if reprice and _account_ready():
        report = _pricing_pass(manual=True, only_keys=reprice, force_price=True)
    mode = cfg.get('watcher_notify', 'important')
    shown = [] if mode == 'off' else [e for e in events if mode == 'all' or e['important']]
    if shown:
        _watch_alert(cfg, shown, report)
    elif report and report['errors']:
        logger.warning('[AutoSMM] Watcher reprice errors: %s', '; '.join(report['errors'][:5]))


def _watch_alert(cfg: Dict[str, Any], events: List[Dict[str, Any]], report: Optional[Dict[str, Any]] = None) -> None:
    parts = [f'⚠️ <b>AutoSMM: провайдер изменил услуги ({len(events)})</b>']
    for event in events[:15]:
        panel = cfg['services'].get(event['panel'], {})
        lots = ', '.join(escape(_short(x['name'], 45)) for _, x in event['lots'][:3])
        more = f' и ещё {len(event["lots"]) - 3}' if len(event['lots']) > 3 else ''
        parts.append(f'\n<b>{escape(panel.get("name", event["panel"]))}</b> · услуга <code>{escape(event["service"])}</code>\n'
                     + '\n'.join('• ' + escape(x) for x in event['changes']) + f'\nЛоты: {lots}{more}')
    if len(events) > 15:
        parts.append(f'\n… и ещё {len(events) - 15} услуг')
    if report is not None:
        if report['updated']:
            parts.append('\n💰 <b>Цены лотов обновлены автоматически:</b>\n' + '\n'.join('• ' + escape(x) for x in report['updated'][:10]))
        elif cfg.get('watcher_autoprice', True):
            parts.append('\n💰 Цены лотов пересчитаны — изменение слишком мало, на Starvell цены оставлены.')
        if report['errors']:
            parts.append('\n❌ Ошибки пересчёта:\n' + '\n'.join('• ' + escape(x) for x in report['errors'][:5]))
    kb = InlineKeyboardMarkup(row_width=1)
    for event in [e for e in events if e['important']][:4]:
        count = sum(1 for _, x in event['lots'] if x.get('funpay_lot_id'))
        if count:
            kb.add(InlineKeyboardButton(f'🔴 Выключить лоты услуги {event["service"]} ({count})',
                                        callback_data=f'wdis:{event["panel"]}:{event["service"]}'))
    mode = cfg.get('watcher_notify', 'important')
    kb.row(*([InlineKeyboardButton('🔕 Только важные', callback_data='wnot:important')] if mode == 'all' else []),
           InlineKeyboardButton('🔇 Выключить', callback_data='wnot:off'), InlineKeyboardButton('✅ Ок', callback_data='wok'))
    _notify_admins('\n'.join(parts)[:4000], kb, html=True)


# ---------------------------------------------------------------------------
# Планировщик фоновых задач
# ---------------------------------------------------------------------------

def _job_intervals(cfg: Dict[str, Any]) -> Dict[str, int]:
    return {'monitor': cfg['poll_interval'], 'reminders': 300, 'pricing': cfg['pricing']['interval'] * 60,
            'balance': 300, 'watcher': 1800, 'tags': 3600}


def _scheduler(c: Any) -> None:
    next_run: Dict[str, float] = {'monitor': time.time() + 5, 'reminders': time.time() + 60, 'pricing': time.time() + 120,
                                  'balance': time.time() + 30, 'watcher': time.time() + 90, 'tags': time.time() + 20}
    while RUNNING:
        now = time.time()
        try:
            cfg = load_config()
            intervals = _job_intervals(cfg)
            for name in list(next_run):
                if now < next_run[name] or not RUNNING:
                    continue
                next_run[name] = now + intervals[name]
                try:
                    if name == 'monitor':
                        _monitor_pass(c)
                    elif name == 'reminders':
                        _reminder_pass(c)
                    elif name == 'pricing' and (cfg['pricing']['enabled'] or any(x.get('stock_target') for x in cfg['lot_mapping'].values())):
                        _pricing_pass()
                    elif name == 'balance':
                        _balance_pass()
                    elif name == 'watcher':
                        _watcher_pass()
                    elif name == 'tags':
                        _tags_pass()
                except Exception:
                    logger.exception('[AutoSMM] Job %s failed', name)
        except Exception:
            logger.exception('[AutoSMM] Scheduler failed')
        for _ in range(5):
            if not RUNNING:
                return
            time.sleep(1)


def _tags_pass() -> None:
    """Дописывает теги лотам, привязанным в прошлых версиях."""
    if not _account_ready():
        return
    cfg = load_config()
    # Лотам из прошлых версий определяем соцсеть и тип по каталогу панели.
    unknown = [(k, v) for k, v in cfg['lot_mapping'].items() if not v.get('platform') or not v.get('kind')]
    if unknown:
        found: Dict[str, Tuple[str, str]] = {}
        for key, lot in unknown:
            try:
                profile = _service_profile(PanelAPI(cfg, lot['panel_id']).service(lot['service_id']))
                found[key] = (profile['platform'], profile['kind'])
            except PanelError:
                continue
        if found:
            with config_tx() as tx:
                for key, (platform, kind) in found.items():
                    if key in tx['lot_mapping']:
                        tx['lot_mapping'][key]['platform'], tx['lot_mapping'][key]['kind'] = platform, kind
    pending = [k for k, v in cfg['lot_mapping'].items() if v.get('funpay_lot_id') and not v.get('tag_written')]
    failures = []
    for key in pending[:40]:
        ok, message = _write_tag(key)
        if not ok:
            failures.append(f'{key}: {message}')
    if failures:
        logger.warning('[AutoSMM] Tag backfill failures: %s', '; '.join(failures[:5]))


def _workers(c: Any) -> None:
    global RUNNING
    RUNNING = True
    thread = _threads.get('scheduler')
    if not thread or not thread.is_alive():
        _threads['scheduler'] = threading.Thread(target=_scheduler, args=(c,), daemon=True, name='autosmm-scheduler')
        _threads['scheduler'].start()


def _stop_workers() -> None:
    global RUNNING
    RUNNING = False


# ---------------------------------------------------------------------------
# Массовые операции с лотами Starvell
# ---------------------------------------------------------------------------

def _bulk_active(lot_ids: List[int], active: bool, progress: Optional[Callable[[int, int], None]] = None) -> Tuple[int, List[str]]:
    changed, errors = 0, []
    for index, lot_id in enumerate(lot_ids, 1):
        try:
            if _set_lot_active(lot_id, active):
                changed += 1
        except Exception as exc:
            errors.append(f'{lot_id}: {_rejection_text(exc)}')
        if progress and (index % 5 == 0 or index == len(lot_ids)):
            progress(index, len(lot_ids))
    return changed, errors


def _sales_off(progress: Optional[Callable[[int, int], None]] = None) -> Tuple[int, List[str]]:
    cfg = load_config()
    lot_ids = [x['funpay_lot_id'] for x in cfg['lot_mapping'].values() if x.get('funpay_lot_id')]
    disabled, errors = [], []
    for index, lot_id in enumerate(lot_ids, 1):
        try:
            if _set_lot_active(lot_id, False):
                disabled.append(lot_id)
        except Exception as exc:
            errors.append(f'{lot_id}: {_rejection_text(exc, cfg)}')
        if progress and (index % 5 == 0 or index == len(lot_ids)):
            progress(index, len(lot_ids))
    _state_set('sales_off', sorted(set((_state_get('sales_off', []) or []) + disabled)))
    return len(disabled), errors


def _sales_on(progress: Optional[Callable[[int, int], None]] = None) -> Tuple[int, List[str]]:
    lot_ids = _state_get('sales_off', []) or []
    blocked = {x for ids in (_state_get('balance_off', {}) or {}).values() for x in ids}
    _state_set('sales_off', [])
    changed, errors = _bulk_active([x for x in lot_ids if x not in blocked], True, progress)
    return changed, errors


def _restyle_lots(keys: List[str], choice: str, progress: Optional[Callable[[int, int], None]] = None) -> Tuple[int, List[str]]:
    """Переоформляет выставленные лоты: тексты меняются, цена и тег сохраняются."""
    cfg = load_config()
    done, errors = 0, []
    apis: Dict[str, PanelAPI] = {}
    for index, key in enumerate(keys, 1):
        lot = cfg['lot_mapping'].get(key)
        if not lot or not lot.get('funpay_lot_id'):
            continue
        try:
            api = apis.get(lot['panel_id']) or PanelAPI(cfg, lot['panel_id'])
            apis[lot['panel_id']] = api
            service = api.service(lot['service_id'])
            fields = _lot_fields(lot['funpay_lot_id'])
            limits = {'summary_ru': 80, 'summary_en': 80, 'desc_ru': _int(cfg.get('description_limit'), 5000),
                      'desc_en': _int(cfg.get('description_limit'), 5000)}
            node = getattr(getattr(fields, 'subcategory', None), 'id', None) or _int(fields.fields.get('node_id'))
            if node:
                try:
                    limits = _form_limits(_node_form(node), cfg)
                except Exception:
                    pass
            price = _float(getattr(fields, 'price', None) or fields.fields.get('price'))
            texts = render_lot_texts(service, lot['quantity'], price, choice if choice != 'keep' else (lot.get('template') or 'auto'),
                                     limits, cfg, lot['tag'])
            updates = {'fields[summary][ru]': texts['summary_ru'], 'fields[summary][en]': texts['summary_en'],
                       'fields[desc][ru]': texts['desc_ru'], 'fields[desc][en]': texts['desc_en']}
            _apply_lot_updates(fields, updates)
            try:
                _save_lot_fields(fields)
            except Exception as exc:
                bad = {str(x[0]) for x in (_funpay_rejection(exc).get('errors') or []) if isinstance(x, (list, tuple)) and x}
                if not bad & {'fields[summary][en]', 'fields[desc][en]', 'fields[name][en]'}:
                    raise
                plain = _plain_english(TAG_RE.sub('', texts['desc_en'])) + f'\n\n#SMM:{lot["tag"]}'
                _apply_lot_updates(fields, {'fields[summary][en]': _summary_field(_plain_english(texts['summary_en']), limits['summary_en']),
                                            'fields[desc][en]': plain})
                _save_lot_fields(fields)
            with config_tx() as tx:
                if key in tx['lot_mapping']:
                    item = tx['lot_mapping'][key]
                    item.update(name=texts['summary_ru'], tag_written=True, platform=texts['platform'], kind=texts['kind'])
                    if choice not in ('keep', 'auto'):
                        item['template'] = choice
            done += 1
        except Exception as exc:
            errors.append(f'{_short(lot["name"], 40)}: {exc if isinstance(exc, PanelError) else _rejection_text(exc, cfg)}')
        if progress and (index % 3 == 0 or index == len(keys)):
            progress(index, len(keys))
    return done, errors


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------

def _stats(days: Optional[int]) -> Dict[str, Any]:
    cutoff = time.time() - days * 86400 if days else None
    out = {'revenue': 0.0, 'cost': 0.0, 'orders': 0, 'done': 0, 'refunded': 0, 'active': 0}
    for order in _orders():
        if cutoff and _float(order.get('created_at'), time.time()) < cutoff:
            continue
        status = str(order.get('status', '')).lower()
        refunded = bool(order.get('is_refunded')) or status == 'refunded'
        out['orders'] += 1
        if refunded:
            out['refunded'] += 1
            continue
        out['revenue'] += _float(order.get('price'))
        out['cost'] += _float(order.get('cost'))
        out['done'] += status in GOOD_STATUSES
        out['active'] += status not in GOOD_STATUSES | BAD_STATUSES and bool(order.get('panel_order_id'))
    out['profit'] = out['revenue'] - out['cost']
    return out


# ---------------------------------------------------------------------------
# Telegram: инфраструктура меню
# ---------------------------------------------------------------------------

def InlineKeyboardButton(text: str, **kwargs: Any) -> _TelegramButton:
    if 'callback_data' in kwargs:
        data = 'autosmm:' + kwargs['callback_data']
        if len(data.encode('utf-8')) > 64:
            logger.warning('[AutoSMM] callback_data too long: %s', data)
            data = data.encode('utf-8')[:64].decode('utf-8', 'ignore')
        kwargs['callback_data'] = data
    return _TelegramButton(text, **kwargs)


def B(text: str, data: str) -> _TelegramButton:
    return InlineKeyboardButton(text, callback_data=data)


def U(text: str, url: str) -> _TelegramButton:
    return _TelegramButton(text, url=url)


def _kb(*rows: Optional[List[_TelegramButton]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    for row in rows:
        if row:
            kb.row(*row)
    return kb


def _on(value: Any) -> str:
    return '🟢' if value else '🔴'


ROUTES: Dict[str, Callable[..., None]] = {}
INPUTS: Dict[str, Callable[..., None]] = {}


def route(*names: str) -> Callable:
    def deco(func: Callable) -> Callable:
        for name in names:
            ROUTES[name] = func
        return func
    return deco


def inp(name: str) -> Callable:
    def deco(func: Callable) -> Callable:
        INPUTS[name] = func
        return func
    return deco


def _answer(q: Any, text: Optional[str] = None, alert: bool = False) -> None:
    if getattr(q, '_autosmm_answered', False):
        return
    try:
        bot.answer_callback_query(q.id, text=text, show_alert=alert)
    except Exception:
        pass
    try:
        q._autosmm_answered = True
    except Exception:
        pass


def _edit(q: Any, text: str, kb: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        bot.edit_message_text(text, q.message.chat.id, q.message.message_id, parse_mode='HTML', reply_markup=kb,
                              disable_web_page_preview=True)
    except Exception as exc:
        if 'message is not modified' in str(exc):
            return
        _show(q.message.chat.id, text, kb)


def _show(chat_id: int, text: str, kb: Optional[InlineKeyboardMarkup] = None) -> Optional[Any]:
    try:
        return bot.send_message(chat_id, text, parse_mode='HTML', reply_markup=kb, disable_web_page_preview=True)
    except Exception as exc:
        logger.warning('[AutoSMM] Telegram send failed: %s', type(exc).__name__)
        try:
            return bot.send_message(chat_id, re.sub(r'<[^>]+>', '', text), reply_markup=kb)
        except Exception:
            return None


def _back_kb(back: str = 'home', label: str = '◀️ Назад') -> InlineKeyboardMarkup:
    return _kb([B(label, back)])


def _ask(target: Any, state: str, prompt: str, data: Optional[Dict[str, Any]] = None, back: str = 'home') -> None:
    """Запрашивает ввод с кнопкой отмены. target — callback или сообщение."""
    kb = _kb([B('❌ Отмена', 'x:' + back)])
    if hasattr(target, 'message') and hasattr(target, 'data'):
        chat_id, user_id = target.message.chat.id, target.from_user.id
        _edit(target, prompt, kb)
        message_id = target.message.message_id
    else:
        chat_id, user_id = target.chat.id, target.from_user.id
        sent = _show(chat_id, prompt, kb)
        message_id = getattr(sent, 'message_id', 0)
    cardinal_instance.telegram.set_state(chat_id, message_id, user_id, 'autosmm:' + state, data or {})


def _clear(message: Any) -> None:
    try:
        cardinal_instance.telegram.clear_state(message.chat.id, message.from_user.id, False)
    except Exception:
        pass


def _progress_job(chat_id: int, title: str, func: Callable[[Callable[[int, int], None]], str],
                  kb_done: Optional[InlineKeyboardMarkup] = None, stop_key: str = '') -> None:
    """Запускает долгую операцию в фоне с прогрессом в одном сообщении."""
    stop_kb = _kb([B('⛔ Остановить', 'stopjob:' + stop_key)]) if stop_key else None
    sent = _show(chat_id, f'⏳ <b>{escape(title)}</b>\n\nПодготовка…', stop_kb)
    message_id = getattr(sent, 'message_id', None)

    def progress(done: int, total: int, extra: str = '') -> None:
        if not message_id:
            return
        try:
            bot.edit_message_text(f'⏳ <b>{escape(title)}</b>\n\n{done}/{total}' + (f'\n{extra}' if extra else ''),
                                  chat_id, message_id, parse_mode='HTML', reply_markup=stop_kb)
        except Exception:
            pass

    def run() -> None:
        try:
            result = func(progress)
        except Exception as exc:
            logger.exception('[AutoSMM] Job %s failed', title)
            result = f'❌ Ошибка: {escape(type(exc).__name__)}'
        try:
            if message_id:
                bot.edit_message_text(result[:4000], chat_id, message_id, parse_mode='HTML', reply_markup=kb_done or _back_kb(),
                                      disable_web_page_preview=True)
            else:
                _show(chat_id, result[:4000], kb_done or _back_kb())
        except Exception:
            _show(chat_id, result[:4000], kb_done or _back_kb())
    _submit(run)


def _page(items: List[Any], page: int, per: int) -> Tuple[List[Any], int, int]:
    pages = max(1, math.ceil(len(items) / per))
    page = max(0, min(_int(page), pages - 1))
    return items[page * per:(page + 1) * per], page, pages


def _nav(prefix: str, page: int, pages: int) -> Optional[List[_TelegramButton]]:
    if pages <= 1:
        return None
    row = []
    if page > 0:
        row.append(B('⬅️', f'{prefix}:{page - 1}'))
    row.append(B(f'{page + 1}/{pages}', 'noop'))
    if page < pages - 1:
        row.append(B('➡️', f'{prefix}:{page + 1}'))
    return row


def _choice_token(choice: str) -> str:
    return 'c-' + choice[7:] if choice.startswith('custom:') else choice


def _token_choice(token: str) -> str:
    return 'custom:' + token[2:] if token.startswith('c-') else token


def _template_buttons(prefix: str, cfg: Dict[str, Any], include_auto: bool = True, include_keep: bool = False) -> List[List[_TelegramButton]]:
    buttons = []
    if include_keep:
        buttons.append(B('📌 Как у лота', f'{prefix}:keep'))
    if include_auto:
        buttons.append(B('🎯 По настройкам', f'{prefix}:auto'))
    buttons += [B(THEMES[key]['title'], f'{prefix}:{key}') for key in THEME_ORDER]
    buttons += [B('🧩 ' + _short(v.get('name', k), 20), f'{prefix}:c-{k}') for k, v in cfg['design']['custom'].items()]
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


# ---------------------------------------------------------------------------
# Главное меню
# ---------------------------------------------------------------------------

def scr_home() -> Tuple[str, InlineKeyboardMarkup]:
    cfg = load_config()
    today = _stats(1)
    enabled = sum(1 for x in cfg['services'].values() if x['enabled'])
    lots = cfg['lot_mapping'].values()
    auto = sum(1 for x in lots if x.get('source') == 'auto')
    with _wlock:
        waiting_count = len(waiting)
    orders = _orders()
    in_work = sum(1 for x in orders if x.get('panel_order_id') and not x.get('is_refunded') and
                  str(x.get('status', '')).lower() not in GOOD_STATUSES | BAD_STATUSES | {'manual', 'refunded', 'partial_resolved'} | REVIEW_STATUSES)
    review = sum(1 for x in orders if x.get('status') in REVIEW_STATUSES)
    sales_off = bool(_state_get('sales_off', []))
    low = [cfg['services'][k]['name'] for k, v in (_state_get('balance_low', {}) or {}).items() if v and k in cfg['services']]
    text = (f'🚀 <b>AutoSMM {VERSION}</b>\n━━━━━━━━━━━━━━━━\n'
            f'Статус: {"✅ АКТИВЕН" if RUNNING else "⏸ ОСТАНОВЛЕН (фоновые процессы)"}\n'
            f'Витрина: {"🛑 лоты сняты с продажи" if sales_off else "🟢 лоты в продаже"}\n'
            f'🔌 Панелей: {enabled}/{len(cfg["services"])} · 🛍 Лотов: {len(cfg["lot_mapping"])} (авто {auto})\n'
            f'⏳ Ждут ссылку: {waiting_count} · ⚙️ В работе: {in_work}'
            + (f' · ⚠️ Решить: {review}' if review else '') +
            f'\n💰 Сегодня: {today["orders"]} зак. · прибыль <b>{today["profit"]:.2f} ₽</b>'
            + (f'\n🔴 Низкий баланс: {escape(", ".join(low))}' if low else '') +
            '\n━━━━━━━━━━━━━━━━')
    kb = _kb([B('⏸ Остановить' if RUNNING else '▶️ Запустить', 'run'),
              B('✅ Вернуть в продажу' if sales_off else '🛑 Снять с продажи', 'sales')],
             [B('🛍 Лоты', 'lots'), B('➕ Новый лот', 'new')],
             [B('📦 Автовыгрузка', 'auto') if CAN_CREATE_LOTS else B('📋 Текст для лота', 'ntx'), B('🎨 Оформление', 'ds')],
             [B('🔌 Панели', 'panels'), B('💰 Цены', 'pr')],
             [B('💬 Сообщения', 'msg'), B('♻️ Рефилл', 'rf')],
             [B('⚙️ Настройки', 'st'), B('🌐 Домены', 'dm')],
             [B(f'⚠️ Проверить заказы ({review})' if review else '⚠️ Проверить заказы', 'review'), B('📊 Статистика', 'stats:today')],
             [U('👤 Автор', AUTHOR_URL), U('📣 Канал', CHANNEL_URL)])
    return text, kb


@route('home')
def r_home(q: Any) -> None:
    _edit(q, *scr_home())


@route('noop')
def r_noop(q: Any) -> None:
    _answer(q)


@route('x')
def r_cancel(q: Any, *back: str) -> None:
    try:
        cardinal_instance.telegram.clear_state(q.message.chat.id, q.from_user.id, False)
    except Exception:
        pass
    _dispatch(q, ':'.join(back) or 'home')


@route('run')
def r_run(q: Any) -> None:
    if RUNNING:
        _stop_workers()
        _answer(q, '⏸ Фоновые процессы остановлены. Приём заказов продолжается.', True)
    else:
        _workers(cardinal_instance)
        _answer(q, '▶️ Запущено: слежение за заказами, автоцена, баланс, вотчер.')
    r_home(q)


@route('sales')
def r_sales(q: Any) -> None:
    if _state_get('sales_off', []):
        _edit(q, '✅ <b>Вернуть лоты в продажу?</b>\n\nБудут включены лоты, которые плагин снял с продажи этой кнопкой '
                 '(кроме выключенных из-за низкого баланса).', _kb([B('✅ Да, включить', 'salesgo:on'), B('Отмена', 'home')]))
    else:
        _edit(q, '🛑 <b>Снять все лоты AutoSMM с продажи?</b>\n\nВсе привязанные лоты будут выключены на Starvell. '
                 'Уже оплаченные заказы продолжат выполняться. Вернуть можно этой же кнопкой.',
              _kb([B('🛑 Да, снять', 'salesgo:off'), B('Отмена', 'home')]))


@route('salesgo')
def r_salesgo(q: Any, mode: str) -> None:
    if not _account_ready():
        _answer(q, 'Аккаунт Starvell ещё не загружен.', True)
        return
    _answer(q)
    def job(progress: Callable) -> str:
        changed, errors = (_sales_on if mode == 'on' else _sales_off)(progress)
        text = (f'✅ Включено лотов: {changed}' if mode == 'on' else f'🛑 Выключено лотов: {changed}')
        if errors:
            text += '\n\nОшибки:\n' + '\n'.join('• ' + escape(x) for x in errors[:15])
        return text
    _progress_job(q.message.chat.id, 'Возвращаю лоты в продажу' if mode == 'on' else 'Снимаю лоты с продажи', job,
                  _back_kb('home', '◀️ Главное меню'))


@route('wdis')
def r_watch_disable(q: Any, panel_id: str, service_id: str) -> None:
    lots = [x['funpay_lot_id'] for x in load_config()['lot_mapping'].values()
            if str(x['panel_id']) == panel_id and str(x['service_id']) == service_id and x.get('funpay_lot_id')]
    _answer(q, f'Выключаю лоты: {len(lots)}…')
    def job(progress: Callable) -> str:
        changed, errors = _bulk_active(lots, False, progress)
        return f'🔴 Выключено лотов: {changed} из {len(lots)}. Включите их сами на Starvell, когда проверите услугу.' + \
               (('\n\nОшибки:\n' + '\n'.join('• ' + escape(x) for x in errors[:10])) if errors else '')
    _progress_job(q.message.chat.id, 'Выключаю лоты по услуге', job)
    try:
        bot.edit_message_reply_markup(q.message.chat.id, q.message.message_id, reply_markup=None)
    except Exception:
        pass


@route('wok')
def r_watch_ok(q: Any) -> None:
    _answer(q, 'Принято.')
    try:
        bot.edit_message_reply_markup(q.message.chat.id, q.message.message_id, reply_markup=None)
    except Exception:
        pass


@route('stopjob')
def r_stopjob(q: Any, key: str = '') -> None:
    job = _jobs.get(key)
    if job:
        job['stop'] = True
        _answer(q, 'Останавливаю после текущего лота…')
    else:
        _answer(q, 'Задача уже завершена.')


# ---------------------------------------------------------------------------
# Проверка заказов
# ---------------------------------------------------------------------------

@route('review')
def r_review(q: Any, page: str = '0') -> None:
    orders = [x for x in _orders() if x.get('status') in REVIEW_STATUSES]
    items, page_n, pages = _page(orders, _int(page), 8)
    labels = {'submission_unknown': '❓ запуск не подтверждён', 'awaiting_refund': '↩️ нужен возврат',
              'refund_unknown': '🛑 возврат не подтверждён', 'partial': '◐ частично выполнен'}
    kb = _kb(*[[B(f'#{x["funpay_order_id"]} · {labels.get(x["status"], x["status"])}', 'ro:' + str(x['funpay_order_id']))] for x in items],
             _nav('review', page_n, pages), [B('◀️ Главное меню', 'home')])
    _edit(q, f'⚠️ <b>Проверить заказы</b>\n\nТребуют решения: {len(orders)}.\n'
             'Перед повтором или возвратом проверьте историю SMM-панели.' if orders else
          '⚠️ <b>Проверить заказы</b>\n\n✅ Всё разобрано — заказов, требующих решения, нет.', kb)


@route('ro')
def r_review_order(q: Any, funpay_id: str) -> None:
    order = _get_order(funpay_id)
    if not order:
        _answer(q, 'Заказ не найден.', True)
        return
    text = (f'⚠️ <b>Заказ #{escape(funpay_id)}</b>\nСостояние: {escape(str(order.get("status")))}\n'
            f'Панель: {escape(str(order.get("panel_id")))} · Услуга: {escape(str(order.get("service_id")))}\n'
            f'Количество: {order.get("quantity")}\nСсылка: {escape(str(order.get("link", "")))}\n'
            f'Покупатель: {escape(str(order.get("buyer_username") or "?"))}\n'
            f'Причина: {escape(str(order.get("error") or order.get("refund_reason") or "нужна ручная проверка"))}\n'
            f'<a href="{_order_url(funpay_id)}">Открыть заказ на Starvell</a>')
    rows = []
    if order['status'] == 'submission_unknown':
        rows.append([B('🔗 Заказ найден в панели', 'rs:attach:' + funpay_id), B('🔄 Повторить', 'rs:retry:' + funpay_id)])
        rows.append([B('↩️ Возврат', 'rs:refund:' + funpay_id)])
    else:
        rows.append([B('✅ Разобрано вручную', 'rs:done:' + funpay_id)])
    rows.append([B('◀️ Список', 'review')])
    _edit(q, text, _kb(*rows))


@route('rs')
def r_resolve(q: Any, action: str, funpay_id: str) -> None:
    prompts = {'attach': 'Найдите заказ в истории панели по ссылке, услуге и количеству. Введите его номер для привязки:',
               'retry': 'Проверьте историю панели. Только если заказа там НЕТ, введите <code>НЕ СОЗДАН</code> — плагин повторит запуск один раз.',
               'refund': 'Проверьте историю панели. Только если заказа там НЕТ, введите <code>НЕ СОЗДАН</code> — будет оформлен возврат.',
               'done': 'Проверьте результат и при необходимости сделайте возврат на Starvell. Затем введите <code>РАЗОБРАНО</code>.'}
    if action in prompts:
        _ask(q, 'review', prompts[action], {'action': action, 'id': funpay_id}, back='ro:' + funpay_id)


@inp('review')
def i_review(message: Any, text: str, data: Dict[str, Any]) -> None:
    action, funpay_id = data['action'], data['id']
    order = _get_order(funpay_id)
    if not order:
        raise ValueError('Заказ уже удалён.')
    c = cardinal_instance
    if action == 'attach':
        if order.get('status') != 'submission_unknown':
            raise ValueError('Запуск этого заказа уже разобран.')
        if not text.isdigit() or _int(text) <= 0:
            raise ValueError('Введите номер заказа SMM-панели (цифры).')
        if any(str(x.get('panel_id')) == str(order['panel_id']) and str(x.get('panel_order_id')) == text for x in _orders()):
            raise ValueError('Этот заказ панели уже привязан к другому заказу Starvell.')
        PanelAPI(load_config(), order['panel_id']).status(text)
        _update_order(funpay_id, panel_order_id=text, status='pending', completion_notified=False, started_at=time.time())
        _remove_waiting(funpay_id)
        _clear(message)
        _show(message.chat.id, '✅ Заказ панели привязан. Слежение за статусом продолжится.', _back_kb('review', '◀️ К списку'))
        values = {'panel_order_id': text, 'order_id': text, 'funpay_order_id': funpay_id, 'link': order.get('link', '')}
        if not _send(c, order['chat_id'], _format(load_config()['messages']['after_confirmation'], values)):
            _show(message.chat.id, '⚠️ Не удалось уведомить покупателя. Заказ уже сохранён.')
    elif action in ('retry', 'refund'):
        if order.get('status') != 'submission_unknown':
            raise ValueError('Запуск этого заказа уже разобран.')
        if text.strip().upper() != 'НЕ СОЗДАН':
            raise ValueError('Сначала проверьте историю панели. Для подтверждения введите: НЕ СОЗДАН')
        _clear(message)
        if action == 'retry':
            _update_order(funpay_id, status='retry_ready')
            _submit(_start_order, c, funpay_id, True)
        else:
            _update_order(funpay_id, cost=0)
            _submit(_refund, c, funpay_id, order['chat_id'], 'продавец подтвердил, что услуга не запущена.')
        _show(message.chat.id, '✅ Решение принято. Результат придёт уведомлением.', _back_kb('review', '◀️ К списку'))
    elif action == 'done':
        if text.strip().upper() != 'РАЗОБРАНО':
            raise ValueError('Для закрытия введите: РАЗОБРАНО')
        if order['status'] == 'partial':
            _update_order(funpay_id, status='partial_resolved')
        else:
            _update_order(funpay_id, status='refunded', is_refunded=True)
        _remove_waiting(funpay_id)
        _clear(message)
        _show(message.chat.id, '✅ Ручное решение сохранено.', _back_kb('review', '◀️ К списку'))


# ---------------------------------------------------------------------------
# Лоты: меню, список, поиск
# ---------------------------------------------------------------------------

LOT_FILTERS = {'all': 'Все', 'auto': 'Автовыгрузка', 'man': 'Ручные', 'nofp': 'Без Starvell ID', 'ref': 'С рефиллом'}


def _filter_lots(cfg: Dict[str, Any], flt: str) -> List[Tuple[str, Dict[str, Any]]]:
    items = list(cfg['lot_mapping'].items())
    if flt == 'auto':
        items = [x for x in items if x[1].get('source') == 'auto']
    elif flt == 'man':
        items = [x for x in items if x[1].get('source') != 'auto']
    elif flt == 'nofp':
        items = [x for x in items if not x[1].get('funpay_lot_id')]
    elif flt == 'ref':
        items = [x for x in items if x[1]['refill']['enabled']]
    elif flt.startswith('b_'):
        items = [x for x in items if x[1].get('batch_id') == flt[2:]]
    elif flt.startswith('p_'):
        items = [x for x in items if str(x[1].get('panel_id')) == flt[2:]]
    return sorted(items, key=lambda x: _int(x[0].split('_')[-1]))


@route('lots')
def r_lots(q: Any) -> None:
    cfg = load_config()
    lots = cfg['lot_mapping'].values()
    no_tag = sum(1 for x in lots if x.get('funpay_lot_id') and not x.get('tag_written'))
    text = (f'🛍 <b>Лоты</b>\n\nВсего: {len(cfg["lot_mapping"])} · автовыгрузка: {sum(1 for x in lots if x.get("source") == "auto")} · '
            f'ручные: {sum(1 for x in lots if x.get("source") != "auto")}\n'
            f'Без Starvell ID: {sum(1 for x in lots if not x.get("funpay_lot_id"))}'
            + (f'\n🏷 Тег ещё не записан: {no_tag} (допишется в фоне)' if no_tag else ''))
    kb = _kb([B('🔍 Поиск лота', 'lsearch'), B('📋 Все лоты', 'll:all:0')],
             [B('🤖 Автовыгрузка', 'll:auto:0'), B('✋ Ручные', 'll:man:0')],
             [B('❗ Без Starvell ID', 'll:nofp:0'), B('📦 Выгрузки', 'bts') if CAN_CREATE_LOTS else B('📋 Текст для лота', 'ntx')],
             [B('🎨 Переоформить лоты', 'dsr') if CAN_CREATE_LOTS else None, B('♻️ Рефилл: массово', 'rfb')],
             [B('📦 Кол-во за 1 шт. всем лотам', 'lqall')],
             [B('◀️ Назад', 'home')])
    _edit(q, text, kb)


@route('ll')
def r_lot_list(q: Any, flt: str = 'all', page: str = '0') -> None:
    cfg = load_config()
    items, page_n, pages = _page(_filter_lots(cfg, flt), _int(page), 10)
    rows = []
    for key, lot in items:
        mark = '' if lot.get('funpay_lot_id') else '❗'
        rows.append([B(f'{mark}{_short(lot["name"], 34)} [ID={lot["service_id"]}, Q={lot["quantity"]}]', 'lot:' + key)])
    rows.append(_nav(f'll:{flt}', page_n, pages))
    rows.append([B('◀️ Лоты', 'lots')])
    title = LOT_FILTERS.get(flt) or ('Выгрузка ' + flt[2:] if flt.startswith('b_') else 'Панель ' + flt[2:])
    total = len(_filter_lots(cfg, flt))
    _edit(q, f'📋 <b>{escape(title)}</b> — {total}\n\nНажмите на лот, чтобы открыть карточку.' if total else
          f'📋 <b>{escape(title)}</b>\n\nЛотов нет.', _kb(*rows))


@route('lsearch')
def r_lot_search(q: Any) -> None:
    _ask(q, 'lot_search', '🔍 Введите Starvell ID лота, номер (lot_3), ID услуги или часть названия:', back='lots')


@inp('lot_search')
def i_lot_search(message: Any, text: str, data: Dict[str, Any]) -> None:
    cfg = load_config()
    needle = text.strip().lower()
    found = []
    for key, lot in cfg['lot_mapping'].items():
        if needle in (key.lower(), str(lot.get('funpay_lot_id') or ''), 'svc' + str(lot['service_id'])) or \
                needle in lot['name'].lower() or needle == str(lot['service_id']):
            found.append((key, lot))
    _clear(message)
    if not found:
        _show(message.chat.id, '🔍 Ничего не найдено.', _kb([B('🔍 Искать снова', 'lsearch'), B('◀️ Лоты', 'lots')]))
        return
    rows = [[B(f'{_short(lot["name"], 40)} [ID={lot["service_id"]}]', 'lot:' + key)] for key, lot in found[:10]]
    rows.append([B('🔍 Искать снова', 'lsearch'), B('◀️ Лоты', 'lots')])
    _show(message.chat.id, f'🔍 Найдено: {len(found)}' + (' (показаны первые 10)' if len(found) > 10 else ''), _kb(*rows))


# ---------------------------------------------------------------------------
# Карточка лота
# ---------------------------------------------------------------------------

def scr_lot(key: str) -> Tuple[str, InlineKeyboardMarkup]:
    cfg = load_config()
    lot = cfg['lot_mapping'].get(key)
    if not lot:
        return '❌ Лот не найден (возможно, удалён).', _back_kb('lots')
    panel = cfg['services'].get(lot['panel_id'])
    mode, value = _effective_markup(cfg, lot)
    refill = lot['refill']
    source = {'auto': '🤖 автовыгрузка', 'manual': '🛠 создан вручную', 'bind': '🔗 привязан', 'legacy': '📁 прежняя версия'}.get(lot['source'], lot['source'])
    platform = _platform_info(lot.get('platform') or 'other')
    kind = KINDS.get(lot.get('kind') or 'generic', KINDS['generic'])
    text = (f'🛍 <b>{escape(lot["name"])}</b>\n<code>{key}</code> · {source}\n━━━━━━━━━━━━━━━━\n'
            f'🔌 Панель: {escape(panel["name"]) if panel else "❌ удалена (#" + escape(lot["panel_id"]) + ")"}\n'
            f'🆔 Услуга: <code>{escape(lot["service_id"])}</code> · 📦 За 1 шт.: <b>{lot["quantity"]}</b>\n'
            f'🏷 Starvell ID: ' + (f'<a href="{LOT_URL.format(lot["funpay_lot_id"])}">{lot["funpay_lot_id"]}</a>'
                               if lot['funpay_lot_id'] else '❗️ не привязан (цена меняться не будет)') + '\n'
            + (f'🔖 Тег: <code>#SMM:{lot["tag"]}</code> — {"✅ записан на Starvell" if lot["tag_written"] else "⚠️ не записан"}\n'
               if HAS_LOT_TAGS else '') +
            f'🧭 Тип: {platform["emoji"]} {escape(platform["name"])} · {kind["emoji"]} {escape(kind["ru"].lower())}\n'
            f'🌐 Домены: {escape(", ".join(lot["domains"]) or "общий список")}\n'
            f'🔢 Опрос: {"ВКЛ" if lot["poll"] else "ВЫКЛ"} · 💰 Автоцена: {"ВКЛ" if lot["autoprice"] else "ВЫКЛ"}\n'
            f'📈 Наценка: {"глобальная " if lot["markup_mode"] == "global" else ""}{_markup_text(mode, value)}'
            + (f' · последняя цена {_money(lot["last_price"])} ₽' if lot.get('last_price') else '') + '\n'
            f'♻️ Рефилл: {"ВКЛ" if refill["enabled"] else "ВЫКЛ"}'
            + (f' ({refill["limit"] if refill["limit_enabled"] else "∞"} раз, {_fmt_hours(refill["hours"]) if refill["period_enabled"] else "бессрочно"})'
               if refill['enabled'] else '') +
            f'\n🎨 Оформление: {escape(_template_name(cfg, lot.get("template") or "auto"))}')
    mm = {'global': '🌍 глоб.', 'fixed': '💵 фикс', 'percent': '➗ %'}[lot['markup_mode']]
    kb = _kb([B('✏️ Название', f'le:name:{key}'), B('🆔 ID услуги', f'le:svc:{key}')],
             [B('📦 Количество', f'le:qty:{key}'), B('🔌 Панель', f'lpanel:{key}')],
             [B('🔗 Starvell ID', f'le:fpid:{key}'), B('🏷 Записать тег', f'ltag:{key}') if HAS_LOT_TAGS else B('📋 Текст для лота', f'ltx:{key}')],
             [B('🌐 Домены', f'le:dom:{key}'), B(f'🔢 Опрос: {"ВКЛ" if lot["poll"] else "ВЫКЛ"}', f'lt:poll:{key}')],
             [B(f'💰 Автоцена: {"ВКЛ" if lot["autoprice"] else "ВЫКЛ"}', f'lt:ap:{key}'), B(f'📈 Наценка: {mm}', f'lt:mm:{key}')],
             [B('✏️ Значение наценки', f'le:mval:{key}'), B('💱 Рассчитать цену', f'lcalc:{key}')],
             [B('💵 Цена на Starvell', f'le:price:{key}'), B(f'♻️ Рефилл: {"ВКЛ" if refill["enabled"] else "ВЫКЛ"}', f'lref:{key}')],
             [B('🎨 Оформление', f'ldes:{key}') if CAN_CREATE_LOTS else None, B('📦 Остаток', f'le:stock:{key}')],
             [B('🟢 Вкл. на Starvell', f'lact:{key}:1'), B('🔴 Выкл. на Starvell', f'lact:{key}:0')],
             [B('❌ Удалить', f'ldel:{key}'), B('◀️ К списку', 'll:all:0')])
    return text, kb


@route('lot')
def r_lot(q: Any, key: str) -> None:
    _edit(q, *scr_lot(key))


LOT_EDIT_PROMPTS = {
    'name': ('✏️ Введите название лота <b>как на Starvell</b> (по нему бот узнаёт заказ, если нет тега):', 'name'),
    'svc': ('🆔 Введите ID услуги в панели (число из раздела Services):', 'svc'),
    'qty': ('📦 Сколько единиц услуги заказывать за 1 шт. лота на Starvell?', 'qty'),
    'fpid': (f'🔗 Введите ID лота Starvell — {LOT_ID_HINT}. Бот проверит, что лот существует:', 'fpid'),
    'dom': ('🌐 Введите домены через запятую (например <code>t.me, telegram.me</code>), <code>auto</code> — по типу услуги, '
            '<code>*</code> — любые, <code>-</code> — общий список:', 'dom'),
    'mval': ('✏️ Введите значение наценки лота (процент или рубли — зависит от режима):', 'mval'),
    'price': ('💵 Введите новую цену лота на Starvell за 1 шт. (₽). Если у лота включена автоцена, '
              'она может изменить цену при следующем пересчёте:', 'price'),
    'stock': ('📦 Сколько штук держать в наличии на Starvell? Плагин будет пополнять остаток, когда он упадёт вдвое. '
              '<code>0</code> — не следить:', 'stock'),
}


@route('le')
def r_lot_edit(q: Any, field: str, key: str) -> None:
    lot = _lot_by_key(key)
    if not lot:
        _answer(q, 'Лот не найден.', True)
        return
    if field == 'mval' and lot['markup_mode'] == 'global':
        _answer(q, 'Сначала переключите «📈 Наценка» на фикс или процент.', True)
        return
    if field in ('price', 'stock') and not lot.get('funpay_lot_id'):
        _answer(q, 'Сначала привяжите Starvell ID лота.', True)
        return
    prompt, _ = LOT_EDIT_PROMPTS[field]
    _ask(q, 'lot_edit', prompt, {'field': field, 'key': key}, back='lot:' + key)


@inp('lot_edit')
def i_lot_edit(message: Any, text: str, data: Dict[str, Any]) -> None:
    field, key = data['field'], data['key']
    cfg = load_config()
    lot = cfg['lot_mapping'].get(key)
    if not lot:
        raise ValueError('Лот уже удалён.')
    if field == 'fpid':
        lot_id = _int(text)
        if lot_id <= 0:
            raise ValueError('Введите числовой ID лота Starvell.')
        other = _lot_key_by_funpay_id(cfg, lot_id)
        if other and other != key:
            raise ValueError(f'Этот лот Starvell уже привязан к {other}.')
        _clear(message)
        _show(message.chat.id, '⏳ Проверяю лот на Starvell…')
        _submit(_bind_funpay_id, message.chat.id, key, lot_id)
        return
    if field == 'price':
        price = _float(text, -1)
        if price <= 0:
            raise ValueError('Цена должна быть больше нуля.')
        _clear(message)
        _show(message.chat.id, '⏳ Сохраняю цену на Starvell…')
        def job() -> None:
            try:
                _edit_lot(lot['funpay_lot_id'], {'price': _money(price)})
                with config_tx() as tx:
                    if key in tx['lot_mapping']:
                        tx['lot_mapping'][key]['last_price'] = price
                _show(message.chat.id, f'✅ Цена {_money(price)} ₽ сохранена на Starvell.', _back_kb('lot:' + key, '◀️ К лоту'))
            except Exception as exc:
                _show(message.chat.id, '❌ Starvell не сохранил цену: ' + escape(_rejection_text(exc)), _back_kb('lot:' + key, '◀️ К лоту'))
        _submit(job)
        return
    service = None
    if field == 'svc':
        if not text.strip().isdigit():
            raise ValueError('ID услуги — это число.')
        service = PanelAPI(cfg, lot['panel_id']).service(text.strip())
    with config_tx() as tx:
        item = tx['lot_mapping'].get(key)
        if not item:
            raise ValueError('Лот уже удалён.')
        if field == 'name':
            if not text.strip():
                raise ValueError('Название не может быть пустым.')
            item['name'] = text.strip()[:200]
        elif field == 'svc' and service is not None:
            item['service_id'] = text.strip()
            profile = _service_profile(service)
            item['platform'], item['kind'] = profile['platform'], profile['kind']
            item['poll'] = item['poll'] or 'poll' in str(service.get('type', '')).lower()
        elif field == 'qty':
            if _int(text) <= 0:
                raise ValueError('Ошибка: нужно число больше нуля.')
            item['quantity'] = _int(text)
        elif field == 'dom':
            value = text.strip().lower()
            if value == 'auto':
                item['domains'] = list(_platform_info(item.get('platform') or 'other').get('domains') or [])
            elif value == '-':
                item['domains'] = []
            else:
                item['domains'] = _domains(value)
                if not item['domains']:
                    raise ValueError('Не удалось распознать домены.')
        elif field == 'mval':
            value = _float(text, float('nan'))
            if not math.isfinite(value) or value < 0 or (item['markup_mode'] == 'percent' and value > 10000):
                raise ValueError('Введите неотрицательное число.')
            item['markup_value'] = value
        elif field == 'stock':
            value = _int(text, -1)
            if value < 0 or value > 99999:
                raise ValueError('Введите число от 0 до 99999.')
            item['stock_target'] = value
    _clear(message)
    _show(message.chat.id, '✅ Сохранено.')
    if field in ('qty', 'svc') and lot.get('funpay_lot_id'):
        _show(message.chat.id, '⏳ Обновляю цену и описание лота на Starvell под новое количество…')
        _submit(_refresh_lots_job, message.chat.id, [key])
        return
    _show(message.chat.id, *scr_lot(key))


def _refresh_lots_job(chat_id: int, keys: List[str]) -> None:
    """После смены количества или услуги: пересчитать цену и пересобрать тексты на Starvell."""
    cfg = load_config()
    keys = [k for k in keys if cfg['lot_mapping'].get(k, {}).get('funpay_lot_id')]
    manual_price = [cfg['lot_mapping'][k]['name'] for k in keys if not cfg['lot_mapping'][k].get('autoprice')]
    report = _pricing_pass(manual=True, only_keys=set(keys), force_price=True) if _account_ready() else \
        {'updated': [], 'errors': ['Аккаунт Starvell ещё не загружен.'], 'skipped': [], 'same': 0}
    done, errors = _restyle_lots(keys, 'keep')
    lines = [f'✅ Обновлено лотов на Starvell: {done} из {len(keys)}']
    if report['updated']:
        lines.append('💰 Цены:\n' + '\n'.join('• ' + escape(x) for x in report['updated'][:15]))
    if manual_price:
        lines.append(f'⚠️ У {len(manual_price)} лотов выключена автоцена — цену проверьте вручную (💵 Цена на Starvell).')
    for item in (report['errors'] + errors)[:10]:
        lines.append('❌ ' + escape(item))
    _show(chat_id, '\n\n'.join(lines), _back_kb('lot:' + keys[0] if len(keys) == 1 else 'lots', '◀️ Назад'))


@route('lqall')
def r_lots_qty_all(q: Any) -> None:
    _ask(q, 'lots_qty', '📦 <b>Количество за 1 шт. для всех лотов</b>\n\nВведите, сколько единиц услуги выдавать за 1 шт. лота '
                        '(обычно <code>1</code>). Бот поменяет количество у всех лотов, пересчитает цены и обновит описания на Starvell.',
         back='lots')


@inp('lots_qty')
def i_lots_qty(message: Any, text: str, data: Dict[str, Any]) -> None:
    value = _int(text)
    if value <= 0:
        raise ValueError('Введите число больше нуля.')
    with config_tx() as tx:
        keys = [k for k, v in tx['lot_mapping'].items() if v['quantity'] != value]
        for key in keys:
            tx['lot_mapping'][key]['quantity'] = value
    _clear(message)
    if not keys:
        _show(message.chat.id, f'ℹ️ У всех лотов уже {value} ед. за 1 шт.', _back_kb('lots', '◀️ Лоты'))
        return
    def job(progress: Callable) -> str:
        cfg = load_config()
        with_fp = [k for k in keys if cfg['lot_mapping'].get(k, {}).get('funpay_lot_id')]
        report = _pricing_pass(manual=True, only_keys=set(with_fp), force_price=True)
        done, errors = _restyle_lots(with_fp, 'keep', progress)
        manual = sum(1 for k in with_fp if not cfg['lot_mapping'][k].get('autoprice'))
        text = (f'📦 Количество {value} ед. за 1 шт. установлено у {len(keys)} лотов.\n'
                f'💰 Цен обновлено: {len(report["updated"])} · 🎨 описаний: {done}')
        if manual:
            text += f'\n⚠️ У {manual} лотов выключена автоцена — проверьте их цену вручную.'
        errs = report['errors'] + errors
        if errs:
            text += '\n\nОшибки:\n' + '\n'.join('• ' + escape(x) for x in errs[:10])
        return text
    _progress_job(message.chat.id, 'Обновляю количество, цены и описания', job, _back_kb('lots', '◀️ Лоты'))


def _bind_funpay_id(chat_id: int, key: str, lot_id: int) -> None:
    if not _account_ready():
        _show(chat_id, '⏳ Аккаунт Starvell ещё не загрузился. Повторите через минуту.', _back_kb('lot:' + key, '◀️ К лоту'))
        return
    try:
        fields = _lot_fields(lot_id)
    except Exception as exc:
        _show(chat_id, f'❌ Лот {lot_id} не найден на вашем аккаунте Starvell ({escape(type(exc).__name__)}).',
              _back_kb('lot:' + key, '◀️ К лоту'))
        return
    name = fields.fields.get('fields[summary][ru]') or fields.fields.get('fields[name][ru]') or ''
    node = getattr(getattr(fields, 'subcategory', None), 'id', None) or _int(fields.fields.get('node_id'))
    with config_tx() as tx:
        if key not in tx['lot_mapping']:
            return
        tx['lot_mapping'][key].update(funpay_lot_id=lot_id, tag_written=False, subcategory=node or None,
                                      **({'name': name} if name else {}))
    ok, message = _write_tag(key)
    _show(chat_id, f'✅ Лот Starvell {lot_id} привязан.\n🏷 {escape(message)}', None)
    _show(chat_id, *scr_lot(key))


@route('lpanel')
def r_lot_panel(q: Any, key: str, panel_id: str = '') -> None:
    cfg = load_config()
    if not panel_id:
        rows = [[B(('✅ ' if cfg['lot_mapping'].get(key, {}).get('panel_id') == pid else '') + p['name'], f'lpanel:{key}:{pid}')]
                for pid, p in cfg['services'].items()]
        rows.append([B('◀️ К лоту', 'lot:' + key)])
        _edit(q, '🔌 Выберите панель для лота:', _kb(*rows))
        return
    with config_tx() as tx:
        if key in tx['lot_mapping'] and panel_id in tx['services']:
            tx['lot_mapping'][key]['panel_id'] = panel_id
    _answer(q, '✅ Панель изменена. Проверьте ID услуги — в другой панели он свой.', True)
    r_lot(q, key)


@route('lt')
def r_lot_toggle(q: Any, what: str, key: str) -> None:
    with config_tx() as tx:
        lot = tx['lot_mapping'].get(key)
        if not lot:
            return
        if what == 'poll':
            lot['poll'] = not lot['poll']
        elif what == 'ap':
            lot['autoprice'] = not lot['autoprice']
            if lot['autoprice'] and not lot.get('funpay_lot_id'):
                _answer(q, '⚠️ У лота нет Starvell ID — цена меняться не сможет, пока вы его не привяжете.', True)
        elif what == 'mm':
            lot['markup_mode'] = {'global': 'fixed', 'fixed': 'percent', 'percent': 'global'}[lot['markup_mode']]
            if lot['markup_mode'] != 'global' and not lot.get('markup_value'):
                lot['markup_value'] = tx['pricing']['fixed' if lot['markup_mode'] == 'fixed' else 'percent']
    r_lot(q, key)


@route('ltag')
def r_lot_tag(q: Any, key: str) -> None:
    _answer(q, '⏳ Записываю тег на Starvell…')
    def job() -> None:
        ok, message = _write_tag(key)
        _show(q.message.chat.id, ('✅ ' if ok else '❌ ') + escape(message) +
              ('' if ok else f'\n\nДобавьте вручную строку <code>#SMM:{escape(load_config()["lot_mapping"].get(key, {}).get("tag", ""))}</code> '
                             'в подробное описание лота на Starvell.'), _back_kb('lot:' + key, '◀️ К лоту'))
    _submit(job)


@route('lcalc')
def r_lot_calc(q: Any, key: str) -> None:
    _answer(q, '⏳ Считаю…')
    def job() -> None:
        cfg = load_config()
        lot = cfg['lot_mapping'].get(key)
        if not lot:
            return
        try:
            info = _lot_price_info(cfg, lot)
        except PanelError as exc:
            _show(q.message.chat.id, f'❌ Посчитать нельзя: {escape(str(exc))}', _back_kb('lot:' + key, '◀️ К лоту'))
            return
        fx = _fx_to_rub(info['currency'], cfg)
        text = (f'💱 <b>Расчёт цены</b> — {escape(_short(lot["name"], 60))}\n\n'
                f'Цена провайдера: {info["rate"]:g} {escape(info["currency"])} за 1000\n'
                + (f'Курс: 1 {escape(info["currency"])} = {fx:.2f} ₽\n' if info['currency'] != 'RUB' else '') +
                f'Количество за 1 шт.: {lot["quantity"]}\n'
                f'Себестоимость 1 шт.: <b>{_money(info["cost_rub"])} ₽</b>\n'
                f'Наценка: {_markup_text(info["mode"], info["value"])}\n'
                f'Цена продажи: <b>{_money(info["price"])} ₽</b>'
                + ('' if lot.get('funpay_lot_id') else '\n\n⚠️ У лота нет Starvell ID — цена на Starvell меняться не будет.'))
        _show(q.message.chat.id, text, _back_kb('lot:' + key, '◀️ К лоту'))
    _submit(job)


@route('lact')
def r_lot_active(q: Any, key: str, value: str) -> None:
    lot = _lot_by_key(key)
    if not lot or not lot.get('funpay_lot_id'):
        _answer(q, 'У лота нет Starvell ID.', True)
        return
    _answer(q, '⏳ Сохраняю на Starvell…')
    def job() -> None:
        try:
            changed = _set_lot_active(lot['funpay_lot_id'], value == '1')
            text = ('🟢 Лот включён на Starvell.' if value == '1' else '🔴 Лот выключен на Starvell.') if changed else 'ℹ️ Лот уже в этом состоянии.'
            if value == '1':
                _state_set('sales_off', [x for x in (_state_get('sales_off', []) or []) if x != lot['funpay_lot_id']])
        except Exception as exc:
            text = '❌ Starvell не сохранил лот: ' + escape(_rejection_text(exc))
        _show(q.message.chat.id, text, _back_kb('lot:' + key, '◀️ К лоту'))
    _submit(job)


@route('ldel')
def r_lot_delete(q: Any, key: str) -> None:
    lot = _lot_by_key(key)
    if not lot:
        _answer(q, 'Лот уже удалён.', True)
        return
    rows = [[B('🗑 Только из плагина', f'ldd:{key}:0')]]
    if lot.get('funpay_lot_id') and CAN_DELETE_LOTS:
        rows.append([B('🔥 Из плагина и с Starvell', f'ldd:{key}:1')])
    rows.append([B('Отмена', 'lot:' + key)])
    _edit(q, f'❌ <b>Удалить лот?</b>\n\n{escape(lot["name"])}\n\nПосле удаления из плагина заказы по этому лоту '
             'перестанут обрабатываться автоматически.', _kb(*rows))


@route('ldd')
def r_lot_delete_do(q: Any, key: str, remote: str) -> None:
    lot = _lot_by_key(key)
    if not lot:
        _answer(q, 'Лот уже удалён.', True)
        return
    def finish(extra: str = '') -> None:
        with config_tx(allow_shrink=True) as tx:
            tx['lot_mapping'].pop(key, None)
        _show(q.message.chat.id, '✅ Лот удалён из плагина.' + extra, _back_kb('lots', '◀️ Лоты'))
    if remote == '1' and lot.get('funpay_lot_id'):
        _answer(q, '⏳ Удаляю на Starvell…')
        def job() -> None:
            try:
                _delete_funpay_lot(lot['funpay_lot_id'])
                finish(' Лот удалён и на Starvell.')
            except Exception as exc:
                _show(q.message.chat.id, '❌ Starvell не удалил лот: ' + escape(_rejection_text(exc)) +
                      '\nЛот в плагине сохранён.', _back_kb('lot:' + key, '◀️ К лоту'))
        _submit(job)
    else:
        _answer(q)
        finish(' Сам лот на Starvell остался.')


@route('ldes')
def r_lot_design(q: Any, key: str) -> None:
    if not CAN_CREATE_LOTS:
        r_lot_text(q, key)
        return
    cfg = load_config()
    lot = cfg['lot_mapping'].get(key)
    if not lot or not lot.get('funpay_lot_id'):
        _answer(q, 'Нужен привязанный Starvell ID.', True)
        return
    rows = _template_buttons(f'ldset:{key}', cfg)
    rows.append([B('◀️ К лоту', 'lot:' + key)])
    _edit(q, f'🎨 <b>Оформление лота</b>\n\nСейчас: {escape(_template_name(cfg, lot.get("template") or "auto"))}\n\n'
             'Выберите тему или свой шаблон — описание лота на Starvell будет переписано (цена и тег сохранятся).', _kb(*rows))


@route('ldset')
def r_lot_design_set(q: Any, key: str, token: str) -> None:
    if not CAN_CREATE_LOTS:
        _answer(q, 'На этой площадке тексты лотов меняются вручную — используйте «📋 Текст для лота».', True)
        return
    choice = _token_choice(token)
    _answer(q, '⏳ Переоформляю лот…')
    with config_tx() as tx:
        if key in tx['lot_mapping']:
            tx['lot_mapping'][key]['template'] = '' if choice == 'auto' else choice
    def job() -> None:
        done, errors = _restyle_lots([key], choice)
        _show(q.message.chat.id, '✅ Лот переоформлен.' if done else '❌ ' + escape('; '.join(errors) or 'Не удалось.'),
              _back_kb('lot:' + key, '◀️ К лоту'))
    _submit(job)


# ---------------------------------------------------------------------------
# Готовый текст лота (для ручного создания или правки на площадке)
# ---------------------------------------------------------------------------

def _text_message(service: Dict[str, Any], quantity: int, price: float, choice: str, header: str) -> List[str]:
    cfg = load_config()
    limits = {'summary_ru': 100, 'summary_en': 100, 'desc_ru': 5000, 'desc_en': 5000}
    texts = render_lot_texts(service, quantity, price, choice, limits, cfg, '')
    ru = (f'{header}\n🎨 {escape(_template_name(cfg, texts["template"]))}\n\n<b>Название (RU):</b>\n<pre>{escape(texts["summary_ru"])}</pre>\n'
          f'<b>Описание (RU):</b>\n<pre>{escape(texts["desc_ru"][:3200])}</pre>')
    en = (f'<b>Title (EN):</b>\n<pre>{escape(texts["summary_en"])}</pre>\n'
          f'<b>Description (EN):</b>\n<pre>{escape(texts["desc_en"][:3400])}</pre>')
    return [ru, en]


@route('ltx', 'ltxt')
def r_lot_text(q: Any, key: str, token: str = '') -> None:
    cfg = load_config()
    lot = cfg['lot_mapping'].get(key)
    if not lot:
        _answer(q, 'Лот не найден.', True)
        return
    _answer(q, '⏳ Готовлю текст…')
    choice = _token_choice(token) if token else (lot.get('template') or 'auto')

    def job() -> None:
        try:
            service = PanelAPI(cfg, lot['panel_id']).service(lot['service_id'])
        except PanelError as exc:
            _show(q.message.chat.id, f'❌ {escape(str(exc))}', _back_kb('lot:' + key, '◀️ К лоту'))
            return
        price = _float(lot.get('last_price'))
        parts = _text_message(service, lot['quantity'], price, choice,
                              f'📋 <b>Текст для лота</b> — нажмите на блок, чтобы скопировать, и вставьте в редактор лота на {MARKET}.')
        _show(q.message.chat.id, parts[0])
        rows = _template_buttons(f'ltxt:{key}', cfg)
        rows.append([B('◀️ К лоту', 'lot:' + key)])
        _show(q.message.chat.id, parts[1], _kb(*rows))
    _submit(job)


# ---------------------------------------------------------------------------
# Рефилл конкретного лота
# ---------------------------------------------------------------------------

@route('lref')
def r_lot_refill(q: Any, key: str) -> None:
    lot = _lot_by_key(key)
    if not lot:
        return
    r = lot['refill']
    text = (f'♻️ <b>Рефилл лота</b>\n{escape(_short(lot["name"], 60))}\n\n'
            f'Рефилл: {"ВКЛ" if r["enabled"] else "ВЫКЛ"}\nЛимит запусков: {"ВКЛ" if r["limit_enabled"] else "ВЫКЛ"} ({r["limit"]})\n'
            f'Срок действия: {"ВКЛ" if r["period_enabled"] else "ВЫКЛ"} ({_fmt_hours(r["hours"])})\n\n'
            'Условия фиксируются в заказе в момент покупки.')
    kb = _kb([B(f'♻️ Рефилл: {"ВКЛ" if r["enabled"] else "ВЫКЛ"}', f'lrt:en:{key}')],
             [B(f'🔁 Лимит: {"ВКЛ" if r["limit_enabled"] else "ВЫКЛ"}', f'lrt:lim:{key}'), B(f'✏️ Запусков ({r["limit"]})', f'lre:limit:{key}')],
             [B(f'⏳ Срок: {"ВКЛ" if r["period_enabled"] else "ВЫКЛ"}', f'lrt:per:{key}'), B(f'✏️ Срок ({r["hours"]} ч)', f'lre:hours:{key}')],
             [B('◀️ К настройкам лота', 'lot:' + key)])
    _edit(q, text, kb)


@route('lrt')
def r_lot_refill_toggle(q: Any, what: str, key: str) -> None:
    with config_tx() as tx:
        lot = tx['lot_mapping'].get(key)
        if lot:
            field = {'en': 'enabled', 'lim': 'limit_enabled', 'per': 'period_enabled'}[what]
            lot['refill'][field] = not lot['refill'][field]
    r_lot_refill(q, key)


@route('lre')
def r_lot_refill_edit(q: Any, what: str, key: str) -> None:
    prompt = '✏️ Сколько раз покупатель может запустить рефилл по одному заказу? (целое, минимум 1)' if what == 'limit' else \
        '✏️ Срок действия рефилла в часах (например 168 = 7 дней):'
    _ask(q, 'lot_refill', prompt, {'what': what, 'key': key}, back='lref:' + key)


@inp('lot_refill')
def i_lot_refill(message: Any, text: str, data: Dict[str, Any]) -> None:
    value = _int(text)
    if value < 1:
        raise ValueError('Нужно целое число не меньше 1.')
    with config_tx() as tx:
        lot = tx['lot_mapping'].get(data['key'])
        if not lot:
            raise ValueError('Лот уже удалён.')
        lot['refill']['limit' if data['what'] == 'limit' else 'hours'] = value
    _clear(message)
    _show(message.chat.id, '✅ Сохранено.', _back_kb('lref:' + data['key'], '◀️ К рефиллу лота'))


# ---------------------------------------------------------------------------
# Новый лот: привязка существующего и ручное создание
# ---------------------------------------------------------------------------

def _wizard(user_id: int, kind: str, reset: bool = False) -> Dict[str, Any]:
    wizard = _wizards.get(user_id)
    if reset or not wizard or wizard.get('kind') != kind:
        wizard = {'kind': kind, 'created': time.time()}
        _wizards[user_id] = wizard
    return wizard


def _panel_rows(prefix: str, back: str) -> List[List[_TelegramButton]]:
    cfg = load_config()
    rows = [[B('🔌 ' + p['name'], f'{prefix}:{pid}')] for pid, p in cfg['services'].items() if p['enabled']]
    rows.append([B('◀️ Назад', back)])
    return rows


def _service_card(service: Dict[str, Any], panel_id: str, quantity: int = 0) -> str:
    profile = _service_profile(service)
    pinfo, kinfo = _platform_info(profile['platform']), KINDS[profile['kind']]
    lines = [f'🧾 <b>{escape(_short(str(service.get("name")), 120))}</b>',
             f'ID: <code>{escape(str(service.get("service")))}</code> · категория: {escape(_short(str(service.get("category") or "—"), 60))}',
             f'Цена: {escape(str(service.get("rate")))} за 1000 · мин {escape(str(service.get("min")))} · макс {escape(str(service.get("max")))}',
             f'Тип: {escape(str(service.get("type", "Default")))} · рефилл: {"да" if service.get("refill") in (True, 1, "1", "true") else "нет"}',
             f'Распознано: {pinfo["emoji"]} {escape(pinfo["name"])} · {kinfo["emoji"]} {escape(kinfo["ru"].lower())}']
    if quantity:
        try:
            cfg = load_config()
            cost, _, currency = _cost_rub(cfg, panel_id, service, quantity)
            lines.append(f'Себестоимость {quantity} ед.: <b>{_money(cost)} ₽</b>' + (f' (валюта панели {currency})' if currency != 'RUB' else ''))
        except PanelError as exc:
            lines.append(f'⚠️ {escape(str(exc))}')
    return '\n'.join(lines)


def _search_services(panel_id: str, text: str) -> List[Dict[str, Any]]:
    services = PanelAPI(load_config(), panel_id).services()
    text = text.strip().lower()
    if text.isdigit():
        exact = [x for x in services if str(x.get('service')) == text]
        if exact:
            return exact
    words = [w for w in re.split(r'\s+', text) if w]
    return [x for x in services if all(w in (str(x.get('name', '')) + ' ' + str(x.get('category', ''))).lower() for w in words)]


@route('new')
def r_new(q: Any) -> None:
    if CAN_CREATE_LOTS:
        _edit(q, '➕ <b>Новый лот</b>\n\n'
                 '🔗 <b>Привязать</b> — у вас уже есть лот на Starvell: бот свяжет его с услугой панели и запишет тег.\n'
                 '🛠 <b>Создать</b> — бот сам создаст красиво оформленный лот на Starvell из услуги панели.',
              _kb([B('🔗 Привязать лот Starvell по ID', 'nb')], [B('🛠 Создать лот из услуги панели', 'nc')],
                  [B('📋 Только текст для лота', 'ntx')], [B('◀️ Назад', 'home')]))
        return
    _edit(q, '➕ <b>Новый лот</b>\n\n'
             f'{MARKET} не даёт создавать лоты через API, поэтому так:\n'
             '1️⃣ <b>📋 Текст для лота</b> — бот подготовит красивое название и описание под услугу панели.\n'
             f'2️⃣ Создайте лот на {MARKET} вручную и вставьте этот текст.\n'
             '3️⃣ <b>🔗 Привязать</b> — введите ID лота, и бот начнёт выполнять по нему заказы, менять цену и следить за балансом.',
          _kb([B('📋 Текст для лота', 'ntx')], [B(f'🔗 Привязать лот {MARKET} по ID', 'nb')], [B('◀️ Назад', 'home')]))


@route('nb')
def r_new_bind(q: Any) -> None:
    if not load_config()['services']:
        _answer(q, 'Сначала добавьте SMM-панель в разделе «🔌 Панели».', True)
        return
    _wizard(q.from_user.id, 'bind', reset=True)
    _ask(q, 'bind_lot', f'🔗 Введите ID лота Starvell — {LOT_ID_HINT}:', back='new')


@inp('bind_lot')
def i_bind_lot(message: Any, text: str, data: Dict[str, Any]) -> None:
    lot_id = _int(text)
    if lot_id <= 0:
        raise ValueError('Введите числовой ID лота Starvell.')
    if not _account_ready():
        raise ValueError('Аккаунт Starvell ещё не загрузился после запуска бота. Подождите минуту и повторите.')
    existing = _lot_key_by_funpay_id(load_config(), lot_id)
    if existing:
        _clear(message)
        _show(message.chat.id, f'ℹ️ Этот лот уже привязан: <code>{existing}</code>.')
        _show(message.chat.id, *scr_lot(existing))
        return
    _clear(message)
    user_id, chat_id = message.from_user.id, message.chat.id
    _show(chat_id, '⏳ Ищу лот на Starvell…')

    def job() -> None:
        try:
            fields = _lot_fields(lot_id)
        except Exception as exc:
            _show(chat_id, f'❌ Лот {lot_id} не найден на вашем аккаунте ({escape(type(exc).__name__)}). Проверьте ID.',
                  _kb([B('🔁 Ввести другой ID', 'nb'), B('◀️ Назад', 'new')]))
            return
        wizard = _wizard(user_id, 'bind')
        wizard.update(lot_id=lot_id, name=fields.fields.get('fields[summary][ru]') or fields.fields.get('fields[name][ru]') or f'Лот #{lot_id}',
                      node=getattr(getattr(fields, 'subcategory', None), 'id', None) or _int(fields.fields.get('node_id')),
                      price=_float(getattr(fields, 'price', None) or fields.fields.get('price')))
        _show(chat_id, f'✅ Лот найден: <b>{escape(wizard["name"])}</b>\nЦена на Starvell: {_money(wizard["price"])} ₽\n\nВыберите SMM-панель:',
              _kb(*_panel_rows('nbp', 'new')))
    _submit(job)


@route('nbp')
def r_bind_panel(q: Any, panel_id: str) -> None:
    wizard = _wizard(q.from_user.id, 'bind')
    if not wizard.get('lot_id'):
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    wizard['panel'] = panel_id
    _ask(q, 'bind_service', '🆔 Введите ID услуги в панели или часть названия для поиска (например «telegram подписчики»):', back='new')


def _service_pick(message: Any, text: str, mode: str) -> None:
    """Общий шаг выбора услуги для привязки и создания."""
    wizard = _wizard(message.from_user.id, mode)
    if not wizard.get('panel'):
        raise ValueError('Мастер устарел — начните заново.')
    found = _search_services(wizard['panel'], text)
    if not found:
        raise ValueError('Услуга не найдена. Введите точный ID или другие слова для поиска.')
    _clear(message)
    if len(found) == 1:
        _service_chosen(message.chat.id, message.from_user.id, mode, str(found[0]['service']))
        return
    prefix = {'bind': 'nbs', 'create': 'ncs', 'text': 'nts'}[mode]
    again = {'bind': 'nbp', 'create': 'ncp', 'text': 'ntp'}[mode]
    rows = [[B(f'{x.get("service")} · {_short(str(x.get("name")), 40)} · {x.get("rate")}', f'{prefix}:{x.get("service")}')]
            for x in found[:12]]
    rows.append([B('🔍 Искать заново', f'{again}:{wizard["panel"]}')])
    _show(message.chat.id, f'🔍 Найдено услуг: {len(found)}' + (' (первые 12)' if len(found) > 12 else '') + '. Выберите:', _kb(*rows))


@inp('bind_service')
def i_bind_service(message: Any, text: str, data: Dict[str, Any]) -> None:
    _service_pick(message, text, 'bind')


@route('nbs', 'ncs', 'nts')
def r_service_chosen(q: Any, service_id: str) -> None:
    _answer(q)
    mode = {'nbs': 'bind', 'ncs': 'create', 'nts': 'text'}[q.data.split(':')[0]]
    _service_chosen(q.message.chat.id, q.from_user.id, mode, service_id)


def _qty_rows(prefix: str, service: Dict[str, Any]) -> List[List[_TelegramButton]]:
    smin, smax = max(1, _int(service.get('min'), 1)), _int(service.get('max'))
    values = sorted({1, smin, *(x for x in (100, 1000) if not smax or x <= smax)})[:4]
    return [[B(('1 шт. = 1 ед.' if v == 1 else f'Пакет {v}'), f'{prefix}:{v}') for v in values]]


def _service_chosen(chat_id: int, user_id: int, mode: str, service_id: str) -> None:
    wizard = _wizard(user_id, mode)
    try:
        service = PanelAPI(load_config(), wizard['panel']).service(service_id)
    except (PanelError, KeyError) as exc:
        _show(chat_id, f'❌ {escape(str(exc))}', _back_kb('new'))
        return
    if str(service.get('type', 'Default')).strip().lower() not in ('default', 'poll'):
        _show(chat_id, f'❌ Тип услуги «{escape(str(service.get("type")))}» не поддерживается. Нужны Default или Poll.', _back_kb('new'))
        return
    wizard['service'] = service
    wizard['service_id'] = str(service['service'])
    profile = _service_profile(service)
    wizard['profile'] = profile
    if mode == 'text':
        sent = _show(chat_id, _service_card(service, wizard['panel']) + '\n\n📦 Сколько единиц услуги в 1 шт. лота? Обычно 1.',
                     _kb(*_qty_rows('ntq', service), [B('❌ Отмена', 'x:new')]))
        cardinal_instance.telegram.set_state(chat_id, getattr(sent, 'message_id', 0), user_id, 'autosmm:text_qty', {})
        return
    if mode == 'create':
        cfg = load_config()
        node = cfg['design']['sections'].get(f'{profile["platform"]}:{profile["kind"]}') or cfg['design']['sections'].get(profile['platform'])
        rows = []
        if node:
            rows.append([B(f'🗺 Раздел из карты: {node}', f'ncn:{node}')])
        rows.append([B('❌ Отмена', 'x:new')])
        sent = _show(chat_id, _service_card(service, wizard['panel']) + '\n\n🗂 Введите ID раздела Starvell (число из ссылки '
                     '<code>funpay.com/lots/ЧИСЛО/</code>)' + (' или нажмите кнопку с разделом из карты.' if node else ':'), _kb(*rows))
        cardinal_instance.telegram.set_state(chat_id, getattr(sent, 'message_id', 0), user_id, 'autosmm:create_node', {})
        return
    sent = _show(chat_id, _service_card(service, wizard['panel']) + '\n\n📦 Сколько единиц услуги заказывать за 1 шт. лота? '
                 'Обычно 1 (1 шт. = 1 подписчик). Введите число или выберите:', _kb(*_qty_rows('nbq', service), [B('❌ Отмена', 'x:new')]))
    cardinal_instance.telegram.set_state(chat_id, getattr(sent, 'message_id', 0), user_id, 'autosmm:bind_qty', {})


@inp('bind_qty')
def i_bind_qty(message: Any, text: str, data: Dict[str, Any]) -> None:
    if _int(text) <= 0:
        raise ValueError('Введите число больше нуля.')
    _clear(message)
    _bind_finish(message.chat.id, message.from_user.id, _int(text))


@route('nbq')
def r_bind_qty(q: Any, value: str) -> None:
    _answer(q)
    try:
        cardinal_instance.telegram.clear_state(q.message.chat.id, q.from_user.id, False)
    except Exception:
        pass
    _bind_finish(q.message.chat.id, q.from_user.id, _int(value))


def _bind_finish(chat_id: int, user_id: int, quantity: int) -> None:
    wizard = _wizard(user_id, 'bind')
    if not wizard.get('service'):
        _show(chat_id, '⌛ Мастер устарел — начните заново.', _back_kb('new'))
        return
    service, profile = wizard['service'], wizard['profile']
    warn = ''
    if quantity < max(1, _int(service.get('min'), 1)):
        warn = f'\n⚠️ Количество меньше минимума услуги ({service.get("min")}): покупатель должен брать от {math.ceil(_int(service.get("min")) / quantity)} шт.'
    key = _register_lot(wizard['name'], wizard['lot_id'], wizard['panel'], wizard['service_id'], quantity,
                        list(_platform_info(profile['platform']).get('domains') or []), source='bind', subcategory=wizard.get('node'),
                        platform=profile['platform'], kind=profile['kind'], poll='poll' in str(service.get('type', '')).lower())
    _wizards.pop(user_id, None)
    _show(chat_id, f'✅ Лот привязан: <code>{key}</code>{escape(warn)}\n⏳ Записываю тег на Starvell…')

    def job() -> None:
        ok, message = _write_tag(key)
        cfg = load_config()
        lot = cfg['lot_mapping'].get(key)
        extra = ''
        try:
            info = _lot_price_info(cfg, lot)
            if wizard.get('price') and wizard['price'] < info['cost_rub']:
                extra = f'\n\n🛑 Цена лота {_money(wizard["price"])} ₽ НИЖЕ себестоимости {_money(info["cost_rub"])} ₽!'
        except PanelError:
            pass
        _show(chat_id, ('🏷 ' if ok else '⚠️ ') + escape(message) + extra)
        _show(chat_id, *scr_lot(key))
    _submit(job)


@route('nc')
def r_new_create(q: Any) -> None:
    if not CAN_CREATE_LOTS:
        _answer(q, f'{MARKET} не даёт создавать лоты через API. Используйте «📋 Текст для лота» и привязку.', True)
        return
    if not load_config()['services']:
        _answer(q, 'Сначала добавьте SMM-панель в разделе «🔌 Панели».', True)
        return
    _wizard(q.from_user.id, 'create', reset=True)
    _edit(q, '🛠 <b>Создание лота</b>\n\nВыберите SMM-панель:', _kb(*_panel_rows('ncp', 'new')))


@route('ncp')
def r_create_panel(q: Any, panel_id: str) -> None:
    wizard = _wizard(q.from_user.id, 'create')
    wizard['panel'] = panel_id
    _ask(q, 'create_service', '🆔 Введите ID услуги или часть названия для поиска (например «tiktok просмотры»):', back='new')


@inp('create_service')
def i_create_service(message: Any, text: str, data: Dict[str, Any]) -> None:
    _service_pick(message, text, 'create')


@inp('create_node')
def i_create_node(message: Any, text: str, data: Dict[str, Any]) -> None:
    if _int(text) <= 0:
        raise ValueError('ID раздела — число больше нуля.')
    _clear(message)
    _create_node_set(message.chat.id, message.from_user.id, _int(text))


@route('ncn')
def r_create_node(q: Any, node: str) -> None:
    _answer(q)
    try:
        cardinal_instance.telegram.clear_state(q.message.chat.id, q.from_user.id, False)
    except Exception:
        pass
    _create_node_set(q.message.chat.id, q.from_user.id, _int(node))


def _create_node_set(chat_id: int, user_id: int, node: int) -> None:
    wizard = _wizard(user_id, 'create')
    if not wizard.get('service'):
        _show(chat_id, '⌛ Мастер устарел — начните заново.', _back_kb('new'))
        return
    wizard['node'] = node
    service = wizard['service']
    sent = _show(chat_id, f'🗂 Раздел: {node}\n\n📦 Сколько единиц услуги в 1 шт. лота? Обычно 1 — тогда покупатель просто берёт нужное '
                 f'количество (минимум услуги: {service.get("min")}).',
                 _kb(*_qty_rows('ncq', service), [B('❌ Отмена', 'x:new')]))
    cardinal_instance.telegram.set_state(chat_id, getattr(sent, 'message_id', 0), user_id, 'autosmm:create_qty', {})


@inp('create_qty')
def i_create_qty(message: Any, text: str, data: Dict[str, Any]) -> None:
    if _int(text) <= 0:
        raise ValueError('Введите число больше нуля.')
    _clear(message)
    _create_qty_set(message.chat.id, message.from_user.id, _int(text))


@route('ncq')
def r_create_qty(q: Any, value: str) -> None:
    _answer(q)
    try:
        cardinal_instance.telegram.clear_state(q.message.chat.id, q.from_user.id, False)
    except Exception:
        pass
    _create_qty_set(q.message.chat.id, q.from_user.id, _int(value))


def _create_qty_set(chat_id: int, user_id: int, quantity: int) -> None:
    wizard = _wizard(user_id, 'create')
    service = wizard.get('service')
    if not service:
        _show(chat_id, '⌛ Мастер устарел — начните заново.', _back_kb('new'))
        return
    smin, smax = max(1, _int(service.get('min'), 1)), _int(service.get('max'))
    if smax and quantity > smax:
        _show(chat_id, f'❌ Количество больше максимума услуги ({smax}). Введите меньше.', _back_kb('new'))
        return
    wizard['qty'] = quantity
    cfg = load_config()
    mode, value = _effective_markup(cfg, None)
    note = '' if quantity >= smin else f'\nℹ️ Покупатель сможет заказать от {math.ceil(smin / quantity)} шт. — это будет указано в лоте.'
    sent = _show(chat_id, f'📦 За 1 шт.: {quantity}{escape(note)}\n\n📈 Наценка: выберите глобальную ({_markup_text(mode, value)}) '
                          'или введите свой процент:', _kb([B(f'🌍 Глобальная ({_markup_text(mode, value)})', 'ncm:g')], [B('❌ Отмена', 'x:new')]))
    cardinal_instance.telegram.set_state(chat_id, getattr(sent, 'message_id', 0), user_id, 'autosmm:create_markup', {})


@inp('create_markup')
def i_create_markup(message: Any, text: str, data: Dict[str, Any]) -> None:
    wizard = _wizard(message.from_user.id, 'create')
    wizard['markup'] = ('percent', _markup(text))
    _clear(message)
    _create_style(message.chat.id)


@route('ncm')
def r_create_markup(q: Any, value: str) -> None:
    wizard = _wizard(q.from_user.id, 'create')
    wizard['markup'] = None
    try:
        cardinal_instance.telegram.clear_state(q.message.chat.id, q.from_user.id, False)
    except Exception:
        pass
    _answer(q)
    _create_style(q.message.chat.id)


def _create_style(chat_id: int) -> None:
    cfg = load_config()
    rows = _template_buttons('nct', cfg)
    rows.append([B('❌ Отмена', 'new')])
    _show(chat_id, '🎨 Выберите оформление лота (можно потом поменять):', _kb(*rows))


@route('nct')
def r_create_style(q: Any, token: str) -> None:
    wizard = _wizard(q.from_user.id, 'create')
    if not wizard.get('qty'):
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    wizard['choice'] = _token_choice(token)
    _answer(q, '⏳ Готовлю предпросмотр…')
    _submit(_create_preview, q.message.chat.id, q.from_user.id)


def _create_preview(chat_id: int, user_id: int) -> None:
    wizard = _wizard(user_id, 'create')
    cfg = load_config()
    try:
        service = wizard['service']
        cost, _, _ = _cost_rub(cfg, wizard['panel'], service, wizard['qty'])
        mode, value = wizard.get('markup') or _effective_markup(cfg, None)
        price = wizard.get('price_override') or _price_from_cost(cost, mode, value)
        if 'form' not in wizard:
            wizard['form'] = _node_form(wizard['node'])
        limits = _form_limits(wizard['form'], cfg)
        texts = render_lot_texts(service, wizard['qty'], price, wizard['choice'], limits, cfg, 'xxxxxxxx')
        if wizard.get('name_override'):
            texts['summary_ru'] = _summary_field(wizard['name_override'], limits['summary_ru'])
    except Exception as exc:
        _show(chat_id, f'❌ {escape(str(exc) if isinstance(exc, (PanelError, RuntimeError)) else type(exc).__name__)}', _back_kb('new'))
        return
    wizard['price'] = price
    wizard['cost'] = cost
    desc = texts['desc_ru'].replace('#SMM:xxxxxxxx', '#SMM:…')
    text = (f'👁 <b>Предпросмотр лота</b>\n\n<b>Название:</b>\n{escape(texts["summary_ru"])}\n\n'
            f'<b>Описание:</b>\n<blockquote expandable>{escape(desc[:2500])}</blockquote>\n\n'
            f'💵 Цена: <b>{_money(price)} ₽</b> за 1 шт. (себестоимость {_money(cost)} ₽)\n'
            f'🗂 Раздел: {wizard["node"]} · 📦 За 1 шт.: {wizard["qty"]}\n'
            f'🎨 {escape(_template_name(cfg, wizard["choice"]))}')
    _show(chat_id, text, _kb([B('🚀 Опубликовать', 'ncgo')],
                             [B('✏️ Название', 'nce:name'), B('💵 Цена', 'nce:price'), B('🎨 Стиль', 'ncst')],
                             [B('🧩 Поля раздела', 'fl:create')] if wizard['form'].get('selects') else None,
                             [B('❌ Отмена', 'new')]))


@route('nce')
def r_create_edit(q: Any, field: str) -> None:
    prompt = '✏️ Введите своё название лота (краткое описание):' if field == 'name' else '💵 Введите цену за 1 шт. в рублях:'
    _ask(q, 'create_edit', prompt, {'field': field}, back='new')


@inp('create_edit')
def i_create_edit(message: Any, text: str, data: Dict[str, Any]) -> None:
    wizard = _wizard(message.from_user.id, 'create')
    if data['field'] == 'name':
        if not text.strip():
            raise ValueError('Название не может быть пустым.')
        wizard['name_override'] = text.strip()
    else:
        price = _float(text, -1)
        if price <= 0:
            raise ValueError('Цена должна быть больше нуля.')
        if wizard.get('cost') and price < wizard['cost']:
            _show(message.chat.id, f'⚠️ Цена ниже себестоимости ({_money(wizard["cost"])} ₽)!')
        wizard['price_override'] = price
    _clear(message)
    _submit(_create_preview, message.chat.id, message.from_user.id)


@route('ncst')
def r_create_restyle(q: Any) -> None:
    _answer(q)
    _create_style(q.message.chat.id)


@route('ncgo')
def r_create_go(q: Any) -> None:
    wizard = _wizard(q.from_user.id, 'create')
    if not wizard.get('price') or wizard.get('running'):
        _answer(q, 'Мастер устарел или уже публикуется.', True)
        return
    if not _account_ready():
        _answer(q, 'Аккаунт Starvell ещё не загружен.', True)
        return
    wizard['running'] = True
    _answer(q, '⏳ Публикую лот…')
    chat_id, user_id = q.message.chat.id, q.from_user.id

    def job() -> None:
        cfg = load_config()
        service, profile = wizard['service'], wizard['profile']
        tag = _new_tag(x.get('tag') for x in cfg['lot_mapping'].values())
        try:
            lot_id, name, warnings = _create_lot(wizard['node'], wizard['panel'], service, wizard['qty'], wizard['price'],
                                                 wizard['choice'], tag, form=wizard.get('form'),
                                                 select_overrides=wizard.get('selects'),
                                                 summary_override=wizard.get('name_override', ''))
        except Exception as exc:
            wizard['running'] = False
            _show(chat_id, '❌ ' + escape(str(exc) if isinstance(exc, (PanelError, LotPublishError, RuntimeError)) else type(exc).__name__),
                  _kb([B('🔁 Повторить', 'ncgo'), B('◀️ Назад', 'new')]))
            return
        refill = None
        if cfg['refill'].get('auto_enable') and (service.get('refill') in (True, 1, '1', 'true') or profile['guarantee_days']):
            refill = {'enabled': True, 'hours': (profile['guarantee_days'] or 30) * 24}
        key = _register_lot(name, lot_id, wizard['panel'], wizard['service_id'], wizard['qty'],
                            list(_platform_info(profile['platform']).get('domains') or []), source='manual', subcategory=wizard['node'],
                            tag=tag, platform=profile['platform'], kind=profile['kind'],
                            template='' if wizard['choice'] == 'auto' else wizard['choice'], price=wizard['price'],
                            markup=wizard.get('markup'), poll='poll' in str(service.get('type', '')).lower(),
                            tag_written=bool(lot_id), refill=refill)
        _wizards.pop(user_id, None)
        _show(chat_id, f'✅ Лот опубликован на Starvell и привязан: <code>{key}</code>' +
              ('\n\n⚠️ ' + '\n⚠️ '.join(escape(x) for x in warnings) if warnings else ''))
        _show(chat_id, *scr_lot(key))
    _submit(job)


# ---------------------------------------------------------------------------
# Автовыгрузка
# ---------------------------------------------------------------------------

@route('auto')
def r_auto(q: Any) -> None:
    if not CAN_CREATE_LOTS:
        _answer(q, f'{MARKET} не даёт создавать лоты через API — автовыгрузка недоступна.', True)
        return
    if not load_config()['services']:
        _answer(q, 'Сначала добавьте SMM-панель.', True)
        return
    _edit(q, '📦 <b>Автовыгрузка</b>\n\nБот создаст на Starvell оформленные лоты сразу для многих услуг панели.\n\nВыберите панель:',
          _kb(*_panel_rows('ap', 'home')))


@route('ap')
def r_auto_panel(q: Any, panel_id: str) -> None:
    _answer(q, '⏳ Загружаю услуги панели…')
    chat_id, user_id = q.message.chat.id, q.from_user.id

    def job() -> None:
        try:
            services = PanelAPI(load_config(), panel_id).services(refresh=True)
        except PanelError as exc:
            _show(chat_id, f'❌ {escape(str(exc))}', _back_kb('auto'))
            return
        services = [x for x in services if str(x.get('type', 'Default')).strip().lower() in ('default', 'poll')]
        if not services:
            _show(chat_id, '❌ В панели нет поддерживаемых услуг (Default / Poll).', _back_kb('auto'))
            return
        for item in services:
            item['_profile'] = _service_profile(item)
        wizard = _wizard(user_id, 'auto', reset=True)
        wizard.update(panel=panel_id, services=services, selected=set(), filter='')
        _show(chat_id, f'📦 Загружено услуг: {len(services)}.\n\nКак группировать?',
              _kb([B('📂 По категориям панели', 'am:cat'), B('🌐 По соцсетям', 'am:plat')], [B('◀️ Назад', 'auto')]))
    _submit(job)


def _auto_groups(wizard: Dict[str, Any]) -> List[Tuple[str, str]]:
    if wizard.get('group_mode') == 'plat':
        keys = sorted({x['_profile']['platform'] for x in wizard['services']},
                      key=lambda k: PLATFORM_ORDER.index(k) if k in PLATFORM_ORDER else 99)
        return [(k, f'{_platform_info(k)["emoji"]} {_platform_info(k)["name"]}') for k in keys]
    cats = sorted({str(x.get('category') or 'Без категории') for x in wizard['services']})
    return [(c, c) for c in cats]


def _group_services(wizard: Dict[str, Any], group: str) -> List[Dict[str, Any]]:
    if wizard.get('group_mode') == 'plat':
        return [x for x in wizard['services'] if x['_profile']['platform'] == group]
    return [x for x in wizard['services'] if str(x.get('category') or 'Без категории') == group]


@route('am')
def r_auto_mode(q: Any, mode: str) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard or wizard.get('kind') != 'auto':
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    wizard['group_mode'] = mode
    r_auto_groups(q, '0')


@route('ag')
def r_auto_groups(q: Any, page: str = '0') -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard or wizard.get('kind') != 'auto':
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    groups = _auto_groups(wizard)
    wizard['groups'] = groups
    items, page_n, pages = _page(list(enumerate(groups)), _int(page), 8)
    rows = []
    for index, (key, label) in items:
        services = _group_services(wizard, key)
        chosen = sum(1 for x in services if str(x['service']) in wizard['selected'])
        rows.append([B(f'{"✅ " if chosen else ""}{_short(label, 40)} ({chosen}/{len(services)})', f'ags:{index}:0')])
    rows.append(_nav('ag', page_n, pages))
    rows.append([B(f'➡️ Далее (выбрано {len(wizard["selected"])})', 'anext')] if wizard['selected'] else None)
    rows.append([B('◀️ Назад', 'auto')])
    _edit(q, f'📦 <b>Автовыгрузка</b> — выберите группу услуг\n\nВыбрано услуг: <b>{len(wizard["selected"])}</b>. '
             'Можно отмечать услуги в разных группах.', _kb(*rows))


@route('ags')
def r_auto_group(q: Any, index: str, page: str = '0') -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard or wizard.get('kind') != 'auto' or _int(index) >= len(wizard.get('groups', [])):
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    key, label = wizard['groups'][_int(index)]
    services = _group_services(wizard, key)
    if wizard.get('filter'):
        words = wizard['filter'].lower().split()
        services = [x for x in services if all(w in str(x.get('name', '')).lower() for w in words)]
    items, page_n, pages = _page(services, _int(page), 8)
    rows = []
    for item in items:
        mark = '✅' if str(item['service']) in wizard['selected'] else '⬜'
        rows.append([B(f'{mark} {item["service"]} · {_short(str(item.get("name")), 34)} · {item.get("rate")}',
                       f'at:{index}:{page_n}:{item["service"]}')])
    rows.append(_nav(f'ags:{index}', page_n, pages))
    rows.append([B('✅ Все в группе', f'aall:{index}:1'), B('⬜ Снять все', f'aall:{index}:0')])
    rows.append([B('🔍 Фильтр' + (f': {_short(wizard["filter"], 12)}' if wizard.get('filter') else ''), f'afilt:{index}'),
                 B('◀️ Группы', 'ag:0')])
    rows.append([B(f'➡️ Далее (выбрано {len(wizard["selected"])})', 'anext')] if wizard['selected'] else None)
    _edit(q, f'📦 <b>{escape(_short(label, 60))}</b>\nУслуг: {len(services)} · выбрано всего: {len(wizard["selected"])}\n\n'
             'Нажимайте на услуги, чтобы отметить. Формат: ID · название · цена за 1000.', _kb(*rows))


@route('at')
def r_auto_toggle(q: Any, index: str, page: str, service_id: str) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard or wizard.get('kind') != 'auto':
        _answer(q, 'Мастер устарел.', True)
        return
    wizard['selected'].symmetric_difference_update({service_id})
    r_auto_group(q, index, page)


@route('aall')
def r_auto_all(q: Any, index: str, value: str) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard or wizard.get('kind') != 'auto':
        return
    key, _ = wizard['groups'][_int(index)]
    ids = {str(x['service']) for x in _group_services(wizard, key)}
    if value == '1':
        wizard['selected'] |= ids
    else:
        wizard['selected'] -= ids
    r_auto_group(q, index, '0')


@route('afilt')
def r_auto_filter(q: Any, index: str) -> None:
    _ask(q, 'auto_filter', '🔍 Введите слова для фильтра по названию услуги (<code>-</code> — сбросить фильтр):',
         {'index': index}, back=f'ags:{index}:0')


@inp('auto_filter')
def i_auto_filter(message: Any, text: str, data: Dict[str, Any]) -> None:
    wizard = _wizards.get(message.from_user.id)
    if not wizard:
        raise ValueError('Мастер устарел.')
    wizard['filter'] = '' if text.strip() == '-' else text.strip()
    _clear(message)
    _show(message.chat.id, '✅ Фильтр применён.', _kb([B('📋 Открыть группу', f'ags:{data["index"]}:0')]))


@route('anext')
def r_auto_next(q: Any) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard or not wizard.get('selected'):
        _answer(q, 'Сначала отметьте услуги.', True)
        return
    _ask(q, 'auto_qty', f'📦 Выбрано услуг: {len(wizard["selected"])}.\n\nСколько единиц услуги выдавать за 1 шт. лота?\n'
                        '• <b>1</b> — обычная продажа: 1 шт. на Starvell = 1 подписчик/просмотр/лайк. Если у услуги есть минимум, '
                        'в лоте будет написано «от N шт.».\n• Больше 1 — лот продаётся пакетами.', back='ag:0')
    try:
        bot.edit_message_reply_markup(q.message.chat.id, q.message.message_id,
                                      reply_markup=_kb([B('1 шт. = 1 ед. (рекомендуется)', 'aq:1')],
                                                       [B('📏 Пакет = минимум услуги', 'aq:min')], [B('❌ Отмена', 'x:ag:0')]))
    except Exception:
        pass


@inp('auto_qty')
def i_auto_qty(message: Any, text: str, data: Dict[str, Any]) -> None:
    if _int(text) <= 0:
        raise ValueError('Введите число больше нуля.')
    wizard = _wizards.get(message.from_user.id)
    if not wizard:
        raise ValueError('Мастер устарел.')
    wizard['qty'] = _int(text)
    wizard.setdefault('adapt', False)
    _clear(message)
    _show(message.chat.id, *scr_auto_section(wizard))


@route('aq')
def r_auto_qty(q: Any, value: str) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard:
        return
    try:
        cardinal_instance.telegram.clear_state(q.message.chat.id, q.from_user.id, False)
    except Exception:
        pass
    wizard['qty'] = 0 if value == 'min' else max(1, _int(value, 1))
    wizard.setdefault('adapt', False)
    _edit(q, *scr_auto_section(wizard))


def _auto_selected(wizard: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [x for x in wizard['services'] if str(x['service']) in wizard['selected']]


def scr_auto_section(wizard: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    cfg = load_config()
    services = _auto_selected(wizard)
    sections = cfg['design']['sections']
    mapped = [x for x in services if sections.get(f'{x["_profile"]["platform"]}:{x["_profile"]["kind"]}') or sections.get(x['_profile']['platform'])]
    text = (f'🗂 <b>Раздел Starvell</b>\n\nУслуг: {len(services)}. По карте разделов найдено: {len(mapped)}.\n\n'
            '• <b>По карте разделов</b> — каждая услуга уйдёт в раздел своей соцсети (настраивается в «🎨 Оформление → 🗺 Карта разделов»).\n'
            '• <b>Один раздел</b> — все услуги в один раздел Starvell.')
    rows = []
    if mapped:
        rows.append([B(f'🗺 По карте разделов ({len(mapped)}/{len(services)})', 'asm')])
    rows.append([B('✏️ Один раздел для всех', 'asn')])
    rows.append([B('❌ Отмена', 'home')])
    return text, _kb(*rows)


@route('asm')
def r_auto_section_map(q: Any) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard:
        return
    wizard['node_mode'] = 'map'
    cfg = load_config()
    unmapped = [x for x in _auto_selected(wizard) if not (cfg['design']['sections'].get(f'{x["_profile"]["platform"]}:{x["_profile"]["kind"]}')
                                                         or cfg['design']['sections'].get(x['_profile']['platform']))]
    if unmapped:
        _ask(q, 'auto_node', f'⚠️ Для {len(unmapped)} услуг нет раздела в карте. Введите ID раздела Starvell для них '
                             'или <code>0</code>, чтобы пропустить их:', {'fallback': True}, back='home')
        return
    wizard['node'] = 0
    _edit(q, *scr_auto_markup(wizard))


@route('asn')
def r_auto_section_one(q: Any) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard:
        return
    wizard['node_mode'] = 'one'
    _ask(q, 'auto_node', '🗂 Введите ID раздела Starvell (число из ссылки <code>funpay.com/lots/ЧИСЛО/</code>):', back='home')


@inp('auto_node')
def i_auto_node(message: Any, text: str, data: Dict[str, Any]) -> None:
    wizard = _wizards.get(message.from_user.id)
    if not wizard:
        raise ValueError('Мастер устарел.')
    node = _int(text, -1)
    if node < 0 or (node == 0 and not data.get('fallback')):
        raise ValueError('Введите ID раздела — число больше нуля.')
    wizard['node'] = node
    _clear(message)
    _show(message.chat.id, *scr_auto_markup(wizard))


def scr_auto_markup(wizard: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    mode, value = _effective_markup(load_config(), None)
    return (f'📈 <b>Наценка</b>\n\nГлобальная: {_markup_text(mode, value)} (лоты будут следовать общим настройкам автоцены).\n'
            'Или задайте свой процент для этой выгрузки.',
            _kb([B(f'🌍 Глобальная ({_markup_text(mode, value)})', 'amk:g'), B('✏️ Свой процент', 'amk:c')], [B('❌ Отмена', 'home')]))


@route('amk')
def r_auto_markup(q: Any, mode: str) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard:
        return
    if mode == 'c':
        _ask(q, 'auto_markup', '✏️ Введите наценку в процентах (например <code>50</code>):', back='home')
        return
    wizard['markup'] = None
    _edit(q, *scr_auto_style(wizard))


@inp('auto_markup')
def i_auto_markup(message: Any, text: str, data: Dict[str, Any]) -> None:
    wizard = _wizards.get(message.from_user.id)
    if not wizard:
        raise ValueError('Мастер устарел.')
    wizard['markup'] = ('percent', _markup(text))
    _clear(message)
    _show(message.chat.id, *scr_auto_style(wizard))


def scr_auto_style(wizard: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    rows = _template_buttons('ath', load_config())
    rows.append([B('❌ Отмена', 'home')])
    return ('🎨 <b>Оформление</b>\n\n«🎯 По настройкам» — для каждой соцсети и типа услуги возьмётся назначенный шаблон или тема по умолчанию.',
            _kb(*rows))


@route('ath')
def r_auto_style(q: Any, token: str) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard:
        return
    wizard['choice'] = _token_choice(token)
    _edit(q, *scr_auto_review(wizard))


def _auto_plan(wizard: Dict[str, Any]) -> Dict[str, Any]:
    """Готовит план выгрузки: количество, раздел, пропуски и дубли."""
    cfg = load_config()
    existing = {(str(v['panel_id']), str(v['service_id']), v['quantity'], v.get('subcategory')) for v in cfg['lot_mapping'].values()}
    plan, skipped, duplicates = [], [], 0
    for service in _auto_selected(wizard):
        smin, smax = max(1, _int(service.get('min'), 1)), _int(service.get('max'))
        qty = wizard['qty'] or smin
        if qty < smin and wizard.get('adapt', False):
            qty = smin  # пакет: 1 шт. лота = минимум услуги
        if smax and qty > smax:
            skipped.append(f'{service["service"]}: максимум услуги {smax} < {qty}')
            continue
        profile = service['_profile']
        node = wizard['node']
        if wizard.get('node_mode') == 'map':
            node = _int(cfg['design']['sections'].get(f'{profile["platform"]}:{profile["kind"]}') or
                        cfg['design']['sections'].get(profile['platform'])) or wizard['node']
        if not node:
            skipped.append(f'{service["service"]}: нет раздела для {profile["platform"]}')
            continue
        if (str(wizard['panel']), str(service['service']), qty, node) in existing:
            duplicates += 1
            continue
        plan.append({'service': service, 'qty': qty, 'node': node})
    return {'plan': plan, 'skipped': skipped, 'duplicates': duplicates}


def scr_auto_review(wizard: Dict[str, Any]) -> Tuple[str, InlineKeyboardMarkup]:
    cfg = load_config()
    info = _auto_plan(wizard)
    wizard['plan'] = info['plan']
    kinds: Dict[str, int] = {}
    for item in info['plan']:
        profile = item['service']['_profile']
        label = f'{_platform_info(profile["platform"])["name"]} · {KINDS[profile["kind"]]["ru"].lower()}'
        kinds[label] = kinds.get(label, 0) + 1
    nodes = sorted({x['node'] for x in info['plan']})
    mode, value = wizard.get('markup') or _effective_markup(cfg, None)
    text = (f'🧾 <b>Проверьте выгрузку</b>\n\nБудет создано лотов: <b>{len(info["plan"])}</b>\n'
            f'Уже выставлены (пропуск дублей): {info["duplicates"]}\nПропущено: {len(info["skipped"])}\n'
            f'Разделы Starvell: {", ".join(map(str, nodes[:8])) or "—"}\n'
            f'За 1 шт.: {wizard["qty"] or "минимум каждой услуги"}'
            f' · {"пакет до минимума услуги" if wizard.get("adapt", False) else "без пакетов (покупатель берёт от минимума)"}\n'
            f'Наценка: {_markup_text(mode, value)} · Оформление: {escape(_template_name(cfg, wizard["choice"]))}\n\n'
            '<b>Типы услуг:</b>\n' + '\n'.join(f'• {escape(k)}: {v}' for k, v in sorted(kinds.items())[:20]))
    if info['skipped']:
        text += '\n\n<b>Пропуски:</b>\n' + '\n'.join('• ' + escape(x) for x in info['skipped'][:8])
    if wizard.get('selects'):
        text += f'\n\n🧩 Поля раздела заданы вручную: {len(wizard["selects"])}'
    rows = [[B(f'🚀 Создать {len(info["plan"])} лотов', 'ago')] if info['plan'] else None,
            [B('👁 Пример лота', 'asamp'), B(f'📏 Пакет по минимуму: {"ВКЛ" if wizard.get("adapt", False) else "ВЫКЛ"}', 'aadapt')],
            [B('🧩 Поля раздела', 'fl:auto')] if wizard.get('node_mode') == 'one' else None,
            [B('❌ Отмена', 'home')]]
    return text[:4000], _kb(*rows)


@route('aadapt')
def r_auto_adapt(q: Any) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard:
        return
    wizard['adapt'] = not wizard.get('adapt', False)
    _edit(q, *scr_auto_review(wizard))


@route('asamp')
def r_auto_sample(q: Any) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard or not wizard.get('plan'):
        _answer(q, 'Нет услуг для примера.', True)
        return
    _answer(q)
    cfg = load_config()
    item = wizard['plan'][_int(wizard.get('sample_index')) % len(wizard['plan'])]
    wizard['sample_index'] = _int(wizard.get('sample_index')) + 1
    try:
        cost, _, _ = _cost_rub(cfg, wizard['panel'], item['service'], item['qty'])
    except PanelError as exc:
        _show(q.message.chat.id, f'❌ {escape(str(exc))}')
        return
    mode, value = wizard.get('markup') or _effective_markup(cfg, None)
    price = _price_from_cost(cost, mode, value)
    texts = render_lot_texts(item['service'], item['qty'], price, wizard['choice'], {'summary_ru': 80, 'summary_en': 80,
                             'desc_ru': 5000, 'desc_en': 5000}, cfg, 'xxxxxxxx')
    _show(q.message.chat.id, f'👁 <b>Пример</b> (услуга {escape(str(item["service"]["service"]))})\n\n<b>{escape(texts["summary_ru"])}</b>\n\n'
                             f'<blockquote expandable>{escape(texts["desc_ru"][:2500])}</blockquote>\n\n💵 {_money(price)} ₽ · раздел {item["node"]}',
          _kb([B('👁 Следующий пример', 'asamp')]))


@route('ago')
def r_auto_go(q: Any) -> None:
    wizard = _wizards.get(q.from_user.id)
    if not wizard or not wizard.get('plan') or wizard.get('running'):
        _answer(q, 'Мастер устарел или выгрузка уже идёт.', True)
        return
    if not _account_ready():
        _answer(q, 'Аккаунт Starvell ещё не загружен.', True)
        return
    wizard['running'] = True
    _answer(q)
    batch_id = secrets.token_hex(3)
    job_state = {'stop': False}
    _jobs[batch_id] = job_state
    user_id = q.from_user.id

    def job(progress: Callable) -> str:
        cfg = load_config()
        plan = wizard['plan']
        created, failed, errors, warnings_total = 0, 0, [], 0
        forms: Dict[int, Dict[str, Any]] = {}
        stop_reason = ''
        processed = 0
        markup = wizard.get('markup')
        with config_tx() as tx:
            tx['batches'][batch_id] = {'created_at': time.time(), 'panel': wizard['panel'], 'count': 0}
        try:
            for index, item in enumerate(plan, 1):
                if job_state['stop']:
                    stop_reason = 'Остановлено вручную.'
                    break
                processed = index
                service, profile = item['service'], item['service']['_profile']
                try:
                    if item['node'] not in forms:
                        forms[item['node']] = _node_form(item['node'])
                    cost, _, _ = _cost_rub(cfg, wizard['panel'], service, item['qty'])
                    mode, value = markup or _effective_markup(cfg, None)
                    price = _price_from_cost(cost, mode, value)
                    tag = _new_tag(x.get('tag') for x in load_config()['lot_mapping'].values())
                    lot_id, name, warns = _create_lot(item['node'], wizard['panel'], service, item['qty'], price, wizard['choice'],
                                                      tag, form=forms[item['node']],
                                                      select_overrides=wizard.get('selects') if wizard.get('node_mode') == 'one' else None)
                    warnings_total += len(warns)
                    refill = None
                    if cfg['refill'].get('auto_enable') and (service.get('refill') in (True, 1, '1', 'true') or profile['guarantee_days']):
                        refill = {'enabled': True, 'hours': (profile['guarantee_days'] or 30) * 24}
                    _register_lot(name, lot_id, wizard['panel'], service['service'], item['qty'],
                                  list(_platform_info(profile['platform']).get('domains') or []), source='auto',
                                  subcategory=item['node'], batch_id=batch_id, tag=tag, platform=profile['platform'],
                                  kind=profile['kind'], template='' if wizard['choice'] == 'auto' else wizard['choice'],
                                  price=price, markup=markup, poll='poll' in str(service.get('type', '')).lower(),
                                  tag_written=bool(lot_id), refill=refill)
                    created += 1
                except Exception as exc:
                    failed += 1
                    reason = str(exc) if isinstance(exc, (PanelError, LotPublishError, RuntimeError)) else type(exc).__name__
                    errors.append(f'{service["service"]}: {_safe_detail(reason, cfg, 200)}')
                    if isinstance(exc, LotPublishError) and exc.stop_batch:
                        stop_reason = _safe_detail(reason, cfg, 300)
                        break
                progress(index, len(plan), f'✅ Создано: {created} · ❌ Ошибок: {failed}')
        finally:
            _jobs.pop(batch_id, None)
            if _wizards.get(user_id) is wizard:
                _wizards.pop(user_id, None)
            with config_tx() as tx:
                if created:
                    tx['batches'][batch_id]['count'] = created
                else:
                    tx['batches'].pop(batch_id, None)
        text = (f'{"⚠️ Выгрузка остановлена" if stop_reason else "✅ Выгрузка завершена"}\n\n'
                f'Создано: <b>{created}</b> · Ошибок: {failed} · Не обработано: {len(plan) - processed}'
                + (f'\nПолей выбрано по умолчанию: {warnings_total} (проверьте пару лотов)' if warnings_total else '')
                + (f'\nВыгрузка: <code>{batch_id}</code>' if created else ''))
        if stop_reason:
            text += '\n\n' + escape(stop_reason)
        if errors:
            text += '\n\n<b>Ошибки:</b>\n' + '\n'.join('• ' + escape(x) for x in errors[:15])
        return text

    _progress_job(q.message.chat.id, 'Создаю лоты на Starvell', job,
                  _kb([B('📦 Открыть выгрузку', f'bt:{batch_id}')], [B('◀️ Главное меню', 'home')]), stop_key=batch_id)


# ---------------------------------------------------------------------------
# Выгрузки (откат)
# ---------------------------------------------------------------------------

@route('bts')
def r_batches(q: Any) -> None:
    cfg = load_config()
    batches = sorted(cfg['batches'].items(), key=lambda x: -_float(x[1].get('created_at')))
    rows = []
    for batch_id, info in batches[:15]:
        count = sum(1 for x in cfg['lot_mapping'].values() if x.get('batch_id') == batch_id)
        when = datetime.fromtimestamp(_float(info.get('created_at'))).strftime('%d.%m %H:%M')
        rows.append([B(f'📦 {batch_id} · {when} · лотов {count}', f'bt:{batch_id}')])
    rows.append([B('◀️ Лоты', 'lots')])
    _edit(q, '📦 <b>Выгрузки</b>\n\nЗдесь можно выключить или удалить лоты одной выгрузки.' if batches else
          '📦 <b>Выгрузки</b>\n\nВыгрузок пока не было.', _kb(*rows))


@route('bt')
def r_batch(q: Any, batch_id: str) -> None:
    cfg = load_config()
    lots = [k for k, v in cfg['lot_mapping'].items() if v.get('batch_id') == batch_id]
    info = cfg['batches'].get(batch_id, {})
    when = datetime.fromtimestamp(_float(info.get('created_at'))).strftime('%d.%m.%Y %H:%M') if info else '—'
    _edit(q, f'📦 <b>Выгрузка {escape(batch_id)}</b>\nДата: {when}\nЛотов в плагине: {len(lots)}',
          _kb([B('📋 Лоты выгрузки', f'll:b_{batch_id}:0')],
              [B('🔴 Выключить все', f'bta:{batch_id}:0'), B('🟢 Включить все', f'bta:{batch_id}:1')],
              [B('🗑 Удалить выгрузку', f'btd:{batch_id}')], [B('◀️ Выгрузки', 'bts')]))


@route('bta')
def r_batch_active(q: Any, batch_id: str, value: str) -> None:
    lots = [v['funpay_lot_id'] for v in load_config()['lot_mapping'].values() if v.get('batch_id') == batch_id and v.get('funpay_lot_id')]
    _answer(q)
    def job(progress: Callable) -> str:
        changed, errors = _bulk_active(lots, value == '1', progress)
        return (f'{"🟢 Включено" if value == "1" else "🔴 Выключено"} лотов: {changed} из {len(lots)}' +
                (('\n\nОшибки:\n' + '\n'.join('• ' + escape(x) for x in errors[:10])) if errors else ''))
    _progress_job(q.message.chat.id, 'Меняю состояние лотов выгрузки', job, _back_kb(f'bt:{batch_id}', '◀️ К выгрузке'))


@route('btd')
def r_batch_delete(q: Any, batch_id: str) -> None:
    _edit(q, f'🗑 <b>Удалить выгрузку {escape(batch_id)}?</b>\n\nВыберите вариант:',
          _kb([B('🗑 Только из плагина', f'btdd:{batch_id}:0')], [B('🔥 Из плагина и с Starvell', f'btdd:{batch_id}:1')],
              [B('Отмена', f'bt:{batch_id}')]))


@route('btdd')
def r_batch_delete_do(q: Any, batch_id: str, remote: str) -> None:
    cfg = load_config()
    lots = [(k, v) for k, v in cfg['lot_mapping'].items() if v.get('batch_id') == batch_id]
    _answer(q)
    def job(progress: Callable) -> str:
        removed, errors = [], []
        for index, (key, lot) in enumerate(lots, 1):
            if remote == '1' and lot.get('funpay_lot_id'):
                try:
                    _delete_funpay_lot(lot['funpay_lot_id'])
                except Exception as exc:
                    errors.append(f'{lot["funpay_lot_id"]}: {_rejection_text(exc)}')
                    continue
            removed.append(key)
            if index % 5 == 0 or index == len(lots):
                progress(index, len(lots))
        with config_tx(allow_shrink=True) as tx:
            for key in removed:
                tx['lot_mapping'].pop(key, None)
            if len(removed) == len(lots):
                tx['batches'].pop(batch_id, None)
        return (f'🗑 Удалено лотов: {len(removed)} из {len(lots)}' + (' (и на Starvell)' if remote == '1' else '') +
                (('\n\nОшибки:\n' + '\n'.join('• ' + escape(x) for x in errors[:10])) if errors else ''))
    _progress_job(q.message.chat.id, 'Удаляю выгрузку', job, _back_kb('bts', '◀️ Выгрузки'))


# ---------------------------------------------------------------------------
# Поля раздела Starvell (выпадающие списки): ручной выбор вместо автоподбора
# ---------------------------------------------------------------------------

def _fields_wizard(user_id: int, mode: str) -> Optional[Dict[str, Any]]:
    wizard = _wizards.get(user_id)
    if not wizard or wizard.get('kind') != mode:
        return None
    return wizard


def _fields_form(wizard: Dict[str, Any]) -> Dict[str, Any]:
    if 'form' not in wizard:
        wizard['form'] = _node_form(wizard['node'])
    return wizard['form']


def scr_fields(wizard: Dict[str, Any], mode: str) -> Tuple[str, InlineKeyboardMarkup]:
    form = _fields_form(wizard)
    selects = [(name, options) for name, options in form['selects'].items() if name not in form['hidden']]
    wizard['_select_names'] = [name for name, _ in selects]
    chosen = wizard.setdefault('selects', {})
    rows = []
    for index, (name, options) in enumerate(selects):
        label = form['labels'].get(name, name)
        value = chosen.get(name)
        text = next((t for v, t in options if v == value), None) if value else None
        rows.append([B(f'{_short(label, 24)}: {_short(text, 20) if text else "🤖 авто"}', f'fs:{mode}:{index}:0')])
    rows.append([B('✅ Готово', 'ncpv' if mode == 'create' else 'arv')])
    return ('🧩 <b>Поля раздела Starvell</b>\n\n«🤖 авто» — бот подберёт значение по типу услуги (подписчики, просмотры, реакции…). '
            'Задайте значение вручную, если раздел требует другое.', _kb(*rows))


@route('fl')
def r_fields(q: Any, mode: str) -> None:
    wizard = _fields_wizard(q.from_user.id, mode)
    if not wizard or not wizard.get('node'):
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    try:
        _edit(q, *scr_fields(wizard, mode))
    except RuntimeError as exc:
        _answer(q, str(exc), True)


@route('fs')
def r_fields_select(q: Any, mode: str, index: str, page: str = '0') -> None:
    wizard = _fields_wizard(q.from_user.id, mode)
    if not wizard or _int(index) >= len(wizard.get('_select_names', [])):
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    name = wizard['_select_names'][_int(index)]
    options = [(v, t) for v, t in wizard['form']['selects'][name] if v]
    items, page_n, pages = _page(list(enumerate(options)), _int(page), 10)
    rows = [[B('🤖 Авто (по типу услуги)', f'fo:{mode}:{index}:-')]]
    rows += [[B(_short(text, 40), f'fo:{mode}:{index}:{i}')] for i, (_, text) in items]
    rows.append(_nav(f'fs:{mode}:{index}', page_n, pages))
    rows.append([B('◀️ Назад', f'fl:{mode}')])
    _edit(q, f'🧩 {escape(wizard["form"]["labels"].get(name, name))}: выберите значение', _kb(*rows))


@route('fo')
def r_fields_option(q: Any, mode: str, index: str, option: str) -> None:
    wizard = _fields_wizard(q.from_user.id, mode)
    if not wizard or _int(index) >= len(wizard.get('_select_names', [])):
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    name = wizard['_select_names'][_int(index)]
    options = [(v, t) for v, t in wizard['form']['selects'][name] if v]
    if option == '-':
        wizard.setdefault('selects', {}).pop(name, None)
    elif _int(option) < len(options):
        wizard.setdefault('selects', {})[name] = options[_int(option)][0]
    _edit(q, *scr_fields(wizard, mode))


@route('ncpv')
def r_create_preview_again(q: Any) -> None:
    _answer(q, '⏳ Готовлю предпросмотр…')
    _submit(_create_preview, q.message.chat.id, q.from_user.id)


@route('arv')
def r_auto_review_again(q: Any) -> None:
    wizard = _fields_wizard(q.from_user.id, 'auto')
    if not wizard:
        _answer(q, 'Мастер устарел — начните заново.', True)
        return
    _edit(q, *scr_auto_review(wizard))


# ---------------------------------------------------------------------------
# Текст для лота из услуги панели (без создания лота)
# ---------------------------------------------------------------------------

@route('ntx')
def r_text_start(q: Any) -> None:
    if not load_config()['services']:
        _answer(q, 'Сначала добавьте SMM-панель в разделе «🔌 Панели».', True)
        return
    _wizard(q.from_user.id, 'text', reset=True)
    _edit(q, '📋 <b>Текст для лота</b>\n\nБот подготовит название и описание под услугу панели. Выберите панель:',
          _kb(*_panel_rows('ntp', 'new')))


@route('ntp')
def r_text_panel(q: Any, panel_id: str) -> None:
    _wizard(q.from_user.id, 'text')['panel'] = panel_id
    _ask(q, 'text_service', '🆔 Введите ID услуги или часть названия для поиска:', back='new')


@inp('text_service')
def i_text_service(message: Any, text: str, data: Dict[str, Any]) -> None:
    _service_pick(message, text, 'text')


@inp('text_qty')
def i_text_qty(message: Any, text: str, data: Dict[str, Any]) -> None:
    if _int(text) <= 0:
        raise ValueError('Введите число больше нуля.')
    _clear(message)
    _text_show(message.chat.id, message.from_user.id, _int(text), 'auto')


@route('ntq')
def r_text_qty(q: Any, value: str) -> None:
    _answer(q)
    try:
        cardinal_instance.telegram.clear_state(q.message.chat.id, q.from_user.id, False)
    except Exception:
        pass
    _text_show(q.message.chat.id, q.from_user.id, _int(value, 1), 'auto')


@route('ntt')
def r_text_theme(q: Any, token: str) -> None:
    _answer(q)
    wizard = _wizard(q.from_user.id, 'text')
    _text_show(q.message.chat.id, q.from_user.id, _int(wizard.get('qty'), 1), _token_choice(token))


def _text_show(chat_id: int, user_id: int, quantity: int, choice: str) -> None:
    wizard = _wizard(user_id, 'text')
    service = wizard.get('service')
    if not service:
        _show(chat_id, '⌛ Мастер устарел — начните заново.', _back_kb('new'))
        return
    wizard['qty'] = max(1, quantity)
    cfg = load_config()
    try:
        cost, _, _ = _cost_rub(cfg, wizard['panel'], service, wizard['qty'])
        mode, value = _effective_markup(cfg, None)
        price = _price_from_cost(cost, mode, value)
        price_line = f'\n💵 Рекомендуемая цена: <b>{_money(price)} ₽</b> за 1 шт. (себестоимость {_money(cost)} ₽, наценка {_markup_text(mode, value)})'
    except PanelError as exc:
        price, price_line = 0.0, f'\n⚠️ Цену посчитать не удалось: {escape(str(exc))}'
    parts = _text_message(service, wizard['qty'], price, choice,
                          f'📋 <b>Текст для лота</b> — нажмите на блок, чтобы скопировать, и вставьте в редактор лота на {MARKET}.'
                          + price_line)
    _show(chat_id, parts[0])
    rows = _template_buttons('ntt', cfg)
    rows.append([B('🔗 Лот создан — привязать по ID', 'nb')])
    rows.append([B('◀️ Меню', 'home')])
    _show(chat_id, parts[1], _kb(*rows))


# ---------------------------------------------------------------------------
# Панели и прокси
# ---------------------------------------------------------------------------

def _balance_line(panel_id: str) -> str:
    info = (_state_get('balances', {}) or {}).get(panel_id)
    if not info:
        return 'неизвестно'
    age = int((time.time() - _float(info.get('ts'))) // 60)
    return f'{info["balance"]:g} {info["currency"]} ({age} мин назад)'


@route('panels')
def r_panels(q: Any) -> None:
    cfg = load_config()
    low = _state_get('balance_low', {}) or {}
    lines = ['🔌 <b>SMM-панели</b>', '']
    rows = []
    for pid, p in cfg['services'].items():
        lines.append(f'{"🟢" if p["enabled"] else "⚫"} <b>{escape(p["name"])}</b> — {escape(_display_url(p["api_url"]))}\n'
                     f'   💰 {escape(_balance_line(pid))} · 💸 мин. {p["min_balance"]:g} ₽'
                     + (' · 🔴 лоты выключены (низкий баланс)' if low.get(pid) else '') +
                     f'\n   🌐 {escape(_resolve_proxy(cfg, p)[1])}')
        rows.append([B(f'⚙️ {p["name"]}', f'pn:{pid}')])
    if not cfg['services']:
        lines.append('Панелей пока нет. Добавьте первую — понадобятся API URL и ключ из кабинета панели.')
    rows += [[B('➕ Добавить панель', 'padd'), B('🔄 Обновить балансы', 'pbal')],
             [B('🧪 Проверить все', 'ptall'), B('🌐 Общий прокси', 'px:g')], [B('◀️ Назад', 'home')]]
    _edit(q, '\n'.join(lines)[:4000], _kb(*rows))


@route('pn')
def r_panel(q: Any, pid: str) -> None:
    cfg = load_config()
    p = cfg['services'].get(pid)
    if not p:
        _edit(q, '❌ Панель не найдена.', _back_kb('panels'))
        return
    lots = sum(1 for x in cfg['lot_mapping'].values() if x['panel_id'] == pid)
    text = (f'🔌 <b>{escape(p["name"])}</b> (#{pid})\nURL: <code>{escape(_display_url(p["api_url"]))}</code>\n'
            f'Ключ: {"задан (скрыт)" if p["api_key"] else "❗ не задан"}\nМетод: {p["request_method"]} · Скидка: {p["discount"]:g}%\n'
            f'💰 Баланс: {escape(_balance_line(pid))}\n💸 Мин. баланс: {p["min_balance"]:g} ₽ {"(выкл)" if not p["min_balance"] else ""}\n'
            f'🌐 Прокси: {escape(_resolve_proxy(cfg, p)[1])}\n🛍 Лотов: {lots}')
    kb = _kb([B('🧪 Проверить', f'pt:{pid}'), B(f'Метод: {p["request_method"]}', f'pm:{pid}')],
             [B('✏️ URL', f'pe:url:{pid}'), B('🔑 Ключ', f'pe:key:{pid}')],
             [B('✏️ Название', f'pe:name:{pid}'), B('🎁 Скидка', f'pe:disc:{pid}')],
             [B('💸 Мин. баланс', f'pe:minb:{pid}'), B('🌐 Прокси', f'px:{pid}')],
             [B('⚫ Выключить' if p['enabled'] else '🟢 Включить', f'ptg:{pid}'), B('🗑 Удалить', f'pdel:{pid}')],
             [B('📋 Лоты панели', f'll:p_{pid}:0'), B('◀️ Панели', 'panels')])
    _edit(q, text, kb)


@route('pm', 'ptg')
def r_panel_toggle(q: Any, pid: str) -> None:
    with config_tx() as tx:
        p = tx['services'].get(pid)
        if not p:
            return
        if q.data.startswith('pm'):
            p['request_method'] = 'GET' if p['request_method'] == 'POST' else 'POST'
        else:
            p['enabled'] = not p['enabled']
    _service_cache.pop(pid, None)
    r_panel(q, pid)


@route('pdel')
def r_panel_delete(q: Any, pid: str) -> None:
    cfg = load_config()
    lots = sum(1 for x in cfg['lot_mapping'].values() if x['panel_id'] == pid)
    if lots:
        _answer(q, f'К панели привязано лотов: {lots}. Сначала перепривяжите или удалите их.', True)
        return
    _edit(q, f'🗑 Удалить панель «{escape(cfg["services"].get(pid, {}).get("name", pid))}»?',
          _kb([B('✅ Да, удалить', f'pdd:{pid}'), B('❌ Отмена', f'pn:{pid}')]))


@route('pdd')
def r_panel_delete_do(q: Any, pid: str) -> None:
    with config_tx(allow_shrink=True) as tx:
        if any(x['panel_id'] == pid for x in tx['lot_mapping'].values()):
            raise ValueError('К панели привязаны лоты.')
        tx['services'].pop(pid, None)
    _service_cache.pop(pid, None)
    _answer(q, '✅ Панель удалена.')
    r_panels(q)


PANEL_PROMPTS = {'url': '✏️ Введите API URL панели, например <code>https://example.com/api/v2</code>\n'
                        '(без <code>?action=</code> и ключа — их бот добавит сам):',
                 'key': '🔑 Отправьте API-ключ. Сообщение с ключом бот сразу удалит из чата.',
                 'name': '✏️ Введите название панели:', 'disc': '🎁 Скидка панели в процентах (от -100 до 100):',
                 'minb': '💸 Порог низкого баланса в рублях. Ниже порога бот выключит лоты этой панели, после пополнения — включит. '
                         '<code>0</code> — выключить защиту:'}


@route('pe')
def r_panel_edit(q: Any, field: str, pid: str) -> None:
    _ask(q, 'panel_edit', PANEL_PROMPTS[field], {'field': field, 'pid': pid}, back=f'pn:{pid}')


@inp('panel_edit')
def i_panel_edit(message: Any, text: str, data: Dict[str, Any]) -> None:
    field, pid = data['field'], data['pid']
    note = ''
    if field == 'key':
        try:
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            note = '\n⚠️ Не удалось удалить сообщение с ключом — удалите его вручную.'
    with config_tx() as tx:
        p = tx['services'].get(pid)
        if not p:
            raise ValueError('Панель уже удалена.')
        if field == 'url':
            p['api_url'] = _api_url(text)
            note = ('\n' + _api_url_warning(p['api_url'])) if _api_url_warning(p['api_url']) else ''
        elif field == 'key':
            if len(text.strip()) < 3:
                raise ValueError('Ключ выглядит слишком коротким.')
            p['api_key'] = text.strip()
        elif field == 'name':
            if not text.strip():
                raise ValueError('Название не может быть пустым.')
            p['name'] = text.strip()[:48]
        elif field == 'disc':
            value = _float(text, float('nan'))
            if not math.isfinite(value) or not -100 <= value <= 100:
                raise ValueError('Скидка — число от -100 до 100.')
            p['discount'] = value
        elif field == 'minb':
            value = _float(text, float('nan'))
            if not math.isfinite(value) or value < 0:
                raise ValueError('Введите неотрицательное число.')
            p['min_balance'] = value
    _service_cache.pop(pid, None)
    _clear(message)
    _show(message.chat.id, '✅ Сохранено.' + escape(note), _back_kb(f'pn:{pid}', '◀️ К панели'))


@route('padd')
def r_panel_add(q: Any) -> None:
    _wizard(q.from_user.id, 'panel', reset=True)
    _ask(q, 'panel_url', '➕ <b>Новая панель</b>\n\nШаг 1/3. Введите API URL, например <code>https://example.com/api/v2</code>:', back='panels')


@inp('panel_url')
def i_panel_url(message: Any, text: str, data: Dict[str, Any]) -> None:
    wizard = _wizard(message.from_user.id, 'panel')
    wizard['url'] = _api_url(text)
    _clear(message)
    warning = _api_url_warning(wizard['url'])
    _ask(message, 'panel_key', (escape(warning) + '\n\n' if warning else '') +
         'Шаг 2/3. Отправьте API-ключ (из профиля или раздела API на сайте панели). Сообщение с ключом бот удалит.', back='panels')


@inp('panel_key')
def i_panel_key(message: Any, text: str, data: Dict[str, Any]) -> None:
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    if len(text.strip()) < 3:
        raise ValueError('Ключ выглядит слишком коротким.')
    _wizard(message.from_user.id, 'panel')['key'] = text.strip()
    _clear(message)
    _ask(message, 'panel_name', 'Шаг 3/3. Введите название панели (например, SoxyLiker):', back='panels')


@inp('panel_name')
def i_panel_name(message: Any, text: str, data: Dict[str, Any]) -> None:
    wizard = _wizard(message.from_user.id, 'panel')
    if not wizard.get('url') or not wizard.get('key'):
        raise ValueError('Мастер устарел — начните заново.')
    with config_tx() as tx:
        pid = str(max([_int(x) for x in tx['services']] + [0]) + 1)
        tx['services'][pid] = _panel(pid, {'name': text.strip() or f'Панель #{pid}', 'api_url': wizard['url'], 'api_key': wizard['key']})
    _wizards.pop(message.from_user.id, None)
    _clear(message)
    _show(message.chat.id, '✅ Панель добавлена. ⏳ Проверяю подключение…')
    _submit(lambda: _show(message.chat.id, _panel_diagnostics(load_config(), pid), _back_kb(f'pn:{pid}', '◀️ К панели')))


def _panel_diagnostics(cfg: Dict[str, Any], panel_id: str) -> str:
    panel = cfg['services'].get(panel_id, {})
    name = escape(str(panel.get('name') or panel_id))
    if not panel.get('enabled', True):
        return f'⚫ {name}: панель выключена.'
    try:
        api = PanelAPI(cfg, panel_id)
        services = api.services(True)
    except PanelError as exc:
        return f'❌ {name}: {escape(str(exc))}'
    try:
        balance = api.balance()
    except PanelError as exc:
        return f'⚠️ {name}: услуг {len(services)}, баланс: {escape(str(exc))}'
    return f'✅ {name}: {len(services)} услуг, баланс {escape(str(balance.get("balance")))} {escape(balance["currency"])}'


@route('pt', 'ptall', 'pbal')
def r_panel_test(q: Any, pid: str = '') -> None:
    cfg = load_config()
    pids = [pid] if pid else list(cfg['services'])
    _answer(q, '⏳ Проверяю…')
    def job() -> None:
        lines = ['🧪 <b>Проверка панелей</b>', ''] + [_panel_diagnostics(cfg, x) for x in pids]
        _show(q.message.chat.id, '\n\n'.join(lines)[:4000], _back_kb(f'pn:{pid}' if pid else 'panels', '◀️ Назад'))
    _submit(job)


def _proxy_target(cfg: Dict[str, Any], target: str) -> Dict[str, Any]:
    return cfg['proxy'] if target == 'g' else cfg['services'][target]['proxy']


PROXY_MODES_GLOBAL = ['bot', 'custom', 'direct']
PROXY_MODES_PANEL = ['plugin', 'bot', 'custom', 'direct']
PROXY_LABELS = {'plugin': 'как у плагина', 'bot': 'прокси бота', 'custom': 'свой прокси', 'direct': 'напрямую'}


@route('px')
def r_proxy(q: Any, target: str) -> None:
    cfg = load_config()
    if target != 'g' and target not in cfg['services']:
        _edit(q, '❌ Панель не найдена.', _back_kb('panels'))
        return
    proxy = _proxy_target(cfg, target)
    title = 'Общий прокси плагина' if target == 'g' else f'Прокси панели «{cfg["services"][target]["name"]}»'
    current = _resolve_proxy(cfg, None if target == 'g' else cfg['services'][target])[1]
    text = (f'🌐 <b>{escape(title)}</b>\n\nРежим: <b>{PROXY_LABELS[proxy["mode"]]}</b>\nПрокси бота: {escape(_mask_proxy(_bot_proxy() or ""))}\n'
            f'Свой прокси: {escape(_mask_proxy(proxy["url"]))}\nСейчас используется: <b>{escape(current)}</b>\n\n'
            'Касается только запросов к SMM-панелям и курса валют. Starvell всегда работает через прокси бота.\n'
            'Форматы: <code>логин:пароль@ip:порт</code>, <code>ip:порт</code>, <code>socks5://логин:пароль@ip:порт</code>.')
    rows = [[B(f'Режим: {PROXY_LABELS[proxy["mode"]]}', f'pxm:{target}')],
            [B('✍️ Свой прокси: ' + ('задан' if proxy['url'] else 'не задан'), f'pxs:{target}')]]
    if proxy['url']:
        rows.append([B('🗑 Удалить свой прокси', f'pxd:{target}')])
    rows.append([B('🧪 Проверить прокси', f'pxt:{target}')])
    rows.append([B('◀️ Назад', 'panels' if target == 'g' else f'pn:{target}')])
    _edit(q, text, _kb(*rows))


@route('pxm')
def r_proxy_mode(q: Any, target: str) -> None:
    with config_tx() as tx:
        proxy = _proxy_target(tx, target)
        modes = PROXY_MODES_GLOBAL if target == 'g' else PROXY_MODES_PANEL
        nxt = modes[(modes.index(proxy['mode']) + 1) % len(modes)] if proxy['mode'] in modes else modes[0]
        if nxt == 'custom' and not proxy['url']:
            nxt = modes[(modes.index('custom') + 1) % len(modes)]
            _answer(q, 'Режим «свой прокси» недоступен, пока прокси не задан.', True)
        proxy['mode'] = nxt
    _service_cache.clear()
    r_proxy(q, target)


@route('pxd')
def r_proxy_delete(q: Any, target: str) -> None:
    with config_tx() as tx:
        proxy = _proxy_target(tx, target)
        proxy['url'] = ''
        proxy['mode'] = 'bot' if target == 'g' else 'plugin'
    _service_cache.clear()
    r_proxy(q, target)


@route('pxs')
def r_proxy_set(q: Any, target: str) -> None:
    _ask(q, 'proxy_set', '✍️ Отправьте прокси одним сообщением (<code>логин:пароль@ip:порт</code> или <code>ip:порт</code>). '
                         'Сообщение будет удалено из чата.', {'target': target}, back=f'px:{target}')


@inp('proxy_set')
def i_proxy_set(message: Any, text: str, data: Dict[str, Any]) -> None:
    value = _normalize_proxy(text)
    try:
        bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    with config_tx() as tx:
        proxy = _proxy_target(tx, data['target'])
        proxy['url'], proxy['mode'] = value, 'custom'
    _service_cache.clear()
    _clear(message)
    _show(message.chat.id, '✅ Прокси сохранён и включён. ⏳ Проверяю…')
    _submit(lambda: _show(message.chat.id, escape(_test_proxy(value)), _back_kb(f'px:{data["target"]}', '◀️ К прокси')))


@route('pxt')
def r_proxy_test(q: Any, target: str) -> None:
    cfg = load_config()
    proxy, label = _resolve_proxy(cfg, None if target == 'g' else cfg['services'].get(target))
    _answer(q, '⏳ Проверяю…')
    _submit(lambda: _show(q.message.chat.id, f'🧪 {escape(label)}\n{escape(_test_proxy(proxy))}', _back_kb(f'px:{target}', '◀️ К прокси')))


# ---------------------------------------------------------------------------
# Цены
# ---------------------------------------------------------------------------

@route('pr')
def r_pricing(q: Any) -> None:
    cfg = load_config()
    p = cfg['pricing']
    lots = cfg['lot_mapping'].values()
    active = sum(1 for x in lots if x['autoprice'] and x.get('funpay_lot_id'))
    text = (f'💰 <b>Ценообразование</b>\n\nАвтопересчёт: {"✅ ВКЛ" if p["enabled"] else "❌ ВЫКЛ"}\n'
            f'Наценка: {"💵 фикс " + format(p["fixed"], "g") + " ₽" if p["mode"] == "fixed" else "➗ " + format(p["percent"], "g") + " %"}\n'
            f'Интервал: {p["interval"]} мин · мин. изменение: {p["min_change"]:g}%\nКурс USD: {escape(_usd_rate_text(cfg))}\n'
            f'Лотов с автоценой: {active} из {len(cfg["lot_mapping"])}\n\n'
            'Цена = себестоимость × (1 + %) или себестоимость + фикс. Цены от 1 ₽ округляются вверх до копеек, '
            'дешёвые — до 6 знаков. Учитывайте комиссию Starvell в наценке.')
    kb = _kb([B(f'Автопересчёт: {"ВКЛ" if p["enabled"] else "ВЫКЛ"}', 'prt:en'), B(f'Режим: {"💵 фикс" if p["mode"] == "fixed" else "➗ %"}', 'prt:mode')],
             [B(f'✏️ Процент ({p["percent"]:g}%)', 'pre:percent'), B(f'✏️ Фикс ({p["fixed"]:g} ₽)', 'pre:fixed')],
             [B(f'⏱ Интервал ({p["interval"]} мин)', 'pre:interval'), B(f'📉 Мин. изменение ({p["min_change"]:g}%)', 'pre:minch')],
             [B(f'💱 Курс USD: {"ручной" if p["usd_mode"] == "manual" else "авто"}', 'prt:usd'), B(f'✏️ Курс ({p["usd_rate"]:g} ₽)', 'pre:usd')],
             [B(f'{"🔔" if p["notify"] else "🔕"} Уведомления', 'prt:notify'), B('🔄 Пересчитать сейчас', 'prnow')],
             [B('◀️ Назад', 'home')])
    _edit(q, text, kb)


@route('prt')
def r_pricing_toggle(q: Any, what: str) -> None:
    with config_tx() as tx:
        p = tx['pricing']
        if what == 'en':
            p['enabled'] = not p['enabled']
        elif what == 'mode':
            p['mode'] = 'percent' if p['mode'] == 'fixed' else 'fixed'
        elif what == 'usd':
            p['usd_mode'] = 'auto' if p['usd_mode'] == 'manual' else 'manual'
        elif what == 'notify':
            p['notify'] = not p['notify']
    r_pricing(q)


PRICING_PROMPTS = {'percent': ('✏️ Наценка в процентах (не меньше 0):', 'percent'), 'fixed': ('✏️ Фиксированная наценка в рублях:', 'fixed'),
                   'interval': ('⏱ Интервал пересчёта в минутах (минимум 15):', 'interval'),
                   'minch': ('📉 Минимальное изменение цены в процентах, при котором цена обновляется на Starvell:', 'min_change'),
                   'usd': ('✏️ Курс USD → RUB для ручного режима:', 'usd_rate')}


@route('pre')
def r_pricing_edit(q: Any, what: str) -> None:
    _ask(q, 'pricing_edit', PRICING_PROMPTS[what][0], {'what': what}, back='pr')


@inp('pricing_edit')
def i_pricing_edit(message: Any, text: str, data: Dict[str, Any]) -> None:
    value = _float(text, float('nan'))
    if not math.isfinite(value) or value < 0:
        raise ValueError('Нужно неотрицательное число.')
    key = PRICING_PROMPTS[data['what']][1]
    if key == 'interval':
        value = max(15, int(value))
    if key == 'usd_rate' and value <= 0:
        raise ValueError('Курс должен быть больше нуля.')
    if key == 'percent' and value > 10000:
        raise ValueError('Слишком большая наценка.')
    with config_tx() as tx:
        tx['pricing'][key] = value
    _clear(message)
    _show(message.chat.id, '✅ Сохранено.', _back_kb('pr', '◀️ К ценообразованию'))


@route('prnow')
def r_pricing_now(q: Any) -> None:
    if not _account_ready():
        _answer(q, 'Аккаунт Starvell ещё не загружен.', True)
        return
    _answer(q)
    def job(progress: Callable) -> str:
        report = _pricing_pass(force=True, manual=True)
        return escape(_pricing_report_text(report))
    _progress_job(q.message.chat.id, 'Пересчитываю цены', job, _back_kb('pr', '◀️ К ценообразованию'))


# ---------------------------------------------------------------------------
# Оформление
# ---------------------------------------------------------------------------

@route('ds')
def r_design(q: Any) -> None:
    cfg = load_config()
    d = cfg['design']
    text = (f'🎨 <b>Оформление лотов</b>\n\nТема по умолчанию: <b>{escape(_template_name(cfg, d["theme"]))}</b>\n'
            f'Свои шаблоны: {len(d["custom"])} · Назначений: {len(d["assign"])} · Разделов в карте: {len(d["sections"])}\n'
            f'Английская версия: {"полноценная" if d.get("english", True) else "транслит"}\n'
            f'Название лота: {"тип + соцсеть + вариант услуги" if d.get("title_source") != "panel" else "название услуги из панели"}\n\n'
            'Порядок выбора оформления: шаблон лота → назначение «соцсеть + тип» → «соцсеть» → «тип» → тема по умолчанию.')
    kb = _kb([B('🎨 Тема по умолчанию', 'dsth'), B('👁 Предпросмотр', 'dsp')],
             [B('🧩 Свои шаблоны', 'dsc'), B('📌 Назначения', 'dsa')],
             [B('🗺 Карта разделов Starvell', 'dsm'), B('🔄 Переоформить лоты', 'dsr')] if CAN_CREATE_LOTS else [B('📋 Текст для лота', 'ntx')],
             [B(f'🏷 Название: {"тип + вариант" if d.get("title_source") != "panel" else "как в панели"}', 'dstn')],
             [B(f'🇬🇧 EN: {"перевод" if d.get("english", True) else "транслит"}', 'dse'), B('◀️ Назад', 'home')])
    _edit(q, text, kb)


@route('dstn')
def r_design_title_source(q: Any) -> None:
    with config_tx() as tx:
        tx['design']['title_source'] = 'panel' if tx['design'].get('title_source') != 'panel' else 'smart'
    _answer(q, 'Новое название применится к новым лотам. Уже выставленные — через «🔄 Переоформить лоты».', True)
    r_design(q)


@route('dse')
def r_design_english(q: Any) -> None:
    with config_tx() as tx:
        tx['design']['english'] = not tx['design'].get('english', True)
    r_design(q)


@route('dsth')
def r_design_theme(q: Any) -> None:
    cfg = load_config()
    rows = [[B(('✅ ' if cfg['design']['theme'] == key else '') + THEMES[key]['title'], f'dss:{key}'), B('👁', f'dsv:{key}')]
            for key in THEME_ORDER]
    rows += [[B(('✅ ' if cfg['design']['theme'] == 'custom:' + k else '') + '🧩 ' + _short(v.get('name', k), 24), f'dss:c-{k}'),
              B('👁', f'dsv:c-{k}')] for k, v in cfg['design']['custom'].items()]
    rows.append([B('◀️ Назад', 'ds')])
    _edit(q, '🎨 <b>Тема по умолчанию</b>\n\nНажмите на тему, чтобы выбрать, или 👁 — чтобы посмотреть пример.', _kb(*rows))


@route('dss')
def r_design_theme_set(q: Any, token: str) -> None:
    with config_tx() as tx:
        tx['design']['theme'] = _token_choice(token)
    _answer(q, '✅ Тема по умолчанию изменена. Уже выставленные лоты можно переоформить в «🔄 Переоформить лоты».', True)
    r_design_theme(q)


def _preview_text(choice: str, platform: str = 'telegram', kind: str = 'followers') -> str:
    cfg = load_config()
    service = _preview_sample(platform, kind)
    texts = render_lot_texts(service, 100, 49.9, choice, {'summary_ru': 80, 'summary_en': 80, 'desc_ru': 5000, 'desc_en': 5000},
                             cfg, 'a1b2c3d4')
    return (f'👁 <b>{escape(_template_name(cfg, texts["template"]))}</b> · {escape(_platform_info(platform)["name"])} · '
            f'{escape(KINDS[kind]["ru"].lower())}\n\n<b>{escape(texts["summary_ru"])}</b>\n\n'
            f'<blockquote expandable>{escape(texts["desc_ru"][:2800])}</blockquote>\n\n🇬🇧 <b>{escape(texts["summary_en"])}</b>')


@route('dsv')
def r_design_view(q: Any, token: str) -> None:
    _answer(q)
    _show(q.message.chat.id, _preview_text(_token_choice(token)), _kb([B('✅ Сделать темой по умолчанию', f'dss:{token}')]))


@route('dsp')
def r_design_preview(q: Any, platform: str = '', kind: str = '') -> None:
    if not platform:
        rows = [[B(f'{PLATFORMS[k]["emoji"]} {PLATFORMS[k]["name"]}', f'dsp:{k}') for k in PLATFORM_ORDER[i:i + 3]]
                for i in range(0, len(PLATFORM_ORDER), 3)]
        rows.append([B('◀️ Назад', 'ds')])
        _edit(q, '👁 Выберите соцсеть для примера:', _kb(*rows))
        return
    if not kind:
        rows = [[B(f'{KINDS[k]["emoji"]} {KINDS[k]["ru"].capitalize()}', f'dsp:{platform}:{k}') for k in KIND_ORDER[i:i + 2]]
                for i in range(0, len(KIND_ORDER), 2)]
        rows.append([B('◀️ Назад', 'dsp')])
        _edit(q, '👁 Выберите тип услуги:', _kb(*rows))
        return
    _answer(q)
    cfg = load_config()
    choice = _template_choice(cfg, {'platform': platform, 'kind': kind}, 'auto')
    _show(q.message.chat.id, _preview_text(choice, platform, kind),
          _kb(*_template_buttons(f'dspt:{platform}:{kind}', cfg, include_auto=False)))


@route('dspt')
def r_design_preview_theme(q: Any, platform: str, kind: str, token: str) -> None:
    _answer(q)
    _edit(q, _preview_text(_token_choice(token), platform, kind),
          _kb(*_template_buttons(f'dspt:{platform}:{kind}', load_config(), include_auto=False)))


@route('dsc')
def r_design_custom(q: Any) -> None:
    cfg = load_config()
    rows = [[B('🧩 ' + _short(v.get('name', k), 40), f'dsci:{k}')] for k, v in cfg['design']['custom'].items()]
    rows.append([B('➕ Создать шаблон', 'dscn'), B('📥 Импорт', 'dscim')])
    rows.append([B('◀️ Назад', 'ds')])
    _edit(q, '🧩 <b>Свои шаблоны</b>\n\nШаблон — это ваш текст названия и описания с переменными. '
             'Подходит, если хочется полностью своё оформление.\n\nПеременные:\n<code>' + escape(CUSTOM_TEMPLATE_VARS) + '</code>', _kb(*rows))


@route('dscn')
def r_design_custom_new(q: Any) -> None:
    _ask(q, 'custom_new', '➕ Введите название нового шаблона (для себя):', back='dsc')


@inp('custom_new')
def i_custom_new(message: Any, text: str, data: Dict[str, Any]) -> None:
    if not text.strip():
        raise ValueError('Название не может быть пустым.')
    template_id = secrets.token_hex(3)
    with config_tx() as tx:
        tx['design']['custom'][template_id] = {
            'name': text.strip()[:40],
            'summary_ru': '⭐ {title} ⭐ {badges}',
            'desc_ru': ('━━━━━━━━━━━━━━━━━━━━\n{emoji} {title}\n━━━━━━━━━━━━━━━━━━━━\n\n⚡ Автовыдача 24/7\n\n'
                        '📦 1 шт. = {per_piece} {unit}\n🧾 Услуга: {clean_name}\n\n📝 Как заказать:\n'
                        '1️⃣ Оплатите от {min_pcs} до {max_pcs} шт.\n2️⃣ Отправьте {link_hint}: {example_link}\n'
                        '3️⃣ Подтвердите запуск «+»\n\n⚠️ Важно:\n{requirements}'),
            'summary_en': '', 'desc_en': ''}
    _clear(message)
    _show(message.chat.id, '✅ Шаблон создан с примером текста — отредактируйте его.', _kb([B('🧩 Открыть шаблон', f'dsci:{template_id}')]))


CUSTOM_FIELDS = {'sr': ('summary_ru', 'Название (RU)'), 'dr': ('desc_ru', 'Описание (RU)'),
                 'se': ('summary_en', 'Название (EN)'), 'de': ('desc_en', 'Описание (EN)')}


@route('dsci')
def r_design_custom_item(q: Any, template_id: str) -> None:
    cfg = load_config()
    item = cfg['design']['custom'].get(template_id)
    if not item:
        _edit(q, '❌ Шаблон не найден.', _back_kb('dsc'))
        return
    text = (f'🧩 <b>{escape(item["name"])}</b>\n\n<b>Название RU:</b>\n<code>{escape(item.get("summary_ru") or "—")}</code>\n\n'
            f'<b>Описание RU:</b>\n<blockquote expandable>{escape((item.get("desc_ru") or "—")[:1500])}</blockquote>\n\n'
            f'<b>EN:</b> {"свои тексты" if item.get("summary_en") or item.get("desc_en") else "автоматический перевод темы"}')
    kb = _kb([B('✏️ Название RU', f'dsce:sr:{template_id}'), B('✏️ Описание RU', f'dsce:dr:{template_id}')],
             [B('✏️ Название EN', f'dsce:se:{template_id}'), B('✏️ Описание EN', f'dsce:de:{template_id}')],
             [B('👁 Предпросмотр', f'dsv:c-{template_id}'), B('📤 Экспорт', f'dscx:{template_id}')],
             [B('🗑 Удалить', f'dscd:{template_id}'), B('◀️ Шаблоны', 'dsc')])
    _edit(q, text[:4000], kb)


@route('dsce')
def r_design_custom_edit(q: Any, field: str, template_id: str) -> None:
    _ask(q, 'custom_edit', f'✏️ Отправьте новый текст: {CUSTOM_FIELDS[field][1]}.\nПеременные: <code>{escape(CUSTOM_TEMPLATE_VARS)}</code>\n'
                           '<code>-</code> — очистить (для EN — использовать автоматический перевод).',
         {'field': field, 'id': template_id}, back=f'dsci:{template_id}')


@inp('custom_edit')
def i_custom_edit(message: Any, text: str, data: Dict[str, Any]) -> None:
    field = CUSTOM_FIELDS[data['field']][0]
    with config_tx() as tx:
        item = tx['design']['custom'].get(data['id'])
        if not item:
            raise ValueError('Шаблон удалён.')
        value = '' if text.strip() == '-' else text
        if field == 'summary_ru' and not value.strip():
            raise ValueError('Название RU обязательно.')
        item[field] = value[:5000]
    _clear(message)
    _show(message.chat.id, '✅ Сохранено.', _kb([B('🧩 К шаблону', f'dsci:{data["id"]}'), B('👁 Предпросмотр', f'dsv:c-{data["id"]}')]))


@route('dscd')
def r_design_custom_delete(q: Any, template_id: str) -> None:
    with config_tx() as tx:
        tx['design']['custom'].pop(template_id, None)
        choice = 'custom:' + template_id
        if tx['design']['theme'] == choice:
            tx['design']['theme'] = 'premium'
        tx['design']['assign'] = {k: v for k, v in tx['design']['assign'].items() if v != choice}
        for lot in tx['lot_mapping'].values():
            if lot.get('template') == choice:
                lot['template'] = ''
    _answer(q, '🗑 Шаблон удалён.')
    r_design_custom(q)


@route('dscx')
def r_design_custom_export(q: Any, template_id: str) -> None:
    item = load_config()['design']['custom'].get(template_id)
    _answer(q)
    if item:
        _show(q.message.chat.id, '📤 Скопируйте JSON шаблона:\n<code>' + escape(json.dumps(item, ensure_ascii=False)) + '</code>')


@route('dscim')
def r_design_custom_import(q: Any) -> None:
    _ask(q, 'custom_import', '📥 Отправьте JSON шаблона (из «📤 Экспорт»):', back='dsc')


@inp('custom_import')
def i_custom_import(message: Any, text: str, data: Dict[str, Any]) -> None:
    try:
        item = json.loads(text)
    except ValueError:
        raise ValueError('Это не JSON.') from None
    if not isinstance(item, dict) or not str(item.get('summary_ru') or '').strip():
        raise ValueError('В шаблоне нет поля summary_ru.')
    template_id = secrets.token_hex(3)
    with config_tx() as tx:
        tx['design']['custom'][template_id] = {'name': str(item.get('name') or 'Импорт')[:40],
                                               **{k: str(item.get(k) or '')[:5000] for k in ('summary_ru', 'desc_ru', 'summary_en', 'desc_en')}}
    _clear(message)
    _show(message.chat.id, '✅ Шаблон импортирован.', _kb([B('🧩 Открыть', f'dsci:{template_id}')]))


@route('dsa')
def r_design_assign(q: Any) -> None:
    cfg = load_config()
    items = list(cfg['design']['assign'].items())
    rows = []
    for index, (key, choice) in enumerate(items[:30]):
        platform, kind = key.split(':')
        label = (f'{"все соцсети" if platform == "*" else _platform_info(platform)["name"]} · '
                 f'{"все типы" if kind == "*" else KINDS.get(kind, KINDS["generic"])["ru"].lower()} → {_template_name(cfg, choice)}')
        rows.append([B('❌ ' + _short(label, 55), f'dsad:{index}')])
    rows.append([B('➕ Назначить', 'dsan'), B('◀️ Назад', 'ds')])
    _edit(q, '📌 <b>Назначения шаблонов</b>\n\nНапример: «Telegram · реакции → 🔥 Яркий». Нажмите ❌, чтобы удалить назначение.', _kb(*rows))


@route('dsad')
def r_design_assign_delete(q: Any, index: str) -> None:
    with config_tx() as tx:
        keys = list(tx['design']['assign'])
        if _int(index) < len(keys):
            tx['design']['assign'].pop(keys[_int(index)], None)
    r_design_assign(q)


@route('dsan')
def r_design_assign_new(q: Any, platform: str = '', kind: str = '') -> None:
    if not platform:
        keys = ['*'] + PLATFORM_ORDER
        rows = [[B('🌍 Все' if k == '*' else f'{PLATFORMS[k]["emoji"]} {PLATFORMS[k]["name"]}', f'dsan:{k}') for k in keys[i:i + 3]]
                for i in range(0, len(keys), 3)]
        rows.append([B('◀️ Назад', 'dsa')])
        _edit(q, '📌 Шаг 1/3. Для какой соцсети?', _kb(*rows))
        return
    if not kind:
        keys = ['*'] + KIND_ORDER
        rows = [[B('🧩 Все типы' if k == '*' else f'{KINDS[k]["emoji"]} {KINDS[k]["ru"].capitalize()}', f'dsan:{platform}:{k}')
                 for k in keys[i:i + 2]] for i in range(0, len(keys), 2)]
        rows.append([B('◀️ Назад', 'dsan')])
        _edit(q, '📌 Шаг 2/3. Для какого типа услуги?', _kb(*rows))
        return
    rows = _template_buttons(f'dsat:{platform}:{kind}', load_config(), include_auto=False)
    rows.append([B('◀️ Назад', f'dsan:{platform}')])
    _edit(q, '📌 Шаг 3/3. Какой шаблон или тему использовать?', _kb(*rows))


@route('dsat')
def r_design_assign_set(q: Any, platform: str, kind: str, token: str) -> None:
    if platform == '*' and kind == '*':
        with config_tx() as tx:
            tx['design']['theme'] = _token_choice(token)
    else:
        with config_tx() as tx:
            tx['design']['assign'][f'{platform}:{kind}'] = _token_choice(token)
    _answer(q, '✅ Назначено.')
    r_design_assign(q)


@route('dsm')
def r_design_sections(q: Any) -> None:
    cfg = load_config()
    sections = cfg['design']['sections']
    rows = [[B(f'{PLATFORMS[k]["emoji"]} {PLATFORMS[k]["name"]}: {sections.get(k) or "—"}', f'dsmp:{k}') for k in PLATFORM_ORDER[i:i + 2]]
            for i in range(0, len(PLATFORM_ORDER), 2)]
    extra = [k for k in sections if ':' in k]
    if extra:
        rows.append([B(f'Особых правил «соцсеть+тип»: {len(extra)}', 'noop')])
    rows.append([B('◀️ Назад', 'ds')])
    _edit(q, '🗺 <b>Карта разделов Starvell</b>\n\nУкажите раздел Starvell для каждой соцсети — тогда автовыгрузка сама разложит '
             'услуги по разделам. ID раздела — число из ссылки <code>funpay.com/lots/ЧИСЛО/</code>.', _kb(*rows))


@route('dsmp')
def r_design_section_set(q: Any, platform: str) -> None:
    _ask(q, 'section_set', f'🗺 Введите ID раздела Starvell для {escape(_platform_info(platform)["name"])} (<code>0</code> — убрать).\n'
                           'Чтобы задать отдельный раздел для типа услуги, введите: <code>ID тип</code>, например <code>1234 views</code>.\n'
                           'Типы: ' + ', '.join(KIND_ORDER), {'platform': platform}, back='dsm')


@inp('section_set')
def i_section_set(message: Any, text: str, data: Dict[str, Any]) -> None:
    parts = text.strip().split()
    node = _int(parts[0], -1) if parts else -1
    kind = parts[1].lower() if len(parts) > 1 else ''
    if node < 0 or (kind and kind not in KINDS):
        raise ValueError('Формат: ID или «ID тип», например 1234 views.')
    key = f'{data["platform"]}:{kind}' if kind else data['platform']
    with config_tx() as tx:
        if node:
            tx['design']['sections'][key] = node
        else:
            tx['design']['sections'].pop(key, None)
    _clear(message)
    _show(message.chat.id, '✅ Сохранено.', _back_kb('dsm', '◀️ К карте разделов'))


@route('dsr')
def r_design_restyle(q: Any) -> None:
    cfg = load_config()
    rows = [[B('Все лоты', 'dsrs:all'), B('Автовыгрузка', 'dsrs:auto'), B('Ручные', 'dsrs:man')]]
    rows += [[B(f'🔌 {p["name"]}', f'dsrs:p_{pid}')] for pid, p in cfg['services'].items()]
    rows.append([B('◀️ Назад', 'ds')])
    _edit(q, '🔄 <b>Переоформить лоты</b>\n\nТексты лотов на Starvell будут собраны заново. Цена, остаток и тег сохраняются.\n\n'
             'Какие лоты переоформить?', _kb(*rows))


@route('dsrs')
def r_design_restyle_scope(q: Any, scope: str) -> None:
    rows = _template_buttons(f'dsrt:{scope}', load_config(), include_keep=True)
    rows.append([B('◀️ Назад', 'dsr')])
    _edit(q, '🎨 Каким оформлением?\n\n«📌 Как у лота» — шаблон, сохранённый у каждого лота (или по настройкам).', _kb(*rows))


@route('dsrt')
def r_design_restyle_go(q: Any, scope: str, token: str) -> None:
    cfg = load_config()
    keys = [k for k, v in _filter_lots(cfg, scope) if v.get('funpay_lot_id')]
    if not keys:
        _answer(q, 'Нет лотов с Starvell ID для переоформления.', True)
        return
    if not _account_ready():
        _answer(q, 'Аккаунт Starvell ещё не загружен.', True)
        return
    _answer(q)
    choice = _token_choice(token)
    def job(progress: Callable) -> str:
        done, errors = _restyle_lots(keys, choice, progress)
        return f'🎨 Переоформлено лотов: {done} из {len(keys)}' + (('\n\nОшибки:\n' + '\n'.join('• ' + escape(x) for x in errors[:15])) if errors else '')
    _progress_job(q.message.chat.id, 'Переоформляю лоты', job, _back_kb('ds', '◀️ Оформление'))


# ---------------------------------------------------------------------------
# Сообщения покупателю
# ---------------------------------------------------------------------------

@route('msg')
def r_messages(q: Any) -> None:
    cfg = load_config()
    parts = ['💬 <b>Сообщения покупателю</b>\n\nStarvell не поддерживает HTML — теги удаляются перед отправкой.\n']
    for key, (label, _) in MESSAGE_INFO.items():
        parts.append(f'<b>{escape(label)}:</b>\n<code>{escape(_short(cfg["messages"][key], 160))}</code>')
    rows = [[B('✏️ ' + MESSAGE_INFO[k][0], f'me:{k}'), B('↩️', f'mr:{k}')] for k in MESSAGE_INFO]
    rows.append([B('◀️ Назад', 'home')])
    _edit(q, '\n\n'.join(parts)[:4000], _kb(*rows))


@route('me')
def r_message_edit(q: Any, key: str) -> None:
    label, variables = MESSAGE_INFO[key]
    current = load_config()['messages'][key]
    _ask(q, 'message_edit', f'✏️ <b>{escape(label)}</b>\n\nТекущий текст:\n<code>{escape(current)}</code>\n\n'
                            f'Переменные: <code>{escape(variables)}</code>\n\nОтправьте новый текст:', {'key': key}, back='msg')


@inp('message_edit')
def i_message_edit(message: Any, text: str, data: Dict[str, Any]) -> None:
    if len(text) > MAX_MESSAGE_LEN:
        raise ValueError(f'Слишком длинный текст: Starvell не примет сообщение длиннее {MAX_MESSAGE_LEN} символов.')
    if not text.strip():
        raise ValueError('Текст не может быть пустым.')
    with config_tx() as tx:
        tx['messages'][data['key']] = text
    _clear(message)
    note = ''
    if data['key'] == 'after_confirmation' and 'чек' not in text.lower():
        note = '\n⚠️ В тексте нет подсказки «чек {panel_order_id}» — покупатели не узнают, как проверить статус.'
    _show(message.chat.id, '✅ Сообщение сохранено.' + escape(note), _back_kb('msg', '◀️ К сообщениям'))


@route('mr')
def r_message_reset(q: Any, key: str) -> None:
    with config_tx() as tx:
        tx['messages'][key] = DEFAULT_MESSAGES[key]
    _answer(q, '↩️ Восстановлен стандартный текст.')
    r_messages(q)


# ---------------------------------------------------------------------------
# Рефилл: общие и массовые настройки
# ---------------------------------------------------------------------------

REFILL_MOMENTS = ['after_start', 'after_complete', 'after_payment', 'never']
REFILL_MOMENT_LABELS = {'after_start': 'после запуска', 'after_complete': 'когда выполнен', 'after_payment': 'сразу после оплаты',
                        'never': 'не отправлять'}


@route('rf')
def r_refill(q: Any) -> None:
    cfg = load_config()
    lots = cfg['lot_mapping'].values()
    orders = _orders()
    active = sum(1 for x in orders if _refill_eligible(x))
    total = sum(_int((x.get('refill') or {}).get('used')) for x in orders)
    moment = cfg['refill']['send_moment']
    text = (f'♻️ <b>Рефилл</b>\n\nЛотов с рефиллом: {sum(1 for x in lots if x["refill"]["enabled"])} из {len(cfg["lot_mapping"])}\n'
            f'Заказов с активным рефиллом: {active} · запущено всего: {total}\n'
            f'Шаблон отправляется: {REFILL_MOMENT_LABELS[moment]}\n'
            f'Автовключение для услуг с гарантией: {"ВКЛ" if cfg["refill"].get("auto_enable") else "ВЫКЛ"}\n\n'
            f'<b>Шаблон:</b>\n<code>{escape(_short(cfg["messages"]["refill_info"], 300))}</code>\n\n'
            'Покупатель запускает рефилл командой !рефилл (или !refill, рефилл, /рефилл).')
    kb = _kb([B(f'💬 Момент: {REFILL_MOMENT_LABELS[moment]}', 'rfm')], [B('✏️ Изменить шаблон', 'me:refill_info')],
             [B(f'{"🔔" if cfg["refill"]["notify"] else "🔕"} Уведомлять меня', 'rfn'),
              B(f'🤖 Автовключение: {"ВКЛ" if cfg["refill"].get("auto_enable") else "ВЫКЛ"}', 'rfa')],
             [B('⚙️ Массовые настройки', 'rfb'), B('📋 Лоты с рефиллом', 'll:ref:0')], [B('◀️ Назад', 'home')])
    _edit(q, text, kb)


@route('rfm', 'rfn', 'rfa')
def r_refill_toggle(q: Any) -> None:
    with config_tx() as tx:
        r = tx['refill']
        if q.data == 'rfm':
            r['send_moment'] = REFILL_MOMENTS[(REFILL_MOMENTS.index(r['send_moment']) + 1) % len(REFILL_MOMENTS)] \
                if r['send_moment'] in REFILL_MOMENTS else 'after_start'
        elif q.data == 'rfn':
            r['notify'] = not r['notify']
        else:
            r['auto_enable'] = not r.get('auto_enable')
    if q.data == 'rfm' and load_config()['refill']['send_moment'] == 'after_payment':
        _answer(q, '⚠️ В момент оплаты ссылки и ID заказа ещё нет — {link} и {order_id} будут пустыми.', True)
    r_refill(q)


REFILL_BULK = {'on': ('✅ Рефилл ВСЕМ', 'enabled', True), 'off': ('❌ Убрать у ВСЕХ', 'enabled', False),
               'limon': ('🔁 Лимит: вкл всем', 'limit_enabled', True), 'limoff': ('🔁 Лимит: выкл всем', 'limit_enabled', False),
               'peron': ('⏳ Срок: вкл всем', 'period_enabled', True), 'peroff': ('⏳ Срок: выкл всем', 'period_enabled', False)}


@route('rfb')
def r_refill_bulk(q: Any) -> None:
    cfg = load_config()
    lots = list(cfg['lot_mapping'].values())
    text = (f'⚙️ <b>Массовые настройки рефилла</b>\n\nРефилл включён: {sum(1 for x in lots if x["refill"]["enabled"])}/{len(lots)}\n'
            f'Лимит включён: {sum(1 for x in lots if x["refill"]["limit_enabled"])}/{len(lots)}\n'
            f'Срок включён: {sum(1 for x in lots if x["refill"]["period_enabled"])}/{len(lots)}')
    kb = _kb([B(REFILL_BULK['on'][0], 'rfbx:on'), B(REFILL_BULK['off'][0], 'rfbx:off')],
             [B(REFILL_BULK['limon'][0], 'rfbx:limon'), B(REFILL_BULK['limoff'][0], 'rfbx:limoff')],
             [B(REFILL_BULK['peron'][0], 'rfbx:peron'), B(REFILL_BULK['peroff'][0], 'rfbx:peroff')],
             [B('✏️ Кол-во запусков всем', 'rfbv:limit'), B('✏️ Срок (часы) всем', 'rfbv:hours')],
             [B('🎯 Выбрать лоты вручную', 'rfs:0')], [B('◀️ Рефилл', 'rf')])
    _edit(q, text, kb)


@route('rfbx')
def r_refill_bulk_confirm(q: Any, action: str) -> None:
    count = len(load_config()['lot_mapping'])
    _edit(q, f'{REFILL_BULK[action][0]}\n\nПрименить к {count} лотам?', _kb([B('✅ Да, применить', f'rfbg:{action}'), B('❌ Отмена', 'rfb')]))


@route('rfbg')
def r_refill_bulk_go(q: Any, action: str) -> None:
    _, field, value = REFILL_BULK[action]
    with config_tx() as tx:
        for lot in tx['lot_mapping'].values():
            lot['refill'][field] = value
    _answer(q, '✅ Применено.')
    r_refill_bulk(q)


@route('rfbv')
def r_refill_bulk_value(q: Any, what: str) -> None:
    _ask(q, 'refill_bulk', '✏️ Введите количество запусков на 1 заказ (минимум 1):' if what == 'limit' else
         '✏️ Введите срок действия рефилла в часах (например 168 = 7 дней):', {'what': what}, back='rfb')


@inp('refill_bulk')
def i_refill_bulk(message: Any, text: str, data: Dict[str, Any]) -> None:
    value = _int(text)
    if value < 1:
        raise ValueError('Нужно целое число не меньше 1.')
    with config_tx() as tx:
        for lot in tx['lot_mapping'].values():
            lot['refill']['limit' if data['what'] == 'limit' else 'hours'] = value
    _clear(message)
    _show(message.chat.id, '✅ Применено ко всем лотам.', _back_kb('rfb', '◀️ К массовым настройкам'))


@route('rfs')
def r_refill_select(q: Any, page: str = '0') -> None:
    cfg = load_config()
    items, page_n, pages = _page(_filter_lots(cfg, 'all'), _int(page), 8)
    rows = [[B(('✅ ' if lot['refill']['enabled'] else '⬜ ') + _short(lot['name'], 44), f'rfst:{key}:{page_n}')] for key, lot in items]
    rows.append(_nav('rfs', page_n, pages))
    rows.append([B('✅ Вкл. всё на стр.', f'rfsp:{page_n}:1'), B('⬜ Выкл. всё на стр.', f'rfsp:{page_n}:0')])
    rows.append([B('🔙 К массовым настройкам', 'rfb')])
    _edit(q, '🎯 <b>Выбор лотов для рефилла</b>\n\nНажмите на лот, чтобы переключить.', _kb(*rows))


@route('rfst')
def r_refill_select_toggle(q: Any, key: str, page: str) -> None:
    with config_tx() as tx:
        if key in tx['lot_mapping']:
            tx['lot_mapping'][key]['refill']['enabled'] = not tx['lot_mapping'][key]['refill']['enabled']
    r_refill_select(q, page)


@route('rfsp')
def r_refill_select_page(q: Any, page: str, value: str) -> None:
    cfg = load_config()
    items, _, _ = _page(_filter_lots(cfg, 'all'), _int(page), 8)
    with config_tx() as tx:
        for key, _ in items:
            if key in tx['lot_mapping']:
                tx['lot_mapping'][key]['refill']['enabled'] = value == '1'
    r_refill_select(q, page)


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

SETTING_TOGGLES = [('auto_refunds', 'Автовозврат'), ('confirm_link', 'Подтверждение ссылки'), ('domain_check', 'Проверка доменов'),
                   ('anti_dumping', 'Антидемпинг'), ('auto_start', 'Автозапуск'), ('notify_new_order', 'Увед. о новом заказе'),
                   ('watcher_enabled', 'Вотчер услуг'), ('respect_blacklist', 'Чёрный список бота'),
                   ('watcher_autoprice', 'Цена за провайдером')]
SETTING_VALUES = {'min_margin': ('Мин. прибыль (₽)', 'float'), 'poll_interval': ('Проверка статусов (сек)', 'int'),
                  'early_complete_minutes': ("Защита от раннего 'выполнено' (мин)", 'int'), 'api_timeout': ('Таймаут API (сек)', 'int'),
                  'remind_hours': ('Напоминание о ссылке (ч, 0 — выкл)', 'int'), 'stale_hours': ('Сообщить мне о заказе без ссылки (ч)', 'int'),
                  'notification_chat_id': ('Чат уведомлений (ID, 0 — админы)', 'int'),
                  'watcher_threshold': ('Вотчер: важное изменение цены от (%)', 'float')}
SETTING_LIMITS = {'watcher_threshold': (0, 1000), 'poll_interval': (20, 3600), 'early_complete_minutes': (0, 1440), 'api_timeout': (5, 180), 'remind_hours': (0, 720),
                  'stale_hours': (0, 720)}


@route('st')
def r_settings(q: Any) -> None:
    cfg = load_config()
    lines = ['⚙️ <b>Настройки</b>', '']
    lines += [f'{_on(cfg[k])} {label}' for k, label in SETTING_TOGGLES]
    lines.append(f'👁 Уведомления вотчера: {WATCH_MODE_LABELS[cfg["watcher_notify"]]}')
    lines.append('<i>«Цена за провайдером» — при изменении цены услуги у провайдера цены лотов с ней пересчитываются сразу.</i>')
    lines.append('')
    lines += [f'• {label}: <code>{escape(str(cfg.get(k) if cfg.get(k) is not None else "—"))}</code>' for k, (label, _) in SETTING_VALUES.items()]
    rows = [[B(f'{_on(cfg[k])} {label}', f'stt:{k}') for k, label in SETTING_TOGGLES[i:i + 2]] for i in range(0, len(SETTING_TOGGLES), 2)]
    rows += [[B('✏️ ' + label, f'ste:{k}')] for k, (label, _) in SETTING_VALUES.items()]
    rows.append([B(f'👁 Уведомления вотчера: {WATCH_MODE_LABELS[cfg["watcher_notify"]]}', 'wnm')])
    rows.append([B(f'⏸ На паузе (/stop): {len(cfg["paused_buyers"])}', 'stp'), B('💾 Бэкап сейчас', 'stb')])
    rows.append([B('◀️ Назад', 'home')])
    _edit(q, '\n'.join(lines), _kb(*rows))


@route('stt')
def r_setting_toggle(q: Any, key: str) -> None:
    if key not in dict(SETTING_TOGGLES):
        return
    with config_tx() as tx:
        tx[key] = not tx[key]
    if key == 'domain_check' and load_config()['domain_check']:
        _answer(q, 'Проверка доменов включена: ссылки должны быть из доменов лота или общего списка «🌐 Домены».', True)
    r_settings(q)


@route('ste')
def r_setting_edit(q: Any, key: str) -> None:
    if key in SETTING_VALUES:
        _ask(q, 'setting_edit', f'✏️ {escape(SETTING_VALUES[key][0])}: введите значение', {'key': key}, back='st')


@inp('setting_edit')
def i_setting_edit(message: Any, text: str, data: Dict[str, Any]) -> None:
    key = data['key']
    _, kind = SETTING_VALUES[key]
    value: Any = _float(text, float('nan')) if kind == 'float' else _int(text, -10 ** 12)
    if (kind == 'float' and (not math.isfinite(value) or value < 0)) or (kind == 'int' and value == -10 ** 12):
        raise ValueError('Введите число.')
    if key in SETTING_LIMITS:
        low, high = SETTING_LIMITS[key]
        if not low <= value <= high:
            raise ValueError(f'Допустимо от {low} до {high}.')
    if key == 'notification_chat_id':
        value = value or None
    with config_tx() as tx:
        tx[key] = value
    _clear(message)
    _show(message.chat.id, '✅ Сохранено.', _back_kb('st', '◀️ К настройкам'))


@route('wnm')
def r_watch_mode(q: Any) -> None:
    with config_tx() as tx:
        tx['watcher_notify'] = WATCH_MODES[(WATCH_MODES.index(tx['watcher_notify']) + 1) % len(WATCH_MODES)]
    r_settings(q)


@route('wnot')
def r_watch_notify(q: Any, mode: str) -> None:
    if mode not in WATCH_MODES:
        return
    with config_tx() as tx:
        tx['watcher_notify'] = mode
    _answer(q, '🔇 Уведомления вотчера выключены. Цены лотов продолжат обновляться сами. Включить: ⚙️ Настройки.'
            if mode == 'off' else '🔕 Теперь только важные изменения (цена от порога, название, лимиты, пропажа услуги).', True)
    try:
        bot.edit_message_reply_markup(q.message.chat.id, q.message.message_id, reply_markup=None)
    except Exception:
        pass


@route('stp')
def r_paused(q: Any) -> None:
    cfg = load_config()
    rows = [[B(f'▶️ Возобновить {item.get("username") or ""} (ID {item.get("id") or "?"})', f'stpr:{index}')]
            for index, item in enumerate(cfg['paused_buyers'][:20])]
    rows.append([B('➕ Поставить на паузу', 'stpa'), B('🔙 Назад', 'st')])
    _edit(q, '⏸ <b>Покупатели на паузе</b>\n\n' + ('Нажмите, чтобы вернуть покупателя в автоматический режим.' if cfg['paused_buyers'] else
          'Список пуст. Чтобы поставить покупателя на паузу, напишите /stop в чате с ним на Starvell, вернуть — /start.'), _kb(*rows))


@route('stpa')
def r_paused_add(q: Any) -> None:
    _ask(q, 'pause_add', '⏸ Введите ник покупателя или его числовой ID. Бот перестанет обрабатывать его сообщения и новые заказы, '
                         'а заказы в работе переведёт в ручной режим.', back='stp')


@inp('pause_add')
def i_pause_add(message: Any, text: str, data: Dict[str, Any]) -> None:
    value = text.strip().lstrip('@')
    if not value:
        raise ValueError('Введите ник или ID.')
    buyer_id, username = (value, '') if value.isdigit() else ('', value)
    known = [x for x in list(waiting.values()) + _orders()
             if (buyer_id and str(x.get('buyer_id')) == buyer_id) or (username and str(x.get('buyer_username') or '').lower() == username.lower())]
    if known:
        buyer_id = buyer_id or str(known[-1].get('buyer_id') or '')
        username = username or str(known[-1].get('buyer_username') or '')
    _clear(message)
    _pause_buyer(cardinal_instance, buyer_id, username, None)
    _show(message.chat.id, f'⏸ Покупатель {escape(username or buyer_id)} на паузе.', _back_kb('stp', '◀️ К списку'))


@route('stpr')
def r_paused_resume(q: Any, index: str) -> None:
    with config_tx() as tx:
        if _int(index) < len(tx['paused_buyers']):
            tx['paused_buyers'].pop(_int(index))
    _answer(q, '▶️ Автообработка возобновлена.')
    r_paused(q)


@route('stb')
def r_backup(q: Any) -> None:
    for path in (CONFIG_PATH, ORDERS_PATH, STATE_PATH):
        _backup(path, force=True)
    _answer(q, f'💾 Резервные копии сохранены в {BACKUP_DIR}', True)


# ---------------------------------------------------------------------------
# Домены
# ---------------------------------------------------------------------------

@route('dm')
def r_domains(q: Any) -> None:
    cfg = load_config()
    domains = _global_domains()
    rows = [[B(d, 'noop'), B('❌', f'dmd:{i}')] for i, d in enumerate(domains[:40])]
    rows.append([B('➕ Добавить', 'dma'), B('↩️ Сбросить', 'dmr')])
    rows.append([B(f'{_on(cfg["domain_check"])} Проверка доменов', 'stt:domain_check'), B('◀️ Назад', 'home')])
    _edit(q, f'🌐 <b>Общий список доменов</b>\n\nПроверка доменов: {"ВКЛ" if cfg["domain_check"] else "ВЫКЛ"}.\n'
             'Используется для лотов без своих доменов. Домен подходит и для поддоменов (vk.com → m.vk.com).', _kb(*rows))


@route('dmd')
def r_domain_delete(q: Any, index: str) -> None:
    domains = _global_domains()
    if _int(index) >= len(domains):
        return
    _edit(q, f'❌ Удалить домен <b>{escape(domains[_int(index)])}</b>?', _kb([B('✅ Да, удалить', f'dmdd:{index}'), B('❌ Отмена', 'dm')]))


@route('dmdd')
def r_domain_delete_do(q: Any, index: str) -> None:
    domains = _global_domains()
    if _int(index) < len(domains):
        domains.pop(_int(index))
        _save_global_domains(domains)
    r_domains(q)


@route('dmr')
def r_domain_reset(q: Any) -> None:
    _save_global_domains(list(DEFAULT_DOMAINS))
    _answer(q, '↩️ Список сброшен к стандартному.')
    r_domains(q)


@route('dma')
def r_domain_add(q: Any) -> None:
    _ask(q, 'domain_add', '➕ Введите домен (например <code>x.com</code>) или ссылку — бот оставит только домен:', back='dm')


@inp('domain_add')
def i_domain_add(message: Any, text: str, data: Dict[str, Any]) -> None:
    new = _domains(text)
    if not new or new == ['*']:
        raise ValueError('Не удалось распознать домен.')
    domains = _global_domains()
    added = [d for d in new if d not in domains]
    if not added:
        raise ValueError('Такой домен уже есть.')
    _save_global_domains(domains + added)
    _clear(message)
    _show(message.chat.id, '✅ Добавлено: ' + escape(', '.join(added)), _back_kb('dm', '◀️ К доменам'))


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------

@route('stats')
def r_stats(q: Any, period: str = 'today') -> None:
    days = {'today': 1, 'week': 7, 'month': 30, 'all': None}.get(period)
    title = {'today': 'Сегодня', 'week': '7 дней', 'month': '30 дней', 'all': 'Всё время'}.get(period, 'Всё время')
    s = _stats(days)
    text = (f'📊 <b>Статистика — {title}</b>\n\n🧾 Заказов: {s["orders"]} · ✅ выполнено: {s["done"]} · ⚙️ в работе: {s["active"]}\n'
            f'↩️ Возвратов: {s["refunded"]}\n\n💵 Выручка: <b>{s["revenue"]:.2f} ₽</b>\n💸 Себестоимость: {s["cost"]:.2f} ₽\n'
            f'✅ Чистая прибыль: <b>{s["profit"]:.2f} ₽</b>\n\nВозвраты исключены из выручки. Себестоимость — по данным панели '
            '(с пересчётом валюты), для частичных заказов — фактическое списание.')
    kb = _kb([B('Сегодня', 'stats:today'), B('7 дней', 'stats:week'), B('30 дней', 'stats:month'), B('Всё время', 'stats:all')],
             [B('◀️ Назад', 'home')])
    _edit(q, text, kb)


# ---------------------------------------------------------------------------
# Starvell Cardinal: адаптеры Telegram (aiogram) и сервиса Starvell
# ---------------------------------------------------------------------------

from aiogram import F  # noqa: E402
from aiogram.filters import Command  # noqa: E402

try:  # aiogram ≥ 3.4
    from aiogram.types import LinkPreviewOptions as _LinkPreviewOptions
except ImportError:  # pragma: no cover
    _LinkPreviewOptions = None


def _aio_markup(kb: Optional[InlineKeyboardMarkup]) -> Any:
    if kb is None:
        return None
    from aiogram.types import InlineKeyboardButton as AioButton, InlineKeyboardMarkup as AioMarkup
    rows = []
    for row in kb.keyboard:
        items = [AioButton(text=b.text, url=b.url) if b.url else AioButton(text=b.text, callback_data=b.callback_data)
                 for b in row if b is not None]
        if items:
            rows.append(items)
    return AioMarkup(inline_keyboard=rows)


def _preview_kwargs(disabled: Any) -> Dict[str, Any]:
    if not disabled:
        return {}
    if _LinkPreviewOptions is not None:
        return {'link_preview_options': _LinkPreviewOptions(is_disabled=True)}
    return {'disable_web_page_preview': True}


class _BotBridge:
    """Интерфейс telebot поверх aiogram Bot: общий код меню вызывает его из рабочих потоков."""

    def __init__(self, aio_bot: Any):
        self.aio = aio_bot

    def send_message(self, chat_id: Any, text: str, parse_mode: Optional[str] = None,
                     reply_markup: Optional[InlineKeyboardMarkup] = None, disable_web_page_preview: Any = None, **_: Any) -> Any:
        return _run(self.aio.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=_aio_markup(reply_markup),
                                          **_preview_kwargs(disable_web_page_preview)))

    def edit_message_text(self, text: str, chat_id: Any, message_id: int, parse_mode: Optional[str] = None,
                          reply_markup: Optional[InlineKeyboardMarkup] = None, disable_web_page_preview: Any = None, **_: Any) -> Any:
        return _run(self.aio.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, parse_mode=parse_mode,
                                               reply_markup=_aio_markup(reply_markup), **_preview_kwargs(disable_web_page_preview)))

    def edit_message_reply_markup(self, chat_id: Any, message_id: int, reply_markup: Optional[InlineKeyboardMarkup] = None, **_: Any) -> Any:
        return _run(self.aio.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=_aio_markup(reply_markup)))

    def answer_callback_query(self, callback_id: str, text: Optional[str] = None, show_alert: bool = False, **_: Any) -> Any:
        return _run(self.aio.answer_callback_query(callback_query_id=callback_id, text=text, show_alert=show_alert), timeout=15)

    def delete_message(self, chat_id: Any, message_id: int, **_: Any) -> Any:
        return _run(self.aio.delete_message(chat_id=chat_id, message_id=message_id), timeout=15)


def _config_admin_ids() -> List[int]:
    try:
        from bot.core.config import BotConfig  # type: ignore
        return [_int(x) for x in (BotConfig.ADMIN_IDS() or []) if _int(x)]
    except Exception:
        return []


class _TelegramAdapter:
    def __init__(self, bridge: _BotBridge):
        self.bot = bridge
        self._states: Dict[Tuple[int, int], Dict[str, Any]] = {}

    @property
    def authorized_users(self) -> Dict[int, Dict[str, Any]]:
        known = (_state_get('starvell', {}) or {}).get('admins', [])
        return {x: {} for x in dict.fromkeys(_config_admin_ids() + [_int(x) for x in known if _int(x)])}

    def get_state(self, chat_id: int, user_id: int) -> Optional[Dict[str, Any]]:
        return self._states.get((_int(chat_id), _int(user_id)))

    def set_state(self, chat_id: int, message_id: int, user_id: int, state: str, data: Optional[Dict[str, Any]] = None) -> None:
        self._states[(_int(chat_id), _int(user_id))] = {'state': state, 'mid': message_id, 'data': data or {}}

    def clear_state(self, chat_id: int, user_id: int, del_msg: bool = False) -> Optional[int]:
        item = self._states.pop((_int(chat_id), _int(user_id)), None)
        return item.get('mid') if item else None


def _remember_admin(user_id: Any) -> None:
    """Сообщения и кнопки плагина пропускает AuthMiddleware ядра — значит, это админ."""
    user_id = _int(user_id)
    if not user_id:
        return
    state = _state_get('starvell', {}) or {}
    admins = [_int(x) for x in state.get('admins', [])]
    if user_id not in admins:
        state['admins'] = (admins + [user_id])[-20:]
        _state_set('starvell', state)


_ORDER_STATUS = {'CREATED': 'PAID', 'PAID': 'PAID', 'COMPLETED': 'CLOSED', 'CLOSED': 'CLOSED', 'REFUND': 'REFUNDED',
                 'REFUNDED': 'REFUNDED', 'FAILED': 'FAILED', 'PRE_CREATED': 'UNPAID'}


def _find_dict(value: Any, key: str, depth: int = 0) -> Optional[Dict[str, Any]]:
    if depth > 4 or not isinstance(value, dict):
        return None
    if isinstance(value.get(key), dict):
        return value[key]
    for item in value.values():
        found = _find_dict(item, key, depth + 1)
        if found:
            return found
    return None


def _sv_order(order_id: Any) -> SimpleNamespace:
    """Заказ Starvell в виде, который понимает общий код (как FunPay Order)."""
    service = _service()
    raw = _fp_call(service.get_order_details, str(order_id))
    if not isinstance(raw, dict):
        raise RuntimeError('Starvell не вернул данные заказа.')
    page = raw.get('pageProps') if isinstance(raw.get('pageProps'), dict) else raw
    order = page.get('order') if isinstance(page.get('order'), dict) else (_find_dict(raw, 'order') or {})
    chat = page.get('chat') if isinstance(page.get('chat'), dict) else (_find_dict(raw, 'chat') or {})
    if not order:
        raise RuntimeError('В ответе Starvell нет данных заказа.')
    buyer = order.get('buyer') if isinstance(order.get('buyer'), dict) else {}
    buyer_id = order.get('buyerId') or buyer.get('id')
    rus = _dig(order, 'offerDetails', 'descriptions', 'rus') or {}
    brief = str(rus.get('briefDescription') or rus.get('description') or '')
    chat_id = chat.get('id')
    if not chat_id and buyer_id:
        finder = getattr(service, 'find_chat_by_user_id', None)
        if callable(finder):
            try:
                chat_id = _fp_call(finder, str(buyer_id))
            except Exception:
                chat_id = None
    status = str(order.get('status') or '').upper()
    return SimpleNamespace(
        id=str(order.get('id') or order_id), status=SimpleNamespace(name=_ORDER_STATUS.get(status, status or 'UNKNOWN')),
        full_description=str(rus.get('description') or ''), short_description=brief, description=brief,
        lot_id=_int(order.get('offerId') or _dig(order, 'offer', 'id')), buyer_id=buyer_id,
        buyer_username=str(buyer.get('username') or buyer.get('nickname') or ''),
        amount=max(1, _int(order.get('quantity'), 1)), price=_float(order.get('basePrice') or order.get('totalPrice')) / 100,
        chat_id=chat_id, date=None)


class _StarvellAccount:
    is_initiated = True
    csrf_token = None
    currency = 'RUB'

    @property
    def proxy(self) -> Optional[Dict[str, str]]:
        """Прокси из настроек Starvell Cardinal — режим «прокси бота» в AutoSMM."""
        try:
            url = getattr(getattr(getattr(_service(), 'api', None), 'config', None), 'proxy_url', None)
        except RuntimeError:
            url = None
        return {'http': url, 'https': url} if url else None

    @property
    def id(self) -> Any:
        return _SV.get('me')

    def get_order(self, order_id: Any, *args: Any, **kwargs: Any) -> SimpleNamespace:
        return _sv_order(order_id)

    def refund(self, order_id: Any) -> None:
        result = _fp_call(_service().refund_order, str(order_id), attempts=2)
        error = _api_error(result)
        if error:
            raise RuntimeError(error)


class _StarvellCardinal:
    """Объект с интерфейсом FunPay Cardinal, нужным общему коду заказов."""
    blacklist: List[str] = []
    proxy: Dict[str, str] = {}
    runner = True

    def __init__(self, telegram: Optional[_TelegramAdapter]):
        self.account = _StarvellAccount()
        self.telegram = telegram

    def send_message(self, chat_id: Any, text: str, *args: Any, **kwargs: Any) -> Any:
        """Сообщение в чат Starvell; длинный текст делится на части по MAX_MESSAGE_LEN."""
        if not chat_id or not text:
            return None
        parts, rest = [], str(text)
        while rest:
            if len(rest) <= MAX_MESSAGE_LEN:
                parts.append(rest)
                break
            cut = rest.rfind('\n', 0, MAX_MESSAGE_LEN)
            cut = cut if cut > MAX_MESSAGE_LEN // 2 else MAX_MESSAGE_LEN
            parts.append(rest[:cut])
            rest = rest[cut:].lstrip('\n')
        result = None
        for part in parts:
            try:
                result = _fp_call(_service().send_message, str(chat_id), part, attempts=3)
            except Exception as exc:
                logger.warning('[AutoSMM] Starvell message to %s failed: %s', chat_id, type(exc).__name__)
                return None
            if _api_error(result):
                logger.warning('[AutoSMM] Starvell refused message to %s: %s', chat_id, _api_error(result))
                return None
        return result if result is not None else True

    def is_blacklisted(self, user: Any) -> bool:
        try:
            from bot.core.config import get_config_manager  # type: ignore
            return bool(user) and bool(get_config_manager().is_blacklisted(str(user)))
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Диспетчеры меню (общие с FunPay-версией)
# ---------------------------------------------------------------------------

def _dispatch(q: Any, data: str) -> None:
    parts = data.split(':')
    handler = ROUTES.get(parts[0])
    if not handler:
        _answer(q, 'Меню устарело — откройте /auto_smm_settings заново.', True)
        return
    local = copy(q)
    local.data = data
    try:
        handler(local, *parts[1:])
    except TypeError as exc:
        logger.warning('[AutoSMM] Bad callback %s: %s', data, exc)
        _answer(q, 'Меню устарело — откройте /auto_smm_settings заново.', True)
    except (ValueError, PanelError, ConfigGuardError, LotPublishError) as exc:
        _answer(q, f'❌ {exc}', True)
    except Exception:
        logger.exception('[AutoSMM] Callback %s failed', data)
        _answer(q, '❌ Ошибка. Подробности в журнале Starvell Cardinal.', True)
    finally:
        _answer(local)


def _on_callback(q: Any) -> None:
    if not _is_admin(q):
        return
    _dispatch(q, str(q.data)[len('autosmm:'):])


def _state(message: Any) -> Tuple[str, Dict[str, Any]]:
    data = cardinal_instance.telegram.get_state(message.chat.id, message.from_user.id)
    if not isinstance(data, dict):
        return '', {}
    return str(data.get('state') or ''), data.get('data') if isinstance(data.get('data'), dict) else {}


def _input(message: Any) -> None:
    if not _is_admin(message):
        return
    state, data = _state(message)
    if not state.startswith('autosmm:'):
        return
    text = str(getattr(message, 'text', '') or '').strip()
    handler = INPUTS.get(state[len('autosmm:'):])
    if not handler:
        _clear(message)
        return
    if not text:
        _show(message.chat.id, '❌ Нужен текст. Попробуйте ещё раз или нажмите «Отмена».')
        return
    try:
        handler(message, text, data)
    except (ValueError, PanelError, LotPublishError, ConfigGuardError) as exc:
        _show(message.chat.id, f'❌ {escape(str(exc))}\n\nПопробуйте ещё раз или нажмите «Отмена».')
    except Exception:
        logger.exception('[AutoSMM] Input %s failed', state)
        _show(message.chat.id, '❌ Ошибка сохранения. Подробности в журнале Starvell Cardinal.')


PLUGIN_COMMANDS = {'/start_smm', '/stop_smm', '/auto_smm_settings'}


def _command_name(message: Any) -> str:
    text = str(getattr(message, 'text', '') or '').strip().lower()
    return text.split(maxsplit=1)[0].split('@', 1)[0] if text.startswith('/') else ''


def auto_smm_settings(message: Any) -> None:
    if _is_admin(message):
        _show(message.chat.id, *scr_home())


def start_smm(message: Any) -> None:
    if _is_admin(message):
        _workers(cardinal_instance)
        _show(message.chat.id, '▶️ Фоновые процессы AutoSMM запущены.')


def stop_smm(message: Any) -> None:
    if _is_admin(message):
        _stop_workers()
        _show(message.chat.id, '⏸ Фоновые процессы остановлены. Приём заказов продолжается — чтобы перестать продавать, '
                               'нажмите «🛑 Снять с продажи» в /auto_smm_settings.')


def _command_dispatch(message: Any) -> None:
    command = _command_name(message)
    handler = {'/start_smm': start_smm, '/stop_smm': stop_smm, '/auto_smm_settings': auto_smm_settings}.get(command)
    if not handler:
        return
    try:
        handler(message)
    except Exception:
        logger.exception('[AutoSMM] Command %s failed', command)
        _show(message.chat.id, '❌ Ошибка AutoSMM. Причина записана в журнал Starvell Cardinal.')


def post_init(c: Any) -> None:
    global _READY
    if _READY:
        return
    _READY = True
    try:
        recovered = _recover_submissions()
        for text in list(_pending_admin):
            _notify_admins(text)
        _pending_admin.clear()
        if recovered:
            _notify_admins(f'⚠️ AutoSMM: после перезапуска {recovered} заказ(ов) требуют проверки — откройте «⚠️ Проверить заказы».')
        if load_config()['auto_start']:
            _workers(c)
        logger.info('[AutoSMM] Started on Starvell. Lots: %s, waiting: %s.', len(load_config()['lot_mapping']), len(waiting))
    except Exception:
        logger.exception('[AutoSMM] Post-init failed')


# ---------------------------------------------------------------------------
# Привязка к Starvell Cardinal
# ---------------------------------------------------------------------------

def _attach(service: Any = None, aio_bot: Any = None) -> None:
    global bot, cardinal_instance
    if service is not None:
        _SV['service'] = service
    if aio_bot is not None and (_SV.get('bot') is not aio_bot or bot is None):
        _SV['bot'] = aio_bot
        bot = _BotBridge(aio_bot)
        if cardinal_instance is None:
            cardinal_instance = _StarvellCardinal(_TelegramAdapter(bot))
        else:
            cardinal_instance.telegram = cardinal_instance.telegram or _TelegramAdapter(bot)
            cardinal_instance.telegram.bot = bot
    if cardinal_instance is None:
        cardinal_instance = _StarvellCardinal(None)
    try:
        loop = asyncio.get_running_loop()
        if _SV.get('loop') is None:
            _SV['loop'], _SV['loop_thread'] = loop, threading.get_ident()
    except RuntimeError:
        pass


def _bootstrap() -> None:
    global waiting
    load_config()
    with _wlock:
        waiting = _load_waiting()
    _orders_raw()
    for attempt in range(5):
        try:
            info = _run(_service().get_user_info())
            user = (info or {}).get('user') or {}
            if user.get('id'):
                _SV['me'], _SV['me_name'] = str(user['id']), str(user.get('username') or '')
                break
        except Exception as exc:
            logger.warning('[AutoSMM] Starvell user info unavailable: %s', type(exc).__name__)
        time.sleep(2 + attempt * 2)
    if not _SV.get('me'):
        _admin_later('⚠️ AutoSMM: не удалось получить профиль Starvell — проверьте session_cookie. Заказы начнут обрабатываться после входа.')
        threading.Thread(target=_retry_bootstrap, daemon=True, name='autosmm-login').start()
        return
    post_init(cardinal_instance)


def _retry_bootstrap() -> None:
    for _ in range(120):
        time.sleep(30)
        try:
            info = _run(_service().get_user_info())
            user = (info or {}).get('user') or {}
            if user.get('id'):
                _SV['me'], _SV['me_name'] = str(user['id']), str(user.get('username') or '')
                post_init(cardinal_instance)
                return
        except Exception:
            continue


def _install_plugin_card_button() -> bool:
    """Карточка плагина в Starvell Cardinal не умеет кнопку настроек — добавляем свою только для AutoSMM."""
    try:
        import importlib
        cp = importlib.import_module('bot.plugins.cp')
    except Exception:
        return False
    original = getattr(cp, 'edit_plugin', None)
    if not callable(original):
        return False
    if getattr(original, '_autosmm', False):
        return True

    def edit_plugin(plugin_data: Any, CBT: Any, uuid: str, offset: int, ask_delete: bool = False, *args: Any, **kwargs: Any) -> Any:
        keyboard = original(plugin_data, CBT, uuid, offset, ask_delete, *args, **kwargs)
        if str(uuid) != UUID or ask_delete:
            return keyboard
        try:
            from aiogram.types import InlineKeyboardButton as AioButton, InlineKeyboardMarkup as AioMarkup
            rows = [[AioButton(text='⚙️ Настройки AutoSMM', callback_data='autosmm:home')]] + list(keyboard.inline_keyboard)
            return AioMarkup(inline_keyboard=rows)
        except Exception:
            return keyboard

    edit_plugin._autosmm = True
    cp.edit_plugin = edit_plugin
    return True


async def _add_bot_commands(aio_bot: Any) -> None:
    """Ядро задаёт свой список команд — дописываем в него команду AutoSMM, чтобы она была в подсказках."""
    if aio_bot is None:
        return
    try:
        from aiogram.types import BotCommand
        current = list(await aio_bot.get_my_commands())
        if not any(getattr(c, 'command', '') == 'auto_smm_settings' for c in current):
            await aio_bot.set_my_commands(current + [BotCommand(command='auto_smm_settings', description='🚀 AutoSMM: настройки')])
    except Exception as exc:
        logger.warning('[AutoSMM] Cannot add bot command: %s', type(exc).__name__)


async def on_init(aio_bot: Any = None, starvell: Any = None, db: Any = None, plugin_manager: Any = None, *args: Any, **kwargs: Any) -> None:
    """BIND_TO_INIT: ядро и сервис Starvell готовы."""
    logger.info('[AutoSMM] Initialising plugin %s for Starvell Cardinal', VERSION)
    _SV['loop'], _SV['loop_thread'] = asyncio.get_running_loop(), threading.get_ident()
    _attach(starvell, aio_bot)
    if _install_plugin_card_button():
        logger.info('[AutoSMM] Settings button added to the plugin card')
    await _add_bot_commands(aio_bot)
    await asyncio.to_thread(_bootstrap)


async def on_stop(*args: Any, **kwargs: Any) -> None:
    _stop_workers()


async def on_new_order(order_data: Any, starvell_service: Any = None, *args: Any, **kwargs: Any) -> None:
    """BIND_TO_NEW_ORDER: новый заказ или смена статуса."""
    _attach(starvell_service, kwargs.get('bot'))
    if not isinstance(order_data, dict):
        return
    order_id = str(order_data.get('id') or '').strip()
    status = str(order_data.get('status') or 'CREATED').upper()
    if not order_id:
        return
    if status in ('REFUND', 'REFUNDED'):
        _submit(_sv_status_changed, order_id)
    elif status in ('CREATED', 'PAID'):
        _submit(_sv_new_order, order_id)


def _sv_new_order(order_id: str) -> None:
    for attempt in range(60):
        if _READY:
            break
        time.sleep(2)
    order = None
    for attempt in range(3):
        try:
            order = _sv_order(order_id)
            break
        except Exception as exc:
            logger.warning('[AutoSMM] Order %s details unavailable: %s', order_id, exc)
            time.sleep(2 + attempt * 3)
    if order is None:
        if load_config()['lot_mapping']:
            _notify_admins(f'⚠️ AutoSMM: не удалось получить данные заказа #{order_id} со Starvell. Если это SMM-заказ — '
                           f'обработайте его вручную.\n{_order_url(order_id)}')
        return
    if order.status.name != 'PAID':
        return
    _handle_new_order(cardinal_instance, order)


def _sv_status_changed(order_id: str) -> None:
    event = SimpleNamespace(order=SimpleNamespace(id=order_id, status=SimpleNamespace(name='REFUNDED')))
    on_funpay_order_status_changed(cardinal_instance, event)


async def on_new_message(message_data: Any, starvell_service: Any = None, *args: Any, **kwargs: Any) -> None:
    """BIND_TO_NEW_MESSAGE: сообщение покупателя (свои сообщения ядро не передаёт)."""
    _attach(starvell_service, kwargs.get('bot'))
    if not isinstance(message_data, dict) or not _READY:
        return
    content = str(message_data.get('content') or message_data.get('text') or '').strip()
    if not content:
        return
    author = str(message_data.get('author') or message_data.get('author_id') or '').strip()
    username = str(message_data.get('author_username') or message_data.get('author_nickname') or '')
    if not author.isdigit():
        username, author = username or author, ''
    msg = SimpleNamespace(text=content, chat_id=message_data.get('chat_id'), author_id=author or None, author=username or None,
                          chat_name=None, interlocutor_id=None)
    await asyncio.to_thread(_on_message, cardinal_instance, msg)


def _wrap_message(message: Any) -> SimpleNamespace:
    return SimpleNamespace(text=getattr(message, 'text', '') or '', message_id=getattr(message, 'message_id', 0),
                           chat=SimpleNamespace(id=message.chat.id), from_user=SimpleNamespace(id=message.from_user.id))


def _wrap_callback(callback: Any) -> SimpleNamespace:
    msg = callback.message
    return SimpleNamespace(id=callback.id, data=callback.data, from_user=SimpleNamespace(id=callback.from_user.id),
                           message=SimpleNamespace(chat=SimpleNamespace(id=msg.chat.id), message_id=msg.message_id))


async def _tg_command(message: Any, **kwargs: Any) -> None:
    _attach(None, kwargs.get('bot'))
    _remember_admin(message.from_user.id)
    await asyncio.to_thread(_command_dispatch, _wrap_message(message))


async def _tg_callback(callback: Any, **kwargs: Any) -> None:
    _attach(None, kwargs.get('bot'))
    _remember_admin(callback.from_user.id)
    if not callback.message:
        await callback.answer('Сообщение устарело — откройте /auto_smm_settings.', show_alert=True)
        return
    await asyncio.to_thread(_on_callback, _wrap_callback(callback))


def _tg_text_filter(message: Any, **kwargs: Any) -> bool:
    """Ловим текст только когда плагин ждёт ввод; команды бота проходят дальше."""
    adapter = getattr(cardinal_instance, 'telegram', None)
    if adapter is None or getattr(message, 'from_user', None) is None:
        return False
    state = adapter.get_state(message.chat.id, message.from_user.id)
    if not state or not str(state.get('state', '')).startswith('autosmm:'):
        return False
    text = str(getattr(message, 'text', '') or '')
    if text.startswith('/'):
        adapter.clear_state(message.chat.id, message.from_user.id)
        return False
    return True


async def _tg_text(message: Any, **kwargs: Any) -> None:
    _remember_admin(message.from_user.id)
    await asyncio.to_thread(_input, _wrap_message(message))


COMMANDS = {
    'auto_smm_settings': {'handler': _tg_command, 'description': '🚀 AutoSMM: настройки', 'filters': [Command('auto_smm_settings')]},
    'start_smm': {'handler': _tg_command, 'description': '▶️ AutoSMM: запустить фон', 'filters': [Command('start_smm')]},
    'stop_smm': {'handler': _tg_command, 'description': '⏸ AutoSMM: остановить фон', 'filters': [Command('stop_smm')]},
}
CALLBACKS = {'autosmm': {'handler': _tg_callback, 'filter': F.data.startswith('autosmm:')}}
TEXT_HANDLERS = {'autosmm_input': {'handler': _tg_text, 'filter': _tg_text_filter}}

BIND_TO_INIT = [on_init]
BIND_TO_STOP = [on_stop]
BIND_TO_NEW_ORDER = [on_new_order]
BIND_TO_NEW_MESSAGE = [on_new_message]
BIND_TO_DELETE = None
