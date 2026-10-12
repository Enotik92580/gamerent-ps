import time

from fpc_fakes import enums, new_order_event, status_event


def due_all(p):
    p.queue.change(lambda q: [i.update(due=time.time() - 1) for i in q["items"].values()])
    p.process_due()


def test_closed_trigger_two_reminders(env):
    mod, c = env("ReviewReminder_LuckyFunpay.py")
    acc, p = c.account, mod.PLUGIN
    p.store.set(second=True, delay1=30, delay2=60)
    short = acc.add_order("ORDER001", buyer="Vasya", desc="Гайд по фарму", status=enums.OrderStatuses.CLOSED)
    mod.new_order(c, new_order_event(short))  # триггер «Оплачен» выключен
    assert p.queue["items"] == {}
    mod.status_changed(c, status_event(short))
    item = p.queue["items"]["ORDER001"]
    assert 29 * 60 < item["due"] - time.time() <= 30 * 60
    p.process_due()  # ещё рано
    assert not c.sent_messages
    due_all(p)
    assert "Vasya, спасибо за покупку" in c.sent_messages[-1][1] and "#ORDER001" in c.sent_messages[-1][1]
    assert p.queue["items"]["ORDER001"]["stage"] == 2
    due_all(p)
    assert "Напоминаю про отзыв на заказ #ORDER001 (Гайд по фарму)" in c.sent_messages[-1][1]
    assert p.queue["items"] == {} and len(c.sent_messages) == 2
    # повторное событие по тому же заказу не ставит его снова
    mod.status_changed(c, status_event(short))
    assert p.queue["items"] == {}


def test_review_or_refund_cancels(env):
    mod, c = env("ReviewReminder_LuckyFunpay.py")
    acc, p = c.account, mod.PLUGIN
    p.store.set(on_paid=True)
    a = acc.add_order("WITHREV1")
    b = acc.add_order("REFUND01")
    mod.new_order(c, new_order_event(a))
    mod.new_order(c, new_order_event(b))
    acc.set_review("WITHREV1", 5)
    acc.set_status("REFUND01", enums.OrderStatuses.REFUNDED)
    due_all(p)
    assert c.sent_messages == [] and p.queue["items"] == {}
    assert p.store["skipped_total"] == 2
    d = acc.add_order("REFUND02")
    mod.new_order(c, new_order_event(d))
    acc.set_status("REFUND02", enums.OrderStatuses.REFUNDED)
    mod.status_changed(c, status_event(d))
    assert p.queue["items"] == {}


def test_queue_survives_restart_and_menu(env):
    mod, c = env("ReviewReminder_LuckyFunpay.py")
    acc, tg = c.account, c.telegram
    tg.message("/reminder")
    tg.click("Оплачен")
    tg.click("Задержка 1")
    tg.message("0")
    assert "допустимо" in tg.screen().text
    tg.click("Ввести заново")
    tg.message("45")
    tg.click("Текст 1")
    tg.message("Отзыв на $order_title из $game от $date, $username?")
    mod.new_order(c, new_order_event(acc.add_order("PERSIST1", buyer="Petya", desc="Ключ")))
    # «перезапуск»: новый экземпляр читает очередь с диска
    mod.PLUGIN = None
    mod.init(c)
    p = mod.PLUGIN
    assert "PERSIST1" in p.queue["items"]
    due_all(p)
    assert c.sent_messages[-1][1].startswith("Отзыв на Ключ из Dota 2 от ") and "Petya?" in c.sent_messages[-1][1]
