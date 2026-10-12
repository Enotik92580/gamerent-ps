from fpc_fakes import enums, new_order_event, system_message_event


def setup(env, **settings):
    mod, c = env("AutoRefund_LuckyFunpay.py")
    mod.PLUGIN.sync = True
    mod.PLUGIN.store.set(enabled=True, **settings)
    return mod, c


def review(c, oid, stars, buyer="Buyer1", price=50.0, currency=enums.Currency.RUB, changed=False):
    if oid not in c.account.orders:
        c.account.add_order(oid, buyer=buyer, price=price, currency=currency, status=enums.OrderStatuses.CLOSED)
    c.account.set_review(oid, stars)
    verb = "изменил отзыв к заказу" if changed else "написал отзыв к заказу"
    return system_message_event(f"Покупатель {buyer} {verb} #{oid}.")


def test_bad_review_refund_and_blacklist(env):
    mod, c = setup(env)
    acc, tg = c.account, c.telegram
    mod.new_message(c, review(c, "AAAAAAAA", 1))
    assert acc.refunded == ["AAAAAAAA"] and "Buyer1" in c.blacklist
    assert "Рейтинг спасён" in tg.screen().text and "добавлен в ЧС" in tg.screen().text
    assert "AAAAAAAA" in c.sent_messages[-1][1]
    # повтор события по тому же заказу — без повторного возврата
    mod.new_message(c, review(c, "AAAAAAAA", 1, changed=True))
    assert acc.refunded == ["AAAAAAAA"]
    # 5 звёзд — ничего
    mod.new_message(c, review(c, "BBBBBBBB", 5, buyer="Good"))
    assert "BBBBBBBB" not in acc.refunded
    # отзыв изменили с 5 на 2 — теперь возврат
    mod.new_message(c, review(c, "BBBBBBBB", 2, buyer="Good", changed=True))
    assert "BBBBBBBB" in acc.refunded


def test_price_limit_and_currency(env):
    mod, c = setup(env, limit=300.0)
    acc, tg = c.account, c.telegram
    mod.new_message(c, review(c, "EXPENSIV", 1, price=5000))
    assert "EXPENSIV" not in acc.refunded and "выше лимита" in tg.screen().text
    mod.new_message(c, review(c, "USDCHEAP", 1, price=3, currency=enums.Currency.USD))  # 270₽
    assert "USDCHEAP" in acc.refunded
    mod.new_message(c, review(c, "USDEXPEN", 1, price=4, currency=enums.Currency.USD))  # 360₽
    assert "USDEXPEN" not in acc.refunded


def test_blacklisted_buyer_and_deleted_review(env):
    mod, c = setup(env, bl_delete=True)
    acc = c.account
    c.blacklist.append("Scammer")
    mod.new_order(c, new_order_event(acc.add_order("SCAM0001", buyer="Scammer", price=100)))
    assert acc.refunded == ["SCAM0001"]
    mod.new_order(c, new_order_event(acc.add_order("NORMAL01", buyer="Normal", price=100)))
    assert acc.refunded == ["SCAM0001"]
    mod.new_message(c, system_message_event("Покупатель Blackmail удалил отзыв к заказу #DDDDDDDD."))
    assert "Blackmail" in c.blacklist


def test_disabled_by_default_and_menu(env):
    mod, c = env("AutoRefund_LuckyFunpay.py")
    mod.PLUGIN.sync = True
    acc, tg = c.account, c.telegram
    mod.new_message(c, review(c, "OFFOFF01", 1))
    assert acc.refunded == []
    tg.message("/autoref")
    assert "выключен" in tg.screen().text
    tg.click("4⭐")
    tg.click("1⭐")
    d = mod.PLUGIN.store.snapshot()
    assert d["stars"]["4"] is True and d["stars"]["1"] is False
    tg.click("Лимит цены")
    tg.message("150,5")
    assert mod.PLUGIN.store["limit"] == 150.5
    tg.click("Текст покупателю")
    tg.message("-")
    tg.click("Плагин")
    assert mod.PLUGIN.store["enabled"] is True
    mod.new_message(c, review(c, "FOURSTAR", 4, price=100))
    assert acc.refunded == ["FOURSTAR"] and not c.sent_messages
