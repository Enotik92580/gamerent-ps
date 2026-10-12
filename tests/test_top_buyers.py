from fpc_fakes import enums, status_event


def test_scan_top_and_live(env, monkeypatch):
    import random
    monkeypatch.setattr(random, "uniform", lambda a, b: 0)
    mod, c = env("TopBuyers_LuckyFunpay.py")
    acc, tg, p = c.account, c.telegram, mod.PLUGIN
    p.sync = True
    C = enums.OrderStatuses.CLOSED
    acc.add_order("K0000001", buyer="Whale", buyer_id=11, price=5000, status=C)
    acc.add_order("K0000002", buyer="Whale", buyer_id=11, price=100, currency=enums.Currency.USD, status=C)  # 9000₽
    for i in range(10):
        acc.add_order(f"S{i:07d}", buyer="Small", buyer_id=12, price=100, status=C)
    acc.add_order("PAID0001", buyer="Payer", buyer_id=13, price=99999)  # не закрыт
    acc.add_order("REF00001", buyer="Ref", buyer_id=14, price=99999, status=enums.OrderStatuses.REFUNDED)
    tg.message("/top_buyers")
    tg.click("Обновить")
    text = tg.screen().text
    assert "Клиентов в базе: <b>2</b>" in text and "Общая выручка: <b>15 000 ₽</b>" in text
    assert text.index("Whale") < text.index("Small") and "🥇" in text and "100 USD" in text
    assert 'href="https://funpay.com/users/11/"' in text
    # живое закрытие и защита от двойного учёта
    acc.set_status("PAID0001", C)
    paid = [s for s in acc.sales if s.id == "PAID0001"][0]
    mod.status_changed(c, status_event(paid))
    mod.status_changed(c, status_event(paid))
    assert p.store["buyers"]["13"]["orders"] == 1
    assert p.scan(full=False) == 0
    tg.message("/top_buyers")
    assert tg.screen().text.split("🥇")[1].startswith(" <a href=\"https://funpay.com/users/13/\">Payer")
    tg.click("Сброс статистики")
    tg.click("Да, сбросить")
    assert len(p.store["buyers"]) == 3 and len(p.store["counted"]) == 13
