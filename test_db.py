import os, tempfile, unittest
from pathlib import Path
from datetime import datetime, timedelta, date
from db import DB, BookingError, parse_facts, facts_text, video_embed

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

    def test_about_seed_and_fields(self):
        self.assertTrue(self.db.seed_about_if_empty())
        self.assertFalse(self.db.seed_about_if_empty())
        a = self.db.get_about()
        self.assertIn("(пример)", a["title"])
        self.assertTrue(a["tagline"])
        self.assertTrue(a["description"])
        self.assertGreaterEqual(len(a["facts"]), 3)
        self.assertIsNone(a["cover"])
        self.db.set_about_field("title", "Студия")
        self.db.set_about_field("tagline", "Слоган")
        self.db.set_about_field("description", "Текст")
        self.db.set_about_field("facts", "Тренер: Анна\nАдрес: центр")
        self.db.set_about_field("cover", "AgAC-file")
        a = self.db.get_about()
        self.assertEqual((a["title"], a["tagline"], a["description"], a["cover"]),
                         ("Студия", "Слоган", "Текст", "AgAC-file"))
        self.assertEqual(a["facts"], [{"label": "Тренер", "value": "Анна"},
                                      {"label": "Адрес", "value": "центр"}])
        self.db.set_about_field("cover", None)
        self.assertIsNone(self.db.get_about()["cover"])
        with self.assertRaises(ValueError):
            self.db.set_about_field("nope", "x")

    def test_about_media_order(self):
        a = self.db.add_about_media("gallery", "photo", file_id="f1", caption="один")
        b = self.db.add_about_media("gallery", "link", url="https://youtu.be/dQw4w9wgGcQ", caption="ролик")
        c = self.db.add_about_media("gallery", "video", file_id="f3")
        p1 = self.db.add_about_media("portfolio", "photo", file_id="p1", title="Кейс", body="Результат")
        titles = [m["caption"] or m["kind"] for m in self.db.about_media("gallery")]
        self.assertEqual(titles, ["один", "ролик", "video"])
        self.assertEqual(self.db.move_about_media(a, -1), "top")
        self.assertEqual(self.db.move_about_media(a, 1), "ok")
        self.assertEqual([m["id"] for m in self.db.about_media("gallery")], [b, a, c])
        self.assertEqual(self.db.move_about_media(c, 1), "bottom")
        # сдвиг в галерее не меняет портфолио
        self.assertEqual([m["id"] for m in self.db.about_media("portfolio")], [p1])
        self.db.add_about_media("portfolio", "photo", file_id="p2", title="Ещё")
        self.assertEqual([m["title"] for m in self.db.about_media("portfolio")], ["Кейс", "Ещё"])
        self.db.update_about_media(p1, "title", "Кейс 2")
        self.assertEqual(self.db.get_about_media(p1)["title"], "Кейс 2")
        self.db.delete_about_media(a)
        self.assertEqual([m["id"] for m in self.db.about_media("gallery")], [b, c])
        self.assertIsNone(self.db.move_about_media(999, 1))
        with self.assertRaises(ValueError):
            self.db.add_about_media("nope", "photo")

    def test_facts_and_embed_helpers(self):
        self.assertEqual(parse_facts("A: 1\n\nпросто"),
                         [{"label": "A", "value": "1"}, {"label": "", "value": "просто"}])
        self.assertEqual(facts_text([{"label": "A", "value": "1"}]), "A: 1")
        self.assertEqual(video_embed("https://youtu.be/abcdefghijk"),
                         "https://www.youtube.com/embed/abcdefghijk")
        self.assertEqual(video_embed("https://vk.com/video-1_2"),
                         "https://vk.com/video_ext.php?oid=-1&id=2")
        self.assertIsNone(video_embed("https://example.com/watch"))

    def test_seed_about_when_already_seeded(self):
        self.assertTrue(self.db.seed_if_empty(NOW.date()))
        self.db.x("DELETE FROM settings WHERE key LIKE 'about_%'")
        self.assertTrue(self.db.seed_if_empty(NOW.date()))  # только about
        self.assertIn("(пример)", self.db.get_about()["title"])
        self.assertFalse(self.db.seed_if_empty(NOW.date()))

    def test_update_field_whitelist(self):
        pid = self.db.add_plan("x", 1, 1, 100)
        self.db.update_field("plans", pid, "price", 500)
        self.assertEqual(self.db.get_plan(pid)["price"], 500)
        with self.assertRaises(ValueError):
            self.db.update_field("plans", pid, "id; DROP", 1)


    def test_product_photos_crud_and_cover(self):
        p = self.db.add_product("A", "d", 100, "TgFileAAA")
        self.assertEqual([x["file_id"] for x in self.db.product_photos(p)], ["TgFileAAA"])
        self.assertEqual(self.db.get_product(p)["photo"], "TgFileAAA")
        self.db.add_product_photo(p, "TgFileBBB")
        self.assertEqual([x["file_id"] for x in self.db.product_photos(p)], ["TgFileAAA", "TgFileBBB"])
        self.assertEqual(self.db.get_product(p)["photo"], "TgFileAAA")  # обложка не заменяется
        # повтор той же ссылки не создаёт дубль
        first = self.db.product_photos(p)[0]["id"]
        self.assertEqual(self.db.add_product_photo(p, "TgFileAAA"), first)
        self.assertEqual(len(self.db.product_photos(p)), 2)
        b = self.db.product_photos(p)[1]["id"]
        self.assertEqual(self.db.move_product_photo(b, -1), "ok")
        self.assertEqual([x["file_id"] for x in self.db.product_photos(p)], ["TgFileBBB", "TgFileAAA"])
        self.assertEqual(self.db.get_product(p)["photo"], "TgFileBBB")
        self.assertEqual(self.db.move_product_photo(b, -1), "top")
        self.assertEqual(self.db.move_product_photo(self.db.product_photos(p)[-1]["id"], 1), "bottom")
        self.db.add_product_photo(p, "asset:ok-name.jpg")
        self.assertEqual(len(self.db.product_photos(p)), 3)
        with self.assertRaises(ValueError):
            self.db.add_product_photo(p, "asset:../etc/passwd")
        with self.assertRaises(ValueError):
            self.db.add_product_photo(p, "https://evil.example/x.jpg")
        with self.assertRaises(ValueError):
            self.db.add_product_photo(p, "asset:foo/bar.jpg")
        with self.assertRaises(ValueError):
            self.db.add_product_photo(p, "asset:ok-name.jpg.exe")
        last = self.db.product_photos(p)[-1]["id"]
        self.db.delete_product_photo(last)
        self.assertEqual(len(self.db.product_photos(p)), 2)
        self.assertEqual(self.db.get_product(p)["photo"], "TgFileBBB")
        self.db.delete_product(p)
        self.assertEqual(self.db.product_photos(p), [])

    def test_product_photos_migration_preserves_single(self):
        import sqlite3
        path = tempfile.mktemp(suffix=".db")
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE products (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, description TEXT, price INTEGER,
                photo TEXT, active INTEGER DEFAULT 1, sort INTEGER);
            INSERT INTO products(title,description,price,photo,sort) VALUES('X','d',1,'CoverFile',1);
            INSERT INTO products(title,description,price,photo,sort) VALUES('Y','d',2,NULL,2);
        """)
        conn.close()
        db = DB(path)
        try:
            x = [p for p in db.products(only_active=False) if p["title"] == "X"][0]
            y = [p for p in db.products(only_active=False) if p["title"] == "Y"][0]
            self.assertEqual([ph["file_id"] for ph in db.product_photos(x["id"])], ["CoverFile"])
            self.assertEqual(x["photo"], "CoverFile")
            self.assertEqual(db.product_photos(y["id"]), [])
            # повторная миграция при новом подключении не дублирует
            db.conn.close()
            db = DB(path)
            self.assertEqual(len(db.product_photos(x["id"])), 1)
            self.assertEqual(db.get_product(x["id"])["photo"], "CoverFile")
        finally:
            db.conn.close()
            os.remove(path)

    def test_resolve_product_asset_safe_path(self):
        from db import resolve_product_asset, is_safe_asset_name, PRODUCT_ASSETS_DIR
        base = Path(tempfile.mkdtemp())
        (base / "ok-name.jpg").write_bytes(b"fakeimg")
        try:
            self.assertTrue(is_safe_asset_name("ok-name.jpg"))
            path = resolve_product_asset("asset:ok-name.jpg", base_dir=base)
            self.assertEqual(path.read_bytes(), b"fakeimg")
            self.assertIsNone(resolve_product_asset("TgFileAAA", base_dir=base))
            with self.assertRaises(ValueError):
                resolve_product_asset("asset:../ok-name.jpg", base_dir=base)
            with self.assertRaises(FileNotFoundError):
                resolve_product_asset("asset:missing.jpg", base_dir=base)
        finally:
            import shutil
            shutil.rmtree(base)

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
