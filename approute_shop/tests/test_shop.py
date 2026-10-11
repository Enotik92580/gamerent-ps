# -*- coding: utf-8 -*-
"""Scenario tests for AppRoute_LuckyFunpay.py (AppRoute Shop) with fake FunPay and AppRoute."""
import importlib.util
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("arshop", Path(__file__).resolve().parents[1] / "AppRoute_LuckyFunpay.py")
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)

SELLER, BUYER = 1, 2
CHAT = "users-1-2"


def make_order(oid="ORD1", status="PAID", amount=1, total="600", **extra):
    data = dict(id=oid, status=status, buyer_id=BUYER, seller_id=SELLER, chat_id=CHAT, amount=amount, sum=total,
                short_description="Steam Gift Card 5 USD, Моментально", subcategory=SimpleNamespace(id=7),
                buyer_username="buyer", currency="RUB")
    data.update(extra)
    return SimpleNamespace(**data)


class LotFields(SimpleNamespace):
    def renew_fields(self):
        return self


class Account:
    def __init__(self):
        self.id = SELLER
        self.orders, self.sent, self.refunds, self.saved = {}, [], [], []
        self.lots = {}
        self.proxy = None

    def get_order(self, oid):
        value = self.orders[oid]
        if isinstance(value, Exception):
            raise value
        return value

    def send_message(self, chat, text, *args):
        self.sent.append(text)
        return True

    def refund(self, oid):
        self.refunds.append(oid)
        self.orders[oid].status = "REFUNDED"

    def get_lot_fields(self, lid):
        return self.lots.setdefault(int(lid), LotFields(price=500.0, amount=10, active=True, title_ru="Steam Gift Card 5 USD",
                                                        title_en="", auto_delivery=False, subcategory=SimpleNamespace(id=7)))

    def save_lot(self, fields):
        self.saved.append((fields.price, fields.amount, fields.active))

    def method(self, *args, **kwargs):
        raise RuntimeError("no editor in tests")


class FakeAPI:
    def __init__(self):
        self.items = {("p1", "i1"): {"id": "i1", "name": "5 USD", "price": "5.00", "currency": "USD", "inStock": 20}}
        self.purchases, self.lookups = [], []
        self.purchase_result = None
        self.purchase_error = None
        self.orders = {}
        self.hidden = {}
        self.balance_value = Decimal("100")

    def __call__(self, settings):
        return self

    def item(self, sid, iid):
        value = self.items.get((sid, iid))
        if value is None:
            raise P.APIError(404, 9)
        return dict(value)

    def service(self, sid):
        return {"id": sid, "items": [v for (s, _), v in self.items.items() if s == sid]}

    def balance(self):
        return self.balance_value

    def purchase(self, body):
        self.purchases.append(body)
        if self.purchase_error:
            raise self.purchase_error
        ref = body["referenceId"]
        result = self.purchase_result or {"status": "SUCCESS", "orderId": "tx-1", "price": "5.00", "currency": "USD",
                                          "vouchers": [{"code": "AAAA-BBBB-CCCC", "serialNumber": "SN1", "expiresAt": "2027-01-01"}]}
        result = dict(result, reference=ref)
        self.orders[ref] = result
        return result

    def find_order(self, reference, unhide=False):
        self.lookups.append((reference, unhide))
        if unhide and reference in self.hidden:
            return self.hidden[reference]
        return self.orders.get(reference)

    def catalog(self, checkpoint=None, on_page=None):
        return [{"id": "p1", "name": "Steam Gift Card", "categoryName": "Gift Cards", "type": "voucher",
                 "items": [dict(v) for (s, _), v in self.items.items() if s == "p1"]}]

    def refresh_limits(self):
        pass


@pytest.fixture
def env(tmp_path):
    account, api = Account(), FakeAPI()
    cardinal = SimpleNamespace(account=account, plugins={}, telegram=None)
    shop = P.Shop(cardinal, store=P.Store(tmp_path / "db.sqlite3"), api_factory=api)
    shop.store.configure(enabled=True, api_key="k" * 16, rate="100", markup="20")
    shop.store.put("lot", "100", {"id": "100", "name": "Steam Gift Card 5 USD", "conditions": ["Steam Gift Card 5 USD"],
                                  "product_id": "p1", "item_id": "i1", "subcategory": 7, "auto_price": True})
    return SimpleNamespace(shop=shop, account=account, api=api, store=shop.store)


def run(env, oid="ORD1", times=1, **order):
    if oid not in env.account.orders:
        env.account.orders[oid] = make_order(oid, **order)
        env.shop.enqueue(oid, CHAT, str(BUYER))
    for _ in range(times):
        record = env.store.get("order", oid)
        record["next_at"] = 0
        env.store.put("order", oid, record)
        env.shop.process(oid)
    return env.store.get("order", oid)


# --- delivery -----------------------------------------------------------------------------------

def test_codes_delivered_right_after_payment(env):
    record = run(env)
    assert record["state"] == "done" and record["reference"] == "fpc-ORD1"
    text = "\n".join(env.account.sent)
    assert "AAAA-BBBB-CCCC" in text and "S/N: SN1" in text and "Срок действия: 2027-01-01" in text
    assert "Steam Gift Card 5 USD" in text and "Количество: 1" in text
    assert env.api.purchases[0] == {"ordersType": "shop", "referenceId": "fpc-ORD1",
                                    "orders": [{"denominationId": "i1", "quantity": 1}]}
    assert record["cost"] == "5.00" and "outbox" not in record or not record["outbox"]
    s = env.store.settings()
    assert s["total_sales"] == 1 and Decimal(s["total_cost"]) == Decimal("5.00")


def test_quantity_from_order_and_numbered_codes(env):
    env.api.purchase_result = {"status": "COMPLETED", "items": [{"pin": "CODE-1"}, {"pin": "CODE-2"}, {"pin": "CODE-3"}]}
    record = run(env, amount=3, total="1800")
    assert env.api.purchases[0]["orders"][0]["quantity"] == 3
    text = "\n".join(env.account.sent)
    assert "1) CODE-1" in text and "3) CODE-3" in text and record["codes_count"] == 3


def test_hidden_codes_are_revealed(env):
    env.api.purchase_result = {"status": "SUCCESS"}
    record = run(env)
    assert record["state"] == "reveal"
    env.api.hidden["fpc-ORD1"] = {"status": "SUCCESS", "reference": "fpc-ORD1", "result": {"codes": [{"code": "HIDDEN-1"}]}}
    record = run(env)
    assert record["state"] == "done" and any("HIDDEN-1" in m for m in env.account.sent)
    assert ("fpc-ORD1", True) in env.api.lookups


def test_esim_block(env):
    env.api.purchase_result = {"status": "SUCCESS", "esim": {"qrCodeUrl": "https://qr.example/1.png",
                               "lpa": "LPA:1$smdp.example.com$MATCH-123", "iccid": "8901", "iosUrl": "https://ios",
                               "androidUrl": "https://android"}, "attributes": {"dataVolume": "5 GB", "price": "3"}}
    run(env)
    text = "\n".join(env.account.sent)
    for part in ("Данные eSIM", "QR: https://qr.example/1.png", "SM-DP+: smdp.example.com", "Activation code: MATCH-123",
                 "ICCID: 8901", "Установка на iOS: https://ios", "Установка на Android: https://android",
                 "Дополнительные данные", "dataVolume: 5 GB"):
        assert part in text
    assert "price: 3" not in text


def test_no_codes_goes_to_seller(env):
    env.api.purchase_result = {"status": "SUCCESS"}
    run(env)
    for _ in range(3):
        record = run(env)
    assert record["state"] == "review" and not env.account.refunds
    assert any("выдаст коды вручную" in m for m in env.account.sent)


def test_partial_delivery_is_marked(env):
    env.api.purchase_result = {"status": "PARTIALLY_COMPLETED", "codes": [{"code": "ONLY-ONE"}]}
    record = run(env, amount=2, total="1200")
    assert record["state"] == "partial" and record["cost"] == "5.00"
    assert any("ONLY-ONE" in m for m in env.account.sent)


# --- idempotency and network ---------------------------------------------------------------------

def test_one_order_one_purchase(env):
    run(env)
    env.shop.enqueue("ORD1", CHAT, str(BUYER))
    run(env, times=3)
    assert len(env.api.purchases) == 1


def test_network_error_checks_provider_then_refunds(env):
    env.api.purchase_error = P.TransientError("down")
    record = run(env)
    assert record["state"] == "pending" and record["uncertain"]
    record["sent_at"] -= 120
    env.store.put("order", "ORD1", record)
    record = run(env, times=5)
    assert record["state"] == "refunded" and env.account.refunds == ["ORD1"]
    assert any("возвращены" in m for m in env.account.sent)


def test_network_error_but_order_created_is_delivered(env):
    env.api.purchase_error = P.TransientError("down")
    run(env)
    env.api.orders["fpc-ORD1"] = {"status": "SUCCESS", "reference": "fpc-ORD1", "codes": [{"code": "LATE-CODE"}]}
    record = run(env)
    assert record["state"] == "done" and not env.account.refunds and any("LATE-CODE" in m for m in env.account.sent)


def test_long_provider_wait_never_refunds(env):
    env.api.purchase_result = {"status": "IN_PROGRESS"}
    record = run(env)
    assert record["state"] == "pending"
    record["sent_at"] -= P.POLL_TIMEOUT + 5
    env.store.put("order", "ORD1", record)
    record = run(env)
    assert record["state"] == "pending" and record["timeout_told"] and not env.account.refunds
    assert any("дольше обычного" in m for m in env.account.sent)
    env.api.orders["fpc-ORD1"] = {"status": "SUCCESS", "reference": "fpc-ORD1", "codes": [{"code": "FINALLY"}]}
    assert run(env)["state"] == "done"


def test_pacer_block_sends_nothing(env):
    env.api.purchase_error = P.PacerBlocked(time.time() + 60)
    record = run(env)
    assert record["state"] == "ready" and "sent_at" not in record and record["next_at"] > time.time() + 30


# --- protections ---------------------------------------------------------------------------------

def test_limit_per_order_refunds(env):
    env.store.configure(max_quantity=2)
    record = run(env, times=3, amount=3, total="1800")
    assert record["state"] == "refunded" and not env.api.purchases
    assert any("лимит" in m for m in env.account.sent)


def test_out_of_stock_disables_lot_and_refunds(env):
    env.api.items[("p1", "i1")]["inStock"] = 0
    record = run(env, times=3)
    assert record["state"] == "refunded" and not env.api.purchases
    assert env.account.lots[100].active is False and env.store.get("lot", "100")["disabled_kind"] == "stock"


def test_not_enough_stock_for_quantity(env):
    env.api.items[("p1", "i1")]["inStock"] = 2
    assert run(env, times=3, amount=3, total="1800")["state"] == "refunded"


def test_on_demand_zero_stock_is_available(env):
    env.api.items[("p1", "i1")].update(inStock=0, available=True)
    assert run(env)["state"] == "done"


def test_low_balance_disables_all_lots(env):
    env.store.put("lot", "200", {"id": "200", "name": "Другой", "conditions": ["нечто другое"], "product_id": "p1", "item_id": "i1"})
    env.api.balance_value = Decimal("1")
    record = run(env, times=3)
    assert record["state"] == "refunded" and not env.api.purchases
    assert env.account.lots[100].active is False and env.account.lots[200].active is False


def test_guard_stops_without_refund(env):
    record = run(env, total="100")  # 1 USDT paid, 5 USDT cost, ×3 + 0.5 = 3.5
    assert record["state"] == "review" and not env.api.purchases and not env.account.refunds
    assert any("ручной проверки" in m for m in env.account.sent)


def test_guard_allows_small_margin(env):
    env.api.items[("p1", "i1")]["price"] = "3.40"
    assert run(env, total="100")["state"] == "done"


def test_guard_usd_order(env):
    assert run(env, total="2", currency="USD")["state"] == "done"
    assert run(env, "ORD2", total="1", currency="USD")["state"] == "review"


def test_guard_can_be_disabled(env):
    env.store.configure(sanity_enabled=False)
    assert run(env, total="1")["state"] == "done"


def test_auto_refund_off_goes_to_seller(env):
    env.store.configure(auto_refund=False)
    env.api.items[("p1", "i1")]["inStock"] = 0
    record = run(env)
    assert record["state"] == "review" and not env.account.refunds
    assert any("вернёт деньги" in m for m in env.account.sent)


@pytest.mark.parametrize("code,kind,all_lots", [(9, "stock", False), (10, "balance", True), (13, "limit", True)])
def test_provider_refusals(env, code, kind, all_lots):
    env.store.put("lot", "200", {"id": "200", "name": "Другой", "conditions": ["нечто"], "product_id": "p9", "item_id": "i9"})
    env.api.purchase_error = P.APIError(422, code)
    record = run(env, times=3)
    assert record["state"] == "refunded"
    assert env.store.get("lot", "100")["disabled_kind"] == kind
    assert (200 in env.account.lots and env.account.lots[200].active is False) == all_lots


def test_provider_rejection_status_refunds(env):
    env.api.purchase_result = {"status": "CANCELLED"}
    assert run(env, times=3)["state"] == "refunded"


def test_rate_limited_purchase_is_resent_with_same_reference(env):
    env.api.purchase_error = P.RateLimitError(time.time() - 1)
    record = run(env)
    assert record["state"] == "pending" and record["rate_limited"]
    env.api.purchase_error = None
    record = run(env, times=4)
    assert record["state"] == "done" and [b["referenceId"] for b in env.api.purchases] == ["fpc-ORD1", "fpc-ORD1"]
    assert not env.account.refunds


def test_unclear_api_error_is_reconciled_not_refunded(env):
    env.api.purchase_error = P.APIError(500, None)
    record = run(env)
    assert record["state"] == "pending" and not env.account.refunds


def test_status_off_ignores_queued_order(env):
    env.store.configure(enabled=False)
    record = run(env)
    assert record["state"] == "ignored" and not env.api.purchases


def test_new_order_hook_respects_status(env, monkeypatch):
    monkeypatch.setattr(P, "SHOP", env.shop)
    event = SimpleNamespace(order=make_order("ORD9"))
    env.store.configure(enabled=False)
    P.new_order(env.shop.cardinal, event)
    assert env.store.get("order", "ORD9") is None
    env.store.configure(enabled=True)
    P.new_order(env.shop.cardinal, event)
    assert env.store.get("order", "ORD9")["state"] == "queued"


def test_orders_of_one_buyer_go_one_by_one(env):
    env.api.purchase_result = {"status": "IN_PROGRESS"}
    run(env, "ORD1")
    record = run(env, "ORD2")
    assert record["state"] == "queued" and len(env.api.purchases) == 1


def test_foreign_order_is_ignored(env):
    record = run(env, short_description="Something else entirely")
    assert record["state"] == "ignored" and not env.account.sent


def test_builtin_autodelivery_blocks(env):
    assert run(env, order_secrets=["x"])["state"] == "review" and not env.api.purchases


# --- matching -----------------------------------------------------------------------------------

def lot(lid, conditions, name="lot"):
    return {"id": lid, "name": name, "conditions": conditions, "item_id": "i", "product_id": "p"}


def test_numbers_match_exactly():
    order = make_order(short_description="Steam 1000 TL")
    assert P.match_lot(order, [lot("1", ["Steam", "100"])]) is None
    assert P.match_lot(order, [lot("1", ["Steam", "1000"])])["id"] == "1"


def test_all_phrases_required_and_best_match_wins():
    order = make_order(short_description="PlayStation Turkey 500 TL gift card")
    lots = [lot("1", ["PlayStation", "500"]), lot("2", ["PlayStation", "500", "Turkey"]), lot("3", ["Xbox"])]
    assert P.match_lot(order, lots)["id"] == "2"


def test_reset_conditions_use_name():
    order = make_order(short_description="Roblox 800 Robux card")
    assert P.match_lot(order, [lot("1", [], name="Roblox 800 Robux")])["id"] == "1"


def test_standardized_parameters():
    order = make_order(short_description="", lot_params=[("country", "Турция"), ("nominal", "500 TL")])
    lots = [lot("1", ["country=Турция", "nominal=500 TL"]), lot("2", ["country=США", "nominal=500 TL"])]
    assert P.match_lot(order, lots)["id"] == "1"


def test_ambiguous_equal_lots_raise():
    order = make_order(short_description="Steam 5 USD")
    with pytest.raises(P.ShopError):
        P.match_lot(order, [lot("1", ["Steam 5 USD"]), lot("2", ["Steam 5 USD"])])


# --- templates and formatting --------------------------------------------------------------------

def test_template_requires_codes():
    with pytest.raises(P.ShopError):
        P.validate_template("Спасибо, {lot_name}")
    assert P.validate_template("Ваш товар: {codes}")


def test_unknown_braces_fall_back_to_standard():
    text = P.render_template("Привет {name} {codes}", lot_name="X", quantity=1, codes="CODE", order_id="A")
    assert text == P.DEFAULT_TEMPLATE.format(lot_name="X", quantity=1, codes="CODE", order_id="A")


def test_custom_template_used(env):
    env.store.configure(template="Заказ {order_id}: {codes}")
    run(env)
    assert env.account.sent[0].startswith("Заказ ORD1: 🔑 Ваши коды:")


def test_long_messages_are_split():
    text = "\n".join(f"{i}) CODE-{i:04d}-XXXXXXXXXXXXXXXX" for i in range(200))
    parts = P.split_message(text)
    assert len(parts) > 1 and all(len(p) <= 1800 for p in parts) and "\n".join(parts) == text


def test_price_formula():
    s = dict(P.DEFAULTS, rate="100", markup="20", min_price="1", max_price="1000000")
    assert P.calculate_price("10", s) == "1200.00"
    assert P.calculate_price("10", dict(s, max_price="1000")) == "1000.00"


def test_lot_id_parsing():
    assert P.lot_id_from_text("75790150") == "75790150"
    assert P.lot_id_from_text("https://funpay.com/lots/offer?id=75790150") == "75790150"
    with pytest.raises(P.ShopError):
        P.lot_id_from_text("https://funpay.com/lots/1086/")


# --- prices, stock, catalogue --------------------------------------------------------------------

def test_sync_updates_price_and_stock(env):
    env.shop.refresh_items()
    changed, errors = env.shop.sync_lots()
    fields = env.account.lots[100]
    assert (changed, errors) == (1, 0) and fields.price == 600.0 and fields.amount == 20


def test_sync_ignores_small_changes(env):
    env.account.get_lot_fields(100).price = 600.3
    env.account.lots[100].amount = 20
    env.shop.refresh_items()
    assert env.shop.sync_lots() == (0, 0)


def test_stock_capped_by_limit_and_on_demand(env):
    env.store.configure(max_quantity=5)
    env.shop.refresh_items()
    env.shop.sync_lots()
    assert env.account.lots[100].amount == 5
    env.api.items[("p1", "i1")].update(inStock=None)
    env.shop.refresh_items()
    env.shop.sync_lots()
    assert env.account.lots[100].active is True and env.account.lots[100].amount == 5


def test_missing_item_disables_then_auto_enables(env):
    del env.api.items[("p1", "i1")]
    env.shop.refresh_items()
    env.shop.sync_lots()
    assert env.account.lots[100].active is False and env.store.get("lot", "100")["disabled_kind"] == "stock"
    env.api.items[("p1", "i1")] = {"id": "i1", "price": "5", "currency": "USD", "inStock": 3}
    env.shop.refresh_items()
    env.shop.sync_lots()
    assert env.account.lots[100].active is True and env.account.lots[100].amount == 3


def test_balance_disabled_lot_is_not_auto_enabled(env):
    env.account.get_lot_fields(100).active = False
    env.store.patch("lot", "100", disabled_kind="balance")
    env.shop.refresh_items()
    env.shop.sync_lots()
    assert env.account.lots[100].active is False


def test_auto_price_off_lot_is_skipped(env):
    env.store.patch("lot", "100", auto_price=False)
    env.shop.refresh_items()
    assert env.shop.sync_lots() == (0, 0) and not env.account.saved


def test_low_balance_alert_once(env):
    sent = []
    env.shop.notify = lambda text, *a, **k: sent.append(text)
    env.shop.record_balance(Decimal("10"))
    env.shop.record_balance(Decimal("9"))
    assert len(sent) == 1
    env.shop.record_balance(Decimal("50"))
    env.shop.record_balance(Decimal("5"))
    assert len(sent) == 2


def test_catalog_excludes_direct_topup():
    pages = [{"items": [{"id": 1, "type": "voucher", "name": "Steam"}, {"id": 2, "type": "direct_topup", "name": "MLBB"}], "total": 2}]
    api = P.AppRouteAPI(dict(P.DEFAULTS, api_key="k" * 16), transport=None)
    api.request = lambda *a, **k: pages[0]
    assert [p["id"] for p in api.catalog()] == [1]


def test_search_ranks_and_translates():
    products = [{"id": "p1", "name": "Steam Wallet Card", "categoryName": "Gift cards",
                 "items": [{"id": "a", "name": "500 TRY", "price": "15"}, {"id": "b", "name": "100 TRY", "price": "3"}]},
                {"id": "p2", "name": "PlayStation Network Turkey", "categoryName": "PlayStation",
                 "items": [{"id": "c", "name": "500 TL", "price": "15"}]}]
    found = P.search_items(products, "стим 500")
    assert found[0][1]["id"] == "a"
    assert P.search_items(products, "playstation турция")[0][1]["id"] == "c"
    assert P.search_items(products, "zzzz") == []


def test_old_table_layout_is_upgraded(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE records (kind TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY(kind,id))")
    db.execute("INSERT INTO records VALUES ('order', 'A1', '{\"id\": \"A1\", \"state\": \"queued\", \"created\": 1}')")
    db.commit()
    db.close()
    store = P.Store(path)
    assert [r["id"] for r in store.orders_in({"queued"})] == ["A1"]


def test_foreign_table_is_reported(tmp_path):
    import sqlite3
    path = tmp_path / "foreign.sqlite3"
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE records (name TEXT)")
    db.commit()
    db.close()
    with pytest.raises(P.ShopError):
        P.Store(path)
