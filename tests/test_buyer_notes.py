from fpc_fakes import message_event, new_order_event


def test_note_commands_and_alerts(env):
    mod, c = env("BuyerNotes_LuckyFunpay.py")
    tg, acc = c.telegram, c.account
    tg.message("/note")
    assert "Использование" in tg.screen().text
    tg.message("/note Buyer1 Постоянный клиент, просил скидку 5%")
    assert "Заметка добавлена" in tg.screen().text
    tg.message("/note buyer1")
    assert "Постоянный клиент" in tg.screen().text
    tg.message("/note Nobody")
    assert "Заметки на <b>Nobody</b> нет" in tg.screen().text
    tg.message("/note b@d! text")
    assert "Ник:" in tg.screen().text
    # сообщение от пользователя с заметкой → уведомление; повтор в течение 5 минут — нет
    n = len(tg.bot.timeline)
    mod.new_message(c, message_event("Привет, есть скидка?"))
    assert "Сообщение от пользователя с заметкой" in tg.screen().text and "есть скидка" in tg.screen().text
    mod.new_message(c, message_event("ау"))
    assert len(tg.bot.timeline) == n + 1
    # свои сообщения и сообщения ботом игнорируются
    mod.new_message(c, message_event("мой ответ", author=acc.username, author_id=acc.id))
    assert len(tg.bot.timeline) == n + 1
    # заказ — всегда уведомление
    mod.new_order(c, new_order_event(acc.add_order("ORD12345", buyer="Buyer1")))
    assert "Заказ от пользователя с заметкой" in tg.screen().text and "просил скидку" in tg.screen().text
    # другой покупатель — тишина
    mod.new_order(c, new_order_event(acc.add_order("ORD99999", buyer="Other")))
    assert "ORD99999" not in tg.screen().text


def test_gui_add_edit_search_delete(env):
    mod, c = env("BuyerNotes_LuckyFunpay.py")
    tg = c.telegram
    tg.message("/notes")
    tg.click("Добавить")
    tg.message("VipUser")
    tg.message("Берёт оптом, выдавать бонус")
    assert "VipUser" in tg.screen().text and "оптом" in tg.screen().text
    tg.click("Изменить")
    tg.message("x" * 1001)
    assert "1000" in tg.screen().text
    tg.click("Ввести заново")
    tg.message("Новый текст")
    assert mod.PLUGIN.get("vipuser")["text"] == "Новый текст"
    for i in range(12):
        mod.PLUGIN.put(f"user{i:02d}", "обычный")
    tg.message("/notes")
    assert "1/2" in str([b.text for b in tg.buttons()])
    tg.click("Поиск")
    tg.message("новый")
    assert "VipUser" in str([b.text for b in tg.buttons()]) and "user01" not in str([b.text for b in tg.buttons()])
    tg.click("VipUser")
    tg.click("Удалить")
    tg.click("Да")
    assert mod.PLUGIN.get("VipUser") is None
    tg.click("Сообщения")
    assert mod.PLUGIN.store["on_message"] is False
    n = len(tg.bot.timeline)
    mod.new_message(c, message_event("hi", author="user01", author_id=9, chat_name="user01"))
    assert len(tg.bot.timeline) == n
