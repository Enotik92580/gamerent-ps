"""Тестовый стенд для плагинов LuckyFunpay.

Используется настоящий FunPayAPI из исходников FunPay Cardinal (путь в переменной FPC_PATH)
и настоящие типы telebot, а сеть FunPay и Telegram заменены имитацией в памяти.
"""
from __future__ import annotations

import datetime
import importlib.util
import itertools
import json
import os
import sys
import types as pytypes
from pathlib import Path

FPC_PATH = os.environ.get("FPC_PATH") or str(Path(__file__).resolve().parents[2] / "FunPayCardinal")
if FPC_PATH not in sys.path:
    sys.path.insert(0, FPC_PATH)

import FunPayAPI  # noqa: E402
from FunPayAPI import types as fpt  # noqa: E402
from FunPayAPI.common import enums, exceptions  # noqa: E402
from FunPayAPI.updater import events  # noqa: E402

PLUGINS = Path(__file__).resolve().parents[1] / "plugins"
ADMIN = 42


def load_plugin(file_name, workdir):
    """Загружает плагин как это делает Cardinal (importlib из файла), рабочая папка = workdir."""
    os.chdir(workdir)
    name = "lf_" + file_name.replace(".py", "")
    spec = importlib.util.spec_from_file_location(name, PLUGINS / file_name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------------------- FunPay
def make_categories():
    games = {}
    data = [(1, "Dota 2", [(101, "Предметы"), (102, "Аккаунты"), (103, "Услуги")]),
            (2, "World of Warcraft", [(201, "Аккаунты"), (202, "Услуги")]),
            (3, "Steam", [(301, "Ключи")])]
    subcats = {}
    for gid, name, subs in data:
        game = fpt.Category(gid, name)
        for sid, sname in subs:
            sub = fpt.SubCategory(sid, sname, enums.SubCategoryTypes.COMMON, game)
            game.add_subcategory(sub)
            subcats[sid] = sub
        games[gid] = game
    chip_game = games[2]
    chip = fpt.SubCategory(2001, "Золото", enums.SubCategoryTypes.CURRENCY, chip_game)
    chip_game.add_subcategory(chip)
    subcats[2001] = chip
    return games, subcats


class FakeResponse:
    def __init__(self, text="", status_code=200, data=None):
        self.text = text if data is None else json.dumps(data)
        self.content = self.text.encode()
        self.status_code = status_code
        self._data = data
        self.request = pytypes.SimpleNamespace(url="https://funpay.com/test", headers={}, body="", method="POST")

    def json(self):
        return self._data if self._data is not None else json.loads(self.text)


class FakeAccount:
    """Имитация FunPayAPI.Account с хранилищем лотов, заказов и чатов."""

    def __init__(self):
        self.id = 777
        self.username = "LuckySeller"
        self.golden_key = "g" * 32
        self.user_agent = "test"
        self.proxy = None
        self.csrf_token = "csrf"
        self.currency = enums.Currency.RUB
        self.locale = "ru"
        self.is_initiated = True
        self.games, self.subcats = make_categories()
        self.lots: dict[int, dict] = {}
        self.lot_nodes: dict[int, int] = {}
        self.chips: dict[int, dict] = {}
        self.orders: dict[str, fpt.Order] = {}
        self.sales: list[fpt.OrderShortcut] = []
        self.sent: list[tuple] = []
        self.refunded: list[str] = []
        self.deleted: list[int] = []
        self.saved: list[dict] = []
        self.raised: list[int] = []
        self.raise_wait: dict[int, int] = {}
        self.http: dict[str, object] = {}
        self.fail_save: set[int] = set()
        self.silent_inactive: set[int] = set()
        self.next_lot_id = 90000
        self.page_size = 3
        self.calls = []

    # --- категории
    def get_category(self, cid):
        return self.games.get(cid)

    @property
    def categories(self):
        return list(self.games.values())

    def get_subcategory(self, typ, sid):
        sub = self.subcats.get(sid)
        return sub if sub and sub.type == typ else None

    def get_sorted_subcategories(self):
        res = {enums.SubCategoryTypes.COMMON: {}, enums.SubCategoryTypes.CURRENCY: {}}
        for s in self.subcats.values():
            res[s.type][s.id] = s
        return res

    # --- лоты
    def add_lot(self, node, title, price=100.0, active=True, secrets=None, amount=None, desc="Описание",
                title_en="", lot_id=None, auto=None, extra=None):
        lot_id = lot_id or self.next_lot_id
        self.next_lot_id = max(self.next_lot_id, lot_id) + 1
        fields = {"csrf_token": "csrf", "offer_id": str(lot_id), "node_id": str(node), "location": "",
                  "fields[summary][ru]": title, "fields[summary][en]": title_en,
                  "fields[desc][ru]": desc, "fields[desc][en]": "", "fields[payment_msg][ru]": "",
                  "fields[payment_msg][en]": "", "fields[images]": "11,12", "price": str(price),
                  "active": "on" if active else "", "deactivate_after_sale": ""}
        if secrets is not None:
            fields["secrets"] = "\n".join(secrets)
            fields["auto_delivery"] = "on" if auto is not False else ""
        elif auto:
            fields["secrets"] = ""
            fields["auto_delivery"] = "on"
        if amount is not None:
            fields["amount"] = str(amount)
        fields.update(extra or {})
        self.lots[lot_id] = fields
        self.lot_nodes[lot_id] = node
        return lot_id

    def get_lot_fields(self, lot_id, node_id=None):
        self.calls.append(("get_lot_fields", lot_id))
        if not lot_id:
            fields = {"csrf_token": "csrf", "offer_id": "0", "node_id": str(node_id), "fields[summary][ru]": "",
                      "fields[summary][en]": "", "fields[desc][ru]": "", "fields[desc][en]": "", "price": "",
                      "active": "on"}
            return fpt.LotFields(0, fields, self.subcats.get(node_id), enums.Currency.RUB, None, None)
        if lot_id not in self.lots:
            raise exceptions.LotParsingError(FakeResponse("", 200), "Лот не найден", lot_id)
        fields = dict(self.lots[lot_id])
        node = int(fields["node_id"])
        return fpt.LotFields(lot_id, fields, self.subcats.get(node), enums.Currency.RUB, None, None)

    def save_lot(self, lot_fields, locale=None):
        lot_fields.csrf_token = self.csrf_token
        fields = dict(lot_fields.renew_fields().fields)
        lot_id = int(fields.get("offer_id") or 0)
        self.saved.append(fields)
        if lot_id in self.fail_save:
            raise exceptions.LotSavingError(FakeResponse(data={"error": 1}), "Ошибка сохранения", lot_id, {})
        if fields.get("deleted") == "1":
            self.lots.pop(lot_id, None)
            self.deleted.append(lot_id)
            return
        if not lot_id:
            lot_id = self.next_lot_id
            self.next_lot_id += 1
            fields["offer_id"] = str(lot_id)
            self.lot_nodes[lot_id] = int(fields["node_id"])
        if lot_id in self.silent_inactive:
            fields["active"] = ""
        clean = {k: ("" if v is None else str(v)) for k, v in fields.items()}
        self.lots[lot_id] = clean
        return

    def delete_lot(self, lot_id):
        self.save_lot(fpt.LotFields(lot_id, {"csrf_token": self.csrf_token, "offer_id": lot_id, "deleted": "1"}))

    def _shortcut(self, lot_id, cls=fpt.MyLotShortcut):
        f = self.lots[lot_id]
        sub = self.subcats[int(f["node_id"])]
        amount = None
        if f.get("auto_delivery") == "on":
            amount = len([s for s in f.get("secrets", "").split("\n") if s])
        elif f.get("amount"):
            amount = int(f["amount"])
        if cls is fpt.MyLotShortcut:
            return fpt.MyLotShortcut(lot_id, None, None, f["fields[summary][ru]"], amount, float(f["price"]),
                                     enums.Currency.RUB, sub, f.get("auto_delivery") == "on", f.get("active") == "on",
                                     "")
        return fpt.LotShortcut(lot_id, None, None, f["fields[summary][ru]"], amount, float(f["price"]),
                               enums.Currency.RUB, sub, None, f.get("auto_delivery") == "on", None, None, "")

    def get_my_subcategory_lots(self, subcategory_id, locale=None):
        self.calls.append(("get_my_subcategory_lots", subcategory_id))
        return [self._shortcut(i) for i, f in self.lots.items() if int(f["node_id"]) == subcategory_id]

    def get_user(self, user_id, locale=None):
        profile = fpt.UserProfile(user_id, self.username, "", True, False, "")
        if user_id == self.id:
            for lot_id, f in self.lots.items():
                if f.get("active") == "on":
                    profile.add_lot(self._shortcut(lot_id, fpt.LotShortcut))
            for sid, chip in self.chips.items():
                if any(v == "on" for k, v in chip.items() if k.endswith("[active]")):
                    sub = self.subcats[sid]
                    profile.add_lot(fpt.LotShortcut(f"{self.id}-{sub.category.id}-{sid}-1", "S1", None, "Золото",
                                                    100, 1.0, enums.Currency.RUB, sub, None, False, None, None, ""))
        return profile

    # --- валюта (chips)
    def add_chip(self, sid, offers):
        fields = {"csrf_token": "csrf", "game": str(self.subcats[sid].category.id), "chip": str(sid),
                  "options[chip_min_sum]": ""}
        for key, (active, price, amount) in offers.items():
            fields[f"offers[{key}][price]"] = str(price)
            fields[f"offers[{key}][amount]"] = str(amount)
            if active:
                fields[f"offers[{key}][active]"] = "on"
        self.chips[sid] = fields

    def get_chip_fields(self, subcategory_id):
        return fpt.ChipFields(self.id, subcategory_id, dict(self.chips[subcategory_id]))

    def save_chip(self, chip_fields, locale=None):
        chip_fields.csrf_token = self.csrf_token
        self.chips[chip_fields.subcategory_id] = dict(chip_fields.renew_fields().fields)

    def save_offer(self, offer_fields, locale=None):
        if isinstance(offer_fields, fpt.ChipFields):
            return self.save_chip(offer_fields)
        return self.save_lot(offer_fields)

    # --- заказы
    def add_order(self, oid, buyer="Buyer1", buyer_id=500, price=100.0, currency=enums.Currency.RUB,
                  status=enums.OrderStatuses.PAID, subcat=101, desc="Тестовый товар", stars=None,
                  date=None, review_text="", secrets=None, amount=1):
        sub = self.subcats[subcat]
        date = date or datetime.datetime.now()
        short = fpt.OrderShortcut(oid, desc, price, currency, buyer, buyer_id, f"users-{buyer_id}-{self.id}",
                                  status, date, f"{sub.category.name}, {sub.name}", sub, "")
        review = fpt.Review(stars, review_text, None, False, "", False, oid, buyer, buyer_id) if stars else None
        fields = {"summary": fpt.LotField("summary", {"ru": desc, "en": desc}, "Краткое описание")}
        order = fpt.Order(oid, status, sub, None, None, fields, amount, price, currency, None, buyer_id, buyer,
                          self.id, self.username, f"users-{buyer_id}-{self.id}", review, secrets or [], "ru")
        self.orders[oid] = order
        self.sales.insert(0, short)
        return short

    def set_status(self, oid, status):
        self.orders[oid].status = status
        for s in self.sales:
            if s.id == oid:
                s.status = status

    def set_review(self, oid, stars, text="ok"):
        order = self.orders[oid]
        order.review = fpt.Review(stars, text, None, False, "", False, oid, order.buyer_username,
                                  order.buyer_id) if stars else None

    def get_order(self, order_id, *args, **kwargs):
        self.calls.append(("get_order", order_id))
        if order_id not in self.orders:
            raise exceptions.RequestFailedError(FakeResponse("", 404))
        return self.orders[order_id]

    def get_sales(self, start_from=None, include_paid=True, include_closed=True, include_refunded=True,
                  exclude_ids=None, id=None, buyer=None, state=None, game=None, section=None, server=None,
                  side=None, locale=None, subcategories=None, **more):
        self.calls.append(("get_sales", start_from, state))
        items = [s for s in self.sales if (state is None or s.status.name.lower() == state)
                 and (include_paid or s.status != enums.OrderStatuses.PAID)
                 and (include_closed or s.status != enums.OrderStatuses.CLOSED)
                 and (include_refunded or s.status != enums.OrderStatuses.REFUNDED)]
        start = 0
        if start_from:
            ids = [s.id for s in items]
            start = ids.index(start_from) if start_from in ids else len(items)
        page = items[start:start + self.page_size]
        nxt = items[start + self.page_size].id if start + self.page_size < len(items) else None
        subs = {f"{s.category.name}, {s.name}": s for s in
                [o.subcategory for o in self.sales if o.subcategory] + list(getattr(self, "sales_subs", []))}
        return nxt, page, "ru", subs if not start_from else None

    def refund(self, order_id):
        self.refunded.append(order_id)
        if order_id in self.orders:
            self.set_status(order_id, enums.OrderStatuses.REFUNDED)

    def send_message(self, chat_id, text=None, chat_name=None, interlocutor_id=None, image_id=None,
                     add_to_ignore_list=True, update_last_saved_message=False, leave_as_unread=False):
        self.sent.append((chat_id, text, chat_name))
        msg = fpt.Message(len(self.sent), text, chat_id, chat_name, None, self.username, self.id, "")
        msg.by_bot = True
        return msg

    def send_image(self, chat_id, image, chat_name=None, *args, **kwargs):
        self.sent.append((chat_id, f"[image {image}]", chat_name))
        return fpt.Message(len(self.sent), None, chat_id, chat_name, None, self.username, self.id, "")

    def upload_image(self, image, type_="chat"):
        return 555

    def get_chat_by_name(self, name, make_request=False):
        return next((c for c in getattr(self, "chats", {}).values() if c.name == name), None)

    def get_chats(self, update=False):
        return dict(getattr(self, "chats", {}))

    def add_chat(self, chat_id, name, text="привет"):
        self.chats = getattr(self, "chats", {})
        self.chats[chat_id] = fpt.ChatShortcut(chat_id, name, text, 0, 0, "", True)
        return self.chats[chat_id]

    def get_chat(self, chat_id, with_history=True, locale=None):
        return fpt.Chat(chat_id, "Buyer1", None, None, [])

    def get_chat_history(self, chat_id, last_message_id=None, interlocutor_username=None, from_id=0):
        return getattr(self, "history", {}).get(chat_id, [])

    def raise_lots(self, category_id, subcategories=None, exclude=None):
        wait = self.raise_wait.get(category_id)
        if wait:
            raise exceptions.RaiseError(FakeResponse(data={"error": 1}), self.games[category_id],
                                        "Подождите 2 часа.", wait)
        self.raised.append(category_id)
        return 14400

    def get_balance(self, lot_id=None):
        self.calls.append(("get_balance", lot_id))
        return getattr(self, "balance", fpt.Balance(0, 0, 0, 0, 0, 0))

    def get_subcategory_public_lots(self, subcategory_type, subcategory_id, locale=None):
        return [self._shortcut(i, fpt.LotShortcut) for i, f in self.lots.items()
                if int(f["node_id"]) == subcategory_id and f.get("active") == "on"]

    def method(self, request_method, api_method, headers, payload, exclude_phpsessid=False,
               raise_not_200=False, locale=None):
        self.calls.append(("method", request_method, api_method, payload))
        handler = None
        for key, value in self.http.items():
            if key in api_method:
                handler = value
                break
        if handler is None:
            return FakeResponse("", 404)
        res = handler(request_method, api_method, payload) if callable(handler) else handler
        if isinstance(res, FakeResponse):
            return res
        return FakeResponse(res) if isinstance(res, str) else FakeResponse(data=res)

    def get_exchange_rate(self, currency):
        return {enums.Currency.USD: (1 / 90, enums.Currency.RUB), enums.Currency.EUR: (1 / 100, enums.Currency.RUB)}.get(
            currency, (1, enums.Currency.RUB))


# ----------------------------------------------------------------------------- Telegram
class Obj(pytypes.SimpleNamespace):
    pass



# ----------------------------------------------------------------------------- проверки как у Telegram
import html.parser as _hp

TG_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "tg-spoiler",
           "blockquote", "span"}


class _TgHtml(_hp.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in TG_TAGS, f"Telegram не поддерживает тег <{tag}>"
        if tag == "a":
            href = dict(attrs).get("href", "")
            assert href.startswith(("http://", "https://", "tg://")), f"Плохая ссылка: {href!r}"
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, f"Незакрытый/лишний тег </{tag}>, стек {self.stack}"
        self.stack.pop()


def check_tg_html(text):
    assert len(text) <= 4096, f"Сообщение длиннее 4096: {len(text)}"
    raw = text
    import re as _re0
    assert not _re0.search(r"<(?![a-zA-Z/])", raw), f"Неэкранированный '<' в {raw[:200]!r}"
    assert not _re0.search(r"&(?!(lt|gt|amp|quot|#x27|#\d+);)", raw), f"Неэкранированный '&' в {raw[:200]!r}"
    parser = _TgHtml()
    parser.feed(raw)
    parser.close()
    assert not parser.stack, f"Незакрытые теги: {parser.stack} в {raw[:200]!r}"
    import re as _re
    for m in _re.finditer(r"&(#?\w+);?", raw):
        ent = m.group(0)
        assert ent in ("&lt;", "&gt;", "&amp;", "&quot;", "&#x27;") or _re.fullmatch(r"&#\d+;", ent), \
            f"Неподдерживаемая HTML-сущность {ent}"


def check_markup(markup):
    if markup is None or not hasattr(markup, "keyboard"):
        return
    for row in markup.keyboard:
        assert len(row) <= 8, "Слишком много кнопок в ряду"
        for b in row:
            text = getattr(b, "text", "")
            assert text and len(text) <= 120, f"Пустой/длинный текст кнопки {text!r}"
            data = getattr(b, "callback_data", None)
            url = getattr(b, "url", None)
            if data is not None:
                assert len(data.encode()) <= 64, f"callback_data > 64 байт: {data}"
            else:
                assert url and url.startswith(("http://", "https://", "tg://")), f"Плохой url кнопки {url!r}"


_BOT_IDS = itertools.count(1000)


class FakeBot:
    def __init__(self, token="1:main", username="main_bot"):
        self.token = token
        self.username = username
        self.sent = []
        self.edits = []
        self.deleted = []
        self.pinned = []
        self.documents = []
        self.answers = []
        self.files = {}
        self.topics = {}
        self.topic_edits = []
        self.chat_members = {}
        self.ids = _BOT_IDS
        self.handlers = []
        self.photos = []

    def get_me(self):
        return Obj(id=int(self.token.split(":")[0]), username=self.username, first_name=self.username)

    def _msg(self, chat_id, text, markup=None, thread=None):
        mid = next(self.ids)
        return Obj(message_id=mid, id=mid, chat=Obj(id=chat_id, type="private"), text=text, reply_markup=markup,
                   message_thread_id=thread)

    def send_message(self, chat_id, text, reply_markup=None, parse_mode=None, disable_web_page_preview=None,
                     message_thread_id=None, disable_notification=None, **kwargs):
        if parse_mode == "HTML":
            check_tg_html(text)
        assert len(text) <= 4096, len(text)
        check_markup(reply_markup)
        msg = self._msg(chat_id, text, reply_markup, message_thread_id)
        self.sent.append(Obj(chat_id=chat_id, text=text, markup=reply_markup, mid=msg.message_id,
                             thread=message_thread_id, parse_mode=parse_mode, silent=disable_notification))
        return msg

    def edit_message_text(self, text, chat_id=None, message_id=None, reply_markup=None, parse_mode=None,
                          disable_web_page_preview=None, **kwargs):
        if parse_mode == "HTML":
            check_tg_html(text)
        check_markup(reply_markup)
        self.edits.append(Obj(chat_id=chat_id, text=text, markup=reply_markup, mid=message_id))
        return Obj(message_id=message_id)

    def edit_message_reply_markup(self, chat_id=None, message_id=None, reply_markup=None, **kwargs):
        return True

    def answer_callback_query(self, callback_query_id, text=None, show_alert=None, **kwargs):
        self.answers.append((callback_query_id, text, show_alert))

    def delete_message(self, chat_id, message_id, *args, **kwargs):
        self.deleted.append((chat_id, message_id))

    def pin_chat_message(self, chat_id, message_id, disable_notification=None):
        self.pinned.append((chat_id, message_id))

    def send_document(self, chat_id, document, caption=None, visible_file_name=None, message_thread_id=None,
                      **kwargs):
        data = document.read() if hasattr(document, "read") else document
        name = visible_file_name or getattr(document, "name", "file")
        self.documents.append(Obj(chat_id=chat_id, data=data, caption=caption, name=name))
        return self._msg(chat_id, caption)

    def send_photo(self, chat_id, photo, caption=None, **kwargs):
        self.photos.append((chat_id, photo, caption, kwargs.get("message_thread_id")))
        return self._msg(chat_id, caption)

    def get_file(self, file_id):
        return Obj(file_path=file_id, file_size=len(self.files.get(file_id, b"")))

    def download_file(self, file_path):
        return self.files[file_path]

    def create_forum_topic(self, chat_id, name, icon_color=None, icon_custom_emoji_id=None):
        tid = next(self.ids)
        self.topics[tid] = (chat_id, name, icon_custom_emoji_id)
        return Obj(message_thread_id=tid, name=name)

    def edit_forum_topic(self, chat_id, message_thread_id, name=None, icon_custom_emoji_id=None):
        self.topic_edits.append((chat_id, message_thread_id, name, icon_custom_emoji_id))
        return True

    def get_forum_topic_icon_stickers(self):
        return [Obj(custom_emoji_id=str(9000 + i), emoji=e) for i, e in enumerate(["🔥", "❤️", "⭐️", "💰", "⚡️", "🎮", "💬", "❗️", "🛍", "🔴", "🟢"])]

    def get_chat(self, chat_id):
        return Obj(id=chat_id, type="supergroup", is_forum=True, username="syncgroup", invite_link=None,
                   title="Sync")

    def get_chat_member(self, chat_id, user_id):
        return self.chat_members.get(user_id, Obj(status="administrator", can_manage_topics=True))

    def send_sticker(self, *a, **k):
        return self._msg(a[0], "sticker")

    def message_handler(self, **kwargs):
        def deco(fn):
            self.handlers.append(("msg", kwargs, fn))
            return fn
        return deco

    def callback_query_handler(self, func, **kwargs):
        def deco(fn):
            self.handlers.append(("cbq", func, fn))
            return fn
        return deco


class FakeTG:
    def __init__(self):
        self.bot = FakeBot()
        self.authorized_users = {ADMIN: {}}
        self.msg_handlers = []
        self.cbq_handlers = []
        self.commands = {}
        self.notifications = []

    def msg_handler(self, handler, **kwargs):
        self.msg_handlers.append((handler, kwargs))

    def cbq_handler(self, handler, func, **kwargs):
        self.cbq_handlers.append((handler, func))

    def add_command_to_menu(self, command, help_text):
        self.commands[command] = help_text

    def send_notification(self, text, keyboard=None, notification_type="10", photo=None, pin=False):
        self.notifications.append((text, keyboard, notification_type))

    # --- имитация пользователя
    def message(self, text=None, chat_id=ADMIN, user_id=ADMIN, chat_type="private", document=None, thread=None,
                photo=None, sticker=None, caption=None, content_type=None, is_topic=None, from_bot=False):
        content = content_type or ("document" if document else "photo" if photo else "sticker" if sticker else "text")
        msg = Obj(message_id=next(self.bot.ids), chat=Obj(id=chat_id, type=chat_type, is_forum=thread is not None),
                  from_user=Obj(id=user_id, username="admin", is_bot=from_bot, first_name="Admin"), text=text,
                  document=document, content_type=content, message_thread_id=thread, photo=photo,
                  sticker=sticker, caption=caption, is_topic_message=bool(thread) if is_topic is None else is_topic,
                  date=0, reply_to_message=None)
        for handler, kwargs in self.msg_handlers:
            types_ = kwargs.get("content_types", ["text"])
            if content not in types_:
                continue
            commands = kwargs.get("commands")
            if commands:
                if not text or not text.startswith("/"):
                    continue
                cmd = text[1:].split()[0].split("@")[0]
                if cmd not in commands:
                    continue
            func = kwargs.get("func")
            if func and not func(msg):
                continue
            handler(msg)
            return True
        return False

    def screen(self, chat_id=ADMIN):
        """Последнее показанное сообщение в чате (новое или отредактированное)."""
        items = [m for m in self.bot.timeline if m.chat_id == chat_id]
        return items[-1] if items else None

    def texts(self, chat_id=ADMIN):
        return [m.text for m in self.bot.timeline if m.chat_id == chat_id]

    def buttons(self, msg=None):
        msg = msg or self.screen()
        if not msg or not msg.markup:
            return []
        return [b for row in msg.markup.keyboard for b in row]

    def click(self, label, msg=None, chat_id=ADMIN, user_id=ADMIN, exact=False):
        msg = msg or self.screen(chat_id)
        for b in self.buttons(msg):
            if (b.text == label) if exact else (label in b.text):
                if b.callback_data is None:
                    raise AssertionError(f"Кнопка {b.text!r} — ссылка {b.url}")
                return self.callback(b.callback_data, chat_id, user_id, mid=msg.mid)
        raise AssertionError(f"Нет кнопки {label!r}. Есть: {[b.text for b in self.buttons(msg)]}")

    def callback(self, data, chat_id=ADMIN, user_id=ADMIN, mid=1):
        call = Obj(id="cb", data=data, from_user=Obj(id=user_id),
                   message=Obj(chat=Obj(id=chat_id, type="private"), message_id=mid, id=mid))
        for handler, func in self.cbq_handlers:
            if func(call):
                handler(call)
                return True
        return False


class TimelineBot(FakeBot):
    """Бот, который записывает общий порядок отправок и правок."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.timeline = []

    def send_message(self, chat_id, text, **kwargs):
        msg = super().send_message(chat_id, text, **kwargs)
        self.timeline.append(self.sent[-1])
        return msg

    def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        res = super().edit_message_text(text, chat_id, message_id, **kwargs)
        self.timeline.append(self.edits[-1])
        return res


class FakeCardinal:
    def __init__(self, telegram=True):
        import configparser
        self.account = FakeAccount()
        self.telegram = FakeTG() if telegram else None
        if telegram:
            self.telegram.bot = TimelineBot()
        self.blacklist = []
        self.plugins = {}
        self.MAIN_CFG = configparser.ConfigParser()
        self.MAIN_CFG.read_dict({"FunPay": {"autoRaise": "1", "oldMsgGetMode": "0", "autoDelivery": "1"},
                                 "Other": {"watermark": "🐦"}, "Telegram": {"enabled": "1"}})
        self.AD_CFG = configparser.ConfigParser()
        self.RAW_AR_CFG = configparser.ConfigParser()
        self.raise_time = {}
        self.raised_time = {}
        self.profile = self.account.get_user(self.account.id)
        self.curr_profile = self.profile
        self.tg_profile = self.profile
        self.sent_messages = []
        self.post_lots_raise_handlers = []

    @property
    def old_mode_enabled(self):
        return self.MAIN_CFG["FunPay"].getboolean("oldMsgGetMode")

    @property
    def autoraise_enabled(self):
        return self.MAIN_CFG["FunPay"].getboolean("autoRaise")

    def refresh_profile(self):
        self.profile = self.curr_profile = self.tg_profile = self.account.get_user(self.account.id)

    def add_telegram_commands(self, uuid, commands):
        for cmd, desc, menu in commands:
            if menu and self.telegram:
                self.telegram.add_command_to_menu(cmd, desc)

    def send_message(self, chat_id, message_text, chat_name=None, interlocutor_id=None, attempts=3,
                     watermark=True):
        self.sent_messages.append((chat_id, message_text, chat_name, watermark))
        return [self.account.send_message(chat_id, message_text, chat_name)]

    def get_order_from_object(self, obj, order_id=None):
        import re
        if not order_id:
            found = re.findall(r"#[A-Z0-9]{8}", str(obj))
            if not found:
                return None
            order_id = found[0][1:]
        return self.account.orders.get(order_id)

    def get_exchange_rate(self, base_currency, target_currency, min_interval=60):
        rates = {"usd": 90.0, "eur": 100.0, "rub": 1.0}
        return rates[base_currency.code] / rates[target_currency.code]

    def get_balance(self, attempts=3):
        return self.account.get_balance(1)

    def run_handlers(self, handlers, args):
        for h in handlers:
            h(*args)


def new_order_event(short):
    return events.NewOrderEvent("tag", short)


def status_event(short):
    return events.OrderStatusChangedEvent("tag", short)


def message_event(text, chat_id="users-500-777", author="Buyer1", author_id=500, chat_name="Buyer1", by_bot=False):
    msg = fpt.Message(next(_mid), text, chat_id, chat_name, author_id, author, author_id, "")
    msg.by_bot = by_bot
    return events.NewMessageEvent("tag", msg)


_mid = itertools.count(1)


def system_message_event(text, chat_id="users-500-777", chat_name="Buyer1"):
    msg = fpt.Message(next(_mid), text, chat_id, chat_name, 500, "FunPay", 0, "")
    return events.NewMessageEvent("tag", msg)
