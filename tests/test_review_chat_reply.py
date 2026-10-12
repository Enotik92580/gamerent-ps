from pathlib import Path

from fpc_fakes import Obj, enums, system_message_event


def setup(env):
    mod, c = env("ReviewChatReply_LuckyFunpay.py")
    mod.PLUGIN.sync = True
    return mod, c


def fire(mod, c, oid, stars, buyer="Buyer1", verb="написал отзыв к заказу"):
    if oid not in c.account.orders:
        c.account.add_order(oid, buyer=buyer, desc="Ключ Steam", status=enums.OrderStatuses.CLOSED)
    c.account.set_review(oid, stars)
    mod.new_message(c, system_message_event(f"Покупатель {buyer} {verb} #{oid}."))


def test_texts_and_gifts(env):
    mod, c = setup(env)
    tg = c.telegram
    Path("storage/products").mkdir(parents=True)
    Path("storage/products/gifts.txt").write_text("PROMO-1\nPROMO-2\nPROMO-3\n", encoding="utf-8")
    tg.message("/review_reply")
    tg.click("Плагин")
    tg.click("⭐⭐⭐⭐⭐")
    tg.click("Статус")
    tg.click("Изменить текст")
    tg.message("Спасибо, $username! Заказ $order_id ($order_title)")
    tg.click("Назад")
    tg.click("Подарки за отзывы")
    tg.click("Подарки: 🔴")
    tg.click("📂 Файл")
    tg.click("gifts.txt")
    assert "осталось <b>3</b>" in tg.screen().text
    tg.click("Порог")
    tg.message("2")
    fire(mod, c, "AAAAAAAA", 5)
    texts = [m[1] for m in c.sent_messages]
    assert texts == ["Спасибо, Buyer1! Заказ AAAAAAAA (Ключ Steam)", "🎁 Спасибо за отзыв! Ваш подарок: PROMO-1"]
    assert c.sent_messages[0][3] is True  # вотермарка
    assert Path("storage/products/gifts.txt").read_text().split() == ["PROMO-2", "PROMO-3"]
    # повтор события — ничего
    fire(mod, c, "AAAAAAAA", 5, verb="изменил отзыв к заказу")
    assert len(c.sent_messages) == 2
    # склейка + без $gift в шаблоне + порог
    mod.PLUGIN.store.change(lambda d: d["gifts"].update(separate=False, text="Держите бонус"))
    fire(mod, c, "BBBBBBBB", 5)
    assert c.sent_messages[-1][1].endswith("Держите бонус\nPROMO-2")
    assert "Подарки заканчиваются" in "\n".join(tg.texts())
    # 3⭐ выключена — тишина
    n = len(c.sent_messages)
    fire(mod, c, "CCCCCCCC", 3)
    assert len(c.sent_messages) == n


def test_empty_file_cycle_and_deleted(env):
    mod, c = setup(env)
    tg = c.telegram
    p = mod.PLUGIN
    Path("storage/products").mkdir(parents=True)
    Path("storage/products/one.txt").write_text("ONLY\n", encoding="utf-8")
    p.store.change(lambda d: (d.update(enabled=True), d["stars"]["5"].update(enabled=True),
                              d["stars"]["deleted"].update(enabled=True),
                              d["gifts"].update(enabled=True, file="one.txt", cycle=True)))
    fire(mod, c, "CYCLE001", 5)
    fire(mod, c, "CYCLE002", 5)
    assert c.sent_messages[1][1].endswith("ONLY") and c.sent_messages[3][1].endswith("ONLY")
    p.store.change(lambda d: d["gifts"].update(cycle=False))
    fire(mod, c, "EMPTY001", 5)
    fire(mod, c, "EMPTY002", 5)
    assert "Подарки закончились" in tg.screen().text
    assert c.sent_messages[-1][1].startswith("Спасибо за покупку")
    mod.new_message(c, system_message_event("Покупатель Buyer1 удалил отзыв к заказу #EMPTY002."))
    assert "удалили отзыв к заказу #EMPTY002" in c.sent_messages[-1][1]


def test_file_manager(env):
    mod, c = setup(env)
    tg = c.telegram
    tg.message("/review_reply")
    tg.click("Подарки за отзывы")
    tg.click("📂 Файл")
    tg.click("Создать новый файл")
    tg.message("../evil")
    assert "Имя файла" in tg.screen().text
    tg.click("Ввести заново")
    tg.message("5_stars")
    assert mod.PLUGIN.store["gifts"]["file"] == "5_stars.txt"
    tg.click("Добавить подарки")
    tg.message("a\n\nb\nc")
    assert Path("storage/products/5_stars.txt").read_text().split() == ["a", "b", "c"]
    tg.click("Скачать файл")
    assert c.telegram.bot.documents[-1].data == b"a\nb\nc\n"
    # загрузка документа для отдельной оценки
    tg.message("/review_reply")
    tg.click("⭐⭐⭐⭐", exact=True)
    tg.click("Свой файл")
    tg.click("Загрузить .txt")
    c.telegram.bot.files["F1"] = "x1\r\nx2\r\n".encode("cp1251")
    tg.message(document=Obj(file_name="4_stars_gifts.txt", file_id="F1", file_size=10))
    assert mod.PLUGIN.store["stars"]["4"]["file"] == "4_stars_gifts.txt"
    assert Path("storage/products/4_stars_gifts.txt").read_text().split() == ["x1", "x2"]
    tg.click("Свой файл")
    tg.click("Загрузить .txt")
    tg.message(document=Obj(file_name="bad.exe", file_id="F1", file_size=10))
    assert ".txt" in tg.screen().text
    # ожидание документа сохраняется — можно сразу прислать правильный
    tg.message(document=Obj(file_name="fixed.txt", file_id="F1", file_size=10))
    assert mod.PLUGIN.store["stars"]["4"]["file"] == "fixed.txt"
