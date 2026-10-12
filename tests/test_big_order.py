from fpc_fakes import ADMIN, enums, new_order_event


def test_thresholds_per_currency(env):
    mod, c = env("BigOrderAlert_LuckyFunpay.py")
    tg, acc = c.telegram, c.account
    n = len(tg.bot.timeline)
    mod.new_order(c, new_order_event(acc.add_order("SMALL001", price=999.99)))
    assert len(tg.bot.timeline) == n
    mod.new_order(c, new_order_event(acc.add_order("BIGRUB01", price=1000, buyer="RichGamer", desc="Прокачка VIP")))
    alert = tg.screen()
    assert "BIG ORDER ALERT" in alert.text and "1 000.00 RUB" in alert.text and "RichGamer" in alert.text
    assert (ADMIN, alert.mid) in tg.bot.pinned
    assert any(b.url and "chat/?node=" in b.url for b in tg.buttons(alert))
    mod.new_order(c, new_order_event(acc.add_order("BIGRUB01", price=1000)))  # повтор события — не дублируем
    assert tg.bot.timeline[-1] is alert
    mod.new_order(c, new_order_event(acc.add_order("USD00001", price=14.9, currency=enums.Currency.USD)))
    assert tg.bot.timeline[-1] is alert
    mod.new_order(c, new_order_event(acc.add_order("USD00002", price=20, currency=enums.Currency.USD)))
    assert "20.00 USD" in tg.screen().text


def test_settings(env):
    mod, c = env("BigOrderAlert_LuckyFunpay.py")
    tg, acc = c.telegram, c.account
    tg.message("/bigorder")
    tg.click("RUB > 1000")
    tg.message("5 000")
    assert mod.PLUGIN.store["limits"]["RUB"] == 5000
    tg.click("EUR > 15")
    tg.message("0")
    tg.click("Закреплять")
    tg.click("Мониторинг включен")
    assert mod.PLUGIN.store["enabled"] is False
    n = len(tg.bot.timeline)
    mod.new_order(c, new_order_event(acc.add_order("XXXXXXXX", price=99999)))
    assert len(tg.bot.timeline) == n
    tg.click("Мониторинг выключен")
    mod.new_order(c, new_order_event(acc.add_order("EUR00001", price=999, currency=enums.Currency.EUR)))
    assert len(tg.bot.timeline) == n + 1  # только перерисовка меню
    mod.new_order(c, new_order_event(acc.add_order("RUB00009", price=6000)))
    assert "BIG ORDER" in tg.screen().text and not tg.bot.pinned
    tg.message("/bigorder")
    tg.click("Тест")
    assert "RichGamer" in tg.screen().text
