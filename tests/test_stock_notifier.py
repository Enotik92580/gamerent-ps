import time

from fpc_fakes import enums, new_order_event


def setup(env):
    mod, c = env("StockNotifier_LuckyFunpay.py")
    mod.PLUGIN.store.set(delay=0)
    return mod, c


def wait_threads():
    import threading
    for t in threading.enumerate():
        if t.name == "StockNotifierCheck":
            t.join(5)


def test_add_lot_and_alert_low_and_empty(env):
    mod, c = setup(env)
    tg, acc = c.telegram, c.account
    lot = acc.add_lot(301, "Ключ GTA V (Steam)", secrets=["k1", "k2", "k3", "k4", "k5", "k6"])
    assert tg.message("/stock_notifier")
    assert "Stock Notifier" in tg.screen().text
    tg.click("Добавить лот")
    tg.message(f"https://funpay.com/lots/offer?id={lot}")
    assert "Лот добавлен" in tg.screen().text and "6" in tg.screen().text
    tg.click("Настроить")
    tg.click("Изменить порог")
    tg.message("abc")
    assert "нужно целое число" in tg.screen().text
    tg.click("Ввести заново")
    tg.message("5")
    assert "Порог: <b>5</b>" in tg.screen().text
    tg.click("Текст: Мало товара")
    tg.message("Осталось $stock из $threshold: $lot_name <b>")
    # sale: 6 -> 5
    acc.lots[lot]["secrets"] = "k1\nk2\nk3\nk4\nk5"
    short = acc.add_order("AAAAAAAA", desc="Ключ GTA V (Steam), 1 шт.", subcat=301)
    mod.new_order(c, new_order_event(short))
    wait_threads()
    alert = tg.screen()
    assert "Осталось <b>5</b> из <b>5</b>: <b>Ключ GTA V (Steam)</b> &lt;b&gt;" == alert.text
    assert any(b.url and f"offerEdit?offer={lot}" in b.url for b in tg.buttons(alert))
    # empty
    acc.lots[lot]["secrets"] = ""
    mod.new_order(c, new_order_event(acc.add_order("BBBBBBBB", desc="Ключ GTA V (Steam)", subcat=301)))
    wait_threads()
    assert "Товар закончился" in tg.screen().text and "<b>0</b>" in tg.screen().text


def test_no_alert_above_threshold_and_unrelated_orders(env):
    mod, c = setup(env)
    tg, acc = c.telegram, c.account
    lot = acc.add_lot(301, "Ключ Cyberpunk", secrets=[f"k{i}" for i in range(20)])
    other = acc.add_lot(101, "Арканы", secrets=["a"])
    mod.PLUGIN.store.change(lambda d: d["lots"].update({str(lot): {"name": "Ключ Cyberpunk", "node": 301,
                                                                     "threshold": 5}}))
    n = len(tg.bot.timeline)
    mod.new_order(c, new_order_event(acc.add_order("CCCCCCCC", desc="Ключ Cyberpunk", subcat=301)))
    mod.new_order(c, new_order_event(acc.add_order("DDDDDDDD", desc="Арканы", subcat=101)))
    wait_threads()
    assert len(tg.bot.timeline) == n
    assert ("get_lot_fields", other) not in acc.calls
    assert mod.PLUGIN.store["lots"][str(lot)]["stock"] == 20


def test_lot_without_autodelivery_and_errors(env):
    mod, c = setup(env)
    tg, acc = c.telegram, c.account
    lot = acc.add_lot(202, "Буст", amount=10)
    tg.message("/stock_notifier")
    tg.click("Добавить лот")
    tg.message(str(lot))
    assert "выключена встроенная автовыдача" in tg.screen().text
    tg.click("Меню")
    assert "⚪️" in str([b.text for b in tg.buttons()])
    tg.click("Добавить лот")
    tg.message("99999999")
    assert "Лот не найден" in tg.screen().text
    tg.click("Меню")
    tg.click("Добавить лот")
    tg.message(str(lot))
    assert "уже отслеживается" in tg.screen().text


def test_delete_toggle_guide_and_unauthorized(env):
    mod, c = setup(env)
    tg, acc = c.telegram, c.account
    lot = acc.add_lot(301, "Ключ", secrets=["1"])
    tg.message("/stock_notifier")
    assert not tg.message("/stock_notifier", user_id=1, chat_id=1) or not tg.texts(1)
    tg.click("Добавить лот")
    tg.message(str(lot))
    tg.click("Настроить")
    tg.click("Удалить из отслеживания")
    tg.click("Да, удалить")
    assert mod.PLUGIN.store["lots"] == {}
    tg.click("Уведомления")
    assert mod.PLUGIN.store["enabled"] is False
    tg.click("Инструкция")
    assert "Как включить" in tg.screen().text
    tg.click("➡️")
    assert "Добавление лота" in tg.screen().text
    assert "stock_notifier" in tg.commands


def test_disabled_plugin_does_not_alert(env):
    mod, c = setup(env)
    acc = c.account
    lot = acc.add_lot(301, "Ключ", secrets=[])
    mod.PLUGIN.store.change(lambda d: d.update(enabled=False, lots={str(lot): {"name": "Ключ", "node": 301,
                                                                              "threshold": 5}}))
    n = len(c.telegram.bot.timeline)
    mod.new_order(c, new_order_event(acc.add_order("EEEEEEEE", desc="Ключ")))
    wait_threads()
    assert len(c.telegram.bot.timeline) == n
