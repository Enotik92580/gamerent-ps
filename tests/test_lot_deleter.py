import pytest

from fpc_fakes import enums


@pytest.fixture
def fast(monkeypatch):
    import random
    monkeypatch.setattr(random, "uniform", lambda a, b: 0)


def setup(env):
    mod, c = env("LotDeleter_LuckyFunpay.py")
    mod.PLUGIN.sync = True
    acc = c.account
    lots = {"a1": acc.add_lot(101, "A1"), "a2": acc.add_lot(101, "A2", active=False),
            "b1": acc.add_lot(102, "B1"), "hidden": acc.add_lot(202, "Скрытый раздел", active=False),
            "w1": acc.add_lot(201, "W1")}
    acc.add_order("ORDERWOW", subcat=202)  # раздел 202 известен только по истории продаж
    return mod, c, lots


def test_delete_selected_active_only(env, fast):
    mod, c, L = setup(env)
    acc, tg = c.account, c.telegram
    tg.message("/dellots")
    tg.click("Удалить из выбранных")
    assert "Сначала выберите категории" in tg.screen().text
    tg.click("Меню")
    tg.click("Выбрать категории")
    labels = [b.text for b in tg.buttons()]
    assert any("World of Warcraft › Услуги (1/0)" in l for l in labels)  # раздел с одним скрытым лотом найден
    tg.click("Dota 2 › Предметы")
    tg.click("Готово")
    tg.click("Удалить из выбранных")
    assert "Будет удалено лотов: <b>1</b>" in tg.screen().text
    tg.click("ДА, УДАЛИТЬ")
    assert "Готово" in tg.screen().text and acc.deleted == [L["a1"]]
    assert L["a2"] in acc.lots
    tg.click("Меню")
    tg.click("Режим")
    tg.click("Удалить из выбранных")
    assert "Будет удалено лотов: <b>1</b>" in tg.screen().text  # остался скрытый A2
    tg.click("ДА, УДАЛИТЬ")
    assert L["a2"] not in acc.lots and L["b1"] in acc.lots


def test_nuclear_needs_two_confirmations(env, fast):
    mod, c, L = setup(env)
    acc, tg = c.account, c.telegram
    tg.message("/dellots")
    tg.click("Удалить ВСЕ")
    assert "ВСЕ 5" in tg.screen().text
    tg.click("Да, я понимаю")
    assert acc.deleted == []
    tg.click("УДАЛИТЬ ВСЁ")
    assert sorted(acc.deleted) == sorted(L.values()) and not acc.lots


def test_stop_and_errors(env, fast):
    mod, c, L = setup(env)
    acc = c.account
    acc.fail_save.add(L["a1"])
    job = mod.Job("t")
    job.progress = lambda force=False: None
    mod.PLUGIN.delete(job, [101, 102], "all")
    assert job.errors == 1 and job.ok == 2 and L["a1"] in acc.lots
