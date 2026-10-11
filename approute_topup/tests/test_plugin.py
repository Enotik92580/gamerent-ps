# -*- coding: utf-8 -*-
"""Scenario tests for AppRouteTopUp_LuckyFunpay.py with fake FunPay and AppRoute."""
import importlib.util
import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("artop", Path(__file__).resolve().parents[1] / "AppRouteTopUp_LuckyFunpay.py")
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)

SELLER, BUYER = 1, 2
CHAT = "users-1-2"


def make_order(oid="ORD1", status="PAID", amount=1, total="100", **extra):
    data = dict(id=oid, status=status, buyer_id=BUYER, seller_id=SELLER, chat_id=CHAT, amount=amount, sum=total,
                short_description="MLBB 86 diamonds", subcategory=SimpleNamespace(id=7), buyer_username="buyer",
                currency="RUB")
    data.update(extra)
    return SimpleNamespace(**data)


class Account:
    def __init__(self):
        self.id = SELLER
        self.orders, self.sent, self.refunds, self.get_calls = {}, [], [], 0
        self.refund_error = None
        self.proxy = None

    def get_order(self, oid):
        self.get_calls += 1
        value = self.orders[oid]
        if isinstance(value, Exception):
            raise value
        return value

    def send_message(self, chat, text, *args):
        self.sent.append(text)
        return True

    def refund(self, oid):
        self.refunds.append(oid)
        if self.refund_error:
            raise self.refund_error


class FakeAPI:
    def __init__(self):
        self.fields = [{"key": "user_id", "label": "ID игрока", "type": "text", "required": True}]
        self.service_name, self.item_price = "Mobile Legends", "1.00"
        self.check_result = {"canRecharge": True, "price": "1.00", "currency": "USD", "attributes": {}}
        self.purchases, self.checks = [], []
        self.purchase_error = None
        self.service_extra = {}

    def __call__(self, settings):
        return self

    def service(self, sid):
        return {"id": sid, "type": "direct_topup", "name": self.service_name, "fields": self.fields, **self.service_extra}

    def item(self, sid, iid):
        return {"id": iid, "price": self.item_price, "currency": "USD", "name": "86 diamonds"}

    def check(self, body):
        self.checks.append(body)
        if isinstance(self.check_result, Exception):
            raise self.check_result
        return self.check_result

    def purchase(self, body):
        self.purchases.append(body)
        if self.purchase_error:
            raise self.purchase_error
        return {"status": "SUCCESS", "reference": body["referenceId"], "price": "1.00", "currency": "USD"}

    def balance(self):
        return Decimal("100")

    def find_order(self, reference):
        return None

    def rates(self):
        return {"baseCurrencyCode": "USD", "items": [{"quoteCurrencyCode": "RUB", "rate": "100"},
                                                      {"quoteCurrencyCode": "KZT", "rate": "500"}]}


@pytest.fixture
def env(tmp_path):
    account, api = Account(), FakeAPI()
    cardinal = SimpleNamespace(account=account, plugins={}, telegram=None)
    shop = P.TopUp(cardinal, store=P.Store(tmp_path / "db.sqlite3"), api_factory=api)
    shop.store.configure(enabled=True, api_key="k" * 16)
    shop.store.put("lot", "100", {"id": "100", "name": "MLBB 86", "conditions": ["MLBB 86"], "item_id": "i1",
                                  "product_id": "p1", "enabled": True, "subcategory": 7, "fixed_fields": {}})
    return SimpleNamespace(shop=shop, account=account, api=api, store=shop.store)


def start(env, oid="ORD1", **order):
    env.account.orders[oid] = make_order(oid, **order)
    env.shop.enqueue(oid, CHAT, str(BUYER))
    env.shop.process(oid)
    return env.store.get("order", oid)


def say(env, text, mid=None):
    mid = mid or str(time.time_ns())
    job = {"id": f"{CHAT}:{mid}", "chat": CHAT, "buyer_id": str(BUYER), "text": text, "created": time.time()}
    return env.shop.handle_message(job)


# --- 0.1 option values -------------------------------------------------------------------------

def test_option_value_wins_over_list_position():
    field = {"key": "server", "options": [{"value": "0", "label": "Asia"}, {"value": "1", "label": "Europe"}, {"value": "2", "label": "America"}]}
    assert P.validate_field(field, "1") == "1"
    assert P.validate_field(field, "europe") == "1"


def test_option_position_still_works_when_no_value_matches():
    field = {"key": "server", "options": [{"value": "eu", "label": "Europe"}, {"value": "as", "label": "Asia"}]}
    assert P.validate_field(field, "2") == "as"


# --- 0.2 no FunPay polling every second --------------------------------------------------------

def test_paused_order_is_not_polled_every_second(env):
    env.store.configure(enabled=False)
    record = start(env)
    assert record["state"] == "queued" and record["next_at"] > time.time() + 50
    calls = env.account.get_calls
    env.shop.process("ORD1")
    assert env.account.get_calls == calls
    assert any("на паузе" in m for m in env.account.sent)


def test_second_order_waits_without_polling(env):
    start(env, "ORD1")
    record = start(env, "ORD2")
    assert record["state"] == "queued" and record["next_at"] > time.time() + 10
    assert any("предыдущий заказ" in m for m in env.account.sent)


# --- 0.3 one currency rule for price and purchase -----------------------------------------------

def test_price_and_purchase_use_the_same_currency(env):
    env.store.configure(amount_currency="RUB")
    # The provider does not declare a currency; the seller set the native one on the lot.
    lot = {"id": "100", "amount_mode": "quantity", "multiplier": "1", "price_nominal": "1", "native_currency": "USD"}
    service = {"name": "Steam Wallet"}
    fields = [{"key": "amount", "type": "amount_input"}]
    assert P.resolve_currency(lot, service, fields[0], env.store.settings()) == "RUB"
    # 1.0.0 priced this per 1 USD while buying RUB (100× overpriced). One piece = 1 RUB = 0.01 USD.
    assert env.shop.unit_cost(lot, {"price": "1", "currency": "USD"}, service, fields) == Decimal("0.01")
    # The provider's own currency wins over the global default.
    assert P.resolve_currency(lot, {"name": "Steam", "amountCurrencyCode": "KZT"}, fields[0], env.store.settings()) == "KZT"


# --- 0.4 transient errors are retried -----------------------------------------------------------

def test_funpay_outage_is_retried_then_reviewed(env):
    env.account.orders["ORD1"] = ConnectionError("down")
    env.shop.enqueue("ORD1", CHAT, str(BUYER))
    env.shop.process("ORD1")
    record = env.store.get("order", "ORD1")
    assert record["state"] == "queued" and record["retries"] == 1 and record["next_at"] > time.time()
    for _ in range(P.MAX_RETRIES):
        env.shop.process("ORD1")
    assert env.store.get("order", "ORD1")["state"] == "review"


def test_retry_counter_resets_after_success(env):
    env.account.orders["ORD1"] = ConnectionError("down")
    env.shop.enqueue("ORD1", CHAT, str(BUYER))
    env.shop.process("ORD1")
    env.account.orders["ORD1"] = make_order()
    env.shop.process("ORD1")
    record = env.store.get("order", "ORD1")
    assert record["state"] == "collect" and record["retries"] == 0


# --- 0.5 a local rate-limit pause is not an unknown payment ------------------------------------

def test_pacer_block_before_send_drops_operation(env):
    record = start(env)
    say(env, "123456")
    env.api.purchase_error = P.PacerBlocked(time.time() + 60)
    env.shop.process("ORD1")  # check
    env.shop.process("ORD1")  # purchase → blocked locally
    record = env.store.get("order", "ORD1")
    assert record["state"] == "ready" and record["transactions"] == []
    assert record["next_at"] > time.time() + 30


def test_legacy_unsent_operation_returns_to_ready(env):
    record = start(env)
    record.update(state="pending", transactions=[{"reference": "lt-x", "state": "unsent", "created": 0,
                                                  "identity": P.RequestPacer.identity(env.store.settings())}])
    env.shop.save(record)
    env.shop.process("ORD1")
    record = env.store.get("order", "ORD1")
    assert record["state"] == "ready" and record["transactions"] == []


# --- full dialog -------------------------------------------------------------------------------

def test_happy_path(env):
    record = start(env)
    assert record["state"] == "collect"
    assert any("Шаг 1 из 1" in m for m in env.account.sent)
    say(env, "123456")
    env.shop.process("ORD1")
    env.shop.process("ORD1")
    record = env.store.get("order", "ORD1")
    assert record["state"] == "done" and record["cost"] == "1.00"
    assert env.api.purchases[0]["orders"][0]["fields"] == [{"key": "user_id", "value": "123456"}]
    # Fresh quote from the check is reused: one checkOnly for one operation.
    assert len(env.api.checks) == 1
    assert "Пополнение выполнено" in env.account.sent[-1]


def test_confirm_only_with_provider_nickname(env):
    start(env)
    say(env, "123456")
    env.shop.process("ORD1")
    assert env.store.get("order", "ORD1")["state"] == "ready"

    env.api.check_result = {"canRecharge": True, "price": "1.00", "currency": "USD", "attributes": {"nickname": "Hero"}}
    start(env, "ORD3", total="100")
    env.store.patch("order", "ORD1", state="done")
    env.shop.process("ORD3")
    say(env, "654321")
    env.shop.process("ORD3")
    record = env.store.get("order", "ORD3")
    assert record["state"] == "confirm" and record["nickname"] == "Hero"
    say(env, "Да, мой!")
    assert env.store.get("order", "ORD3")["state"] == "ready"


def test_step_numbering_with_prefilled_field(env):
    env.api.fields = [{"key": "user_id", "label": "ID", "type": "text", "required": True},
                      {"key": "zone_id", "label": "Zone", "type": "text", "required": True}]
    start(env, lot_params=[("uid", "777")])
    record = env.store.get("order", "ORD1")
    assert record["data"]["user_id"] == "777"
    assert any("ответить на 1 вопрос" in m for m in env.account.sent)
    assert any("Шаг 1 из 1" in m for m in env.account.sent)


def test_early_message_waits_for_first_question(env):
    env.account.orders["ORD1"] = make_order()
    env.shop.enqueue("ORD1", CHAT, str(BUYER))
    assert say(env, "123456", mid="early") is False  # kept until the dialog starts
    env.shop.process("ORD1")
    job = {"id": f"{CHAT}:early", "chat": CHAT, "buyer_id": str(BUYER), "text": "123456", "created": time.time()}
    assert env.shop.handle_message(job) is True
    assert env.store.get("order", "ORD1")["data"]["user_id"] == "123456"


def test_lost_question_field_is_reasked_not_looping(env):
    record = start(env)
    record["asking"] = "gone"
    env.shop.save(record)
    assert say(env, "123") is True
    assert env.store.get("order", "ORD1")["asking"] == "user_id"


def test_precheck_field_error_lets_buyer_retry(env):
    start(env)
    say(env, "1")
    env.api.check_result = P.APIError(400, 4, [{"field": "user_id", "code": "INVALID"}])
    env.shop.process("ORD1")
    record = env.store.get("order", "ORD1")
    assert record["state"] == "collect" and record["attempts"] == 1
    assert any("ID игрока" in m for m in env.account.sent)


# --- refunds -----------------------------------------------------------------------------------

def test_unavailable_reason_then_auto_refund(env):
    start(env)
    say(env, "1")
    env.api.check_result = P.APIError(422, 9)
    env.account.get_lot_fields = lambda lid: (_ for _ in ()).throw(RuntimeError())
    env.shop.process("ORD1")
    record = env.store.get("order", "ORD1")
    assert record["state"] == "refund_pending"
    assert any("временно недоступен" in m and "возвращены автоматически" in m for m in env.account.sent)
    env.shop.process("ORD1")
    assert env.account.refunds == ["ORD1"]
    env.account.orders["ORD1"] = make_order(status="REFUNDED")
    env.store.patch("order", "ORD1", next_at=0)
    env.shop.process("ORD1")
    assert env.store.get("order", "ORD1")["state"] == "refunded"


def test_refund_request_is_retried_while_still_paid(env):
    record = start(env)
    record.update(state="refund_pending", next_at=0)
    env.shop.save(record)
    env.account.refund_error = ConnectionError("timeout")
    for _ in range(3):
        env.store.patch("order", "ORD1", next_at=0)
        env.shop.process("ORD1")
    assert env.account.refunds == ["ORD1"] * 3
    env.store.patch("order", "ORD1", next_at=0)
    env.shop.process("ORD1")
    assert env.store.get("order", "ORD1")["state"] == "review"


# --- status changes, compatibility -------------------------------------------------------------

def test_refunded_on_funpay_closes_waiting_dialog(env):
    start(env)
    env.account.orders["ORD1"] = make_order(status="REFUNDED")
    assert env.shop.handle_status({"id": "ORD1"}) is True
    assert env.store.get("order", "ORD1")["state"] == "closed"


def test_order_without_currency_attribute_is_accepted(env):
    order = make_order()
    del order.currency
    env.shop.paid({"id": "ORD1"}, order)
    with pytest.raises(P.ShopError):
        env.shop.paid({"id": "ORD1"}, make_order(currency="USD"))


def test_match_lot_with_lot_params_and_unknown_subcategory():
    order = SimpleNamespace(short_description="Steam", subcategory=None, lot_params=[("Способ", "По логину")])
    lot = {"id": "1", "item_id": "x", "subcategory": 99, "conditions": ["method=По логину"]}
    assert P.match_lot(order, [lot]) == [lot]


def test_manual_alternatives_only_for_seller_conditions():
    order = SimpleNamespace(short_description="Steam by login", subcategory=None)
    auto = {"id": "1", "item_id": "x", "conditions": ["По логину / by login"]}
    assert P.match_lot(order, [auto]) == []
    manual = dict(auto, conditions_manual=True)
    assert P.match_lot(order, [manual]) == [manual]


# --- templates, helpers ------------------------------------------------------------------------

def test_guide_placeholders_are_accepted(env):
    P.validate_template("question", "Шаг {step}/{total}: {label}")
    with pytest.raises(P.ShopError):
        P.validate_template("question", "Шаг {step}")
    env.store.configure(templates={"question": "Шаг {step}/{total}: {label}"})
    start(env)
    assert "Шаг 1/1: ID игрока" in env.account.sent


def test_fixed_amount_is_per_piece(env):
    lot = {"amount_mode": "fixed", "fixed_amount": "500"}
    assert env.shop.amount_value(lot, 3, env.store.settings()) == Decimal("1500")


def test_confirm_answers():
    assert P.confirm_answer("Да.") is True
    assert P.confirm_answer("да, мой") is True
    assert P.confirm_answer("+") is True
    assert P.confirm_answer("нет") is False
    assert P.confirm_answer("не мой") is False
    assert P.confirm_answer("что?") is None


def test_masked_data_hides_secrets():
    data = P.masked_data({"login": "user", "password": "hunter22"}, [{"key": "password", "type": "password"}])
    assert data["login"] == "user" and "hunter" not in data["password"]


def test_ids_and_order_parsing():
    assert P.order_id_from_text("#abcd1234") == "ABCD1234"
    assert P.order_id_from_text("https://funpay.com/orders/ABCD1234/") == "ABCD1234"
    with pytest.raises(P.ShopError):
        P.order_id_from_text("12")


def test_ton_address_is_not_a_username():
    field = {"key": "account"}
    address = "UQ" + "A" * 46
    assert P.validate_field(field, address, "Telegram TON") == address


def test_balance_accepts_usdt():
    api = P.AppRouteAPI({"region": "RU", "proxy_mode": "bot", "proxy": "", "api_key": "k" * 16},
                        transport=lambda *a, **k: SimpleNamespace(
                            status_code=200, headers={},
                            json=lambda: {"statusCode": 0, "data": {"items": [{"currency": "USDT", "available": "5"}]}}))
    assert api.balance() == Decimal("5")


# --- storage -----------------------------------------------------------------------------------

def test_store_migrates_old_journal(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE records (kind TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY(kind,id))")
    db.execute("INSERT INTO records VALUES ('order','A',?)", (json.dumps({"id": "A", "state": "collect", "created": 1, "next_at": 0, "chat": CHAT}),))
    db.execute("INSERT INTO records VALUES ('order','B',?)", (json.dumps({"id": "B", "state": "done", "created": 2}),))
    db.commit()
    db.close()
    store = P.Store(path)
    assert [r["id"] for r in store.due_orders(time.time(), P.ACTIVE_STATES)] == ["A"]
    assert [r["id"] for r in store.orders_in(P.WAIT_STATES, [CHAT])] == ["A"]
    assert store.count_states() == {"collect": 1, "done": 1}


def test_seen_marks_are_pruned(tmp_path):
    store = P.Store(tmp_path / "db.sqlite3")
    store.put("seen", "old", {"at": time.time() - 10 * 86400})
    store.put("seen", "new", {"at": time.time()})
    assert store.prune("seen", time.time() - 7 * 86400) == 1
    assert store.get("seen", "new")
