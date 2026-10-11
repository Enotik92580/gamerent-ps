"""Имитация FunPay Cardinal, FunPayAPI, Telegram и Roblox для автотестов плагина."""
import enum
import importlib.util
import json
import os
import re
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_PATH = os.path.join(os.path.dirname(HERE), "AutoVipRoblox_LuckyFunpay.py")


# --- модули, которые в реальности даёт Cardinal ------------------------------
class InlineKeyboardButton:
    def __init__(self, text, callback_data=None, url=None):
        self.text, self.callback_data, self.url = text, callback_data, url


class InlineKeyboardMarkup:
    def __init__(self, row_width=3):
        self.keyboard = []

    def row(self, *buttons):
        self.keyboard.append(list(buttons))
        return self

    def add(self, *buttons):
        return self.row(*buttons)

    def buttons(self):
        return [b for row in self.keyboard for b in row]


class OrderStatuses(enum.Enum):
    PAID = 0
    CLOSED = 1
    REFUNDED = 2


def install_fake_modules():
    telebot = types.ModuleType("telebot")
    telebot_types = types.ModuleType("telebot.types")
    telebot_types.InlineKeyboardButton = InlineKeyboardButton
    telebot_types.InlineKeyboardMarkup = InlineKeyboardMarkup
    telebot.types = telebot_types
    tg_bot = types.ModuleType("tg_bot")
    tg_bot.CBT = types.SimpleNamespace(PLUGINS_LIST="40", PLUGIN_SETTINGS="46", EDIT_PLUGIN="41")
    fp = types.ModuleType("FunPayAPI")
    fp_common = types.ModuleType("FunPayAPI.common")
    fp_enums = types.ModuleType("FunPayAPI.common.enums")
    fp_enums.OrderStatuses = OrderStatuses
    fp_enums.MessageTypes = types.SimpleNamespace(NEW_FEEDBACK=1, FEEDBACK_CHANGED=2)
    fp_updater = types.ModuleType("FunPayAPI.updater")
    fp_events = types.ModuleType("FunPayAPI.updater.events")
    fp_events.NewMessageEvent = object
    fp_events.NewOrderEvent = object
    sys.modules.update({"telebot": telebot, "telebot.types": telebot_types, "tg_bot": tg_bot,
                        "tg_bot.static_keyboards": types.SimpleNamespace(CLEAR_STATE_BTN=lambda: None),
                        "FunPayAPI": fp, "FunPayAPI.common": fp_common, "FunPayAPI.common.enums": fp_enums,
                        "FunPayAPI.updater": fp_updater, "FunPayAPI.updater.events": fp_events})


def load_module(path, name):
    install_fake_modules()
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- Telegram -----------------------------------------------------------------
class Msg:
    _next = 100

    def __init__(self, chat_id, text, user_id=1, message_id=None):
        Msg._next += 1
        self.chat = types.SimpleNamespace(id=chat_id)
        self.from_user = types.SimpleNamespace(id=user_id)
        self.text = text
        self.message_id = message_id or Msg._next


class FakeBot:
    def __init__(self):
        self.sent = []  # (chat_id, text, markup)

    def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw.get("reply_markup")))
        return Msg(chat_id, text)

    def edit_message_text(self, text, chat_id, message_id, **kw):
        self.sent.append((chat_id, text, kw.get("reply_markup")))

    def reply_to(self, message, text, **kw):
        self.sent.append((message.chat.id, text, None))

    def answer_callback_query(self, *a, **kw):
        pass

    def delete_message(self, *a, **kw):
        pass

    def last(self):
        return self.sent[-1]

    def texts(self):
        return [t for _, t, _ in self.sent]


class FakeTG:
    def __init__(self):
        self.bot = FakeBot()
        self.authorized_users = [1]
        self.states = {}
        self.msg_handlers, self.cbq_handlers = [], []

    def msg_handler(self, handler, **kw):
        self.msg_handlers.append((handler, kw))

    def cbq_handler(self, handler, func, **kw):
        self.cbq_handlers.append((handler, func))

    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.states[(chat_id, user_id)] = {"state": state, "mid": message_id, "data": data or {}}

    def get_state(self, chat_id, user_id):
        return self.states.get((chat_id, user_id))

    def clear_state(self, chat_id, user_id, del_msg=False):
        self.states.pop((chat_id, user_id), None)

    # имитация действий администратора
    def click(self, data, chat_id=1):
        call = types.SimpleNamespace(id="1", data=data, from_user=types.SimpleNamespace(id=1),
                                     message=Msg(chat_id, "", message_id=5))
        for handler, func in self.cbq_handlers:
            if func(call):
                handler(call)
                return
        raise AssertionError("нет обработчика для " + data)

    def type(self, text, chat_id=1):
        message = Msg(chat_id, text)
        for handler, kw in self.msg_handlers:
            if "commands" in kw:
                if text.startswith("/") and text[1:].split()[0] in kw["commands"]:
                    handler(message)
                    return
                continue
            if kw.get("func") and kw["func"](message):
                handler(message)
                return

    def buttons(self):
        markup = self.bot.last()[2]
        return [] if markup is None else markup.buttons()

    def button(self, label_part):
        for chat, text, markup in reversed(self.bot.sent):
            if markup is None:
                continue
            for button in markup.buttons():
                if label_part in button.text:
                    return button.callback_data
        raise AssertionError("нет кнопки " + label_part)


# --- FunPay ------------------------------------------------------------------
class FakeLotFields:
    def __init__(self, lot_id, title, active=True, price=10.0):
        self.lot_id = lot_id
        self.title_ru = title
        self.active = active
        self.price = price
        self.fields = {"offer_id": str(lot_id), "csrf_token": "x", "fields[summary][ru]": title}

    def renew_fields(self):
        self.fields["active"] = "on" if self.active else ""
        self.fields["price"] = str(self.price)
        return self


OFFER_FORM = """
<form class="form-offer-editor" action="https://funpay.com/lots/offerSave">
<input type="hidden" name="csrf_token" value="tok"><input type="hidden" name="offer_id" value="0">
<input type="hidden" name="node_id" value="402">
<div class="form-group"><label class="control-label">Игра</label>
 <select name="fields[game]"><option value=""></option><option value="1">Adopt Me</option><option value="99">[Прочие]</option></select></div>
<div class="form-group"><label>Название игры</label><input name="fields[game_name][ru]" value=""><input name="fields[game_name][en]" value=""></div>
<div class="form-group"><label>Тип</label><select name="fields[type]"><option value="">-</option><option value="5">Предметы</option><option value="7">Услуги</option></select></div>
<div class="form-group"><label>Краткое описание</label><input name="fields[summary][ru]"><input name="fields[summary][en]"></div>
<div class="form-group"><label>Подробное описание</label><textarea name="fields[desc][ru]"></textarea><textarea name="fields[desc][en]"></textarea></div>
<div class="form-group"><label>Цена</label><input name="price" value=""></div>
<div class="form-group"><label>Наличие</label><input name="amount" value=""></div>
<div class="form-group"><input type="checkbox" name="active" checked></div>
</form>"""


class Response:
    def __init__(self, text="", data=None):
        self.text = text
        self._data = data
        self.content = text.encode()

    def json(self):
        return self._data


class FakeAccount:
    def __init__(self):
        self.id = 999
        self.username = "seller"
        self.is_initiated = True
        self.currency = "₽"
        self.messages = []  # (chat_id, text)
        self.lots = {}
        self.saved = []
        self.refunds = []
        self.orders = {}
        self.offers = {}  # id -> fields
        self.deleted_offers = []
        self.next_offer = 5000

    def send_message(self, chat_id, text, chat_name=None, *a, **kw):
        self.messages.append((str(chat_id), text))
        return types.SimpleNamespace(id=len(self.messages))

    def get_lot_fields(self, lot_id):
        if lot_id in self.offers:
            lf = FakeLotFields(lot_id, self.offers[lot_id]["fields[summary][ru]"])
            lf.fields = dict(self.offers[lot_id])
            return lf
        if lot_id not in self.lots:
            raise RuntimeError("not found")
        return self.lots[lot_id]

    def save_lot(self, fields):
        self.saved.append((fields.lot_id, fields.active, fields.price))

    def refund(self, order_id):
        self.refunds.append(order_id)

    def get_order(self, order_id):
        return self.orders[order_id]

    def delete_lot(self, lot_id):
        self.deleted_offers.append(int(lot_id))
        self.offers.pop(int(lot_id), None)

    def method(self, request_method, api_method, headers, payload, exclude_phpsessid=False, raise_not_200=False):
        if api_method.startswith("lots/offerEdit?node="):
            return Response(OFFER_FORM)
        if api_method == "lots/offerSave":
            assert payload.get("fields[game]") == "99", payload
            assert payload.get("fields[type]") == "7", payload
            self.next_offer += 1
            self.offers[self.next_offer] = dict(payload)
            return Response(data={"done": True, "url": f"https://funpay.com/lots/offer?id={self.next_offer}"})
        raise AssertionError("unexpected " + api_method)

    def chat_texts(self, chat_id):
        return [t for c, t in self.messages if c == str(chat_id)]


class FakeCardinal:
    def __init__(self, uuid):
        self.account = FakeAccount()
        self.telegram = FakeTG()
        self.plugins = {uuid: types.SimpleNamespace(enabled=True)}
        self.blacklist = []
        self.old_mode_enabled = False

    def send_message(self, chat_id, text, chat_name=None, *a, **kw):
        return [self.account.send_message(chat_id, text, chat_name)]

    def add_telegram_commands(self, uuid, commands):
        self.commands = commands


def order(order_id, description, buyer_id=111, chat_id=111, amount=1, price=100.0, buyer="buyer"):
    return types.SimpleNamespace(id=order_id, description=description, buyer_id=buyer_id, buyer_username=buyer,
                                 chat_id=chat_id, amount=amount, price=price, status=OrderStatuses.PAID)


# --- Roblox --------------------------------------------------------------------
class World:
    """Состояние «Roblox»: серверы, пользователи, заявки в друзья."""

    def __init__(self):
        self.cookies = {}       # cookie -> (uid, name)
        self.servers = {}       # sid -> dict(owner, active, code, place, users)
        self.users = {}         # name -> uid
        self.requests = set()   # (owner_uid, from_uid)
        self.friends = set()    # (owner_uid, uid)
        self.fail_deactivate = False
        self.calls = []

    def add_server(self, sid, owner_uid, place="109983668079237"):
        self.servers[str(sid)] = {"owner": owner_uid, "active": False, "code": "c0", "place": place, "users": set(),
                                  "n": 0}


def make_fake_api(plugin, world):
    RobloxError = plugin.RobloxError

    class FakeRobloxAPI(plugin.RobloxAPI):
        def request(self, method, url, write=False, **kw):
            world.calls.append((method, url))
            return Response(data={})

        def _me(self):
            if self.cookie not in world.cookies:
                raise RobloxError("куки недействительны (401)", 401)
            return world.cookies[self.cookie]

        def _server(self, sid):
            server = world.servers.get(str(sid))
            if not server or server["owner"] != self._me()[0]:
                raise RobloxError("HTTP 403", 403)
            return server

        def _details(self, sid):
            s = self._server(sid)
            return {"id": int(sid), "active": s["active"], "joinCode": s["code"],
                    "game": {"rootPlace": {"id": int(s["place"])}},
                    "permissions": {"friendsAllowed": False, "users": [{"id": int(u)} for u in s["users"]]}}

        def call(self, method, url, write=False, **kw):
            world.calls.append((method, url, json.dumps(kw.get("json")) if kw.get("json") else None))
            body = kw.get("json") or {}
            if url.endswith("/v1/users/authenticated"):
                uid, name = self._me()
                return {"id": int(uid), "name": name}
            m = re.search(r"/v1/vip-servers/(\d+)/permissions$", url)
            if m:
                s = self._server(m.group(1))
                for u in body.get("usersToAdd", []):
                    s["users"].add(str(u))
                for u in body.get("usersToRemove", []):
                    s["users"].discard(str(u))
                return {}
            m = re.search(r"/v1/vip-servers/(\d+)$", url)
            if m:
                sid = m.group(1)
                s = self._server(sid)
                if method == "PATCH":
                    if "active" in body:
                        if world.fail_deactivate and body["active"] is False:
                            raise RobloxError("HTTP 500", 500)
                        s["active"] = body["active"]
                    if body.get("newJoinCode"):
                        s["n"] += 1
                        s["code"] = f"code{sid}-{s['n']}"
                return self._details(sid)
            if "/private-servers" in url:
                return {"data": []}
            m = re.search(r"users\.roblox\.com/v1/users/(\d+)$", url)
            if m:
                for name, uid in world.users.items():
                    if uid == m.group(1):
                        return {"id": int(uid), "name": name}
                raise RobloxError("HTTP 404", 404)
            if url.endswith("/v1/usernames/users"):
                name = body["usernames"][0]
                uid = world.users.get(name)
                return {"data": [{"id": int(uid), "name": name}] if uid else []}
            m = re.search(r"/v1/users/(\d+)/friends/statuses", url)
            if m:
                owner, uid = m.group(1), str(kw["params"]["userIds"])
                status = "Friends" if (owner, uid) in world.friends else (
                    "RequestReceived" if (owner, uid) in world.requests else "NotFriends")
                return {"data": [{"id": int(uid), "status": status}]}
            m = re.search(r"/v1/users/(\d+)/accept-friend-request", url)
            if m:
                owner = self._me()[0]
                if (owner, m.group(1)) not in world.requests:
                    raise RobloxError("HTTP 400", 400)
                world.requests.discard((owner, m.group(1)))
                world.friends.add((owner, m.group(1)))
                return {}
            m = re.search(r"/v1/users/(\d+)/unfriend", url)
            if m:
                world.friends.discard((self._me()[0], m.group(1)))
                return {}
            raise AssertionError("unexpected Roblox call " + url)

    return FakeRobloxAPI
