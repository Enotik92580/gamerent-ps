import pytest


@pytest.fixture
def fast(monkeypatch):
    import random
    monkeypatch.setattr(random, "uniform", lambda a, b: 0)


def setup(env):
    mod, c = env("WordReplacer_LuckyFunpay.py")
    mod.PLUGIN.sync = True
    acc = c.account
    a = acc.add_lot(101, "Арканы | Discord: Vasyan#1234", desc="Пишите в Discord: Vasyan#1234\n<b>Discord: Vasyan#1234</b>")
    b = acc.add_lot(101, "Сеты", desc="Без контактов", active=False)
    d = acc.add_lot(201, "WoW аккаунт", desc="Discord: Vasyan#1234")
    e = acc.add_lot(301, "Ключ", desc="Discord: Vasyan#1234")
    return mod, c, (a, b, d, e)


def test_replace_in_selected_categories(env, fast):
    mod, c, (a, b, d, e) = setup(env)
    tg, acc = c.telegram, c.account
    tg.message("/replace_word")
    tg.click("Начать замену")
    tg.click("Заголовки и Описания")
    tg.click("Next") if False else None
    tg.click("➡️ Далее")
    assert "Выберите хотя бы одну" in tg.screen().text
    tg.message("/replace_word")
    tg.click("Начать замену")
    tg.click("Заголовки и Описания")
    tg.click("Dota 2 › Предметы")
    tg.click("World of Warcraft › Аккаунты")
    tg.click("➡️ Далее")
    tg.message("Vasyan#1234")
    tg.message("ProSeller#9999")
    assert "Найти: <code>Vasyan#1234</code>" in tg.screen().text and "Категорий: 2" in tg.screen().text
    tg.click("Поехали")
    assert "Готово" in tg.screen().text and "Изменено лотов: <b>2</b>" in tg.screen().text
    assert acc.lots[a]["fields[summary][ru]"] == "Арканы | Discord: ProSeller#9999"
    assert acc.lots[a]["fields[desc][ru]"] == "Пишите в Discord: ProSeller#9999\n<b>Discord: ProSeller#9999</b>"
    assert acc.lots[d]["fields[desc][ru]"] == "Discord: ProSeller#9999"
    assert acc.lots[e]["fields[desc][ru]"] == "Discord: Vasyan#1234"  # категория не выбрана
    assert acc.lots[b]["active"] == ""  # выключенный лот не включился


def test_delete_mode_titles_only(env, fast):
    mod, c, (a, b, d, e) = setup(env)
    tg, acc = c.telegram, c.account
    tg.message("/replace_word")
    tg.click("Начать замену")
    tg.click("🏷 Заголовки")
    tg.click("Выбрать всё")
    tg.click("➡️ Далее")
    tg.message(" | Discord: Vasyan#1234")
    tg.click("Просто удалить")
    assert "удалить" in tg.screen().text
    tg.click("Поехали")
    assert acc.lots[a]["fields[summary][ru]"] == "Арканы"
    assert "Vasyan#1234" in acc.lots[a]["fields[desc][ru]"]


def test_empty_title_protection_and_save_error(env, fast):
    mod, c, (a, b, d, e) = setup(env)
    acc = c.account
    p = mod.PLUGIN
    job = mod.Job("t")
    job.progress = lambda force=False: None
    sub = acc.subcats[301]
    p.run(job, [sub], "titles", "Ключ", "")
    assert job.errors == 1 and "пустым" in job.last_error
    acc.fail_save.add(d)
    job = mod.Job("t")
    job.progress = lambda force=False: None
    p.run(job, [acc.subcats[201]], "desc", "Vasyan", "X")
    assert job.errors == 1 and job.ok == 0


def test_helpers(env):
    mod, c = env("WordReplacer_LuckyFunpay.py")
    assert mod.replace_outside_tags('<a href="x">x</a> x', "x", "y") == ('<a href="x">y</a> y', 2)
