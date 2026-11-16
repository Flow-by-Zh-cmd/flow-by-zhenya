"""Тесты HTTP API Mini App на временной БД, с фиктивным ботом (никаких сообщений в Telegram)."""
import json, os, tempfile, time, unittest
from datetime import timedelta

TMP = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(TMP, "t.db")
os.environ["BOT_TOKEN"] = "123456:TEST-token"   # не реальный токен
os.environ["OWNER_IDS"] = ""
os.environ["PAYMENT_PROVIDER_TOKEN"] = ""

from aiohttp.test_utils import AioHTTPTestCase
import bot as core
import webapp
from tgauth import sign_init_data

OWNER, CLIENT = 1001, 2002


class FakeBot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, reply_markup=None, **kw): self.sent.append((chat_id, text))
    async def create_invoice_link(self, **kw): return "https://t.me/$invoice-test"


def init_for(uid, name="Тест"):
    return sign_init_data({"auth_date": str(int(time.time())),
                           "user": json.dumps({"id": uid, "first_name": name}, separators=(",", ":"), ensure_ascii=False)},
                          core.BOT_TOKEN)


class WebAppTest(AioHTTPTestCase):
    async def get_application(self):
        self.fb = FakeBot()
        core.db.conn.executescript("DELETE FROM slots; DELETE FROM bookings; DELETE FROM subscriptions; DELETE FROM orders;"
                                   "DELETE FROM cart; DELETE FROM settings; DELETE FROM plans; DELETE FROM products;"
                                   "DELETE FROM about_media; DELETE FROM sqlite_sequence;")
        core.db.seed_if_empty(core.now().date())
        core.db.claim_owner(OWNER)
        core.PAY_TOKEN = ""
        return webapp.make_app(core, self.fb)

    async def call(self, path, body=None, uid=CLIENT, init=None):
        h = {"X-Telegram-Init-Data": init if init is not None else init_for(uid)}
        r = await (self.client.post(path, json=body, headers=h) if body is not None else self.client.get(path, headers=h))
        return r.status, await r.json()

    async def test_auth_required(self):
        self.assertEqual((await self.call("/api/me", init=""))[0], 401)
        bad = init_for(CLIENT).replace("auth_date=", "auth_date=1")
        self.assertEqual((await self.call("/api/me", init=bad))[0], 401)
        self.assertEqual((await self.call("/api/admin/orders"))[0], 403)
        r = await self.client.get("/")
        self.assertEqual(r.status, 200)

    async def test_booking_flow(self):
        st, days = await self.call("/api/days")
        d = days[0]["date"]
        st, slots = await self.call(f"/api/slots?date={d}")
        sid = slots[0]["id"]
        st, j = await self.call("/api/book", {"slot_id": sid})
        self.assertEqual((st, j["code"]), (400, "NO_SUB"))
        # заявка на абонемент без платёжного токена -> уведомление владельцу с кнопкой
        st, j = await self.call("/api/buy", {"plan_id": 1})
        self.assertTrue(j["requested"])
        self.assertIn("Заявка на абонемент", self.fb.sent[-1][1]); self.assertEqual(self.fb.sent[-1][0], OWNER)
        core.db.credit_subscription(CLIENT, 4, 30, "test", now=core.now())
        st, j = await self.call("/api/book", {"slot_id": sid})
        self.assertEqual((st, j["sub"]["left"]), (200, 3))
        self.assertIn("Новая запись", self.fb.sent[-1][1])
        st, slots = await self.call(f"/api/slots?date={d}")
        mine = [s for s in slots if s["status"] == "mine"][0]
        st, bs = await self.call("/api/bookings")
        self.assertEqual(len(bs), 1)
        st, j = await self.call("/api/cancel", {"booking_id": mine["booking_id"]})
        if bs[0]["can_cancel"]:
            self.assertEqual((st, j["sub"]["left"]), (200, 4))
            self.assertIn("Отмена записи", self.fb.sent[-1][1])
        else:
            self.assertEqual(st, 400)

    async def test_buy_with_token_returns_invoice(self):
        core.PAY_TOKEN = "TEST:provider"
        st, j = await self.call("/api/buy", {"plan_id": 1})
        self.assertEqual(j["invoice_url"], "https://t.me/$invoice-test")

    async def test_shop_and_admin(self):
        await self.call("/api/cart", {"product_id": 1, "delta": 2})
        await self.call("/api/cart", {"product_id": 2, "delta": 1})
        st, cart = await self.call("/api/cart")
        self.assertEqual(cart["total"], 2 * 250000 + 90000)
        st, j = await self.call("/api/order", {"delivery": "delivery", "address": "", "phone": "+79001234567"})
        self.assertEqual(st, 400)
        st, j = await self.call("/api/order", {"delivery": "delivery", "address": "Екатеринбург, ул. Ленина 1", "phone": "+7 900 123-45-67"})
        self.assertEqual(st, 200); oid = j["order_id"]
        self.assertIn("Новый заказ", self.fb.sent[-1][1])
        # admin
        st, orders = await self.call("/api/admin/orders", uid=OWNER)
        self.assertEqual(orders["orders"][0]["id"], oid)
        st, j = await self.call("/api/admin/order", {"order_id": oid, "status": "shipped", "paid": True}, uid=OWNER)
        self.assertEqual(st, 200)
        self.assertEqual((core.db.get_order(oid)["status"], core.db.get_order(oid)["paid"]), ("shipped", 1))
        self.assertEqual(self.fb.sent[-1][0], CLIENT)
        st, j = await self.call("/api/admin/order", {"order_id": oid, "status": "bogus"}, uid=OWNER)
        self.assertEqual(st, 400)

    async def test_owner_management_applies_to_api(self):
        self.assertEqual((await self.call("/api/admin/orders", uid=CLIENT))[0], 403)
        core.db.add_owner(CLIENT)
        self.assertEqual((await self.call("/api/admin/orders", uid=CLIENT))[0], 200)
        st, me = await self.call("/api/me", uid=CLIENT)
        self.assertTrue(me["is_owner"])
        core.db.remove_owner(CLIENT, core.ENV_OWNER_IDS)
        self.assertEqual((await self.call("/api/admin/orders", uid=CLIENT))[0], 403)
        with self.assertRaises(core.BookingError):
            core.db.remove_owner(OWNER, core.ENV_OWNER_IDS)   # последний владелец
        # владелец из OWNER_IDS: виден в списке как 🔒 и без кнопки удаления
        old = set(core.ENV_OWNER_IDS)
        core.ENV_OWNER_IDS.add(777)
        try:
            text, markup = core.owners_view()
            self.assertIn("777", text); self.assertIn("🔒", text)
            datas = [b.callback_data for row in markup.inline_keyboard for b in row]
            self.assertNotIn("aowdel:777", datas); self.assertIn(f"aowdel:{OWNER}", datas)
            self.assertEqual((await self.call("/api/admin/orders", uid=777))[0], 200)
        finally:
            core.ENV_OWNER_IDS.clear(); core.ENV_OWNER_IDS.update(old)

    async def test_public_about(self):
        r = await self.client.get("/api/public/about")
        self.assertEqual(r.status, 200)
        j = await r.json()
        self.assertIn("(пример)", j["title"])
        self.assertGreaterEqual(len(j["facts"]), 3)
        self.assertEqual(j["bot_url"], "https://t.me/Flowbyzhenya_bot")
        self.assertEqual(j["gallery"], [])
        self.assertEqual((await self.call("/api/me", init=""))[0], 401)
        self.assertEqual((await self.call("/api/days", init=""))[0], 401)
        self.assertEqual((await self.call("/api/admin/orders", init=""))[0], 401)
        r = await self.client.get("/about")
        self.assertEqual(r.status, 200)
        self.assertIn("О проекте", await r.text())
        r = await self.client.get("/")
        self.assertEqual(r.status, 200)
        self.assertEqual((await self.client.get("/media/999999")).status, 404)
        self.assertEqual((await self.client.get("/media/cover")).status, 404)
        core.db.set_about_field("title", "Новое имя")
        core.db.add_about_media("gallery", "link", url="https://youtu.be/abcdefghijk", caption="ролик")
        core.db.add_about_media("portfolio", "photo", title="Кейс (пример)", body="Результат")
        j = await (await self.client.get("/api/public/about")).json()
        self.assertEqual(j["title"], "Новое имя")
        self.assertEqual(j["gallery"][0]["embed"], "https://www.youtube.com/embed/abcdefghijk")
        self.assertEqual(j["portfolio"][0]["title"], "Кейс (пример)")

    async def test_admin_schedule(self):
        day = (core.now().date() + timedelta(days=3)).isoformat()
        st, j = await self.call("/api/admin/slots", {"date": day, "times": ["7:00", "21.30"], "capacity": 5}, uid=OWNER)
        self.assertEqual(j["added"], ["07:00", "21:30"])
        st, slots = await self.call(f"/api/admin/day?date={day}", uid=OWNER)
        s7 = [s for s in slots if s["time"] == "07:00"][0]
        self.assertEqual((s7["capacity"], s7["title"]), (5, "Групповая тренировка"))
        core.db.credit_subscription(CLIENT, 2, 30, "t", now=core.now())
        await self.call("/api/book", {"slot_id": s7["id"]})
        st, slots = await self.call(f"/api/admin/day?date={day}", uid=OWNER)
        s7 = [s for s in slots if s["time"] == "07:00"][0]
        self.assertEqual(s7["people"][0]["name"], "Тест")
        st, j = await self.call("/api/admin/slot/delete", {"slot_id": s7["id"]}, uid=OWNER)
        self.assertEqual(st, 400)  # есть записи, без force
        st, j = await self.call("/api/admin/booking/cancel", {"booking_id": s7["people"][0]["booking_id"]}, uid=OWNER)
        self.assertEqual(st, 200)
        self.assertEqual(core.db.subscription_summary(CLIENT, core.now().date())[0], 2)
        st, j = await self.call("/api/admin/clear_free", {"date": day}, uid=OWNER)
        self.assertGreaterEqual(j["deleted"], 2)
        st, days = await self.call("/api/admin/days", uid=OWNER)
        self.assertEqual(len(days), 30)


if __name__ == "__main__":
    unittest.main()
