import pytest

from fpc_fakes import ADMIN, FakeBot, Obj, enums, fpt, events, new_order_event

GROUP = -100777


def make(env, monkeypatch, ready=True):
    mod, c = env("ChatSync_LuckyFunpay.py")
    helpers = {}

    def fake_make_bot(token):
        if token not in helpers:
            uid = token.split(":")[0]
            helpers[token] = FakeBot(token, username=("funpay_h" if not uid.startswith("9") else "other_") + uid)
            helpers[token].timeline = []
        return helpers[token]

    monkeypatch.setattr(mod, "make_bot", fake_make_bot)
    c.telegram.bot.token = "123456789:" + "M" * 35
    p = mod.PLUGIN
    p.sync = True
    if ready:
        p.store.set(bots=[{"token": f"10000000{i}:{'A' * 35}", "id": 100000000 + i, "username": f"funpay_h{i}"}
                          for i in range(4)], group=GROUP)
    return mod, c, p, helpers


def sent_in_group(c, helpers):
    items = [m for m in c.telegram.bot.sent if m.chat_id == GROUP]
    for h in helpers.values():
        items += [m for m in h.sent if m.chat_id == GROUP]
    return sorted(items, key=lambda m: m.mid)


def buyer_msg(text, chat_id=5001, name="Buyer1", author_id=500, mid=None):
    msg = fpt.Message(mid or abs(hash(text)) % 100000, text, chat_id, name, author_id, name, author_id, "")
    return events.NewMessageEvent("t", msg)


def test_setup_flow(env, monkeypatch):
    mod, c, p, helpers = make(env, monkeypatch, ready=False)
    tg = c.telegram
    tg.message("/chat_sync")
    assert "Создайте ещё 4" in tg.screen().text and "❌ Нет" in tg.screen().text
    assert "Настройки переключателей" not in str([b.text for b in tg.buttons()])
    tg.click("Добавить Telegram бота")
    tg.message("not a token")
    assert "Ошибка при получении данных бота" in tg.screen().text
    tg.click("Ввести заново")
    tg.message("900000001:" + "B" * 35)
    assert "должен начинаться с funpay" in tg.screen().text
    tg.click("Ввести заново")
    tg.message(c.telegram.bot.token)
    assert "Основного бота добавлять не нужно" in tg.screen().text
    for i in range(4):
        tg.click("Главный") if i else tg.click("Меню")
        tg.click("Добавить Telegram бота")
        tg.message(f"20000000{i}:" + "C" * 35)
        assert "добавлен" in tg.screen().text
    tg.click("Главный")
    tg.click("Добавить Telegram бота")
    tg.message("200000000:" + "C" * 35)
    assert "уже добавлен" in tg.screen().text
    tg.click("Меню")
    assert "Создайте группу" in tg.screen().text
    # привязка группы
    tg.message("/setup_sync_chat")
    assert "нужно вводить в созданной группе" in tg.screen().text
    tg.message("/setup_sync_chat", chat_id=GROUP, chat_type="supergroup")
    assert "Группа для синхронизации FunPay чатов установлена" in tg.screen(GROUP).text
    assert p.ready()
    c.telegram.bot.chat_members[200000001] = Obj(status="member")
    c.telegram.bot.chat_members[200000002] = Obj(status="administrator", can_manage_topics=False)
    c.telegram.bot.chat_members[200000003] = Obj(status="left")
    tg.message("/chat_sync")
    labels = [b.text for b in tg.buttons()]
    assert "🔒" in labels and "📛" in labels and "➕" in labels and "Есть проблемы" in tg.screen().text
    tg.click("🔒", exact=True)
    assert "не администратор" in str(c.telegram.bot.answers[-1])
    # удаление помощника
    before = len(p.store["bots"])
    tg.click("🗑️")
    assert len(p.store["bots"]) == before - 1 and not p.ready()
    # смена группы с подтверждением
    p.store.set(bots=p.store["bots"] + [{"token": "300000001:" + "D" * 35, "id": 300000001, "username": "funpay_x"}],
                topics={"1": 5})
    tg.message("/setup_sync_chat", chat_id=-100888, chat_type="supergroup")
    assert "Ты уверен" in tg.screen(-100888).text
    tg.click("Да", chat_id=-100888)
    assert p.store["group"] == -100888 and p.store["topics"] == {}
    tg.message("/delete_sync_chat")
    tg.click("Да")
    assert p.store["group"] is None


def test_funpay_to_telegram_and_back(env, monkeypatch):
    mod, c, p, helpers = make(env, monkeypatch)
    acc, tg = c.account, c.telegram
    mod.new_message(c, buyer_msg("Здравствуйте, есть в наличии?"))
    msgs = sent_in_group(c, helpers)
    assert len(c.telegram.bot.topics) + sum(len(h.topics) for h in helpers.values()) == 1
    thread = p.store["topics"]["5001"]
    assert "Чат с" in msgs[0].text and "Продажи" in msgs[0].text
    assert msgs[1].thread == thread and "👤 <b>Buyer1</b>\nЗдравствуйте, есть в наличии?" in msgs[1].text
    # ответ из темы → FunPay
    tg.message("Да, есть!", chat_id=GROUP, chat_type="supergroup", thread=thread)
    assert acc.sent[-1] == (5001, "Да, есть!", "Buyer1")
    # эхо нашего же сообщения не дублируется
    echo_id = len(acc.sent)
    n = len(sent_in_group(c, helpers))
    mod.new_message(c, buyer_msg("Да, есть!", author_id=acc.id, name="Buyer1", mid=echo_id))
    assert len(sent_in_group(c, helpers)) == n
    # своё сообщение с сайта
    mod.new_message(c, buyer_msg("пишу с сайта", author_id=acc.id, mid=77777))
    assert "🫵 <b>Вы</b>\nпишу с сайта" in sent_in_group(c, helpers)[-1].text
    # команда не пересылается, вне темы — ошибка
    tg.message("/stats", chat_id=GROUP, chat_type="supergroup", thread=thread)
    assert "Статистика по пользователю Buyer1" in sent_in_group(c, helpers)[-1].text
    tg.message("/watch", chat_id=GROUP, chat_type="supergroup")
    assert "нужно вводить в теме покупателя" in c.telegram.bot.sent[-1].text
    # посторонний в группе — игнор
    before = len(acc.sent)
    tg.message("я не админ", chat_id=GROUP, chat_type="supergroup", thread=thread, user_id=5)
    assert len(acc.sent) == before


def test_glue_system_and_topic_title(env, monkeypatch):
    mod, c, p, helpers = make(env, monkeypatch)
    acc = c.account
    p.sync = True
    p.flush_delay = 30
    # склейка: два сообщения подряд одного автора в одной пачке
    p.sync = False
    mod.new_message(c, buyer_msg("первое", mid=1))
    mod.new_message(c, buyer_msg("второе", mid=2))
    p.sync = True
    p.flush("5001")
    text = sent_in_group(c, helpers)[-1].text
    assert text.count("Buyer1") == 1 and "первое\nвторое" in text
    # оплата заказа → статистика и название темы
    acc.add_order("ABCDEFGH", buyer="Buyer1", price=150)
    sys_msg = fpt.Message(10, "Покупатель Buyer1 оплатил заказ #ABCDEFGH. Dota 2, Предметы, Тест. Buyer1, не забудьте потом нажать кнопку «Подтвердить выполнение заказа».", 5001,
                          "Buyer1", 500, "FunPay", 0, "")
    mod.new_message(c, events.NewMessageEvent("t", sys_msg))
    edits = c.telegram.bot.topic_edits + [e for h in helpers.values() for e in h.topic_edits]
    assert edits and edits[-1][2] == "1|0|0👤Buyer1 (5001)"
    assert edits[-1][3] == "9003"  # 💰 — иконка «есть оплаченный заказ»
    assert "Статистика по пользователю" in sent_in_group(c, helpers)[-1].text


def test_deleted_topic_recreated(env, monkeypatch):
    mod, c, p, helpers = make(env, monkeypatch)
    mod.new_message(c, buyer_msg("раз", mid=1))
    old = p.store["topics"]["5001"]

    class Gone(Exception):
        description = "Bad Request: message thread not found"

    calls = {"n": 0}
    for bot in [c.telegram.bot] + list(helpers.values()):
        real = bot.send_message

        def failing(chat_id, text, _real=real, **kw):
            if kw.get("message_thread_id") == old:
                raise Gone()
            return _real(chat_id, text, **kw)

        bot.send_message = failing
    mod.new_message(c, buyer_msg("два", mid=2))
    assert p.store["topics"]["5001"] != old
    assert "два" in sent_in_group(c, helpers)[-1].text


def test_switches_and_sync_all(env, monkeypatch):
    mod, c, p, helpers = make(env, monkeypatch)
    tg, acc = c.telegram, c.account
    tg.message("/chat_sync")
    tg.click("Настройки переключателей")
    tg.click("Моно шрифт")
    tg.click("Скрывать вотермарку")
    assert p.switch("mono") and p.switch("hide_watermark")
    mod.new_message(c, buyer_msg("🐦\nавтоответ бота", author_id=acc.id, mid=5))
    assert "<tg-spoiler>🐦</tg-spoiler> <code>автоответ бота</code>" in sent_in_group(c, helpers)[-1].text
    acc.add_chat(6001, "Alice")
    acc.add_chat(6002, "Bob")
    tg.message("/sync_chats", chat_id=GROUP, chat_type="supergroup")
    assert {"6001", "6002"} <= set(p.store["topics"])
