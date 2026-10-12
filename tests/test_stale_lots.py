import datetime

import pytest

from fpc_fakes import enums


@pytest.fixture
def fast(monkeypatch):
    import random
    import time as _t
    monkeypatch.setattr(random, "uniform", lambda a, b: 0)
    real = _t.sleep
    monkeypatch.setattr(_t, "sleep", lambda s: real(0))


def test_scan_colors_and_deactivate(env, fast):
    mod, c = env("StaleLots_LuckyFunpay.py")
    acc, tg, p = c.account, c.telegram, mod.PLUGIN
    p.sync = True
    now = datetime.datetime.now()
    fresh = acc.add_lot(101, "Арканы")
    old100 = acc.add_lot(101, "Сеты героев")
    old200 = acc.add_lot(102, "Аккаунт 5000")
    ghost = acc.add_lot(102, "Никому не нужный")
    excluded = acc.add_lot(103, "Редкая услуга")
    inactive = acc.add_lot(103, "Старый выключенный", active=False)
    acc.add_order("A0000001", desc="Арканы, 1 шт.", subcat=101, date=now - datetime.timedelta(days=3),
                  status=enums.OrderStatuses.CLOSED)
    acc.add_order("A0000002", desc="Сеты героев", subcat=101, date=now - datetime.timedelta(days=100),
                  status=enums.OrderStatuses.CLOSED)
    acc.add_order("A0000003", desc="Аккаунт 5000", subcat=102, date=now - datetime.timedelta(days=200),
                  status=enums.OrderStatuses.CLOSED)
    acc.add_order("A0000004", desc="Сеты героев", subcat=102, date=now, status=enums.OrderStatuses.CLOSED)  # другой раздел
    tg.message("/stale_lots")
    tg.click("Исключения")
    tg.click("Добавить ID")
    tg.message(f"https://funpay.com/lots/offer?id={excluded}")
    tg.click("Назад")
    tg.click("Начать поиск")
    labels = [b.text for b in tg.buttons()]
    stale = [l for l in labels if " · " in l and "дн" in l or "нет продаж" in l]
    assert any(l.startswith("💀 Никому не нужный") for l in stale)
    assert any(l.startswith("🔴 Аккаунт 5000 · 200 дн") for l in stale)
    assert any(l.startswith("🟠 Сеты героев · 100 дн") for l in stale)
    assert not any("Арканы" in l or "Редкая" in l or "выключенный" in l for l in labels)
    tg.click("Аккаунт 5000")
    assert acc.lots[old200]["active"] == ""
    tg.click("Выключить ВСЕ")
    assert acc.lots[old100]["active"] == "" and acc.lots[ghost]["active"] == ""
    assert acc.lots[fresh]["active"] == "on"
    # без пустышек и с порогом 150
    tg.click("К списку")
    tg.click("Пустышки")
    tg.click("Порог")
    tg.message("150")
    for lot in (old100, old200, ghost):
        acc.lots[lot]["active"] = "on"
    tg.click("Начать поиск")
    names = [i["name"] for i in p.store["result"]]
    assert names == ["Аккаунт 5000"]
