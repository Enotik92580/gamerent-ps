import time
import types

import pytest

BEAR = 5170233102089322756


class FakeClient:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.is_connected = False
        self.connects = 0
        FakeClient.instances.append(self)

    async def connect(self):
        self.connects += 1
        self.is_connected = True
        return True

    async def disconnect(self):
        self.is_connected = False

    async def get_stars_balance(self):
        return 321.0


def test_runtime_reuses_connection_and_reconnects_on_proxy_change(ag, env, monkeypatch):
    FakeClient.instances.clear()
    monkeypatch.setattr(ag, "Client", FakeClient)
    ag.CONFIG.update(api_id=1, api_hash="0" * 32, proxy="1.2.3.4:8080")
    account = dict(ag.DEFAULT_ACCOUNT, id="tg_1", session_string="s" * 60)
    ag.ACCOUNTS[:] = [account]
    runtime = ag.TelegramRuntime()
    monkeypatch.setattr(ag, "RUNTIME", runtime)
    for _ in range(3):
        assert runtime.run(ag.refresh_account_balance(account), timeout=10) == 321
    assert len(FakeClient.instances) == 1 and FakeClient.instances[0].connects == 1
    assert FakeClient.instances[0].kwargs["device_model"] == ag.DEVICE_MODEL
    ag.CONFIG["proxy"] = "5.6.7.8:3128"
    runtime.run(ag.refresh_account_balance(account), timeout=10)
    assert len(FakeClient.instances) == 2 and not FakeClient.instances[0].is_connected
    assert FakeClient.instances[1].kwargs["proxy"]["hostname"] == "5.6.7.8"
    runtime.drop_sync("tg_1")
    assert not FakeClient.instances[1].is_connected


def test_runtime_timeout_is_reported(ag):
    import asyncio
    runtime = ag.TelegramRuntime()
    with pytest.raises(TimeoutError):
        runtime.run(asyncio.sleep(5), timeout=0.1)


class Telegram:
    def __init__(self, bot):
        self.bot = bot
        self.authorized_users = {42: {}}
        self.states = {}

    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.states[(chat_id, user_id)] = {"state": state}

    def get_state(self, chat_id, user_id):
        return self.states.get((chat_id, user_id))

    def clear_state(self, chat_id, user_id, del_msg=False):
        self.states.pop((chat_id, user_id), None)


CALLBACKS = [
    "ag:main", "ag:settings", "ag:protection", "ag:recipient_settings", "ag:limits", "ag:set_limit:units",
    "ag:cancel:ag:limits", "ag:test", "ag:stats:1", "ag:stats:7", "ag:stats:all", "ag:review",
    "ag:rv:ABCD1234", "ag:rva:refund_ask:ABCD1234", "ag:rva:done_ask:ABCD1234", "ag:help", "ag:set_api",
    "ag:set_proxy", "ag:set_comment", "ag:set_threshold", "ag:set_star_cost", "ag:set_fee", "ag:messages",
    "ag:message_presets", "ag:msg:success", "ag:toggle_confirm", "ag:toggle_reminder", "ag:toggle_refunds",
    "ag:toggle_protect", "ag:toggle_hide", "ag:accounts", "ag:add_phone", "ag:add_session",
    "ag:del_account:tg_1", "ag:lots", "ag:edit_lot:lot_x", "ag:tag:lot_x", "ag:delete_lot:lot_x",
    "ag:cat:1", "ag:gift:%d" % BEAR, "ag:catalog", "ag:export", "ag:toggle_running", "ag:toggle_running",
]


def test_every_menu_button_renders(ag, env, monkeypatch):
    bot = env.telegram.bot
    bot.send_document = lambda *a, **k: bot.sent.append((0, "document"))
    env.telegram = Telegram(bot)
    monkeypatch.setattr(ag, "INITIALIZED", True)
    monkeypatch.setattr(ag, "DEPENDENCY_READY", True)
    monkeypatch.setattr(ag, "_safe_json_save", lambda path, data: None)
    ag.CONFIG.update(api_id=1, api_hash="0" * 32)
    ag.ACCOUNTS[:] = [dict(ag.DEFAULT_ACCOUNT, id="tg_1", label="Иван (@ivan)", session_string="s" * 60, balance=350)]
    ag.LOTS["lot_x"] = {"name": "Мишка", "funpay_lot_id": 1, "lot_id": 1, "gift_ids": [BEAR], "enabled": False}
    ag.CATALOG["items"] = [dict(g, available=True) for g in ag.STANDARD_GIFTS_CATALOG]
    ag.CATALOG["live_checked_at"] = time.time()
    order = {"order_id": "ABCD1234", "status": "needs_review", "buyer_id": 5, "chat_id": "7", "units": 1,
             "gift_ids": [BEAR], "created_at": time.time(), "last_error": "x", "username": "durov",
             "delivery": [{"sequence": 1, "unit": 1, "gift_id": BEAR, "status": "unknown"}]}
    ag.ORDERS["ABCD1234"] = order
    for data in CALLBACKS:
        call = types.SimpleNamespace(
            id="1", data=data, from_user=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(chat=types.SimpleNamespace(id=42, type="private"), message_id=1))
        ag.cb(call)
        errors = [text for _, text in bot.sent if "Ошибка" in str(text)]
        assert not errors, (data, errors)
    texts = "\n".join(str(t) for _, t in bot.sent)
    assert "⭐ ⭐" not in texts and "⭐⭐" not in texts
    assert "❓ Оплата части подарков не подтверждена" in texts


def test_init_registers_panel_and_starts_workers(ag, env, monkeypatch):
    import threading
    registered = {}
    bot = env.telegram.bot
    telegram = Telegram(bot)
    telegram.msg_handler = lambda handler, **kw: registered.setdefault("msg", handler)
    telegram.mdw_handler = lambda handler, **kw: registered.setdefault("mdw", handler)
    telegram.cbq_handler = lambda handler, func=None, **kw: registered.setdefault("cbq", []).append(handler)
    fake = types.SimpleNamespace(telegram=telegram, account=env.account,
                                 add_telegram_commands=lambda uuid, cmds: registered.setdefault("cmds", cmds))
    for path in (ag.CONFIG_PATH, ag.ORDERS_PATH):
        if path.exists():
            path.unlink()
    monkeypatch.setattr(ag, "WORKERS_STARTED", False)
    ag.init_commands(fake)
    assert not ag.STARTUP_ERROR and ag.INITIALIZED, ag.STARTUP_ERROR
    assert {"msg", "mdw", "cbq", "cmds"} <= set(registered)
    assert ag.ORDERS_PATH.exists() and ag.CONFIG_PATH.exists() and ag.CONFIG["scan_since"] > 0
    names = {t.name for t in threading.enumerate()}
    assert {"AutoGiftEvents", "AutoGiftDelivery", "AutoGiftBackground", "AutoGiftTelegram"} <= names
    ag.init_commands(fake)  # a second PRE_INIT call is ignored
