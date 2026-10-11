# -*- coding: utf-8 -*-
"""Every Telegram panel screen and input with a fake telebot."""
import sys
import time
import types
from types import SimpleNamespace

import pytest

from test_shop import P, run
from test_shop import env  # noqa: F401  pytest fixture


class Button:
    def __init__(self, text, url=None, callback_data=None):
        self.text, self.url, self.callback_data = text, url, callback_data


class Markup:
    def __init__(self):
        self.rows = []

    def row(self, *buttons):
        self.rows.append(buttons)


@pytest.fixture(autouse=True)
def fake_telebot(monkeypatch):
    telebot = types.ModuleType("telebot")
    telebot.types = types.ModuleType("telebot.types")
    telebot.types.InlineKeyboardButton, telebot.types.InlineKeyboardMarkup = Button, Markup
    monkeypatch.setitem(sys.modules, "telebot", telebot)
    monkeypatch.setitem(sys.modules, "telebot.types", telebot.types)
    cbt = SimpleNamespace(PLUGIN_SETTINGS="plugin_settings", PLUGINS_LIST="43", EDIT_PLUGIN="44")
    monkeypatch.setitem(sys.modules, "tg_bot", SimpleNamespace(CBT=cbt))


class Bot:
    def __init__(self):
        self.messages, self.markups, self.deleted, self.not_modified = [], [], [], False

    def send_message(self, chat, text, **kwargs):
        self.messages.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return SimpleNamespace(message_id=len(self.messages))

    def edit_message_text(self, text, chat, mid, **kwargs):
        if self.not_modified:
            raise RuntimeError("Bad Request: message is not modified")
        self.messages.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return SimpleNamespace(message_id=mid)

    def delete_message(self, chat, mid):
        self.deleted.append(mid)

    def answer_callback_query(self, *args):
        pass


@pytest.fixture
def ui(env):
    bot = Bot()
    env.shop.cardinal.telegram = SimpleNamespace(bot=bot, authorized_users={10})
    env.store.put("catalog", "main", {"products": env.api.catalog() + [
        {"id": "p2", "name": "PlayStation Network Turkey", "categoryName": "PlayStation",
         "items": [{"id": "c", "name": "500 TL", "price": "15", "currency": "USD", "inStock": 0}]}]})
    panel = P.TelegramUI(env.shop)
    return SimpleNamespace(panel=panel, bot=bot, env=env)


def labels(ui):
    return [b.text for row in ui.bot.markups[-1].rows for b in row]


ACTIONS = [("home",), ("api",), ("balance", "api"), ("balance", "home"), ("proxy_mode",), ("proxy_mode",), ("catalog",),
           ("refresh_catalog",), ("lots", 0), ("lot", "100"), ("suggest", "100", 0), ("results", "100", "steam", "search", 0),
           ("methods", "100"), ("categories", "100", 0), ("category", "100", "Gift Cards", 0),
           ("product", "100", "Gift Cards", "p1", 0), ("bind", "100", "p1", "i1"), ("lot_auto_price", "100"),
           ("lot_auto_price", "100"), ("pricing",), ("toggle", "auto_price", "pricing"), ("toggle", "sanity_enabled", "pricing"),
           ("toggle", "sanity_enabled", "pricing"), ("sync_now",), ("template",), ("reset_template",), ("stats",),
           ("history", 0), ("order", "ORD1", 0), ("guide", 0), ("guide", 7), ("plugin_card",), ("plugins",),
           ("ask", "markup", ""), ("ask", "conditions", "100"), ("toggle", "notifications", "home"),
           ("toggle", "notifications", "home"), ("toggle", "auto_refund", "home"), ("region",), ("region",)]


def test_every_screen_renders(ui):
    run(ui.env)
    for action in ACTIONS:
        ui.panel.dispatch(10, 10, action, mid=5)
    text = "\n".join(ui.bot.messages)
    for part in ("Успешных продаж: 1", "Подключение к AppRoute успешно", "Каталог обновлён", "Автоподбор", "Лот привязан",
                 "Цена лота = цена в USDT × курс × (1 + наценка)", "Готово", "Шаблон выдачи", "Выручка FunPay", "fpc-ORD1",
                 "AppRoute Shop", "быстрый старт"):
        assert part in text, part


def test_main_menu_buttons_follow_the_guide(ui):
    ui.panel.home(10)
    assert labels(ui) == ["Статус 🟢", "Уведомления 🔔", "Авто-возврат 🟢", "🔑 Доступ к API", "🔄 Обновить баланс", "📦 Лоты",
                          "💲 Ценообразование", "🗂 Каталог", "✉️ Шаблон выдачи", "📊 Статистика", "📜 История",
                          "📖 Инструкция", "👤 Автор", "◀️ В плагины"]
    back = ui.bot.markups[-1].rows[-1][0]
    assert back.callback_data == "43:0"


def test_pricing_buttons(ui):
    ui.panel.pricing(10)
    names = labels(ui)
    assert "📐 Порог предохранителя: x3" in names and "⚡ Обновить цены сейчас" in names
    ui.env.store.configure(sanity_enabled=False)
    ui.panel.pricing(10)
    assert not any(n.startswith("📐") for n in labels(ui))


def test_add_lot_flow(ui):
    panel, env = ui.panel, ui.env
    env.account.lots[555] = env.account.get_lot_fields(555)
    env.account.lots[555].title_ru = "PlayStation Турция 500 TL"
    lid = panel.apply_input("lot_id", "", "https://funpay.com/lots/offer?id=555")
    assert lid == "555" and env.store.get("draft", "555")["conditions"] == ["PlayStation Турция 500 TL"]
    panel.results(10, lid, "", 0)
    assert "🚫 PlayStation Network Turkey — 500 TL" in labels(ui)[0]
    panel.dispatch(10, 10, ("bind", "555", "p2", "c"), mid=5)
    lot = env.store.get("lot", "555")
    assert lot["item_id"] == "c" and env.store.get("draft", "555") is None
    assert "Лот привязан" in ui.bot.messages[-1]
    # Re-adding keeps the lot settings.
    panel.apply_input("name", "555", "Моё название")
    panel.apply_input("lot_id", "", "555")
    panel.dispatch(10, 10, ("bind", "555", "p1", "i1"), mid=5)
    assert env.store.get("lot", "555")["name"] == "Моё название" and "настройки лота сохранены" in ui.bot.messages[-1]


def test_cancel_drops_draft(ui):
    ui.panel.apply_input("lot_id", "", "777")
    ui.panel.dispatch(10, 10, ("cancel", "777"))
    assert ui.env.store.get("draft", "777") is None and ui.env.store.get("lot", "777") is None


def test_standardized_lot(ui, monkeypatch):
    env = ui.env
    fields = env.account.get_lot_fields(888)
    fields.title_ru = ""
    html = ('<form class="form-offer-editor"><div class="form-group"><select name="fields[country]">'
            '<option value="1" selected>Турция</option></select></div><div class="form-group"><select name="fields[nominal]">'
            '<option value="5" selected>500 TL</option></select></div></form>')
    env.account.method = lambda *a, **k: SimpleNamespace(content=html.encode())
    info = env.shop.read_lot("888")
    assert info["standardized"] and info["conditions"] == ["country=Турция", "nominal=500 TL"]
    env.store.put("draft", "888", info)
    ui.panel.results(10, "888", "", 0)
    assert "стандартизованный лот" in ui.bot.messages[-1]


def test_inputs(ui):
    panel, env = ui.panel, ui.env
    panel.apply_input("conditions", "100", "Steam, 5 USD\nGift")
    assert env.store.get("lot", "100")["conditions"] == ["Steam", "5 USD", "Gift"]
    with pytest.raises(P.ShopError):
        panel.apply_input("conditions", "100", ",".join(str(i) for i in range(9)))
    panel.apply_input("conditions", "100", "-")
    assert env.store.get("lot", "100")["conditions"] == []
    panel.apply_input("interval", "", "5")
    assert env.store.settings()["interval"] == 20
    panel.apply_input("markup", "", "25%")
    assert env.store.settings()["markup"] == "25"
    with pytest.raises(P.ShopError):
        panel.apply_input("rate", "", "0")
    with pytest.raises(P.ShopError):
        panel.apply_input("min_price", "", "99999999")
    with pytest.raises(P.ShopError):
        panel.apply_input("template", "", "без кодов {lot_name}")
    panel.apply_input("template", "", "Ваш {lot_name}: {codes}")
    panel.apply_input("proxy", "", "user:pass@1.2.3.4:8080")
    assert env.store.settings()["proxy_mode"] == "own"
    panel.apply_input("proxy", "", "-")
    assert env.store.settings()["proxy_mode"] == "bot" and env.store.settings()["proxy"] == ""
    lot, item = panel.apply_input("ids", "100", "p1:i1")
    assert lot["item_id"] == "i1"


def test_api_key_message_is_deleted(ui):
    panel = ui.panel
    ui.env.shop.submit = lambda action, service=True: action()
    panel.prompt(10, 10, "api_key")
    msg = SimpleNamespace(chat=SimpleNamespace(id=10, type="private"), from_user=SimpleNamespace(id=10),
                          text="NEWKEY-1234567890", message_id=99)
    assert panel.has_input(msg)
    panel.message(msg)
    assert 99 in ui.bot.deleted and ui.env.store.settings()["api_key"] == "NEWKEY-1234567890"


def test_stale_form_and_commands_pass_through(ui):
    panel = ui.panel
    panel.pending[("10", "10")] = ("rate", "", 1, time.time() - 1000)
    msg = SimpleNamespace(chat=SimpleNamespace(id=10), from_user=SimpleNamespace(id=10), text="hello")
    assert panel.has_input(msg) is False and not panel.pending
    panel.pending[("10", "10")] = ("rate", "", 1, time.time())
    msg.text = "/menu"
    assert panel.has_input(msg) is False
    msg.text = "/shop_cancel"
    assert panel.has_input(msg) is True


def test_command_registers_notification_chat(ui):
    msg = SimpleNamespace(chat=SimpleNamespace(id=-500), from_user=SimpleNamespace(id=10))
    ui.panel.command(msg)
    assert -500 in ui.env.store.settings()["notify_chats"]
    msg.from_user = SimpleNamespace(id=11)
    ui.panel.command(SimpleNamespace(chat=SimpleNamespace(id=-600), from_user=SimpleNamespace(id=11)))
    assert -600 not in ui.env.store.settings()["notify_chats"]


def test_not_modified_edit_sends_no_duplicate(ui):
    ui.bot.not_modified = True
    ui.panel.show(10, "same", mid=5)
    assert ui.bot.messages == []


def test_region_change_blocked_during_purchase(ui):
    ui.env.api.purchase_result = {"status": "IN_PROGRESS"}
    run(ui.env)
    with pytest.raises(P.ShopError):
        ui.panel.dispatch(10, 10, ("region",))


def test_delete_lot_is_immediate(ui):
    ui.panel.dispatch(10, 10, ("delete_lot", "100"))
    assert ui.env.store.get("lot", "100") is None and "Привязанные лоты" in ui.bot.messages[-1]


class FakeTG:
    def __init__(self, bot):
        self.bot, self.authorized_users, self.msg, self.cbq = bot, [10], [], []

    def msg_handler(self, handler, **kwargs):
        self.msg.append((handler, kwargs))

    def cbq_handler(self, handler, func, **kwargs):
        self.cbq.append((handler, func))


def test_panel_registers_when_telegram_appears_after_pre_init(env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for name, value in (("SHOP", None), ("STARTUP_ERROR", ""), ("UI_REGISTERED", False)):
        monkeypatch.setattr(P, name, value)
    cardinal = SimpleNamespace(account=env.account, plugins={}, telegram=None, add_telegram_commands=lambda *a: None)
    P.init(cardinal)
    assert P.SHOP is not None and not P.UI_REGISTERED
    cardinal.telegram = FakeTG(Bot())
    P.init(cardinal)  # POST_INIT
    assert P.UI_REGISTERED and any(k.get("commands") == ["approute_shop"] for _, k in cardinal.telegram.msg)
    settings = [f for _, f in cardinal.telegram.cbq if f(SimpleNamespace(data=f"plugin_settings:{P.UUID}:0"))]
    assert settings


def test_storage_error_still_answers_command(env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "storage").mkdir()
    (tmp_path / "storage" / "plugins").write_text("")
    for name, value in (("SHOP", None), ("STARTUP_ERROR", ""), ("UI_REGISTERED", False)):
        monkeypatch.setattr(P, name, value)
    bot = Bot()
    cardinal = SimpleNamespace(account=env.account, plugins={}, telegram=FakeTG(bot), add_telegram_commands=lambda *a: None)
    P.init(cardinal)
    assert P.SHOP is None and P.STARTUP_ERROR
    handler = next(h for h, k in cardinal.telegram.msg if k.get("commands") == ["approute_shop"])
    handler(SimpleNamespace(chat=SimpleNamespace(id=10)))
    assert "не запустился" in bot.messages[-1]
