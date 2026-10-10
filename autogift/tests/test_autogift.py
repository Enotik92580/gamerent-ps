import asyncio
import json
import time
import types
from contextlib import asynccontextmanager
from datetime import datetime

import pytest
from pyrogram import errors
from pyrogram.enums import ChatType

BEAR = 5170233102089322756   # 15 ⭐
ROSE = 5168103777563050263   # 25 ⭐


# ---------------------------------------------------------------- parsing

def test_parse_proxy_formats(ag):
    assert ag.parse_proxy("1.2.3.4:8080") == {"scheme": "http", "hostname": "1.2.3.4", "port": 8080}
    p = ag.parse_proxy("http://user:pa:ss@1.2.3.4:3128/")
    assert p["username"] == "user" and p["password"] == "pa:ss" and p["port"] == 3128
    assert ag.parse_proxy("socks5://1.2.3.4:1080")["scheme"] == "socks5"
    assert ag.parse_proxy("https://1.2.3.4:80")["scheme"] == "http"
    assert ag.parse_proxy("") is None
    for bad in ("1.2.3.4", "1.2.3.4:abc", "1.2.3.4:70000", "ftp://1.2.3.4:21"):
        with pytest.raises(ValueError):
            ag.parse_proxy(bad)


@pytest.mark.parametrize("text,bare,expected", [
    ("@durov", False, "durov"),
    ("t.me/durov", False, "durov"),
    ("https://t.me/durov/", False, "durov"),
    ("https://telegram.me/@durov", False, "durov"),
    ("durov", False, None),
    ("durov", True, "durov"),
    ("refund", True, None),
    ("привет", True, None),
    ("@ab", True, None),
    ("hello world", True, None),
])
def test_parse_buyer_username(ag, text, bare, expected):
    assert ag.parse_buyer_username(text, allow_bare=bare) == expected


@pytest.mark.parametrize("text,expected", [
    ("!вз", (None, "refund")),
    ("!БЭК", (None, "refund")),
    ("!refund", (None, "refund")),
    ("!вз ABCD1234", ("ABCD1234", "refund")),
    ("!gift abcd1234 @durov", ("ABCD1234", "@durov")),
    ("!gift #ABCD1234 refund", ("ABCD1234", "refund")),
    ("!gift ABCD1234 confirm 1a2b3c4d", ("ABCD1234", "+")),
    ("+ ABCD1234", ("ABCD1234", "+")),
    ("-ABCD1234", ("ABCD1234", "-")),
    ("+", (None, "+")),
    ("−", (None, "-")),
    ("@durov", (None, "@durov")),
])
def test_parse_buyer_command(ag, text, expected):
    assert ag._parse_buyer_command(text) == expected


def test_parse_gift_ids_ignores_text_lines(ag):
    ids, unknown = ag.parse_gift_ids(f"Мишка\n{BEAR}\n{BEAR}\nроза: {ROSE}")
    assert ids == [BEAR, BEAR, ROSE] and unknown == []
    ids, unknown = ag.parse_gift_ids("123456789012")
    assert ids == [] and unknown == ["123456789012"]


# ---------------------------------------------------------------- templates

def test_template_validation_and_doc_variables(ag, env):
    assert ag.validate_template("✅ {total_sent} подарков для {tg_user_at}, заказ #{order_id}, {price} ₽ {{ok}}") is None
    assert "Неизвестные" in ag.validate_template("{nope}")
    assert "скобках" in ag.validate_template("broken {")
    assert ag.validate_template("{}") is not None
    text = ag.format_customer_message("{tg_user_at} {total_sent}/{gift_count} {star_cost} <b>x</b>", ag.TEMPLATE_SAMPLE)
    assert text == "@durov 2/2 15 x"


# ---------------------------------------------------------------- consent

def _order(ag, **extra):
    data = {"order_id": "ABCD1234", "buyer_id": 555, "chat_id": "777", "status": "ready_confirm",
            "lot_key": "lot_x", "binding_token": "a" * 32, "gift_ids": [BEAR, ROSE], "units": 1,
            "username": "durov", "recipient_id": 1, "display_name": "Pavel", "comment": "", "hide_name": True,
            "created_at": time.time(), "delivery": []}
    data.update(extra)
    ag.delivery_items(data)
    return data


def test_consent_does_not_expire_during_delivery(ag, env):
    data = _order(ag)
    ag.give_consent(data)
    assert ag._delivery_consent_valid(data)
    data["confirmed"]["confirmed_at"] = time.time() - 3600  # an hour-long queue is fine
    assert ag._delivery_consent_valid(data)
    data["confirmed"]["confirmed_at"] = time.time() - ag.CONSENT_TTL - 1
    assert not ag._delivery_consent_valid(data)
    data["confirmed"]["confirmed_at"] = time.time()
    data["recipient_id"] = 2  # consent never transfers to another recipient
    assert not ag._delivery_consent_valid(data)


# ---------------------------------------------------------------- lot protection

def test_protection_decision_does_not_flap_on_stale_catalog(ag):
    lot = {"gift_ids": [BEAR, ROSE]}
    fallback = {BEAR: 15, ROSE: 25}
    # Catalog request failed: no reactivation, but no blanket pause either.
    assert ag.protection_decision(lot, 1000, False, {}, fallback, False) is None
    # Not enough Stars is still detected from last known prices.
    assert ag.protection_decision(lot, 30, False, {}, fallback, False) is True
    # Fresh catalog: missing gift pauses; enough balance allows the lot.
    assert ag.protection_decision(lot, 1000, False, {BEAR: 15}, fallback, True) is True
    assert ag.protection_decision(lot, 1000, False, fallback, fallback, True) is False
    assert ag.protection_decision(lot, 1000, True, fallback, fallback, True) is True


def test_unavailable_balances_need_three_cycles(ag, env, monkeypatch):
    ag.ACCOUNTS.append(dict(ag.DEFAULT_ACCOUNT, id="tg_1", session_string="s" * 60, balance=None))
    ag.LOTS["lot_x"] = {"enabled": True, "gift_ids": [BEAR], "binding": {"lot_id": "1"}}
    monkeypatch.setattr(ag, "_binding_valid", lambda lot: True)
    ag.CATALOG.update(live_checked_at=time.time(), items=[{"id": BEAR, "price": 15, "available": True}])
    calls = []
    monkeypatch.setattr(ag, "lot_is_active", lambda lid: True)
    monkeypatch.setattr(ag, "set_lot_active", lambda lid, active: calls.append(active) or True)
    for _ in range(2):
        asyncio.run(ag.update_lot_protection())
    assert calls == []
    asyncio.run(ag.update_lot_protection())
    assert calls == [False] and ag.LOTS["lot_x"]["paused_by_autogift"]


# ---------------------------------------------------------------- storage

def test_persist_writes_journal_before_config(ag, env, monkeypatch):
    order = []
    monkeypatch.setattr(ag, "_safe_json_save", lambda path, data: order.append(path.name))
    ag._persist_all()
    assert order[0] == ag.ORDERS_PATH.name and order[-1] == ag.CONFIG_PATH.name


def test_damaged_config_restores_from_backup_but_journal_fails_closed(ag, env):
    ag.CONFIG_PATH.write_text(json.dumps({"api_id": 7}), encoding="utf-8")
    ag._backup_path(ag.CONFIG_PATH).write_text(json.dumps({"api_id": 7}), encoding="utf-8")
    ag.CONFIG_PATH.write_text("{broken", encoding="utf-8")
    assert ag._safe_json_load(ag.CONFIG_PATH, {}) == {"api_id": 7}
    ag.ORDERS_PATH.write_text("{broken", encoding="utf-8")
    ag._backup_path(ag.ORDERS_PATH).write_text("{}", encoding="utf-8")
    with pytest.raises(ag.StorageError):
        ag._safe_json_load(ag.ORDERS_PATH, {})
    ag.STORAGE_ERROR = ""
    ag.ORDERS_PATH.unlink()
    ag.CONFIG_PATH.unlink()


def test_migration_resumes_confirmed_orders_and_flags_inflight(ag, env, monkeypatch):
    monkeypatch.setattr(ag, "_safe_json_save", lambda path, data: None)
    ready = _order(ag, order_id="AAAA1111")
    ag.give_consent(ready)
    ready["status"] = "sending"
    inflight = _order(ag, order_id="BBBB2222", status="sending")
    inflight["delivery"][0]["status"] = "sending"
    ag.ORDERS.update({"AAAA1111": ready, "BBBB2222": inflight})
    ag.migrate_lots()
    assert ready["status"] == "ready" and ready["resume_after_restart"]
    assert inflight["status"] == "needs_review" and inflight["delivery"][0]["status"] == "unknown"


def test_archive_moves_old_finished_orders(ag, env):
    old = _order(ag, order_id="OLD00001", status="completed", created_at=1.0, order_price=100.0, currency="RUB")
    old["updated_at"] = 1.0
    ag.ORDERS["OLD00001"] = old
    ag.ORDERS["NEW00001"] = _order(ag, order_id="NEW00001", status="completed")
    old["updated_at"] = 1.0
    assert ag.archive_old_orders() == 1
    assert "OLD00001" not in ag.ORDERS and "NEW00001" in ag.ORDERS
    assert any(r["order_id"] == "OLD00001" for r in ag.order_stats(None)["rows"])
    ag.ARCHIVE_PATH.unlink()


# ---------------------------------------------------------------- delivery

class FakeApp:
    def __init__(self, account, script):
        self.account = account
        self.script = script

    async def get_chat(self, username):
        return types.SimpleNamespace(id=1, type=ChatType.PRIVATE, first_name="Pavel")


def _delivery_env(ag, monkeypatch, accounts, script):
    """script(account_id, gift_number) -> None or an exception to raise."""
    ag.ACCOUNTS[:] = [dict(ag.DEFAULT_ACCOUNT, id=aid, session_string="s" * 60, balance=bal) for aid, bal in accounts]
    ag.CATALOG.update(live_checked_at=time.time(), items=[{"id": BEAR, "price": 15, "available": True},
                                                          {"id": ROSE, "price": 25, "available": True}])
    sent = []

    @asynccontextmanager
    async def fake_client(account):
        yield FakeApp(account, script)

    async def fake_send(app, recipient, gift_id, hide_name, comment, expected_price, before_submit):
        n = len(sent) + 1
        failure = script(app.account["id"], n, "before")
        if failure:
            raise failure
        before_submit(1000 + n)
        failure = script(app.account["id"], n, "after")
        if failure:
            raise failure
        sent.append((app.account["id"], gift_id))
        return {"form_id": 1000 + n, "result_type": "PaymentResult"}

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(ag, "account_client", fake_client)
    monkeypatch.setattr(ag, "send_gift_raw", fake_send)
    monkeypatch.setattr(ag, "order_ownership", lambda c, data: "ok")
    monkeypatch.setattr(ag, "refresh_catalog_for_menu", noop)
    monkeypatch.setattr(ag, "refresh_all_balances", noop)
    monkeypatch.setattr(ag, "update_lot_protection", noop)
    monkeypatch.setattr(ag, "GIFT_SEND_DELAY", 0)
    return sent


def _ready_order(ag, units=2):
    data = _order(ag, units=units)
    ag.give_consent(data)
    data["status"] = "sending"
    ag.ORDERS[data["order_id"]] = data
    return data


def test_delivery_completes_on_one_account(ag, env, monkeypatch):
    sent = _delivery_env(ag, monkeypatch, [("tg_1", 500)], lambda a, n, phase: None)
    data = _ready_order(ag)
    assert asyncio.run(ag.execute_order(env, data)) == "completed"
    assert sent == [("tg_1", BEAR), ("tg_1", ROSE), ("tg_1", BEAR), ("tg_1", ROSE)]
    assert all(i["status"] == "sent" for i in data["delivery"])
    assert ag.ACCOUNTS[0]["balance"] == 500 - 80 and ag.ACCOUNTS[0]["gifts_sent"] == 4


def test_floodwait_switches_account_without_duplicates(ag, env, monkeypatch):
    def script(account_id, n, phase):
        if account_id == "tg_1" and n == 2 and phase == "after":
            return errors.FloodWait(value=300)
    sent = _delivery_env(ag, monkeypatch, [("tg_1", 500), ("tg_2", 500)], script)
    data = _ready_order(ag)
    assert asyncio.run(ag.execute_order(env, data)) == "completed"
    assert [g for _, g in sent] == [BEAR, ROSE, BEAR, ROSE]
    assert sent[0][0] == "tg_1" and {a for a, _ in sent[1:]} == {"tg_2"}
    assert ag.ACCOUNTS[0]["cooldown_until"] > time.time()
    assert data["delivery"][1]["attempts"][0]["status"] == "rejected"


def test_timeout_after_payment_is_never_retried(ag, env, monkeypatch):
    def script(account_id, n, phase):
        if n == 2 and phase == "after":
            return TimeoutError("no answer")
    sent = _delivery_env(ag, monkeypatch, [("tg_1", 500), ("tg_2", 500)], script)
    data = _ready_order(ag)
    assert asyncio.run(ag.execute_order(env, data)) == "needs_review"
    assert len(sent) == 1
    assert [i["status"] for i in data["delivery"]] == ["sent", "unknown", "pending", "pending"]


def test_connection_error_before_payment_switches_account(ag, env, monkeypatch):
    def script(account_id, n, phase):
        if account_id == "tg_1" and phase == "before":
            return ConnectionError("proxy reset")
    sent = _delivery_env(ag, monkeypatch, [("tg_1", 500), ("tg_2", 400)], script)
    data = _ready_order(ag, units=1)
    assert asyncio.run(ag.execute_order(env, data)) == "completed"
    assert {a for a, _ in sent} == {"tg_2"}


def test_not_enough_stars_on_one_account_fails_without_sending(ag, env, monkeypatch):
    sent = _delivery_env(ag, monkeypatch, [("tg_1", 50), ("tg_2", 50)], lambda *a: None)
    data = _ready_order(ag)  # 80 ⭐ needed; 50 + 50 cannot be pooled
    assert asyncio.run(ag.execute_order(env, data)) == "failed_no_delivery"
    assert sent == [] and "80 Stars" in data["last_error"]


# ---------------------------------------------------------------- buyer dialogue

def _accepted_order(ag, env, monkeypatch, status="await_username"):
    data = _order(ag, status=status, username=None, recipient_id=None, display_name=None)
    ag.ORDERS[data["order_id"]] = data
    monkeypatch.setattr(ag, "order_ownership", lambda c, d: "ok")

    async def fake_validate(username):
        return ("ok", "Pavel", 99) if username.lower() == "durov" else ("not_found", "Пользователь не найден", None)
    monkeypatch.setattr(ag, "validate_username", fake_validate)
    return data


def test_bare_username_then_plus_queues_delivery(ag, env, monkeypatch):
    data = _accepted_order(ag, env, monkeypatch)
    ag.handle_buyer_message(env, "777", "555", 200, "durov")
    assert data["status"] == "ready_confirm" and data["recipient_id"] == 99
    assert "Напишите +" in env.account.messages[-1][1]
    ag.handle_buyer_message(env, "777", "555", 50, "+")      # older than the prompt: ignored
    assert data["status"] == "ready_confirm"
    ag.handle_buyer_message(env, "777", "555", 10_000, "+")
    assert data["status"] == "ready" and ag._delivery_consent_valid(data)
    assert ag.DELIVERY_QUEUE.get_nowait() == ("ABCD1234", "777")
    assert "в очереди" in env.account.messages[-1][1]


def test_unknown_username_and_chatter_get_replies(ag, env, monkeypatch):
    data = _accepted_order(ag, env, monkeypatch)
    ag.handle_buyer_message(env, "777", "555", 200, "@nobody_here")
    assert "не найден" in env.account.messages[-1][1] and data["status"] == "await_username"
    ag.handle_buyer_message(env, "777", "555", 201, "привет, когда подарок?")
    assert "@username" in env.account.messages[-1][1]
    count = len(env.account.messages)
    ag.handle_buyer_message(env, "777", "555", 202, "алло")  # rate-limited hint
    assert len(env.account.messages) == count


def test_without_confirmation_bare_names_are_not_accepted(ag, env, monkeypatch):
    ag.CONFIG["require_confirmation"] = False
    data = _accepted_order(ag, env, monkeypatch)
    ag.handle_buyer_message(env, "777", "555", 200, "durov")
    assert data["status"] == "await_username"
    ag.handle_buyer_message(env, "777", "555", 201, "t.me/durov")
    assert data["status"] == "ready" and not ag.DELIVERY_QUEUE.empty()


def test_refund_command(ag, env, monkeypatch):
    data = _accepted_order(ag, env, monkeypatch)
    env.account.orders["ABCD1234"] = types.SimpleNamespace(status=ag.enums.OrderStatuses.PAID)
    ag.handle_buyer_message(env, "777", "555", 200, "!вз")
    assert env.account.refunded == ["ABCD1234"] and data["status"] == "refunded"
    assert any("Возврат по команде" in text for _, text in env.telegram.bot.sent)


def test_refund_refused_while_sending(ag, env, monkeypatch):
    data = _accepted_order(ag, env, monkeypatch, status="sending")
    ag.handle_buyer_message(env, "777", "555", 200, "!refund")
    assert env.account.refunded == [] and "уже выдаётся" in env.account.messages[-1][1]
    assert data["status"] == "sending"


def test_message_hook_only_queues_relevant_chats(ag, env):
    ag.ORDERS["ABCD1234"] = _order(ag, status="await_username")
    msg = lambda chat, text: types.SimpleNamespace(message=types.SimpleNamespace(
        author_id=555, chat_id=chat, text=text, id=1))
    ag.message_hook(env, msg("other", "@durov"))
    assert ag.EVENT_QUEUE.empty()
    ag.message_hook(env, msg("777", "@durov"))
    func, args = ag.EVENT_QUEUE.get_nowait()
    assert func is ag.handle_buyer_message and args[-1] == "@durov"


# ---------------------------------------------------------------- backfill

def test_scan_picks_only_recent_unknown_paid_orders(ag, env):
    ag.LOTS["lot_x"] = {"enabled": True}
    paid = ag.enums.OrderStatuses.PAID
    now = datetime.now()
    shortcut = lambda oid, status=paid, date=now: types.SimpleNamespace(id=oid, status=status, date=date)
    ag.ORDERS["KNOWN001"] = {"order_id": "KNOWN001"}
    ag.SEEN_ORDERS["SEEN0001"] = time.time()
    env.account.get_sales = lambda **kw: (None, [
        shortcut("NEW00001"), shortcut("KNOWN001"), shortcut("SEEN0001"),
        shortcut("OLD00001", date=datetime.fromtimestamp(time.time() - 3 * 86400)),
        shortcut("CLOSED01", status=ag.enums.OrderStatuses.CLOSED), shortcut("NODATE01", date=None)])
    assert ag.scan_paid_orders(env) == 1
    func, args = ag.EVENT_QUEUE.get_nowait()
    assert func is ag.accept_paid_order and args[1] == "NEW00001"


def test_chatter_does_not_hit_funpay(ag, env, monkeypatch):
    _accepted_order(ag, env, monkeypatch)
    calls = []
    monkeypatch.setattr(ag, "order_ownership", lambda c, d: calls.append(1) or "ok")
    ag.handle_buyer_message(env, "777", "555", 200, "спасибо, жду")
    assert calls == [] and "@username" in env.account.messages[-1][1]
