def test_clone_via_command_and_menu(env):
    mod, c = env("LotCloner_LuckyFunpay.py")
    tg, acc = c.telegram, c.account
    src = acc.add_lot(102, "Аккаунт Dota 2 5000 MMR", price=1500, title_en="Dota account", secrets=["log:pass"],
                      desc="Подробно", extra={"fields[server]": "EU"})
    tg.message(f"/clone_lot {src}")
    assert "Лот склонирован" in tg.screen().text
    new_ids = [i for i in acc.lots if i != src]
    assert len(new_ids) == 1
    new = acc.lots[new_ids[0]]
    old = acc.lots[src]
    assert new["fields[summary][ru]"] == "Аккаунт Dota 2 5000 MMR (Копия)"
    assert new["fields[summary][en]"] == "Dota account (Copy)"
    assert new["fields[desc][ru]"] == "Подробно" and new["price"] == "1500.0"
    assert new["fields[images]"] == "11,12" and new["fields[server]"] == "EU"
    assert new["active"] == "" and new["secrets"] == "" and new.get("auto_delivery") == ""
    assert old["active"] == "on" and old["secrets"] == "log:pass"  # оригинал не тронут
    assert str(new_ids[0]) in tg.screen().text
    # через меню с копированием автовыдачи
    tg.message("/clone_lot")
    tg.click("Копировать автовыдачу")
    tg.click("Клонировать лот")
    tg.message(f"https://funpay.com/lots/offer?id={src}")
    newest = max(acc.lots)
    assert acc.lots[newest]["secrets"] == "log:pass" and acc.lots[newest]["auto_delivery"] == "on"
    assert acc.lots[newest]["active"] == ""


def test_clone_errors(env):
    mod, c = env("LotCloner_LuckyFunpay.py")
    tg, acc = c.telegram, c.account
    tg.message("/clone_lot 123")
    assert "Лот не найден" in tg.screen().text
    tg.message("/clone_lot abc")
    assert "Нужен ID лота" in tg.screen().text
    src = acc.add_lot(101, "Арканы")
    acc.fail_save.add(0)
    tg.message(f"/clone_lot {src}")
    assert "FunPay не сохранил копию" in tg.screen().text
