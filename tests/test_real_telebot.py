"""Плагины на настоящем telebot: команды и кнопки проходят через реальный диспетчер TeleBot и обёртки Cardinal."""
import json
import time

import pytest
import telebot
from telebot import apihelper, types

import fpc_fakes
from fpc_fakes import ADMIN, PLUGINS

FILES = sorted(p.name for p in PLUGINS.glob("*_LuckyFunpay.py"))


class Recorder:
    def __init__(self):
        self.calls = []
        self.mid = 100

    def __call__(self, token, method_name, method="get", params=None, files=None):
        params = dict(params or {})
        self.calls.append((method_name, params))
        if method_name in ("sendMessage", "editMessageText"):
            text = params.get("text", "")
            if params.get("parse_mode") == "HTML":
                fpc_fakes.check_tg_html(text)
            self.mid += 1
            return {"message_id": self.mid, "date": int(time.time()), "text": text,
                    "chat": {"id": int(params.get("chat_id", ADMIN)), "type": "private"}}
        if method_name == "getMe":
            return {"id": 1, "is_bot": True, "first_name": "bot", "username": "main_bot"}
        if method_name in ("getChatMember",):
            return {"status": "administrator", "user": {"id": 1, "is_bot": True, "first_name": "b"},
                    "can_manage_topics": True}
        return True


@pytest.fixture
def real_bot(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(apihelper, "_make_request", rec)
    return rec


def make_tg(bot):
    from tg_bot.bot import TGBot
    tg = TGBot.__new__(TGBot)
    tg.bot = bot
    tg.authorized_users = {ADMIN: {}}
    tg.commands = {}
    return tg


def update_message(text, uid=ADMIN):
    return types.Update.de_json(json.dumps({"update_id": 1, "message": {
        "message_id": 5, "date": 0, "text": text, "chat": {"id": uid, "type": "private"},
        "from": {"id": uid, "is_bot": False, "first_name": "A"},
        "entities": [{"offset": 0, "length": len(text.split()[0]), "type": "bot_command"}] if text.startswith("/") else []}}))


def update_callback(data, uid=ADMIN):
    return types.Update.de_json(json.dumps({"update_id": 2, "callback_query": {
        "id": "7", "data": data, "chat_instance": "1", "from": {"id": uid, "is_bot": False, "first_name": "A"},
        "message": {"message_id": 9, "date": 0, "text": "x", "chat": {"id": uid, "type": "private"}}}}))


def sent(rec):
    return [p for m, p in rec.calls if m in ("sendMessage", "editMessageText")]


@pytest.mark.parametrize("file_name", FILES)
def test_plugin_on_real_telebot(env, real_bot, file_name):
    import tg_bot.bot  # noqa: F401  (Cardinal включает middleware до создания бота)
    bot = telebot.TeleBot("123456:" + "A" * 35, threaded=False)
    mod, c = env(file_name, init=False)
    c.telegram = make_tg(bot)
    mod.init(c)
    panel = mod.PLUGIN.ui
    command = panel.COMMAND or panel.COMMANDS[0][0]
    bot.process_new_updates([update_message(f"/{command}")])
    msgs = sent(real_bot)
    assert msgs, "панель не открылась"
    markup = json.loads(msgs[-1]["reply_markup"])
    buttons = [b for row in markup["inline_keyboard"] for b in row]
    guide = next(b for b in buttons if "Инструкция" in b["text"])
    bot.process_new_updates([update_callback(guide["callback_data"])])
    assert "инструкция" in sent(real_bot)[-1]["text"]
    assert any(m == "answerCallbackQuery" for m, _ in real_bot.calls)
    # открытие из списка плагинов Cardinal
    n = len(sent(real_bot))
    bot.process_new_updates([update_callback(f"47:{mod.UUID}:0")])
    assert len(sent(real_bot)) == n + 1
    # чужой пользователь не получает ответа
    n = len(sent(real_bot))
    bot.process_new_updates([update_message(f"/{command}", uid=777)])
    bot.process_new_updates([update_callback(guide["callback_data"], uid=777)])
    assert len(sent(real_bot)) == n
