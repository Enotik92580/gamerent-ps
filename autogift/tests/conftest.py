"""Test harness: stub FunPayAPI/Cardinal, real telebot + Kurigram (pyrogram)."""

import asyncio
import enum
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "AutoGift_LuckyFunpay.py"


def _install_stubs():
    funpay = types.ModuleType("FunPayAPI")
    updater = types.ModuleType("FunPayAPI.updater")
    events = types.ModuleType("FunPayAPI.updater.events")
    enums_mod = types.ModuleType("FunPayAPI.enums")

    class NewOrderEvent:
        def __init__(self, order):
            self.order = order

    class NewMessageEvent:
        def __init__(self, message):
            self.message = message

    class OrderStatuses(enum.Enum):
        PAID = 0
        CLOSED = 1
        REFUNDED = 2

    events.NewOrderEvent = NewOrderEvent
    events.NewMessageEvent = NewMessageEvent
    enums_mod.OrderStatuses = OrderStatuses
    funpay.enums = enums_mod
    funpay.updater = updater
    updater.events = events
    sys.modules.update({"FunPayAPI": funpay, "FunPayAPI.updater": updater,
                        "FunPayAPI.updater.events": events, "FunPayAPI.enums": enums_mod})
    cardinal = types.ModuleType("cardinal")
    cardinal.Cardinal = object
    sys.modules["cardinal"] = cardinal


@pytest.fixture(scope="session")
def ag(tmp_path_factory):
    workdir = tmp_path_factory.mktemp("cardinal")
    os.chdir(workdir)
    _install_stubs()
    spec = importlib.util.spec_from_file_location("autogift_plugin", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    sys.modules["autogift_plugin"] = module
    spec.loader.exec_module(module)
    assert module._load_telegram_dependency()
    return module


class FakeBot:
    def __init__(self):
        self.sent = []

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return types.SimpleNamespace(message_id=len(self.sent))

    def edit_message_text(self, text, chat_id, message_id, **kwargs):
        self.sent.append((chat_id, text))

    def answer_callback_query(self, *args, **kwargs):
        pass


class FakeAccount:
    def __init__(self, ag):
        self.ag = ag
        self.id = 1000
        self.messages = []
        self.orders = {}
        self.refunded = []
        self._next_message_id = 100

    def send_message(self, chat_id, text):
        self._next_message_id += 1
        self.messages.append((str(chat_id), text))
        return types.SimpleNamespace(id=self._next_message_id)

    def get_order(self, order_id):
        return self.orders[order_id]

    def refund(self, order_id):
        self.refunded.append(order_id)
        self.orders[order_id].status = self.ag.enums.OrderStatuses.REFUNDED


class FakeCardinal:
    def __init__(self, ag):
        self.account = FakeAccount(ag)
        self.telegram = types.SimpleNamespace(authorized_users={42: {}}, bot=FakeBot())


@pytest.fixture
def env(ag, monkeypatch):
    """Fresh plugin state for every test."""
    ag.CONFIG.clear()
    ag.CONFIG.update(ag.DEFAULT_CONFIG)
    ag.CONFIG["scan_since"] = 1.0
    ag.ACCOUNTS[:] = []
    ag.LOTS.clear()
    ag.ORDERS.clear()
    ag.SEEN_ORDERS.clear()
    ag.NOTIFY_ONCE.clear()
    ag.PROCESSING_ORDERS.clear()
    ag.QUEUED_ORDERS.clear()
    while not ag.DELIVERY_QUEUE.empty():
        ag.DELIVERY_QUEUE.get_nowait()
    while not ag.EVENT_QUEUE.empty():
        ag.EVENT_QUEUE.get_nowait()
    ag.CATALOG.clear()
    ag.CATALOG.update({"updated_at": 0, "items": []})
    ag.PROTECTION_STATE.update(bad_cycles=0)
    ag.STORAGE_ERROR = ""
    ag.RUNNING = True
    c = FakeCardinal(ag)
    ag.CARDINAL_INSTANCE = c
    ag.BOT = c.telegram.bot
    # Tests run coroutines directly instead of on the runtime thread.
    monkeypatch.setattr(ag, "run_coro", lambda coro, timeout=None: asyncio.run(coro))
    return c
