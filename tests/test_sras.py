PAGE = """<html><body data-app-data='{}'><div class="user-link-name">LuckySeller</div>
<div class="page-content"><table>{rows}</table></div></body></html>"""


def row(sid, name, need, have=0):
    return (f'<tr><td><a href="https://funpay.com/lots/{sid}/">{name}</a></td>'
            f'<td>Требуется отзывов: {need}</td><td>У вас: {have}</td></tr>')


def setup(env, rows):
    mod, c = env("SrasMonitor_LuckyFunpay.py")
    state = {"rows": rows}
    c.account.http["sras/info"] = lambda m, a, p: PAGE.replace("{rows}", "".join(state["rows"]))
    return mod, c, state


def test_status_and_notifications(env):
    mod, c, state = setup(env, [])
    tg = c.telegram
    tg.message("/sras")
    assert "Ещё не проверялось" in tg.screen().text
    tg.click("Проверить сейчас")
    assert "Все чисто" in tg.screen().text
    tg.click("Включить уведомления")
    assert mod.PLUGIN.store["notify"] is True
    state["rows"] = [row(81, "Dota 2 &gt; Предметы", 5)]
    n = len(tg.bot.timeline)
    mod.PLUGIN.check()
    alert = tg.bot.timeline[n]
    assert "УЖЕСТОЧЕНЫ" in alert.text and "Dota 2 &gt; Предметы" in alert.text
    assert "0 (нет)" in alert.text and "5⭐" in alert.text
    tg.message("/sras")
    assert "Найдены ограничения: 1" in tg.screen().text
    state["rows"] = [row(81, "Dota 2 &gt; Предметы", 3)]
    mod.PLUGIN.check()
    assert "ОСЛАБЛЕНЫ" in tg.screen().text and "3⭐" in tg.screen().text
    state["rows"] = []
    mod.PLUGIN.check()
    assert "снова видны" in tg.screen().text


def test_first_check_is_baseline_and_errors(env):
    mod, c, state = setup(env, [row(81, "Dota", 5)])
    tg = c.telegram
    mod.PLUGIN.store.set(notify=True)
    c.account.http["sras/info"] = lambda m, a, p: "<html><body>login</body></html>"
    tg.message("/sras")
    tg.click("Проверить сейчас")
    assert "не удалась" in tg.screen().text
    c.account.http["sras/info"] = lambda m, a, p: PAGE.replace("{rows}", "".join(state["rows"]))
    n = len(tg.bot.timeline)
    mod.PLUGIN.check()
    assert len(tg.bot.timeline) == n  # baseline, без уведомления
    assert "Dota" in str(mod.PLUGIN.store["items"])


def test_interval_and_notify_off(env):
    mod, c, state = setup(env, [])
    tg = c.telegram
    tg.message("/sras")
    tg.click("Интервал")
    tg.message("5")
    assert "допустимо" in tg.screen().text
    tg.click("Ввести заново")
    tg.message("60")
    assert mod.PLUGIN.store["interval"] == 60
    mod.PLUGIN.check()
    state["rows"] = [row(1, "X", 2)]
    n = len(tg.bot.timeline)
    mod.PLUGIN.check()
    assert len(tg.bot.timeline) == n  # уведомления выключены


def test_parse_sras_variants():
    import sys
    mod = sys.modules.get("lf_SrasMonitor_LuckyFunpay")
    assert mod is not None
    items = mod.parse_sras(PAGE.replace("{rows}", '<li><a href="/chips/2001/">WoW Gold</a> нужно 10 ⭐</li>'))
    assert items["chips-2001"]["required"] == 10
