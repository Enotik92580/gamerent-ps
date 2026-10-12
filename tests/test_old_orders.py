import datetime
import threading

from fpc_fakes import enums


def wait():
    for t in threading.enumerate():
        if t.name == "OldOrdersScan":
            t.join(10)


def test_scan_finds_only_old_paid(env):
    mod, c = env("OldOrders_LuckyFunpay.py")
    acc, tg = c.account, c.telegram
    old = datetime.datetime.now() - datetime.timedelta(hours=30)
    new = datetime.datetime.now() - datetime.timedelta(hours=2)
    for i in range(7):
        acc.add_order(f"OLD{i:05d}", date=old - datetime.timedelta(hours=i), buyer=f"B{i}")
    acc.add_order("NEW00001", date=new)
    acc.add_order("CLS00001", date=old, status=enums.OrderStatuses.CLOSED)
    acc.add_order("REF00001", date=old, status=enums.OrderStatuses.REFUNDED)
    tg.message("/old_orders")
    tg.click("Начать сканирование")
    wait()
    data = mod.PLUGIN.store.snapshot()
    assert sorted(o["id"] for o in data["result"]) == [f"OLD{i:05d}" for i in range(7)]
    assert data["pages"] == 3  # по 3 заказа на странице
    assert "Найдено «зависших» сделок: 7" in tg.screen().text
    tg.click("Сформировать отчет")
    report = tg.screen().text
    assert "<code>Здравствуйте! Прошу подтвердить" in report and "#OLD00006" in report and "NEW00001" not in report


def test_clean_and_custom_report(env):
    mod, c = env("OldOrders_LuckyFunpay.py")
    tg = c.telegram
    tg.message("/old_orders")
    tg.click("Начать сканирование")
    wait()
    assert "Всё чисто" in tg.screen().text
    tg.click("Текст отчёта")
    tg.message("без переменной")
    assert "$orders" in tg.screen().text
    tg.click("Ввести заново")
    tg.message("Закройте $orders (более $hours ч)")
    tg.click("Порог")
    tg.message("48")
    c.account.add_order("ZZZZZZZZ", date=datetime.datetime.now() - datetime.timedelta(hours=49))
    tg.click("Начать сканирование")
    wait()
    tg.click("Сформировать отчет")
    assert "Закройте #ZZZZZZZZ (более 48 ч)" in tg.screen().text


def test_report_splits_many_orders(env):
    mod, c = env("OldOrders_LuckyFunpay.py")
    orders = [{"id": f"X{i:07d}", "ts": 0, "buyer": "b", "price": 1, "currency": "₽", "desc": ""} for i in range(85)]
    assert len(mod.PLUGIN.report(orders)) == 3
