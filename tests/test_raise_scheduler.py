import datetime

from fpc_fakes import ADMIN

MSK = datetime.timezone(datetime.timedelta(hours=3))


def at(h, m=0, day=12):
    return datetime.datetime(2026, 10, day, h, m, tzinfo=MSK).timestamp()


def setup(env):
    mod, c = env("RaiseScheduler_LuckyFunpay.py")
    acc = c.account
    acc.add_lot(101, "Dota lot")
    acc.add_lot(201, "WoW lot")
    c.refresh_profile()
    return mod, c, mod.PLUGIN


def test_helpers(env):
    mod, c = env("RaiseScheduler_LuckyFunpay.py")
    assert mod.parse_times("9:00, 14:30\n23.15 9:00") == ["09:00", "14:30", "23:15"]
    last, nxt = mod.slot_times(["09:00", "21:00"], at(10))
    assert last == at(9) and nxt == at(21)
    last, nxt = mod.slot_times(["09:00", "21:00"], at(8))
    assert last == at(21, day=11) and nxt == at(9)
    assert mod.min_gap(["12:00", "14:00"]) == 2 * 3600 and mod.min_gap(mod.PRESETS["4"]) == 6 * 3600


def test_general_schedule_and_cooldown(env):
    mod, c, p = setup(env)
    acc, tg = c.account, c.telegram
    tg.message("/raise_schedule")
    tg.click("Время по умолчанию")
    tg.click("×2/день")
    assert p.store["default"] == ["09:00", "21:00"]
    tg.click("Назад")
    tg.click("Категории")
    tg.click("Выбрать все")
    # первый запуск в 10:00 — слот 09:00 недавно прошёл, поднимаем
    p.tick(at(9, 20))
    assert sorted(acc.raised) == [1, 2]
    assert c.raise_time[1] > at(9, 20) and c.raise_time[2] > at(9, 20)  # Cardinal не поднимет сам
    p.tick(at(15))
    assert len(acc.raised) == 2  # до 21:00 тишина
    # 21:00 — Dota на КД, WoW поднимается
    acc.raise_wait[1] = 3600
    p.tick(at(21, 0))
    assert acc.raised.count(2) == 2 and acc.raised.count(1) == 1
    assert p.store["state"]["1"]["pending"] == at(21)
    p.tick(at(21, 30))  # retry ещё не наступил
    assert acc.raised.count(1) == 1
    p.tick(at(22, 31))  # >90 минут — предупреждение, повтор ещё на КД
    assert "не поднимается по расписанию уже более 90 минут" in tg.screen().text
    del acc.raise_wait[1]
    p.tick(at(22, 40))  # повтор только после КД, который назвал FunPay
    assert acc.raised.count(1) == 1
    p.tick(at(23, 32))
    assert acc.raised.count(1) == 2 and not p.store["state"]["1"]["pending"]
    kinds = [e["kind"] for e in p.store["log"]]
    assert "miss" in kinds and kinds[0] == "raised"


def test_individual_and_exclusion(env):
    mod, c, p = setup(env)
    acc, tg = c.account, c.telegram
    p.store.set(default=["09:00"], all=True)
    tg.message("/raise_schedule")
    tg.click("Категории")
    tg.click("World of Warcraft")
    tg.click("Исключить из общего расписания")
    assert p.status(2) == "excluded"
    tg.click("К категориям")
    tg.click("Dota 2")
    tg.click("Добавить время")
    tg.message("12:00, 25:00")
    assert "не время" in tg.screen().text
    tg.click("Ввести заново")
    tg.message("12:00, 18:00")
    assert p.times_for(1) == ["12:00", "18:00"] and "Зазор меньше 4 часов" not in tg.screen().text
    tg.click("🗑 18:00")
    assert p.times_for(1) == ["12:00"]
    p.tick(at(12, 1))
    assert acc.raised == [1] and 2 not in c.raise_time  # исключённую поднимает Cardinal как обычно
    tg.click("Сбросить на время по умолчанию")
    assert p.times_for(1) == ["09:00"]
    tg.message("/raise_schedule")
    tg.click("Категории")
    tg.click("Снять со всех")
    assert p.times_for(1) is None and p.times_for(2) is None


def test_global_autoraise_off(env):
    mod, c, p = setup(env)
    p.store.set(default=["09:00"], all=True)
    c.MAIN_CFG["FunPay"]["autoRaise"] = "0"
    p.tick(at(9, 5))
    assert c.account.raised == []
    c.telegram.message("/raise_schedule")
    assert "Глобальное автоподнятие выключено" in c.telegram.screen().text
