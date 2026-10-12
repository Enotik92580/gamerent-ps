def setup(env):
    mod, c = env("Deactivator_LuckyFunpay.py")
    p = mod.PLUGIN
    p.sync = True
    p.pause = (0, 0)
    acc = c.account
    lots = {"a": acc.add_lot(101, "Арканы"), "b": acc.add_lot(102, "Аккаунт"), "w": acc.add_lot(201, "WoW"),
            "off": acc.add_lot(101, "Уже выключен", active=False)}
    acc.add_chip(2001, {"1": (True, 1.5, 100), "2": (True, 2, 50), "3": (False, 3, 1)})
    return mod, c, p, lots


def test_all_off_and_on(env, monkeypatch):
    mod, c, p, L = setup(env)
    import time as _t
    real = _t.sleep
    monkeypatch.setattr(_t, "sleep", lambda s: real(0))
    acc, tg = c.account, c.telegram
    tg.message("/deactivator")
    tg.click("Деактивировать все")
    tg.click("Да, выключить")
    assert "Готово" in tg.screen().text
    assert all(acc.lots[L[k]]["active"] == "" for k in "abw")
    assert "offers[1][active]" not in acc.chips[2001] and "offers[2][active]" not in acc.chips[2001]
    saved = p.store["saved"]
    assert len(saved) == 5 and f"lot:{L['off']}" not in saved
    tg.click("Список")
    assert "Dota 2 › Предметы" in tg.screen().text and f"offerEdit?offer={L['a']}" in tg.screen().text
    tg.click("Выгрузить файлом")
    assert c.telegram.bot.documents[-1].name == "deactivated_lots.txt"
    # активация: один лот FunPay не включил, один удалён на сайте
    acc.silent_inactive.add(L["b"])
    del acc.lots[L["w"]]
    tg.message("/deactivator")
    tg.click("Активировать все")
    tg.click("Да, включить")
    assert acc.lots[L["a"]]["active"] == "on" and acc.lots[L["off"]]["active"] == ""
    assert acc.chips[2001]["offers[1][active]"] == "on" and "offers[3][active]" not in acc.chips[2001]
    assert list(p.store["saved"]) == [f"lot:{L['b']}"]  # ID не потерян
    failures = [t for t in tg.texts() if "Проблемные позиции" in t][-1]
    assert "1. " in failures and "2. " in failures and "не найден" in failures


def test_by_category_and_clear(env, monkeypatch):
    mod, c, p, L = setup(env)
    import time as _t
    real = _t.sleep
    monkeypatch.setattr(_t, "sleep", lambda s: real(0))
    acc, tg = c.account, c.telegram
    tg.message("/deactivator")
    tg.click("Валюты")  # без таблиц валют
    tg.click("🔴 По категории")
    assert not any("Золото" in b.text for b in tg.buttons())
    tg.click("Dota 2 › Предметы")
    tg.click("Да, выключить")
    assert acc.lots[L["a"]]["active"] == "" and acc.lots[L["b"]]["active"] == "on"
    assert list(p.store["saved"]) == [f"lot:{L['a']}"]
    tg.message("/deactivator")
    tg.click("🟢 По категории")
    tg.click("Dota 2 › Предметы (1)")
    assert acc.lots[L["a"]]["active"] == "on" and p.store["saved"] == {}
    p.store.set(saved={"lot:1": {"kind": "lot", "id": 1, "name": "x", "sub": 101, "sub_name": "s", "game": "g"}})
    tg.message("/deactivator")
    tg.click("Очистить список")
    tg.click("Да, очистить")
    assert p.store["saved"] == {}
