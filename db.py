"""Слой данных для бота тренера. Вся бизнес-логика здесь, чтобы её можно было тестировать без Telegram.

Бэкенды: SQLite (локально / постоянный диск) или PostgreSQL (DATABASE_URL=postgres://..., например Neon).
SQL пишется в одном диалекте (плейсхолдеры `?`), для Postgres он переводится в `%s`."""
import json
import logging
import sqlite3
from datetime import datetime, timedelta, date

log = logging.getLogger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, phone TEXT,
    created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, start TEXT NOT NULL, duration INTEGER DEFAULT 60,
    capacity INTEGER DEFAULT 1, title TEXT DEFAULT 'Персональная тренировка');
CREATE INDEX IF NOT EXISTS ix_slots_start ON slots(start);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, sessions INTEGER, days INTEGER,
    price INTEGER, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, plan_id INTEGER, title TEXT,
    sessions_total INTEGER, sessions_left INTEGER, expires TEXT, source TEXT,
    created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, slot_id INTEGER, user_id INTEGER, subscription_id INTEGER,
    status TEXT DEFAULT 'active', created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS ix_book_slot ON bookings(slot_id, status);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, description TEXT, price INTEGER,
    photo TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS cart (user_id INTEGER, product_id INTEGER, qty INTEGER,
    PRIMARY KEY (user_id, product_id));
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, items TEXT, total INTEGER,
    delivery TEXT, address TEXT, phone TEXT, status TEXT DEFAULT 'new', paid INTEGER DEFAULT 0,
    created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, ref_id INTEGER, amount INTEGER,
    currency TEXT, tg_charge_id TEXT, provider_charge_id TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
"""

_TS = "to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
SCHEMA_PG = f"""
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users (
    id BIGINT PRIMARY KEY, username TEXT, full_name TEXT, phone TEXT, created TEXT DEFAULT {_TS});
CREATE TABLE IF NOT EXISTS slots (
    id BIGSERIAL PRIMARY KEY, start TEXT NOT NULL, duration INTEGER DEFAULT 60,
    capacity INTEGER DEFAULT 1, title TEXT DEFAULT 'Персональная тренировка');
CREATE INDEX IF NOT EXISTS ix_slots_start ON slots(start);
CREATE TABLE IF NOT EXISTS plans (
    id BIGSERIAL PRIMARY KEY, title TEXT, sessions INTEGER, days INTEGER, price BIGINT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS subscriptions (
    id BIGSERIAL PRIMARY KEY, user_id BIGINT, plan_id BIGINT, title TEXT, sessions_total INTEGER,
    sessions_left INTEGER, expires TEXT, source TEXT, created TEXT DEFAULT {_TS});
CREATE TABLE IF NOT EXISTS bookings (
    id BIGSERIAL PRIMARY KEY, slot_id BIGINT, user_id BIGINT, subscription_id BIGINT,
    status TEXT DEFAULT 'active', created TEXT DEFAULT {_TS});
CREATE INDEX IF NOT EXISTS ix_book_slot ON bookings(slot_id, status);
CREATE TABLE IF NOT EXISTS products (
    id BIGSERIAL PRIMARY KEY, title TEXT, description TEXT, price BIGINT, photo TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS cart (user_id BIGINT, product_id BIGINT, qty INTEGER, PRIMARY KEY (user_id, product_id));
CREATE TABLE IF NOT EXISTS orders (
    id BIGSERIAL PRIMARY KEY, user_id BIGINT, items TEXT, total BIGINT, delivery TEXT, address TEXT, phone TEXT,
    status TEXT DEFAULT 'new', paid INTEGER DEFAULT 0, created TEXT DEFAULT {_TS});
CREATE TABLE IF NOT EXISTS payments (
    id BIGSERIAL PRIMARY KEY, user_id BIGINT, kind TEXT, ref_id BIGINT, amount BIGINT, currency TEXT,
    tg_charge_id TEXT, provider_charge_id TEXT, created TEXT DEFAULT {_TS});
"""

TABLES = ["settings", "users", "slots", "plans", "subscriptions", "bookings", "products", "cart", "orders", "payments"]

ORDER_STATUSES = {
    "new": "Новый", "confirmed": "Подтверждён", "shipped": "Передан в доставку",
    "ready": "Готов к выдаче", "done": "Выполнен", "cancelled": "Отменён",
}

FMT = "%Y-%m-%d %H:%M"


class BookingError(Exception):
    pass


def is_pg_url(s):
    return bool(s) and s.startswith(("postgres://", "postgresql://"))


class DB:
    def __init__(self, path_or_url):
        self.pg = is_pg_url(path_or_url)
        self.target = path_or_url
        self._in_tx = False
        self._connect()
        if self.pg:
            for stmt in SCHEMA_PG.split(";"):
                if stmt.strip():
                    self.x(stmt)
        else:
            self.conn.executescript(SCHEMA)

    def _connect(self):
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            self.conn = psycopg.connect(self.target, autocommit=True, row_factory=dict_row,
                                        connect_timeout=15, keepalives=1, keepalives_idle=60,
                                        prepare_threshold=None)  # совместимо с пулером (PgBouncer) Neon
        else:
            self.conn = sqlite3.connect(self.target, isolation_level=None)  # autocommit; явные транзакции
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON")

    # ---------- helpers ----------
    def _exec(self, sql, a):
        if not self.pg:
            return self.conn.execute(sql, a)
        import psycopg
        stmt = sql.strip().upper()
        sql = sql.replace("BEGIN IMMEDIATE", "BEGIN").replace("?", "%s")
        for attempt in (1, 2):
            try:
                cur = self.conn.execute(sql, a)
                if stmt.startswith("BEGIN"):
                    self._in_tx = True
                elif stmt.startswith(("COMMIT", "ROLLBACK")):
                    self._in_tx = False
                return cur
            except (psycopg.OperationalError, psycopg.InterfaceError) as ex:
                broken = self.conn.closed or self.conn.broken
                if stmt.startswith("ROLLBACK") and broken:
                    self._in_tx = False
                    self._connect()
                    return None
                if attempt == 2 or self._in_tx or not broken:
                    raise
                log.warning("Postgres connection lost (%s), reconnecting", ex)
                self._connect()

    def q(self, sql, *a):
        return self._exec(sql, a).fetchall()

    def q1(self, sql, *a):
        return self._exec(sql, a).fetchone()

    def x(self, sql, *a):
        return self._exec(sql, a)

    def insert(self, sql, *a):
        """INSERT, возвращает id новой строки."""
        if self.pg:
            return self._exec(sql + " RETURNING id", a).fetchone()["id"]
        return self._exec(sql, a).lastrowid

    # ---------- settings / owner ----------
    def get_setting(self, key, default=None):
        r = self.q1("SELECT value FROM settings WHERE key=?", key)
        return r["value"] if r else default

    def set_setting(self, key, value):
        self.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               key, str(value))

    def db_owner_ids(self):
        v = self.get_setting("owner_ids", "")
        return {int(i) for i in v.split(",") if i.strip()}

    def _set_owner_ids(self, ids):
        self.set_setting("owner_ids", ",".join(str(i) for i in sorted(ids)))

    def add_owner(self, user_id):
        """Добавляет владельца (хранится в БД). Возвращает False, если уже владелец."""
        ids = self.db_owner_ids()
        if user_id in ids:
            return False
        self._set_owner_ids(ids | {user_id})
        self.set_setting("owner_claimed", "1")
        return True

    def remove_owner(self, user_id, permanent_ids=()):
        """Удаляет владельца из БД. Владельцев из OWNER_IDS (.env) удалить нельзя; последнего владельца — тоже."""
        permanent = set(permanent_ids)
        if user_id in permanent:
            raise BookingError("Этот владелец задан в настройках сервера (OWNER_IDS) — удалить его можно только там.")
        ids = self.db_owner_ids()
        if user_id not in ids:
            raise BookingError("Этот пользователь не владелец.")
        if len((ids | permanent) - {user_id}) < 1:
            raise BookingError("Нельзя удалить последнего владельца.")
        self._set_owner_ids(ids - {user_id})

    def claim_owner(self, user_id):
        """Первый вызвавший становится владельцем. Возвращает True при успехе."""
        self.x("BEGIN IMMEDIATE")
        try:
            if self.db_owner_ids() or self.get_setting("owner_claimed") == "1":
                self.x("ROLLBACK")
                return False
            self.set_setting("owner_ids", str(user_id))
            self.set_setting("owner_claimed", "1")
            self.x("COMMIT")
            return True
        except Exception:
            self.x("ROLLBACK")
            raise

    # ---------- users ----------
    def upsert_user(self, uid, username, full_name):
        self.x("""INSERT INTO users(id,username,full_name) VALUES(?,?,?)
                  ON CONFLICT(id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name""",
               uid, username, full_name)

    def get_user(self, uid):
        return self.q1("SELECT * FROM users WHERE id=?", uid)

    def find_user(self, text):
        text = text.strip()
        if text.lstrip("-").isdigit():
            return self.get_user(int(text))
        return self.q1("SELECT * FROM users WHERE lower(username)=lower(?)", text.lstrip("@"))

    def list_users(self, limit=50):
        return self.q("SELECT * FROM users ORDER BY created DESC LIMIT ?", limit)

    def set_phone(self, uid, phone):
        self.x("UPDATE users SET phone=? WHERE id=?", phone, uid)

    # ---------- slots ----------
    def add_slot(self, start: datetime, capacity=1, title="Персональная тренировка", duration=60):
        return self.insert("INSERT INTO slots(start,duration,capacity,title) VALUES(?,?,?,?)",
                           start.strftime(FMT), duration, capacity, title)

    def get_slot(self, sid):
        return self.q1("""SELECT s.*, (SELECT COUNT(*) FROM bookings b WHERE b.slot_id=s.id AND b.status='active') booked
                          FROM slots s WHERE s.id=?""", sid)

    def slots_on(self, day: date):
        return self.q("""SELECT s.*, (SELECT COUNT(*) FROM bookings b WHERE b.slot_id=s.id AND b.status='active') booked
                         FROM slots s WHERE substr(s.start,1,10)=? ORDER BY s.start""", day.isoformat())

    def days_with_slots(self, now: datetime, days=14):
        end = (now + timedelta(days=days)).strftime(FMT)
        return self.q("""SELECT substr(s.start,1,10) d, COUNT(*) total,
                           CAST(SUM(CASE WHEN s.capacity > (SELECT COUNT(*) FROM bookings b WHERE b.slot_id=s.id AND b.status='active') THEN 1 ELSE 0 END) AS INTEGER) free
                         FROM slots s WHERE s.start>? AND s.start<=? GROUP BY d ORDER BY d""",
                      now.strftime(FMT), end)

    def slot_bookings(self, sid):
        return self.q("""SELECT b.*, u.username, u.full_name, u.phone FROM bookings b LEFT JOIN users u ON u.id=b.user_id
                         WHERE b.slot_id=? AND b.status='active' ORDER BY b.id""", sid)

    def set_slot_capacity(self, sid, cap):
        self.x("UPDATE slots SET capacity=? WHERE id=?", cap, sid)

    def delete_slot(self, sid):
        """Удаляет слот; активные записи отменяются, занятия возвращаются. Возвращает список user_id для уведомления."""
        self.x("BEGIN IMMEDIATE")
        try:
            bs = self.q("SELECT * FROM bookings WHERE slot_id=? AND status='active'", sid)
            for b in bs:
                self._refund(b)
                self.x("UPDATE bookings SET status='cancelled_by_owner' WHERE id=?", b["id"])
            self.x("DELETE FROM slots WHERE id=?", sid)
            self.x("COMMIT")
            return [b["user_id"] for b in bs]
        except Exception:
            self.x("ROLLBACK")
            raise

    def generate_template(self, start_day: date, weeks, weekdays, hour_from, hour_to, capacity=1,
                          title="Персональная тренировка"):
        """weekdays: набор 0..6 (0=Пн). Часы [hour_from, hour_to) по часу. Пропускает существующие слоты на то же время."""
        created = 0
        for i in range(weeks * 7):
            d = start_day + timedelta(days=i)
            if d.weekday() not in weekdays:
                continue
            for h in range(hour_from, hour_to):
                st = datetime(d.year, d.month, d.day, h, 0)
                if self.q1("SELECT 1 FROM slots WHERE start=?", st.strftime(FMT)):
                    continue
                self.add_slot(st, capacity, title)
                created += 1
        return created

    # ---------- plans ----------
    def plans(self, only_active=True):
        return self.q("SELECT * FROM plans" + (" WHERE active=1" if only_active else "") + " ORDER BY sessions")

    def get_plan(self, pid):
        return self.q1("SELECT * FROM plans WHERE id=?", pid)

    def add_plan(self, title, sessions, days, price):
        return self.insert("INSERT INTO plans(title,sessions,days,price) VALUES(?,?,?,?)",
                      title, sessions, days, price)

    def update_field(self, table, rid, field, value):
        allowed = {"plans": {"title", "sessions", "days", "price", "active"},
                   "products": {"title", "description", "price", "photo", "active"}}
        if field not in allowed.get(table, ()):
            raise ValueError("bad field")
        self.x(f"UPDATE {table} SET {field}=? WHERE id=?", value, rid)

    def delete_plan(self, pid):
        self.x("DELETE FROM plans WHERE id=?", pid)

    # ---------- subscriptions ----------
    def credit_subscription(self, uid, sessions, days, title, plan_id=None, source="manual", now=None):
        now = now or datetime.now()
        expires = (now.date() + timedelta(days=days)).isoformat()
        return self.insert("""INSERT INTO subscriptions(user_id,plan_id,title,sessions_total,sessions_left,expires,source)
                         VALUES(?,?,?,?,?,?,?)""", uid, plan_id, title, sessions, sessions, expires, source)

    def active_subscriptions(self, uid, today: date):
        return self.q("""SELECT * FROM subscriptions WHERE user_id=? AND sessions_left>0 AND expires>=?
                         ORDER BY expires, id""", uid, today.isoformat())

    def subscription_summary(self, uid, today: date):
        subs = self.active_subscriptions(uid, today)
        return sum(s["sessions_left"] for s in subs), subs

    # ---------- bookings ----------
    def book(self, uid, sid, now: datetime):
        self.x("BEGIN IMMEDIATE")
        try:
            slot = self.get_slot(sid)
            if not slot:
                raise BookingError("Слот не найден — возможно, его удалили.")
            start = datetime.strptime(slot["start"], FMT)
            if start <= now:
                raise BookingError("Это время уже прошло.")
            if self.q1("SELECT 1 FROM bookings WHERE slot_id=? AND user_id=? AND status='active'", sid, uid):
                raise BookingError("Вы уже записаны на это время.")
            if slot["booked"] >= slot["capacity"]:
                raise BookingError("Увы, все места на это время заняты.")
            sub = self.q1("""SELECT * FROM subscriptions WHERE user_id=? AND sessions_left>0 AND expires>=?
                             ORDER BY expires, id LIMIT 1""", uid, start.date().isoformat())
            if not sub:
                raise BookingError("NO_SUB")
            self.x("UPDATE subscriptions SET sessions_left=sessions_left-1 WHERE id=?", sub["id"])
            bid = self.insert("INSERT INTO bookings(slot_id,user_id,subscription_id) VALUES(?,?,?)",
                         sid, uid, sub["id"])
            self.x("COMMIT")
            return bid
        except Exception:
            self.x("ROLLBACK")
            raise

    def _refund(self, b):
        if b["subscription_id"]:
            self.x("UPDATE subscriptions SET sessions_left=sessions_left+1 WHERE id=?", b["subscription_id"])

    def get_booking(self, bid):
        return self.q1("""SELECT b.*, s.start, s.title FROM bookings b JOIN slots s ON s.id=b.slot_id WHERE b.id=?""", bid)

    def cancel_booking(self, bid, uid, now: datetime, min_hours: float, by_owner=False):
        self.x("BEGIN IMMEDIATE")
        try:
            b = self.get_booking(bid)
            if not b or b["status"] != "active" or (not by_owner and b["user_id"] != uid):
                raise BookingError("Запись не найдена или уже отменена.")
            start = datetime.strptime(b["start"], FMT)
            if not by_owner and start - now < timedelta(hours=min_hours):
                raise BookingError(f"Отменить запись можно не позднее чем за {min_hours:g} ч до начала. "
                                   "Напишите тренеру, если что-то случилось.")
            self._refund(b)
            self.x("UPDATE bookings SET status=? WHERE id=?", "cancelled_by_owner" if by_owner else "cancelled", bid)
            self.x("COMMIT")
            return b
        except Exception:
            self.x("ROLLBACK")
            raise

    def user_upcoming(self, uid, now: datetime):
        return self.q("""SELECT b.id, s.start, s.title FROM bookings b JOIN slots s ON s.id=b.slot_id
                         WHERE b.user_id=? AND b.status='active' AND s.start>? ORDER BY s.start""",
                      uid, now.strftime(FMT))

    # ---------- products / cart ----------
    def products(self, only_active=True):
        return self.q("SELECT * FROM products" + (" WHERE active=1" if only_active else "") + " ORDER BY id")

    def get_product(self, pid):
        return self.q1("SELECT * FROM products WHERE id=?", pid)

    def add_product(self, title, description, price, photo=None):
        return self.insert("INSERT INTO products(title,description,price,photo) VALUES(?,?,?,?)",
                      title, description, price, photo)

    def delete_product(self, pid):
        self.x("DELETE FROM cart WHERE product_id=?", pid)
        self.x("DELETE FROM products WHERE id=?", pid)

    def cart_add(self, uid, pid, delta=1):
        self.x("""INSERT INTO cart(user_id,product_id,qty) VALUES(?,?,?)
                  ON CONFLICT(user_id,product_id) DO UPDATE SET qty=cart.qty+excluded.qty""", uid, pid, delta)
        self.x("DELETE FROM cart WHERE user_id=? AND qty<=0", uid)

    def cart(self, uid):
        rows = self.q("""SELECT c.product_id, c.qty, p.title, p.price FROM cart c JOIN products p ON p.id=c.product_id
                         WHERE c.user_id=? AND p.active=1 ORDER BY p.id""", uid)
        return rows, sum(r["qty"] * r["price"] for r in rows)

    def cart_clear(self, uid):
        self.x("DELETE FROM cart WHERE user_id=?", uid)

    def create_order(self, uid, delivery, address, phone):
        self.x("BEGIN IMMEDIATE")
        try:
            rows, total = self.cart(uid)
            if not rows:
                raise BookingError("Корзина пуста.")
            items = [{"id": r["product_id"], "title": r["title"], "qty": r["qty"], "price": r["price"]} for r in rows]
            oid = self.insert("INSERT INTO orders(user_id,items,total,delivery,address,phone) VALUES(?,?,?,?,?,?)",
                         uid, json.dumps(items, ensure_ascii=False), total, delivery, address, phone)
            self.x("DELETE FROM cart WHERE user_id=?", uid)
            self.x("COMMIT")
            return oid
        except Exception:
            self.x("ROLLBACK")
            raise

    def get_order(self, oid):
        return self.q1("SELECT o.*, u.username, u.full_name FROM orders o LEFT JOIN users u ON u.id=o.user_id WHERE o.id=?", oid)

    def orders(self, limit=20):
        return self.q("SELECT o.*, u.username, u.full_name FROM orders o LEFT JOIN users u ON u.id=o.user_id ORDER BY o.id DESC LIMIT ?", limit)

    def set_order_status(self, oid, status):
        if status not in ORDER_STATUSES:
            raise ValueError(status)
        self.x("UPDATE orders SET status=? WHERE id=?", status, oid)

    def set_order_paid(self, oid, paid=1):
        self.x("UPDATE orders SET paid=? WHERE id=?", paid, oid)

    # ---------- payments ----------
    def record_payment(self, uid, kind, ref_id, amount, currency, tg_id, prov_id):
        if self.q1("SELECT 1 FROM payments WHERE tg_charge_id=?", tg_id):
            return False  # идемпотентность
        self.x("""INSERT INTO payments(user_id,kind,ref_id,amount,currency,tg_charge_id,provider_charge_id)
                  VALUES(?,?,?,?,?,?,?)""", uid, kind, ref_id, amount, currency, tg_id, prov_id)
        return True

    # ---------- seed ----------
    def seed_if_empty(self, today: date):
        if self.get_setting("seeded") == "1":
            return False
        if not self.q1("SELECT 1 FROM plans"):
            self.add_plan("4 занятия (пример)", 4, 30, 1000000)
            self.add_plan("8 занятий (пример)", 8, 45, 1800000)
            self.add_plan("12 занятий (пример)", 12, 60, 2400000)
        if not self.q1("SELECT 1 FROM products"):
            self.add_product("Протеин сывороточный 900 г (пример)", "Шоколадный вкус. Пример товара — замените на свой.", 250000)
            self.add_product("Резинки для фитнеса, набор 5 шт. (пример)", "Разная жёсткость, чехол в комплекте. Пример товара.", 90000)
            self.add_product("Шейкер 700 мл (пример)", "С отделением для порошка. Пример товара.", 50000)
        if not self.q1("SELECT 1 FROM slots"):
            start = today + timedelta(days=1)
            self.generate_template(start, 1, {0, 1, 2, 3, 4}, 9, 20, 1)
            # пример групповой тренировки по субботам
            for i in range(7):
                d = start + timedelta(days=i)
                if d.weekday() == 5:
                    self.add_slot(datetime(d.year, d.month, d.day, 11, 0), 8, "Групповая тренировка (пример)")
        self.set_setting("seeded", "1")
        return True
