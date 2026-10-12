def setup(env):
    mod, c = env("AutoStockCloner_LuckyFunpay.py")
    p = mod.PLUGIN
    p.sync = True
    p.pause = (0, 0)
    return mod, c, p


def test_restock_only_selected_manual_lots(env):
    mod, c, p = setup(env)
    acc, tg = c.account, c.telegram
    gold = acc.add_lot(201, "Золото 1000", amount=300)
    full = acc.add_lot(201, "Золото 5000", amount=900)
    keys = acc.add_lot(201, "Ключи", secrets=["k1"])  # автовыдача — не трогаем
    other = acc.add_lot(101, "Арканы", amount=1)  # раздел не выбран
    tg.message("/stock_cloner")
    tg.click("Мин")
    tg.message("500")
    tg.click("Макс")
    tg.message("10")
    assert "Мин не может быть больше Макс" in tg.screen().text
    tg.click("Ввести заново")
    tg.message("10000")
    tg.click("Выбрать категории")
    tg.click("World of Warcraft")
    tg.click("Аккаунты")
    assert "✅ Аккаунты" in [b.text for b in tg.buttons()]
    tg.click("Меню")
    tg.click("Проверить сейчас")
    assert "Пополнено лотов: 1" in tg.screen().text
    assert 500 <= int(acc.lots[gold]["amount"]) <= 10000
    assert acc.lots[full]["amount"] == "900" and acc.lots[other]["amount"] == "1"
    assert acc.lots[keys]["secrets"] == "k1"


def test_cloner(env):
    mod, c, p = setup(env)
    acc, tg = c.account, c.telegram
    guide = acc.add_lot(101, "Гайд", secrets=["guide-link-1", "guide-link-2"])
    empty = acc.add_lot(101, "Пустой", secrets=[])
    tg.message("/stock_cloner")
    tg.click("Клонатор автовыдачи")
    tg.click("Поддерживать 50")
    tg.message("5")
    tg.click("Добавить лот")
    tg.message(str(empty))
    assert "клонировать пустоту" in tg.screen().text
    tg.click("Ввести заново")
    tg.message(f"https://funpay.com/lots/offer?id={guide}")
    assert "Гайд — сейчас 2 строк" in tg.screen().text
    p.run_once()
    assert acc.lots[guide]["secrets"].split("\n") == ["guide-link-1", "guide-link-2"] * 2 + ["guide-link-1"]
    assert acc.lots[guide]["auto_delivery"] == "on"
    # раскупили всё — предупреждение один раз
    acc.lots[guide]["secrets"] = ""
    p.run_once()
    p.run_once()
    assert sum("не осталось строк" in t for t in tg.texts()) == 1
    # автовыдачу выключили — клонатор включит обратно
    acc.lots[guide]["secrets"] = "x"
    acc.lots[guide]["auto_delivery"] = ""
    p.run_once()
    assert acc.lots[guide]["auto_delivery"] == "on" and acc.lots[guide]["secrets"].count("x") == 5
