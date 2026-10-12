import datetime

from fpc_fakes import enums, message_event, new_order_event


def test_scan_render_and_realtime(env, monkeypatch):
    mod, c = env("ActivityHeatmap_LuckyFunpay.py")
    import random
    monkeypatch.setattr(random, "uniform", lambda a, b: 0)
    acc, tg = c.account, c.telegram
    p = mod.PLUGIN
    p.sync = True
    msk = datetime.timezone(datetime.timedelta(hours=3))
    friday_evening = datetime.datetime(2026, 10, 9, 20, 30, tzinfo=msk)  # пятница
    monday_night = datetime.datetime(2026, 10, 5, 2, 0, tzinfo=msk)
    for i in range(6):
        acc.add_order(f"FRI{i:05d}", date=friday_evening + datetime.timedelta(minutes=i),
                      status=enums.OrderStatuses.CLOSED)
    acc.add_order("MON00001", date=monday_night, status=enums.OrderStatuses.CLOSED)
    acc.add_order("REF00001", date=monday_night, status=enums.OrderStatuses.REFUNDED)
    tg.message("/activity")
    tg.click("Обновить")
    assert "учтено заказов — 7" in "\n".join(tg.texts())
    text = tg.screen().text
    assert "Лучший день недели: <b>Пятница</b>" in text and "Пиковый час: <b>20:00–21:00</b>" in text
    fri_row = [l for l in text.split("\n") if l.startswith("<code>Пт")][0]
    assert fri_row.endswith("🔴") and "🟢" in [l for l in text.split("\n") if l.startswith("<code>Пн")][0]
    # повторный скан не удваивает
    assert p.scan() == 0
    # живые события: заказ и первое сообщение; второе сообщение подряд не считается
    mod.new_order(c, new_order_event(acc.add_order("LIVE0001")))
    mod.new_message(c, message_event("привет", chat_id=9001))
    mod.new_message(c, message_event("ещё", chat_id=9001))
    mod.new_message(c, message_event("мой", chat_id=9001, author_id=acc.id))
    d = p.store.snapshot()
    assert sum(map(sum, d["orders"])) == 8 and sum(map(sum, d["messages"])) == 1
    tg.message("/activity")
    tg.click("Цвета")
    assert "ПИК" in str(tg.bot.answers[-1])
    tg.click("Сброс")
    tg.click("Да")
    assert sum(map(sum, p.store["orders"])) == 8  # пересканировали историю + LIVE0001 в списке продаж
