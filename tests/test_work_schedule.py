import threading

import pytest


@pytest.fixture
def fast(monkeypatch):
    import time as _time
    import random as _random
    monkeypatch.setattr(_random, "uniform", lambda a, b: 0)
    real = _time.sleep
    monkeypatch.setattr(_time, "sleep", lambda s: real(0) if s > 0 else None)


def join():
    for t in threading.enumerate():
        if t.name.startswith("WorkSchedule") and t.name != "WorkSchedule":
            t.join(10)


def test_window_logic(env):
    mod, c = env("WorkSchedule_LuckyFunpay.py")
    w = mod.in_window
    assert w("09:00", "23:00", 9 * 60) and not w("09:00", "23:00", 23 * 60) and not w("09:00", "23:00", 8 * 60 + 59)
    assert w("22:00", "06:00", 23 * 60) and w("22:00", "06:00", 60) and not w("22:00", "06:00", 12 * 60)
    assert w("10:00", "10:00", 0)
    assert mod.parse_time("9.05") == "09:05"
    with pytest.raises(mod.UserError):
        mod.parse_time("25:00")


def test_auto_hide_and_show(env, fast, monkeypatch):
    mod, c = env("WorkSchedule_LuckyFunpay.py")
    acc, tg = c.account, c.telegram
    a = acc.add_lot(101, "Лот A")
    b = acc.add_lot(102, "Лот B")
    off = acc.add_lot(103, "Был выключен", active=False)
    acc.add_chip(2001, {"1": (True, 1.5, 1000), "2": (False, 2, 10)})
    p = mod.PLUGIN
    p.store.set(enabled=True)
    state = {"work": False}
    monkeypatch.setattr(p, "desired", lambda: state["work"])
    p.tick()
    assert acc.lots[a]["active"] == "" and acc.lots[b]["active"] == ""
    assert "offers[1][active]" not in acc.chips[2001]
    assert p.store["state"] == "off" and len(p.store["hidden"]) == 3
    assert "Продажи скрыты" in tg.screen().text and "<b>3</b>" in tg.screen().text
    n = len(tg.bot.timeline)
    p.tick()  # та же сторона графика — ничего не делаем
    assert len(tg.bot.timeline) == n
    state["work"] = True
    p.tick()
    assert acc.lots[a]["active"] == "on" and acc.lots[b]["active"] == "on"
    assert acc.lots[off]["active"] == ""  # не трогаем то, что не скрывали
    assert acc.chips[2001]["offers[1][active]"] == "on" and "offers[2][active]" not in acc.chips[2001]
    assert p.store["state"] == "on" and p.store["hidden"] == []


def test_manual_buttons_and_failures(env, fast):
    mod, c = env("WorkSchedule_LuckyFunpay.py")
    acc, tg = c.account, c.telegram
    a = acc.add_lot(101, "Лот A")
    b = acc.add_lot(102, "Лот B")
    tg.message("/schedule")
    tg.click("Скрыть все")
    join()
    assert "Продажи скрыты" in tg.screen().text
    acc.silent_inactive.add(b)  # FunPay «сохранил», но лот остался выключен
    tg.click("Меню")
    tg.click("Показать все")
    join()
    text = tg.screen().text
    assert "Продажи открыты" in text and "Ошибок: 1" in text and "Лот B" in text
    assert acc.lots[a]["active"] == "on"
    assert [t["id"] for t in mod.PLUGIN.store["hidden"]] == [b]  # не потеряли ID


def test_settings_input(env):
    mod, c = env("WorkSchedule_LuckyFunpay.py")
    tg = c.telegram
    tg.message("/schedule")
    tg.click("Часовой пояс")
    tg.message("+5")
    tg.click("Старт")
    tg.message("8:30")
    tg.click("Конец")
    tg.message("1:00")
    d = mod.PLUGIN.store.snapshot()
    assert (d["offset"], d["start"], d["end"]) == (5, "08:30", "01:00")
    assert "UTC+5" in tg.screen().text
