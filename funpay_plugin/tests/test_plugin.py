"""Сценарии из руководства Auto VIP Roblox. Запуск: python3 -m unittest discover funpay_plugin/tests"""
import itertools
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fakes  # noqa: E402

COUNTER = itertools.count()
COOKIE = "_|WARNING:-DO-NOT-SHARE-THIS.--Sharing-this-will-allow-someone-to-log-in-as-you|_OWNER"


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def time(self):
        return self.t


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = fakes.load_module(fakes.PLUGIN_PATH, f"avr_plugin_{next(COUNTER)}")
        self.p.DB_PATH = os.path.join(self.tmp.name, "new.db")
        self.p.OLD_DB_PATH = os.path.join(self.tmp.name, "old.db")
        self.clock = Clock()
        self.p.time = types.SimpleNamespace(time=self.clock.time, sleep=lambda s: None, strftime=time.strftime,
                                            localtime=time.localtime)
        self.world = fakes.World()
        self.world.cookies[COOKIE] = ("900", "OwnerNick")
        self.p.RobloxAPI = fakes.make_fake_api(self.p, self.world)
        self.c = fakes.FakeCardinal(self.p.UUID)
        self.tg = self.c.telegram
        self.acc = self.c.account

    def tearDown(self):
        self.p.STOP.set()
        self.p.EXECUTOR.shutdown(wait=True)
        if self.p.S is not None:
            self.p.S.con.close()
        self.tmp.cleanup()

    # --- помощники ---
    def init(self):
        self.p.init_plugin(self.c)
        return self.p.S

    def idle(self):
        for _ in range(500):
            with self.p.GUARD:
                if not self.p.RUNNING:
                    return
            time.sleep(0.01)
        self.fail("операции не завершились")

    def chat(self, text, buyer=111, chat=111, name="buyer"):
        self.p.handle_chat_message(chat, buyer, name, text, next(COUNTER))
        self.idle()
        return self.acc.chat_texts(chat)[-1] if self.acc.chat_texts(chat) else None

    def tg_last(self):
        return self.tg.bot.last()[1]

    def setup_shop(self, servers=("111", "222", "333")):
        S = self.init()
        self.tg.click("avr17:acc_add")
        self.tg.type(COOKIE)
        self.assertIn("✅ Аккаунт OwnerNick добавлен.", self.tg.bot.texts())
        for sid in servers:
            self.world.add_server(sid, "900")
        self.tg.click("avr17:cat_add")
        self.tg.type("Steal a Brainrot")
        self.tg.click("avr17:cs_add:1")
        self.tg.type(" ".join(servers))
        self.assertTrue(any(f"Добавлено серверов: {len(servers)}" in t for t in self.tg.bot.texts()))
        self.acc.lots[500] = fakes.FakeLotFields(500, "Аренда VIP 24ч")
        self.tg.click("avr17:lot_add")
        self.tg.type("500")
        self.tg.type("1,5")
        self.assertIn("✅ Лот Аренда VIP 24ч (1.5ч) добавлен.", self.tg.bot.texts())
        self.tg.click("avr17:lcb:500:1")
        return S


class TestShop(Base):
    def test_panel_setup_and_validation(self):
        S = self.init()
        self.tg.type("/vip_config")
        self.assertIn("Auto VIP Roblox", self.tg_last())
        self.assertIn("❌ Не задан", self.tg_last())
        self.tg.click("avr17:proxy")
        self.tg.type("bad proxy")
        self.assertIn("❌ Неверный формат прокси. Попробуйте еще раз.", self.tg_last())
        self.tg.type("socks5://user:pass@10.0.0.1:8080")
        self.assertEqual(S.get("global_proxy"), "user:pass@10.0.0.1:8080")
        self.tg.click("avr17:acc_add")
        self.tg.type("garbage")
        self.assertIn("Невалидный куки или ошибка прокси", self.tg_last())
        self.tg.type(COOKIE)
        self.tg.click("avr17:acc")
        self.assertIn("1. OwnerNick", self.tg_last())
        # Лот: проверки ID, дубликатов, часов
        self.acc.lots[500] = fakes.FakeLotFields(500, "Аренда VIP 24ч")
        self.acc.lots[501] = fakes.FakeLotFields(501, "  аренда  vip 24Ч ")
        self.tg.click("avr17:lot_add")
        self.tg.type("abc")
        self.assertIn("ID лота должен быть числом", self.tg_last())
        self.tg.type("777")
        self.assertIn("Не удалось прочитать название лота 777", self.tg_last())
        self.tg.type("500")
        self.tg.type("0")
        self.assertIn("больше 0", self.tg_last())
        self.tg.type("24")
        self.tg.click("avr17:lot_add")
        self.tg.type("500")
        self.assertIn("Лот 500 уже добавлен в плагин", self.tg_last())
        self.tg.type("501")
        self.assertIn("Лот с таким названием уже добавлен", self.tg_last())
        lot = S.q1("SELECT * FROM lots WHERE lot_id=500")
        self.assertEqual((lot["price_normal"], lot["price_event"]), (50, 100))
        # Переименование на FunPay → дубль виден, обновление названия
        S.ex("INSERT INTO lots(lot_id,name,hours) VALUES(502,'Аренда VIP 24ч',1)")
        self.tg.click("avr17:lots:0")
        self.assertIn("Одинаковые названия лотов", self.tg_last())
        self.acc.lots[502] = fakes.FakeLotFields(502, "Аренда VIP 3ч")
        self.tg.click("avr17:lr:502")
        self.assertTrue(any("Стало: Аренда VIP 3ч" in t for t in self.tg.bot.texts()))
        # Привязки лота
        self.tg.click("avr17:ls:500")
        self.tg.type("12345")
        self.assertIn("нет в списке", self.tg_last() + "".join(self.tg.bot.texts()[-3:]))
        self.tg.click("avr17:lf:500")
        self.tg.type("0")
        self.assertEqual(S.q1("SELECT friend_limit FROM lots WHERE lot_id=500")[0], 0)

    def test_every_menu_opens(self):
        S = self.setup_shop()
        self.p.process_order(fakes.order("ORD1", "Аренда VIP 24ч"))
        rid = S.q1("SELECT rental_id FROM rentals")[0]
        routes = ["main", "srv:0", "acc", "cats", "cat:1", "lots:0", "lot:500", "lc:500", "rents:0", f"rent:{rid}",
                  "bon", "bon_el", "bon_dl", "stats", "hist", "rev", "fr", "pd", "tpl"]
        for route in routes:
            before = len(self.tg.bot.sent)
            self.tg.click("avr17:" + route)
            self.assertGreater(len(self.tg.bot.sent), before, route)
            self.assertNotIn("Не удалось выполнить действие", self.tg_last(), route)
        self.tg.click("avr17:pd")
        for key in ("paid_title_ru", "te:paid_lot_created:pd"):
            self.tg.click("avr17:" + ("pd_s:" + key if not key.startswith("te") else key))
            self.assertIn("Отмена ввода", [b.text for b in self.tg.buttons()][0])
        self.tg.click("46:" + self.p.UUID + ":0")
        self.assertIn("Auto VIP Roblox", self.tg_last())

    def test_purchase_each_order_new_rental_and_commands(self):
        S = self.setup_shop()
        self.p.process_order(fakes.order("ORD1", "Аренда VIP 24ч, 2 шт.", amount=2, price=200))
        texts = self.acc.chat_texts(111)
        self.assertIn("privateServerLinkCode=", texts[-2])
        self.assertIn("⏰ Время аренды: 3 ч.", texts[-2])
        self.assertIn("!друзья", texts[-1])  # приглашение
        self.assertIn("users/900/profile", texts[-1])
        self.p.process_order(fakes.order("ORD1", "Аренда VIP 24ч"))  # повтор события — не обрабатывается
        self.p.process_order(fakes.order("ORD2", "Аренда VIP 24ч", price=100))
        rentals = S.q("SELECT * FROM rentals ORDER BY created_at")
        self.assertEqual(len(rentals), 2)
        self.assertNotEqual(rentals[0]["server_id"], rentals[1]["server_id"])
        self.assertTrue(all(self.world.servers[r["server_id"]]["active"] for r in rentals))
        # !время — список всех аренд
        reply = self.chat("!время")
        self.assertIn("1) Аренда VIP 24ч · заказ #ORD1", reply)
        self.assertIn("2) Аренда VIP 24ч · заказ #ORD2", reply)
        # !ссылка — выбор сервера
        reply = self.chat("!ссылка")
        self.assertIn("К какому применить !ссылка", reply)
        self.assertIsNone(self.p.parse_command("привет"))
        count = len(self.acc.messages)
        self.chat("спасибо, всё понятно")  # обычная переписка не перехватывается
        self.assertEqual(len(self.acc.messages), count)
        self.assertIn("Введите номер от 1 до 2", self.chat("5"))
        old_link = S.q1("SELECT link FROM rentals WHERE order_id='ORD2'")[0]
        reply = self.chat("2")
        self.assertIn("Новая ссылка на сервер (заказ #ORD2)", reply)
        self.assertNotEqual(S.q1("SELECT link FROM rentals WHERE order_id='ORD2'")[0], old_link)
        self.chat("!ссылка")
        self.assertIn("Выбор отменён", self.chat("отмена"))
        self.assertIn("!друзья ВашНик", self.chat("!друзья"))
        self.assertIn("Свободных общих серверов", self.chat("!бронь"))
        self.assertIn("!время", self.chat("!команды"))
        self.assertIn("Нет активной аренды", self.chat("!время", buyer=222, chat=222))
        # Заработано
        self.assertAlmostEqual(self.p.earned_total(), 300)

    def test_no_server_refund_and_link_failure(self):
        S = self.setup_shop(servers=("111",))
        self.p.process_order(fakes.order("A", "Аренда VIP 24ч"))
        self.p.process_order(fakes.order("B", "Аренда VIP 24ч", buyer_id=5, chat_id=5))
        self.assertIn("B", self.acc.refunds)
        self.assertIn("Все серверы для этого лота сейчас заняты", self.acc.chat_texts(5)[-1])
        self.assertIn("Деньги возвращены", self.acc.chat_texts(5)[-1])
        # Сервер другого владельца → ошибка генерации ссылки, без автовозврата
        S.ex("INSERT INTO servers(server_id,added_at) VALUES('999',0)")
        self.world.add_server("999", "12345")
        self.acc.lots[600] = fakes.FakeLotFields(600, "Рандом лот")
        S.ex("INSERT INTO lots(lot_id,name,hours) VALUES(600,'Рандом лот',1)")
        self.p.process_order(fakes.order("C", "Рандом лот", buyer_id=6, chat_id=6))
        self.assertEqual(self.acc.chat_texts(6)[-1], "❌ Ошибка генерации ссылки. Обратитесь к администратору.")
        self.assertNotIn("C", self.acc.refunds)
        self.assertIsNone(S.q1("SELECT 1 FROM rentals WHERE order_id='C'"))
        # Чёрный список и чужие лоты
        self.c.blacklist = ["bad"]
        self.p.process_order(fakes.order("D", "Аренда VIP 24ч", buyer="bad", buyer_id=7, chat_id=7))
        self.p.process_order(fakes.order("E", "Совсем другой товар", buyer_id=8, chat_id=8))
        self.assertEqual(self.acc.chat_texts(7) + self.acc.chat_texts(8), [])

    def test_bonus_rules_and_templates(self):
        S = self.setup_shop()
        self.tg.click("avr17:bon_a")
        self.tg.type("5")
        self.assertIn("Неверный формат", self.tg_last())
        self.tg.type("a:b")
        self.assertIn("Используйте числа", self.tg_last())
        self.tg.type("3:1")
        self.tg.click("avr17:bon_a")
        self.tg.type("10:5")
        self.assertEqual(self.p.volume_bonus(3), 1)
        self.assertEqual(self.p.volume_bonus(15), 1)  # первое подходящее правило, без умножения
        self.p.process_order(fakes.order("A", "Аренда VIP 24ч", amount=2))
        self.assertIn("🎁 Бонус: +1 час в подарок!", self.acc.chat_texts(111)[-2])
        self.assertEqual(S.q1("SELECT hours FROM rentals")[0], 4)
        # Шаблон с неизвестной переменной → упрощённое сообщение
        self.tg.click("avr17:te:post_payment:tpl")
        self.tg.type("Привет {nope} {link}")
        self.assertTrue(any("неизвестные переменные" in t for t in self.tg.bot.texts()[-2:]))
        self.p.process_order(fakes.order("B", "Аренда VIP 24ч", buyer_id=9, chat_id=9))
        self.assertTrue(self.acc.chat_texts(9)[0].startswith("🎮 VIP-сервер выдан!"))
        self.tg.click("avr17:te:post_payment:tpl")
        self.tg.type("<b>Ссылка</b> {link} на {hours} ч, #{order_id}, {price} руб")
        self.p.process_order(fakes.order("C", "Аренда VIP 24ч", buyer_id=10, chat_id=10, price=77))
        self.assertTrue(self.acc.chat_texts(10)[0].startswith("Ссылка https://www.roblox.com/games/"))
        self.assertIn(", #C, 77 руб", self.acc.chat_texts(10)[0])

    def test_friends_limit_and_paid_addition(self):
        S = self.setup_shop()
        self.tg.click("avr17:fr_l")
        self.tg.type("1")
        self.tg.click("avr17:pd_t")
        self.assertTrue(S.flag("paid_active"))
        self.p.process_order(fakes.order("A", "Аренда VIP 24ч"))
        rental = S.q1("SELECT * FROM rentals")
        server = self.world.servers[rental["server_id"]]
        self.world.users.update({"Kid1": "501", "Kid2": "502", "Kid3": "503"})
        self.assertIn("Не нашёл игрока или входящую заявку", self.chat("!друзья Kid1"))
        self.world.requests.add(("900", "501"))
        self.chat("!друзья Kid1")
        texts = self.acc.chat_texts(111)
        self.assertIn("✅ Kid1 добавлен(а) в друзья и на VIP-сервер", texts[-2])
        self.assertIn("Бесплатные добавления для заказа #A закончились", texts[-1])
        self.assertIn("501", server["users"])
        self.assertIn("уже добавлен(а)", self.chat("!друзья Kid1"))
        # Лимит исчерпан → создаётся временный лот с тегом
        reply = self.chat("!друзья Kid2")
        self.assertIn("https://funpay.com/lots/offer?id=5001", reply)
        offer = self.acc.offers[5001]
        tag = S.q1("SELECT tag FROM friend_lots")[0]
        self.assertTrue(offer["fields[summary][ru]"].endswith(tag))
        self.assertEqual(offer["fields[game_name][ru]"], "Roblox VIP сервер")
        self.assertIn("уже есть неоплаченный лот", self.chat("!друзья Kid3"))
        # Оплата: заявка от Kid2 ещё не отправлена → добавление сохраняется
        self.p.process_order(fakes.order("P1", f"Добавление друга {tag}", amount=2, price=100))
        texts = self.acc.chat_texts(111)
        self.assertIn(5001, self.acc.deleted_offers)
        self.assertTrue(any("Начислено добавлений: 2" in t for t in texts[-4:]))
        self.assertTrue(any("Оплаченное добавление сохранено" in t for t in texts[-3:]))
        self.assertIn("осталось добавлений: 2", texts[-1])
        self.world.requests.add(("900", "502"))
        self.assertIn("✅ Kid2 добавлен(а)", self.chat("!друзья Kid2"))
        self.assertAlmostEqual(self.p.earned_total(), 200)
        # Лот, купленный другим покупателем → возврат
        self.world.requests.add(("900", "503"))
        self.world.requests.add(("900", "504"))
        self.world.users["Kid4"] = "504"
        self.chat("!друзья Kid3")
        self.chat("!друзья Kid4")
        tag2 = S.q1("SELECT tag FROM friend_lots WHERE state='active'")[0]
        self.p.process_order(fakes.order("P2", f"Добавление друга {tag2}", buyer_id=4242, chat_id=4242))
        self.assertIn("P2", self.acc.refunds)
        self.assertIn("создан для другого покупателя", self.acc.chat_texts(4242)[-1])
        # Тихое завершение из Telegram: сервер выключен, друзья удалены, покупателю ничего не пришло
        count = len(self.acc.chat_texts(111))
        self.tg.click(f"avr17:rq:{rental['rental_id']}")
        self.idle()
        self.assertTrue(any("тихо, покупатель не уведомлён" in t for t in self.tg.bot.texts()))
        self.assertEqual(len(self.acc.chat_texts(111)), count)
        self.assertFalse(server["active"])
        self.assertEqual(server["users"], set())
        self.assertNotIn(("900", "501"), self.world.friends)
        self.assertEqual(S.q1("SELECT status FROM rentals")[0], "completed")
        self.assertEqual(S.q1(f"SELECT state FROM friend_lots WHERE tag='{tag2}'")[0], "paid")

    def test_timers_warning_expiry_review_extend(self):
        S = self.setup_shop()
        self.p.process_order(fakes.order("A", "Аренда VIP 24ч"))  # 1.5 ч
        rental = S.q1("SELECT * FROM rentals")
        self.tg.click(f"avr17:rx:{rental['rental_id']}")
        self.tg.type("2")
        self.assertIn("🔄 Аренда продлена!", self.acc.chat_texts(111)[-1])
        self.assertEqual(S.q1("SELECT hours FROM rentals")[0], 3.5)
        self.clock.t += 3.5 * 3600 - 10 * 60
        self.p.worker_tick()
        self.idle()
        self.assertIn("осталось 15 минут", self.acc.chat_texts(111)[-1])
        self.p.worker_tick()
        self.assertEqual(sum("15 минут" in t for t in self.acc.chat_texts(111)), 1)
        self.clock.t += 11 * 60
        self.p.worker_tick()
        self.idle()
        self.assertIn("https://funpay.com/orders/A/", self.acc.chat_texts(111)[-1])
        self.assertTrue(any("🏁" in t for t in self.tg.bot.texts()))
        self.assertFalse(self.world.servers[rental["server_id"]]["active"])
        # Бонус за отзыв после окончания аренды: сообщение есть, часы не добавлены
        self.acc.orders["A"] = types.SimpleNamespace(review=None)
        self.assertIn("Отзыв не найден", self.chat("!отзыв"))
        self.acc.orders["A"] = types.SimpleNamespace(review=types.SimpleNamespace(stars=3, text="норм"))
        self.assertIn("Спасибо за отзыв", self.chat("!отзыв"))
        self.assertIn("уже получен", self.chat("!отзыв"))
        # Отзыв по активной аренде продлевает её и подставляет {new_time}
        self.p.process_order(fakes.order("B", "Аренда VIP 24ч"))
        self.acc.orders["B"] = types.SimpleNamespace(review=types.SimpleNamespace(stars=5, text=""))
        end_before = S.q1("SELECT end_time FROM rentals WHERE order_id='B'")[0]
        reply = self.chat("!отзыв")
        self.assertEqual(S.q1("SELECT end_time FROM rentals WHERE order_id='B'")[0], end_before + 3600)
        self.assertIn(time.strftime("%d.%m %H:%M", time.localtime(end_before + 3600)), reply)

    def test_cleanup_retry_keeps_server_busy(self):
        S = self.setup_shop(servers=("111",))
        self.p.process_order(fakes.order("A", "Аренда VIP 24ч"))
        self.world.fail_deactivate = True
        self.clock.t += 2 * 3600
        self.p.worker_tick()
        self.idle()
        self.assertEqual(S.q1("SELECT status FROM rentals")[0], "ending")
        self.assertTrue(any("Не удалось выключить сервер" in t for t in self.tg.bot.texts()))
        self.p.process_order(fakes.order("B", "Аренда VIP 24ч", buyer_id=2, chat_id=2))
        self.assertIn("B", self.acc.refunds)  # сервер не выдаётся повторно
        self.world.fail_deactivate = False
        self.clock.t += 60
        self.p.worker_tick()
        self.idle()
        self.assertEqual(S.q1("SELECT status FROM rentals WHERE order_id='A'")[0], "completed")

    def test_prices_and_lot_sync(self):
        S = self.setup_shop(servers=("111",))
        self.tg.click("avr17:lpe:500")
        self.tg.type("150")
        self.tg.click("avr17:mode")
        self.idle()
        self.assertIn((500, True, 150.0), self.acc.saved)
        self.acc.lots[500].active = True
        self.p.process_order(fakes.order("A", "Аренда VIP 24ч"))
        self.p.sync_lots(full=False)
        self.assertFalse(self.acc.lots[500].active)  # нет свободных серверов → лот выключен
        self.tg.click("avr17:lotmgmt")
        self.assertFalse(S.flag("lot_management"))
        self.assertIn("🔴 Активировать лоты", [b.text for b in self.tg.buttons()])

    def test_category_delete_moves_lots_to_random(self):
        S = self.setup_shop()
        self.tg.click("avr17:cat_del:1")
        self.assertEqual(S.q1("SELECT category_id FROM lots WHERE lot_id=500")[0], None)
        self.assertEqual(self.p.free_general_count(), 3)
        self.tg.click("avr17:srv:0")
        self.assertIn("общий", self.tg_last())


V16_SCHEMA = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, cookie TEXT NOT NULL, proxy TEXT NOT NULL DEFAULT '',
  username TEXT, roblox_id TEXT, enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
CREATE TABLE categories (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, game_id TEXT);
CREATE TABLE servers (server_id TEXT PRIMARY KEY, category_id INTEGER, owner_account_id INTEGER, place_id TEXT,
  status TEXT NOT NULL DEFAULT 'free', verified INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE lots (lot_id INTEGER PRIMARY KEY, hours REAL NOT NULL DEFAULT 1, category_id INTEGER, server_id TEXT,
  bind_type TEXT NOT NULL DEFAULT 'random', price_normal REAL NOT NULL DEFAULT 0, price_event REAL NOT NULL DEFAULT 0,
  friend_limit INTEGER NOT NULL DEFAULT -1, active INTEGER NOT NULL DEFAULT 1, name TEXT, paused INTEGER NOT NULL DEFAULT 0);
CREATE TABLE wholesale_bonuses (id INTEGER PRIMARY KEY AUTOINCREMENT, buy_hours REAL NOT NULL, get_hours REAL NOT NULL);
CREATE TABLE rentals (rental_id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL, chat_id TEXT NOT NULL, lot_id INTEGER NOT NULL,
  server_id TEXT NOT NULL, owner_account_id INTEGER NOT NULL, hours REAL NOT NULL, start_time REAL NOT NULL,
  end_time REAL NOT NULL, status TEXT NOT NULL, price REAL NOT NULL DEFAULT 0, cleanup_reason TEXT, link TEXT);
CREATE TABLE payments (order_id TEXT PRIMARY KEY, rental_id TEXT, buyer_id TEXT NOT NULL, chat_id TEXT NOT NULL,
  lot_id INTEGER, paid_hours REAL NOT NULL, bonus_hours REAL NOT NULL, price REAL NOT NULL DEFAULT 0, state TEXT NOT NULL,
  review_bonus_given INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE rental_friends (rental_id TEXT NOT NULL, roblox_id TEXT NOT NULL, name TEXT,
  remove_from_server INTEGER NOT NULL DEFAULT 1, unfriend_on_cleanup INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'added', PRIMARY KEY(rental_id, roblox_id));
"""


class TestMigration(Base):
    def test_v16_database_is_imported(self):
        old = sqlite3.connect(self.p.OLD_DB_PATH)
        old.executescript(V16_SCHEMA)
        t = self.clock.t
        old.executescript(f"""
            INSERT INTO settings VALUES('global_proxy','http://u:p@1.1.1.1:3128'),('event_mode','1'),
              ('friends_default_limit','2'),('msg_friend_added','Готово, {{friend_name}}!'),
              ('msg_post_payment','old {{link}} {{owner_url}}');
            INSERT INTO accounts VALUES(0,'','','UNBOUND',NULL,0,0),(1,'{COOKIE}','','OwnerNick','900',1,0);
            INSERT INTO categories VALUES(1,'SAB','109983668079237');
            INSERT INTO servers VALUES('111',1,1,'109983668079237','occupied',1,1),('222',NULL,1,'1','free',1,1),
              ('333',NULL,1,'1','free',1,0);
            INSERT INTO lots VALUES(500,24,1,NULL,'category',50,80,-1,1,'Аренда VIP 24ч',0),
              (501,1,NULL,'222','server',10,20,3,1,'Аренда 1ч',0);
            INSERT INTO wholesale_bonuses VALUES(1,5,1);
            INSERT INTO rentals VALUES('r1','111','111',500,'111',1,24,{t - 3600},{t + 3600 * 23},'active',150,NULL,'L');
            INSERT INTO rentals VALUES('r0','112','112',501,'222',1,1,{t - 9000},{t - 5400},'completed',10,'expired',NULL);
            INSERT INTO payments VALUES('ORDOLD','r1','111','111',500,24,0,150,'issued',1,{t - 3600});
            INSERT INTO payments VALUES('ORD0','r0','112','112',501,1,0,10,'issued',0,{t - 9000});
            INSERT INTO rental_friends VALUES('r1','501','Kid',1,1,'added');
        """)
        old.commit()
        old.close()
        S = self.init()
        self.assertEqual(S.get("global_proxy"), "u:p@1.1.1.1:3128")
        self.assertEqual(S.get("event_mode"), "1")
        self.assertEqual(S.get("tpl_friend_added"), "Готово, {friend_name}!")
        self.assertEqual(S.get("tpl_post_payment", ""), "")  # старые переменные → стандартный шаблон
        self.assertEqual([a["id"] for a in S.q("SELECT id FROM accounts")], [1])
        self.assertEqual(sorted(r[0] for r in S.q("SELECT server_id FROM servers")), ["111", "222"])
        lots = {r["lot_id"]: r for r in S.q("SELECT * FROM lots")}
        self.assertEqual((lots[500]["category_id"], lots[500]["friend_limit"]), (1, None))
        self.assertEqual((lots[501]["server_id"], lots[501]["friend_limit"]), ("222", 3))
        rental = S.q1("SELECT * FROM rentals WHERE rental_id='r1'")
        self.assertEqual((rental["order_id"], rental["status"], rental["price"]), ("ORDOLD", "active", 150))
        self.assertEqual(S.q1("SELECT unfriend FROM rental_friends WHERE rental_id='r1'")[0], 1)
        self.assertEqual(S.q1("SELECT status FROM rentals WHERE rental_id='r0'")[0], "completed")
        self.assertTrue(S.q1("SELECT 1 FROM reviews WHERE order_id='ORDOLD'"))
        self.assertTrue(S.q1("SELECT 1 FROM processed_orders WHERE order_id='ORD0'"))
        self.assertAlmostEqual(self.p.earned_total(), 160)
        # Перенесённая аренда работает с командами и завершается по таймеру
        self.world.add_server("111", "900")
        self.world.servers["111"]["active"] = True
        self.assertIn("Заказ #ORDOLD", self.chat("!время") or "")
        self.p.start_plugin(self.c)
        self.assertTrue(any("Данные прежней версии перенесены" in t for t in self.tg.bot.texts()))
        self.clock.t += 24 * 3600
        self.p.worker_tick()
        self.idle()
        self.assertEqual(S.q1("SELECT status FROM rentals WHERE rental_id='r1'")[0], "completed")


if __name__ == "__main__":
    unittest.main()
