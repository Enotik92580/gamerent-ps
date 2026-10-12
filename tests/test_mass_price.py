import pytest


@pytest.fixture
def fast(monkeypatch):
    import random
    monkeypatch.setattr(random, "uniform", lambda a, b: 0)


def test_math(env):
    mod, c = env("MassPriceEditor_LuckyFunpay.py")
    f = mod.new_price
    assert f(100, "fix", 99) == 99 and f(100, "add", 50) == 150 and f(100, "sub", 150) == 0.01
    assert f(100, "up", 10) == 110 and f(99.99, "down", 5) == 94.99 and f(33.333, "up", 0.5) == 33.5


def test_wizard_changes_only_selected(env, fast):
    mod, c = env("MassPriceEditor_LuckyFunpay.py")
    mod.PLUGIN.sync = True
    acc, tg = c.account, c.telegram
    a = acc.add_lot(101, "Арканы", price=100)
    b = acc.add_lot(101, "Сеты", price=250.5, active=False)
    d = acc.add_lot(102, "Аккаунт", price=1000)
    w = acc.add_lot(201, "WoW", price=500)
    tg.message("/mass_price_editor")
    tg.click("Выбрать игру")
    tg.click("Dota 2")
    tg.click("➡️ Далее")
    assert "хотя бы один раздел" in tg.screen().text
    tg.click("Меню")
    tg.click("Выбрать игру")
    tg.click("Dota 2")
    tg.click("Предметы")
    tg.click("➡️ Далее")
    tg.click("Понизить на %")
    tg.message("150")
    assert "допустимо" in tg.screen().text
    tg.click("Ввести заново")
    tg.message("10%")
    assert "понизить на 10%" in tg.screen().text
    tg.click("Да, поехали")
    assert "Готово" in tg.screen().text and "Цена изменена у <b>2</b>" in tg.screen().text
    assert acc.lots[a]["price"] == "90.0" and acc.lots[b]["price"] == "225.45"
    assert acc.lots[d]["price"] == "1000" and acc.lots[w]["price"] == "500"
    assert acc.lots[b]["active"] == ""


def test_stop_button(env, fast):
    mod, c = env("MassPriceEditor_LuckyFunpay.py")
    acc = c.account
    for i in range(5):
        acc.add_lot(101, f"Лот {i}", price=10)
    job = mod.Job("t")
    job.progress = lambda force=False: job.stop.set() if job.done == 2 else None
    mod.PLUGIN.run(job, [acc.subcats[101]], "fix", 20)
    assert job.done == 2 and sum(1 for l in acc.lots.values() if l["price"] == "20.0") == 2
