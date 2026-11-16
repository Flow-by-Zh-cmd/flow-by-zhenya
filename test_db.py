import os, tempfile, unittest
from datetime import datetime, timedelta, date
from db import DB, BookingError

NOW = datetime(2026, 11, 16, 10, 0)


PG_URL = os.getenv("TEST_DATABASE_URL")  # например postgresql://postgres@127.0.0.1:55432/trainer_test


class T(unittest.TestCase):
    def make_db(self):
        self.path = tempfile.mktemp(suffix=".db")
        return DB(self.path)

    def setUp(self):
        self.db = self.make_db()
        for uid in (1, 2, 3, 5_000_000_000):  # реальные Telegram ID бывают > 2^31
            self.db.upsert_user(uid, f"u{uid}", f"User {uid}")

    def tearDown(self):
        self.db.conn.close()
        if not self.db.pg:
            os.remove(self.path)

    def test_seed(self):
        self.assertTrue(self.db.seed_if_empty(NOW.date()))
        self.assertFalse(self.db.seed_if_empty(NOW.date()))
        self.assertEqual(len(self.db.plans()), 3)
        self.assertEqual(len(self.db.products()), 3)
        self.assertGreater(len(self.db.days_with_slots(NOW, 14)), 0)

    def test_claim_owner_once(self):
        self.assertTrue(self.db.claim_owner(1))
        self.assertFalse(self.db.claim_owner(2))
        self.assertEqual(self.db.db_owner_ids(), {1})

    def test_owners_add_remove(self):
        self.assertTrue(self.db.claim_owner(1))
        self.assertTrue(self.db.add_owner(2))
        self.assertFalse(self.db.add_owner(2))
        self.assertEqual(self.db.db_owner_ids(), {1, 2})
        self.db.remove_owner(2)
        self.assertEqual(self.db.db_owner_ids(), {1})
        with self.assertRaises(BookingError):      # последнего владельца удалить нельзя
            self.db.remove_owner(1)
        with self.assertRaises(BookingError):      # не владелец
            self.db.remove_owner(3)
        # владельцы из .env: их нельзя удалить, но при их наличии можно удалить последнего из БД
        with self.assertRaises(BookingError):
            self.db.remove_owner(99, permanent_ids={99})
        self.db.remove_owner(1, permanent_ids={99})
        self.assertEqual(self.db.db_owner_ids(), set())
        self.assertFalse(self.db.claim_owner(3))   # /claim_owner остаётся отключённым
        self.db.add_owner(5_000_000_000)
        self.assertIn(5_000_000_000, self.db.db_owner_ids())

    def test_booking_requires_subscription(self):
        sid = self.db.add_slot(NOW + timedelta(days=1))
        with self.assertRaises(BookingError) as c:
            self.db.book(1, sid, NOW)
        self.assertEqual(str(c.exception), "NO_SUB")

    def test_book_and_cancel(self):
        self.db.credit_subscription(1, 4, 30, "4", now=NOW)
        sid = self.db.add_slot(NOW + timedelta(days=1))
        bid = self.db.book(1, sid, NOW)
        self.assertEqual(self.db.subscription_summary(1, NOW.date())[0], 3)
        with self.assertRaises(BookingError):
            self.db.book(1, sid, NOW)  # duplicate
        self.db.cancel_booking(bid, 1, NOW, 12)
        self.assertEqual(self.db.subscription_summary(1, NOW.date())[0], 4)
        with self.assertRaises(BookingError):
            self.db.cancel_booking(bid, 1, NOW, 12)  # already cancelled

    def test_cancel_too_late_and_other_user(self):
        self.db.credit_subscription(1, 4, 30, "4", now=NOW)
        sid = self.db.add_slot(NOW + timedelta(hours=5))
        bid = self.db.book(1, sid, NOW)
        with self.assertRaises(BookingError):
            self.db.cancel_booking(bid, 2, NOW, 12)
        with self.assertRaises(BookingError):
            self.db.cancel_booking(bid, 1, NOW, 12)
        self.db.cancel_booking(bid, 999, NOW, 0, by_owner=True)
        self.assertEqual(self.db.subscription_summary(1, NOW.date())[0], 4)

    def test_capacity(self):
        for u in (1, 2, 3):
            self.db.credit_subscription(u, 4, 30, "4", now=NOW)
        sid = self.db.add_slot(NOW + timedelta(days=1), capacity=2, title="Группа")
        self.db.book(1, sid, NOW)
        self.db.book(2, sid, NOW)
        with self.assertRaises(BookingError):
            self.db.book(3, sid, NOW)
        self.assertEqual(self.db.subscription_summary(3, NOW.date())[0], 4)  # not deducted on failure

    def test_past_slot_and_expiry(self):
        self.db.credit_subscription(1, 4, 3, "short", now=NOW)
        with self.assertRaises(BookingError):
            self.db.book(1, self.db.add_slot(NOW - timedelta(hours=1)), NOW)
        with self.assertRaises(BookingError) as c:
            self.db.book(1, self.db.add_slot(NOW + timedelta(days=10)), NOW)  # after expiry
        self.assertEqual(str(c.exception), "NO_SUB")

    def test_uses_soonest_expiring_and_runs_out(self):
        a = self.db.credit_subscription(1, 1, 10, "a", now=NOW)
        self.db.credit_subscription(1, 1, 60, "b", now=NOW)
        s1 = self.db.add_slot(NOW + timedelta(days=1))
        s2 = self.db.add_slot(NOW + timedelta(days=2))
        s3 = self.db.add_slot(NOW + timedelta(days=3))
        b1 = self.db.book(1, s1, NOW)
        self.assertEqual(self.db.get_booking(b1)["subscription_id"], a)
        self.db.book(1, s2, NOW)
        with self.assertRaises(BookingError):
            self.db.book(1, s3, NOW)

    def test_delete_slot_refunds(self):
        self.db.credit_subscription(1, 2, 30, "x", now=NOW)
        sid = self.db.add_slot(NOW + timedelta(days=1))
        self.db.book(1, sid, NOW)
        self.assertEqual(self.db.delete_slot(sid), [1])
        self.assertEqual(self.db.subscription_summary(1, NOW.date())[0], 2)
        self.assertEqual(self.db.user_upcoming(1, NOW), [])

    def test_template_skips_duplicates(self):
        n = self.db.generate_template(date(2026, 11, 16), 1, {0, 1, 2, 3, 4}, 9, 20)
        self.assertEqual(n, 55)
        self.assertEqual(self.db.generate_template(date(2026, 11, 16), 1, {0, 1, 2, 3, 4}, 9, 20), 0)

    def test_shop_order(self):
        p1 = self.db.add_product("A", "d", 10000)
        p2 = self.db.add_product("B", "d", 2550)
        self.db.cart_add(1, p1); self.db.cart_add(1, p1); self.db.cart_add(1, p2)
        self.db.cart_add(1, p2, -1)
        rows, total = self.db.cart(1)
        self.assertEqual((len(rows), total), (1, 20000))
        oid = self.db.create_order(1, "delivery", "Екатеринбург, ул. Ленина 1", "+79001234567")
        self.assertEqual(self.db.cart(1)[1], 0)
        o = self.db.get_order(oid)
        self.assertEqual((o["total"], o["status"], o["paid"]), (20000, "new", 0))
        self.db.set_order_status(oid, "shipped"); self.db.set_order_paid(oid)
        self.assertEqual(self.db.get_order(oid)["status"], "shipped")
        with self.assertRaises(BookingError):
            self.db.create_order(1, "pickup", None, "1")

    def test_payment_idempotent(self):
        self.assertTrue(self.db.record_payment(1, "plan", 1, 100, "RUB", "tg1", "p1"))
        self.assertFalse(self.db.record_payment(1, "plan", 1, 100, "RUB", "tg1", "p1"))

    def test_product_sort(self):
        a = self.db.add_product("A", "d", 100)
        b = self.db.add_product("B", "d", 200)
        c = self.db.add_product("C", "d", 300)
        self.assertEqual([p["title"] for p in self.db.products()], ["A", "B", "C"])
        self.assertEqual(self.db.move_product(a, -1), "top")
        self.assertEqual([p["title"] for p in self.db.products()], ["A", "B", "C"])
        self.assertEqual(self.db.move_product(a, 1), "ok")
        self.assertEqual([p["title"] for p in self.db.products()], ["B", "A", "C"])
        self.assertEqual(self.db.move_product(c, 1), "bottom")
        self.assertEqual([p["title"] for p in self.db.products()], ["B", "A", "C"])
        self.assertEqual(self.db.move_product(c, -1), "ok")
        self.assertEqual([p["title"] for p in self.db.products()], ["B", "C", "A"])
        # скрытые товары тоже участвуют в порядке
        self.db.update_field("products", b, "active", 0)
        self.assertEqual([p["title"] for p in self.db.products()], ["C", "A"])
        self.assertEqual(self.db.move_product(c, -1), "ok")
        self.assertEqual([p["title"] for p in self.db.products(only_active=False)], ["C", "B", "A"])
        # новый товар встаёт в конец
        self.db.add_product("D", "d", 400)
        self.assertEqual([p["title"] for p in self.db.products(only_active=False)], ["C", "B", "A", "D"])
        self.assertIsNone(self.db.move_product(99999, 1))
        # корзина следует тому же порядку
        self.db.update_field("products", b, "active", 1)
        self.db.cart_add(1, a)
        self.db.cart_add(1, b)
        self.db.cart_add(1, c)
        titles = [r["title"] for r in self.db.cart(1)[0]]
        self.assertEqual(titles, ["C", "B", "A"])

    def test_product_sort_migration(self):
        import sqlite3
        path = tempfile.mktemp(suffix=".db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE products (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, description TEXT, price INTEGER,
                photo TEXT, active INTEGER DEFAULT 1);
            INSERT INTO products(title,description,price) VALUES('X','d',1);
            INSERT INTO products(title,description,price) VALUES('Y','d',2);
        """)
        conn.close()
        db = DB(path)
        try:
            rows = db.products(only_active=False)
            self.assertEqual([p["title"] for p in rows], ["X", "Y"])
            self.assertEqual(rows[0]["sort"], rows[0]["id"])
            self.assertEqual(rows[1]["sort"], rows[1]["id"])
            self.assertEqual(db.move_product(rows[0]["id"], 1), "ok")
            self.assertEqual([p["title"] for p in db.products()], ["Y", "X"])
            z = db.add_product("Z", "d", 3)
            self.assertEqual([p["title"] for p in db.products()], ["Y", "X", "Z"])
            self.assertEqual(db.get_product(z)["sort"], 3)
        finally:
            db.conn.close()
            os.remove(path)

    def test_update_field_whitelist(self):
        pid = self.db.add_plan("x", 1, 1, 100)
        self.db.update_field("plans", pid, "price", 500)
        self.assertEqual(self.db.get_plan(pid)["price"], 500)
        with self.assertRaises(ValueError):
            self.db.update_field("plans", pid, "id; DROP", 1)


@unittest.skipUnless(PG_URL, "TEST_DATABASE_URL не задан")
class TPostgres(T):
    def make_db(self):
        import psycopg
        with psycopg.connect(PG_URL, autocommit=True) as c:
            c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        return DB(PG_URL)

    def test_big_user_id_and_reconnect(self):
        uid = 5_000_000_000
        self.db.credit_subscription(uid, 2, 30, "x", now=NOW)
        # имитируем обрыв соединения (как при засыпании Neon): следующий запрос должен переподключиться
        self.db.conn.close()
        self.assertEqual(self.db.subscription_summary(uid, NOW.date())[0], 2)


if __name__ == "__main__":
    unittest.main()
