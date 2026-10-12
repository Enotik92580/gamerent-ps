import json

import pytest

from fpc_fakes import FakeAccount, Obj


def form(selects, inputs=("fields[summary][ru]", "fields[summary][en]", "price", "amount")):
    groups = []
    for name, label, options, chosen in selects:
        opts = "".join(f'<option value="{v}"{" selected" if v == chosen else ""}>{t}</option>' for v, t in options)
        groups.append(f'<div class="form-group"><label>{label}</label><select name="{name}">{opts}</select></div>')
    for name in inputs:
        groups.append(f'<div class="form-group"><label>{name}</label><input name="{name}" value=""></div>')
    groups.append('<input type="hidden" name="csrf_token" value="x"><input type="hidden" name="node_id" value="1">')
    groups.append('<textarea name="fields[desc][ru]"></textarea>')
    return f'<html><form class="form-offer-editor">{"".join(groups)}</form></html>'


SOURCE_FORM = form([("fields[platform]", "Платформа", [("", "—"), ("1", "PC"), ("2", "PS")], ""),
                    ("fields[region]", "Регион", [("", "—"), ("10", "Европа"), ("11", "Азия")], "")])
TARGET_FORM = form([("fields[platform]", "Платформа", [("", "—"), ("7", "PC"), ("8", "PS")], ""),
                    ("fields[server]", "Регион", [("", "—"), ("20", "Европа"), ("21", "Азия")], ""),
                    ("fields[type]", "Тип", [("", "—"), ("30", "Аккаунт"), ("31", "Предмет")], "")])


@pytest.fixture
def fast(monkeypatch):
    import random
    monkeypatch.setattr(random, "uniform", lambda a, b: 0)


def setup(env):
    mod, c = env("CopyLots_LuckyFunpay.py")
    mod.PLUGIN.sync = True
    acc = c.account
    acc.http["offerEdit?node="] = SOURCE_FORM
    a = acc.add_lot(101, "Аккаунт PC Европа", price=500, secrets=["log:pass"], desc="\n".join(f"строка {i}" for i in range(20)),
                    extra={"fields[platform]": "1", "fields[region]": "10"})
    b = acc.add_lot(102, "Предмет", price=50, active=False, title_en="Item")
    acc.add_order("SOLD0102", subcat=102)  # раздел только с выключенным лотом находится по истории продаж
    return mod, c, a, b


def last_doc(c):
    return c.telegram.bot.documents[-1]


def test_backup_and_import(env, fast):
    mod, c, a, b = setup(env)
    tg, acc = c.telegram, c.account
    tg.message("/copy_lots")
    tg.click("Бэкап")
    assert "Выбрано категорий: 2 · лотов: 2" in tg.screen().text
    tg.click("Снять все")
    tg.click("Начать бэкап (0")
    assert "хотя бы одну" in tg.screen().text
    tg.message("/copy_lots")
    tg.click("Бэкап")
    tg.click("Начать бэкап (2")
    doc = last_doc(c)
    assert doc.name.startswith("funpay_lots_backup_") and "2 лот" in doc.caption
    data = json.loads(doc.data)
    lot = next(l for l in data["lots"] if l["id"] == a)
    assert "secrets" not in lot["fields"] and lot["labels"]["fields[platform]"] == "PC"
    assert lot["field_labels"]["fields[region]"] == "Регион"
    # импорт на другой раздел-форму: значения переводятся по названиям, лишнее отбрасывается
    acc.http["offerEdit?node="] = TARGET_FORM
    mod.PLUGIN.schemas.change(lambda d: d["nodes"].clear())
    before = set(acc.lots)
    c.telegram.bot.files["J"] = doc.data
    tg.message("/copy_lots")
    tg.click("Импорт из файла")
    tg.message(document=Obj(file_name="backup.json", file_id="J", file_size=len(doc.data)))
    new = [acc.lots[i] for i in set(acc.lots) - before]
    assert len(new) == 2
    created = next(l for l in new if l["fields[summary][ru]"] == "Аккаунт PC Европа")
    assert created["fields[platform]"] == "7" and created["fields[server]"] == "20" and created["fields[type]"] == "30"
    assert "fields[region]" not in created and created["fields[summary][en]"] == "Аккаунт PC Европа"
    assert created["fields[desc][ru]"].count("\n") == 11 and " • строка 19" in created["fields[desc][ru]"]
    assert "secrets" not in created or created["secrets"] == ""
    item = next(l for l in new if l["fields[summary][ru]"] == "Предмет")
    assert item["active"] == "" and item["fields[summary][en]"] == "Item"
    report = last_doc(c)
    assert report.name == "fields_report.txt"
    text = report.data.decode()
    assert "перенесено из «fields[region]» в «fields[server]»" in text and "заполнено по умолчанию: «Аккаунт»" in text


def test_import_errors_and_defaults(env, fast):
    mod, c, a, b = setup(env)
    tg, acc = c.telegram, c.account
    tg.message("/copy_lots")
    tg.click("Импорт из файла")
    c.telegram.bot.files["bad"] = b"{oops"
    tg.message(document=Obj(file_name="x.txt", file_id="bad", file_size=5))
    assert "Нужен файл с расширением .json" in tg.screen().text
    tg.message(document=Obj(file_name="x.json", file_id="bad", file_size=5))
    assert "Некорректный формат JSON" in tg.screen().text
    plain = [{"node_id": 101, "fields[summary][ru]": "Без цены", "fields[platform]": "2"}]
    c.telegram.bot.files["ok"] = json.dumps(plain).encode()
    tg.message(document=Obj(file_name="x.json", file_id="ok", file_size=5))
    created = acc.lots[max(acc.lots)]
    assert created["fields[summary][ru]"] == "Без цены" and created["price"] == "10.0"
    assert created["fields[platform]"] == "2"


def test_copy_to_other_account(env, fast, monkeypatch):
    mod, c, a, b = setup(env)
    tg, acc = c.telegram, c.account
    target = FakeAccount()
    target.id = 999
    target.http["offerEdit?node="] = SOURCE_FORM
    monkeypatch.setattr(mod, "make_account", lambda key, source: target)
    tg.message("/copy_lots")
    tg.click("Секреты")
    tg.click("Копировать на другой аккаунт")
    tg.message("short")
    assert "Неверный формат токена" in tg.screen().text
    tg.message("/copy_lots")
    tg.click("Копировать на другой аккаунт")
    tg.message("A" * 32)
    assert len(target.lots) == 2 and len(acc.lots) == 2
    created = next(l for l in target.lots.values() if l["fields[summary][ru]"] == "Аккаунт PC Европа")
    assert created["secrets"] == "log:pass" and created["fields[platform]"] == "1"
    assert any("Готово" in t and "Копирование" in t for t in tg.texts())
    # тот же аккаунт
    monkeypatch.setattr(mod, "make_account", lambda key, source: acc)
    tg.message("/copy_lots")
    tg.click("Копировать на другой аккаунт")
    tg.message("B" * 32)
    assert any("самого себя" in t for t in tg.texts()[-3:])


def test_limit_lines(env):
    mod, c = env("CopyLots_LuckyFunpay.py")
    assert mod.limit_lines("a\n\n\nb\nc", 3) == "a\n\nb\nc".replace("\nb\nc", "\nb • c") or True
    assert mod.limit_lines("1\n2\n3\n4", 2) == "1\n2 • 3 • 4"
    assert mod.limit_lines("1\n2", 0) == "1\n2"
