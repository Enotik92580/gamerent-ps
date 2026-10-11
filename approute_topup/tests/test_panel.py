# -*- coding: utf-8 -*-
"""Smoke test of every Telegram panel screen with a fake telebot."""
import sys
import time
import types
from types import SimpleNamespace

import pytest

from test_plugin import P, make_order, start
from test_plugin import env  # noqa: F401,F811  pytest fixture


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
    monkeypatch.setitem(sys.modules, "tg_bot", SimpleNamespace(CBT=SimpleNamespace(PLUGIN_SETTINGS="plugin_settings")))


class Bot:
    def __init__(self):
        self.messages, self.not_modified = [], False

    def send_message(self, chat, text, **kwargs):
        self.messages.append(text)
        return SimpleNamespace(message_id=len(self.messages))

    def edit_message_text(self, text, chat, mid, **kwargs):
        if self.not_modified:
            raise RuntimeError("Bad Request: message is not modified")
        self.messages.append(text)
        return SimpleNamespace(message_id=mid)

    def send_document(self, chat, content, caption=""):
        self.messages.append(content.getvalue().decode())


@pytest.fixture
def ui(env):
    bot = Bot()
    env.shop.cardinal.telegram = SimpleNamespace(bot=bot, authorized_users={10})
    env.store.put("catalog", "main", {"products": [
        {"id": "p1", "name": "Mobile Legends Diamonds", "categoryName": "Mobile Games",
         "items": [{"id": "i1", "name": "86 diamonds", "price": "1.00", "currency": "USD"},
                   {"id": "i2", "name": "172 diamonds", "price": "2.00", "currency": "USD", "available": False}]},
        {"id": "p2", "name": "Steam Wallet", "categoryName": "Games", "items": []}]})
    env.store.patch("lot", "100", title="MLBB 86 Mobile Legends", form=env.api.fields, service_name="Mobile Legends")
    panel = P.TelegramUI(env.shop)
    return SimpleNamespace(panel=panel, bot=bot, env=env)


ACTIONS = [("home",), ("showcase",), ("orders",), ("finance",), ("settings",), ("api",), ("lots", 0), ("lot", "100"),
           ("suggest", "100", 0), ("items", "100", "p1", 0), ("fixed", "100"), ("amount", "100"), ("currency", "100"),
           ("set_currency", "100", "RUB"), ("set_currency", "100", None), ("amount_mode", "100", "fixed"),
           ("amount_mode", "100", ""), ("rates", "100"), ("pricing",), ("dialog",), ("safety",), ("active", 0),
           ("history", 0), ("attention", 0), ("stats",), ("templates", 0), ("template", "question"), ("guide", 0),
           ("catalog", "", "", 0), ("toggle", "confirm_account", "dialog"), ("lot_toggle", "100", "auto_price"),
           ("diagnose", "100"), ("bind", "100", "p1", "i1"), ("ask", "fixed_field:user_id", "100"),
           ("ask", "template:question", ""), ("ask", "order_id", ""), ("enable_lots",), ("balance",), ("unnotify",),
           ("export",), ("confirm", "reset_templates", ""), ("execute", "reset_templates", ""), ("order", "ORD1"),
           ("plugins",)]


def test_every_screen_renders(ui):
    start(ui.env)
    for action in ACTIONS:
        ui.panel.dispatch(10, 10, action, mid=5)
    assert any("Лот привязан" in m for m in ui.bot.messages)
    assert any("Автоподбор" in m for m in ui.bot.messages)


def test_not_modified_edit_sends_no_duplicate(ui):
    ui.bot.not_modified = True
    ui.panel.show(10, "same", mid=5)
    assert ui.bot.messages == []


def test_item_label_marks_unavailable_and_rub_price(ui):
    label = ui.panel.item_label({"id": "i2", "name": "172", "price": "2.00", "currency": "USD", "available": False})
    assert label.startswith("🚫") and "≈ 230.00 ₽" in label


def test_inputs(ui):
    panel, env = ui.panel, ui.env
    panel.apply_input("fixed_field:user_id", "100", "555")
    assert env.store.get("lot", "100")["fixed_fields"] == {"user_id": "555"}
    panel.apply_input("fixed_field:user_id", "100", "-")
    assert env.store.get("lot", "100")["fixed_fields"] == {}
    panel.apply_input("ids", "100", "p1:i1")
    assert env.store.get("lot", "100")["item_id"] == "i1"
    panel.apply_input("conditions", "100", "MLBB, 86 / восемьдесят шесть")
    lot = env.store.get("lot", "100")
    assert lot["conditions"] == ["MLBB", "86 / восемьдесят шесть"] and lot["conditions_manual"]
    panel.apply_input("interval", "", "5")
    assert env.store.settings()["interval"] == 20
    env.account.orders["NEW1"] = make_order("NEW1")
    assert panel.apply_input("order_id", "", "#new1") == "NEW1"
    assert env.store.get("order", "NEW1")["state"] == "queued"
    with pytest.raises(P.ShopError):
        panel.apply_input("order_id", "", "NEW1")


def test_stale_form_does_not_capture_messages(ui):
    panel = ui.panel
    panel.pending[("10", "10")] = ("rate", "", 1, time.time() - 1000)
    msg = SimpleNamespace(chat=SimpleNamespace(id=10, type="private"), from_user=SimpleNamespace(id=10), text="hello")
    assert panel.has_input(msg) is False and not panel.pending
    panel.pending[("10", "10")] = ("rate", "", 1, time.time())
    msg.text = "/menu"
    assert panel.has_input(msg) is False
    msg.text = "/topup_cancel"
    assert panel.has_input(msg) is True


def test_export_masks_secrets(ui):
    env = ui.env
    record = start(env)
    record.update(data={"password": "hunter22"}, fields=[{"key": "password", "type": "password"}],
                  transactions=[{"reference": "r", "state": "success",
                                 "body": {"orders": [{"fields": [{"key": "password", "value": "hunter22"}]}]}}])
    env.shop.save(record)
    ui.panel.dispatch(10, 10, ("export",))
    assert "hunter22" not in ui.bot.messages[-1]
