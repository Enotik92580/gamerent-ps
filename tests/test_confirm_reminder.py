import time

from fpc_fakes import enums, new_order_event, status_event


def due_all(p):
    p.queue.change(lambda q: [i.update(due=time.time() - 1) for i in q["items"].values()])
    p.process_due()


def test_two_stage_and_status_check(env):
    mod, c = env("ConfirmReminder_LuckyFunpay.py")
    acc, tg, p = c.account, c.telegram, mod.PLUGIN
    tg.message("/confirm_reminder")
    tg.click("Второе напоминание")
    tg.click("Задержка 2")
    tg.message("30")
    a = acc.add_order("PAID0001", buyer="Kolya", desc="Аккаунт")
    mod.new_order(c, new_order_event(a))
    item = p.queue["items"]["PAID0001"]
    assert 14 * 60 < item["due"] - time.time() <= 15 * 60
    due_all(p)
    assert "Kolya, вижу, вы уже получили товар" in c.sent_messages[-1][1] and "#PAID0001" in c.sent_messages[-1][1]
    stage2 = p.queue["items"]["PAID0001"]
    assert stage2["stage"] == 2 and 29 * 60 < stage2["due"] - time.time() <= 30 * 60
    due_all(p)
    assert "напоминаю про заказ #PAID0001 (Аккаунт)" in c.sent_messages[-1][1]
    assert p.queue["items"] == {}


def test_confirmed_or_refunded_is_silent(env):
    mod, c = env("ConfirmReminder_LuckyFunpay.py")
    acc, p = c.account, mod.PLUGIN
    for oid in ("CONF0001", "REF00001", "LATE0001"):
        mod.new_order(c, new_order_event(acc.add_order(oid)))
    acc.set_status("CONF0001", enums.OrderStatuses.CLOSED)
    mod.status_changed(c, status_event(acc.sales[2]))  # событие о подтверждении CONF0001
    acc.set_status("REF00001", enums.OrderStatuses.REFUNDED)  # событие не пришло — проверка перед отправкой
    due_all(p)
    assert [m[1] for m in c.sent_messages if "LATE0001" in m[1]] and len(c.sent_messages) == 1
    assert p.store["skipped_total"] == 1
