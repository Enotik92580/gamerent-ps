from fpc_fakes import enums, fpt, status_event

PAGE = """<html><form action="https://funpay.com/withdraw/withdraw" class="form-withdraw">
<input type="hidden" name="csrf_token" value="t"><input name="amount_int" value="">
<select name="currency_id"><option value="rub">RUB</option></select>
<select name="ext_currency_id"><option value="">—</option><option value="card_rub" data-currency="rub">Банковская карта</option>
<option value="fps" data-currency="rub">СБП</option><option value="card_usd" data-currency="usd">Card USD</option></select>
<select name="bank_id"><option value="1">Сбербанк</option><option value="2">Т-Банк</option><option value="3">Альфа-Банк</option></select>
<input name="otp_code" value=""></form></html>"""


def setup(env, balance=1500.0):
    mod, c = env("AutoWithdraw_LuckyFunpay.py")
    acc = c.account
    acc.balance = fpt.Balance(balance + 500, balance, 0, 0, 0, 0)
    acc.http["account/balance"] = PAGE
    sent = []

    def withdraw(method, api, payload):
        sent.append(dict(payload))
        return {"error": False, "amount_ext": str(int(payload["amount_int"]) * 0.97)}

    acc.http["withdraw/withdraw"] = withdraw
    acc.get_wallets = lambda: [fpt.Wallet("card_rub", "2200 **** 1234", 0, 5, True, "Карта"),
                               fpt.Wallet("fps", "+79990001122", 1, 6, False, "СБП")]
    mod.PLUGIN.sync = True
    return mod, c, sent


def test_totp_rfc_vector(env):
    mod, c = env("AutoWithdraw_LuckyFunpay.py")
    secret = mod.clean_secret("GEZD GNBV GY3T QOJQ GEZD GNBV GY3T QOJQ")
    assert mod.totp(secret, at=59) == "287082" and mod.totp(secret, at=1111111109) == "081804"


def test_setup_sbp_trigger_and_auto(env):
    mod, c, sent = setup(env)
    tg, p = c.telegram, mod.PLUGIN
    tg.message("/autowithdraw")
    tg.click("Вкл")
    assert "Сначала настройте реквизиты" in tg.screen().text
    tg.click("Меню")
    tg.click("Настроить реквизиты")
    tg.click("₽ (RUB)")
    labels = [b.text for b in tg.buttons()]
    assert "СБП" in labels and "Card USD" not in labels
    tg.click("СБП")
    tg.click("Поиск банка")
    tg.message("-банк")
    assert [b.text for b in tg.buttons()][:2] == ["Т-Банк", "Альфа-Банк"]
    tg.click("Т-Банк")
    assert "+79990001122" in str([b.text for b in tg.buttons()])
    tg.click("+79990001122")
    d = p.store.snapshot()
    assert (d["method"], d["bank"], d["wallet"]) == ("fps", "2", "+79990001122")
    tg.message("/autowithdraw")
    tg.click("Триггер")
    tg.message("1000")
    tg.click("Остаток")
    tg.message("200")
    tg.click("2FA")
    tg.click("Ввести секрет")
    tg.message("bad!")
    assert "Секрет 2FA" in tg.screen().text
    tg.click("Ввести заново")
    tg.message("CQ7K5US5WSDSNICQ")
    tg.click("Показать текущий код")
    assert "Текущий код" in str(c.telegram.bot.answers[-1])
    tg.message("/autowithdraw")
    tg.click("Вкл")
    assert p.store["enabled"]
    # покупатель подтвердил заказ → баланс 1500 ≥ 1200 → вывод 1300
    short = c.account.add_order("CLOSED01", status=enums.OrderStatuses.CLOSED)
    mod.status_changed(c, status_event(short))
    assert sent[-1]["amount_int"] == "1300" and sent[-1]["ext_currency_id"] == "fps" and sent[-1]["bank_id"] == "2"
    assert sent[-1]["wallet"] == "+79990001122" and len(sent[-1]["otp_code"]) == 6
    assert "Автовывод" in tg.screen().text and "1300" in tg.screen().text
    # баланс ниже порога — ничего
    c.account.balance = fpt.Balance(1100, 1100, 0, 0, 0, 0)
    mod.status_changed(c, status_event(short))
    assert len(sent) == 1


def test_manual_and_funpay_error(env):
    mod, c, sent = setup(env, balance=750.4)
    tg, p = c.telegram, mod.PLUGIN
    p.store.set(method="card_rub", method_label="Карта", wallet="2200123412341234", wallet_label="2200…1234",
                reserve=50)
    tg.message("/autowithdraw")
    tg.click("Вывести всё сейчас")
    assert "Вывести <b>700 ₽</b>" in tg.screen().text
    tg.click("Да, вывести")
    assert sent[-1]["amount_int"] == "700" and "otp_code" not in sent[-1] or sent[-1]["otp_code"] == ""
    c.account.http["withdraw/withdraw"] = {"error": True, "msg": "Введите код из приложения-аутентификатора"}
    tg.message("/autowithdraw")
    tg.click("Вывести всё сейчас")
    tg.click("Да, вывести")
    assert "Настройте Google Authenticator" in tg.screen().text
    tg.click("Меню")
    tg.click("История")
    assert "✅ 700" in tg.screen().text and "❌" in tg.screen().text
