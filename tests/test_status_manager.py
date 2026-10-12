import time

from fpc_fakes import message_event


def test_presets_shortcuts_and_chat(env):
    mod, c = env("StatusManager_LuckyFunpay.py")
    tg, acc, p = c.telegram, c.account, mod.PLUGIN
    tg.message("/status")
    assert "Текущий статус: <b>🟢 Онлайн</b>" in tg.screen().text
    tg.click("Добавить")
    tg.message("🛒 Ушёл в магазин")
    assert p.store["presets"][-1] == "🛒 Ушёл в магазин"
    tg.message("/s1")
    assert "Статус установлен: <b>💤 Сплю</b>" in tg.screen().text
    assert p.store["current"] == "💤 Сплю"
    tg.message("/s5")
    assert p.store["current"] == "🛒 Ушёл в магазин"
    tg.message("/s9")
    assert "Статуса №9 нет" in tg.screen().text
    p.store.set(since=time.time() - 4 * 3600 - 15 * 60)
    tg.message("/status")
    assert "Установлен: 4ч 15м назад" in tg.screen().text
    tg.message("/status_clear")
    assert p.store["current"] == "" and len(p.store["history"]) == 2
    # активность и #status
    mod.new_message(c, message_event("ответ клиенту", author=acc.username, author_id=acc.id))
    assert time.time() - p.store["last_activity"] < 5
    p.store.set(last_activity=1)
    mod.new_message(c, message_event("автоответ", author=acc.username, author_id=acc.id, by_bot=True))
    mod.new_message(c, message_event("покупатель", author="Buyer1", author_id=500))
    assert p.store["last_activity"] == 1
    tg.message("/s2")
    mod.new_message(c, message_event("#status", author=acc.username, author_id=acc.id, chat_id=123))
    assert c.sent_messages[-1][0] == 123 and "🍔 Обед" in c.sent_messages[-1][1]
    assert p.store["last_activity"] == 1
    # удаление
    tg.message("/status")
    tg.click("Удаление")
    tg.click("🗑 1. 💤 Сплю")
    assert p.store["presets"][0] == "🍔 Обед"
    # посторонний не может ставить статус
    tg.message("/s1", user_id=7, chat_id=7)
    assert p.store["current"] == "🍔 Обед"
