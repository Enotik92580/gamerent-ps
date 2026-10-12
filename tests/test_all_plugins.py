"""Общие проверки для каждого плагина: поля Cardinal, UUID, панель, инструкция, кнопки автора."""
import uuid

import pytest

from fpc_fakes import PLUGINS

FILES = sorted(p.name for p in PLUGINS.glob("*_LuckyFunpay.py"))


@pytest.mark.parametrize("file_name", FILES)
def test_plugin_contract(env, file_name):
    mod, c = env(file_name)
    for field in ("NAME", "VERSION", "DESCRIPTION", "CREDITS", "SETTINGS_PAGE", "UUID", "BIND_TO_DELETE"):
        assert hasattr(mod, field), field
    assert str(uuid.UUID(mod.UUID, version=4)) == mod.UUID
    assert mod.CREDITS == "@LuckyFunPay_Support"
    assert mod.CHANNEL_URL == "https://t.me/LuckyFunPay"
    assert "ИНСТРУКЦИЯ" in mod.__doc__
    tg = c.telegram
    panel = mod.PLUGIN.ui
    # открытие панели из списка плагинов Cardinal
    assert tg.callback(f"47:{mod.UUID}:0")
    home = tg.screen()
    assert mod.html.escape(mod.NAME, quote=False) in home.text
    labels = [b.text for b in tg.buttons(home)]
    assert any("Инструкция" in t for t in labels), labels
    urls = [b.url for b in tg.buttons(home) if b.url]
    assert mod.AUTHOR_URL in urls and mod.CHANNEL_URL in urls
    # все страницы инструкции открываются
    tg.click("Инструкция")
    for page in range(len(panel.GUIDE)):
        assert "инструкция" in tg.screen().text
        if page < len(panel.GUIDE) - 1:
            tg.click("➡️")
    # команды добавлены в меню
    assert panel.COMMAND in tg.commands or all(cmd in tg.commands for cmd, *_ in panel.COMMANDS)
    # посторонний пользователь не видит панель
    before = len(tg.bot.timeline)
    tg.message("/" + (panel.COMMAND or panel.COMMANDS[0][0]), user_id=1, chat_id=1)
    assert len(tg.bot.timeline) == before


@pytest.mark.parametrize("file_name", FILES)
def test_plugin_without_telegram(env, file_name):
    mod, c = env(file_name, telegram=False)
    assert mod.PLUGIN is not None
