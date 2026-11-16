"""HTTP API + статика Telegram Mini App. Работает в том же процессе, что и бот, и использует ту же БД (db.py)."""
import json
import logging
import os
from datetime import datetime, date, timedelta
from io import BytesIO

from aiohttp import web

from db import BookingError, ORDER_STATUSES, FMT
from tgauth import validate_init_data, AuthError

log = logging.getLogger("webapp")
BASE = os.path.dirname(os.path.abspath(__file__))
SRC = " (из приложения)"


def make_app(core, bot):
    db = core.db
    photo_cache = {}

    def err(msg, status=400):
        return web.json_response({"error": msg}, status=status)

    @web.middleware
    async def auth_mw(request, handler):
        if not request.path.startswith("/api/") or request.path.startswith("/api/photo/"):
            return await handler(request)
        init = request.headers.get("X-Telegram-Init-Data", "")
        try:
            data = validate_init_data(init, core.BOT_TOKEN)
        except AuthError as ex:
            return err(f"unauthorized: {ex}", 401)
        u = data["user"]
        full = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
        db.upsert_user(u["id"], u.get("username"), full)
        request["uid"] = u["id"]
        if request.path.startswith("/api/admin/") and not core.is_owner(u["id"]):
            return err("forbidden", 403)
        try:
            return await handler(request)
        except BookingError as ex:
            return err(str(ex))
        except (KeyError, ValueError, TypeError, json.JSONDecodeError) as ex:
            log.warning("bad request %s: %r", request.path, ex)
            return err("Некорректный запрос")

    async def body(request):
        return await request.json() if request.can_read_body else {}

    def ok(**kw):
        return web.json_response({"ok": True, **kw})

    def sub_info(uid):
        left, subs = db.subscription_summary(uid, core.now().date())
        return {"left": left, "subs": [{"title": s["title"], "left": s["sessions_left"], "total": s["sessions_total"],
                                         "expires": s["expires"]} for s in subs]}

    # ---------- pages ----------
    async def index(request):
        resp = web.FileResponse(os.path.join(BASE, "webapp", "index.html"))
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------- client ----------
    async def me(request):
        uid = request["uid"]
        urow = db.get_user(uid)
        return web.json_response({"id": uid, "is_owner": core.is_owner(uid), "studio": core.STUDIO_NAME,
                                  "phone": urow["phone"] if urow else None,
                                  "address": core.STUDIO_ADDRESS, "cancel_hours": core.CANCEL_HOURS,
                                  "payments": bool(core.PAY_TOKEN), "sub": sub_info(uid),
                                  "cart_count": sum(r["qty"] for r in db.cart(uid)[0])})

    async def days(request):
        rows = db.days_with_slots(core.now(), core.BOOKING_DAYS)
        return web.json_response([{"date": r["d"], "total": r["total"], "free": r["free"]} for r in rows])

    async def slots(request):
        uid = request["uid"]
        d = date.fromisoformat(request.query["date"])
        n = core.now()
        mine = {r["start"]: r["id"] for r in db.user_upcoming(uid, n)}
        out = []
        for s in db.slots_on(d):
            if datetime.strptime(s["start"], FMT) <= n:
                continue
            st = "mine" if s["start"] in mine else ("free" if s["booked"] < s["capacity"] else "taken")
            out.append({"id": s["id"], "time": s["start"][11:], "title": s["title"], "duration": s["duration"],
                        "capacity": s["capacity"], "booked": s["booked"], "status": st,
                        "booking_id": mine.get(s["start"])})
        return web.json_response(out)

    async def book(request):
        uid = request["uid"]
        sid = int((await body(request))["slot_id"])
        try:
            db.book(uid, sid, core.now())
        except BookingError as ex:
            if str(ex) == "NO_SUB":
                return web.json_response({"error": "Для записи нужен действующий абонемент с оставшимися занятиями.",
                                          "code": "NO_SUB"}, status=400)
            raise
        await core.notify_new_booking(bot, uid, sid, SRC)
        return ok(sub=sub_info(uid))

    async def cancel(request):
        uid = request["uid"]
        b = db.cancel_booking(int((await body(request))["booking_id"]), uid, core.now(), core.CANCEL_HOURS)
        await core.notify_cancel(bot, uid, b, SRC)
        return ok(sub=sub_info(uid))

    async def bookings(request):
        rows = db.user_upcoming(request["uid"], core.now())
        n = core.now()
        return web.json_response([{"id": r["id"], "start": r["start"], "title": r["title"],
                                   "can_cancel": datetime.strptime(r["start"], FMT) - n >= timedelta(hours=core.CANCEL_HOURS)}
                                  for r in rows])

    async def plans(request):
        return web.json_response([{"id": p["id"], "title": p["title"], "sessions": p["sessions"], "days": p["days"],
                                   "price": p["price"]} for p in db.plans()])

    async def buy(request):
        uid = request["uid"]
        p = db.get_plan(int((await body(request))["plan_id"]))
        if not p or not p["active"]:
            return err("Тариф недоступен")
        if core.PAY_TOKEN:
            link = await bot.create_invoice_link(**core.plan_invoice_kwargs(p))
            return ok(invoice_url=link)
        await core.notify_plan_request(bot, uid, p, SRC)
        return ok(requested=True)

    async def products(request):
        return web.json_response([{"id": p["id"], "title": p["title"], "description": p["description"],
                                   "price": p["price"], "photo": f"/api/photo/{p['id']}?v={abs(hash(p['photo'])) % 10**8}"
                                   if p["photo"] else None} for p in db.products()])

    async def photo(request):
        p = db.get_product(int(request.match_info["pid"]))
        if not p or not p["photo"] or not p["active"]:
            raise web.HTTPNotFound()
        fid = p["photo"]
        if fid not in photo_cache:
            buf = BytesIO()
            f = await bot.get_file(fid)
            await bot.download_file(f.file_path, buf)
            photo_cache[fid] = buf.getvalue()
        return web.Response(body=photo_cache[fid], content_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=86400"})

    def cart_json(uid):
        rows, total = db.cart(uid)
        return {"items": [{"product_id": r["product_id"], "title": r["title"], "qty": r["qty"], "price": r["price"]}
                          for r in rows], "total": total}

    async def cart(request):
        return web.json_response(cart_json(request["uid"]))

    async def cart_change(request):
        uid = request["uid"]
        b = await body(request)
        pid, delta = int(b["product_id"]), int(b.get("delta", 1))
        p = db.get_product(pid)
        if delta > 0 and (not p or not p["active"]):
            return err("Товар недоступен")
        db.cart_add(uid, pid, max(-99, min(99, delta)))
        return web.json_response(cart_json(uid))

    async def order(request):
        uid = request["uid"]
        b = await body(request)
        delivery = b["delivery"]
        if delivery not in ("pickup", "delivery"):
            return err("Выберите способ получения")
        address = (b.get("address") or "").strip()[:500] or None
        phone = (b.get("phone") or "").strip()[:40]
        if delivery == "delivery" and (not address or len(address) < 5):
            return err("Укажите адрес доставки подробнее")
        if len("".join(c for c in phone if c.isdigit())) < 10:
            return err("Укажите телефон полностью, например +7 900 123-45-67")
        db.set_phone(uid, phone)
        oid = db.create_order(uid, delivery, address if delivery == "delivery" else None, phone)
        o = db.get_order(oid)
        await core.notify_new_order(bot, oid, SRC)
        res = {"order_id": oid, "total": o["total"]}
        if core.PAY_TOKEN:
            res["invoice_url"] = await bot.create_invoice_link(**core.order_invoice_kwargs(o))
        return ok(**res)

    async def my_orders(request):
        rows = db.q("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", request["uid"])
        return web.json_response([{"id": o["id"], "total": o["total"], "status": ORDER_STATUSES.get(o["status"]),
                                   "paid": bool(o["paid"])} for o in rows])

    # ---------- admin ----------
    async def a_days(request):
        n = core.now()
        stats = {r["d"]: r for r in db.days_with_slots(n.replace(hour=0, minute=0) - timedelta(minutes=1), core.BOOKING_DAYS + 1)}
        out = []
        for i in range(core.BOOKING_DAYS):
            d = (n.date() + timedelta(days=i)).isoformat()
            r = stats.get(d)
            booked = db.q1("""SELECT COUNT(*) c FROM bookings b JOIN slots s ON s.id=b.slot_id
                              WHERE b.status='active' AND substr(s.start,1,10)=?""", d)["c"]
            out.append({"date": d, "slots": r["total"] if r else 0, "booked": booked})
        return web.json_response(out)

    async def a_day(request):
        d = date.fromisoformat(request.query["date"])
        out = []
        for s in db.slots_on(d):
            bs = db.slot_bookings(s["id"])
            out.append({"id": s["id"], "time": s["start"][11:], "title": s["title"], "capacity": s["capacity"],
                        "booked": s["booked"], "past": datetime.strptime(s["start"], FMT) <= core.now(),
                        "people": [{"booking_id": b["id"], "name": b["full_name"] or str(b["user_id"]),
                                    "username": b["username"], "phone": b["phone"]} for b in bs]})
        return web.json_response(out)

    async def a_add_slots(request):
        b = await body(request)
        d = date.fromisoformat(b["date"])
        cap = int(b.get("capacity", 1))
        if cap < 1 or cap > 100:
            return err("Количество мест: от 1 до 100")
        title = (b.get("title") or "").strip()[:100] or ("Персональная тренировка" if cap == 1 else "Групповая тренировка")
        added = []
        for t in b["times"]:
            t = str(t).strip().replace(".", ":")
            if ":" not in t:
                t += ":00"
            tm = datetime.strptime(t, "%H:%M")
            db.add_slot(datetime(d.year, d.month, d.day, tm.hour, tm.minute), cap, title)
            added.append(tm.strftime("%H:%M"))
        if not added:
            return err("Укажите время")
        return ok(added=added)

    async def a_delete_slot(request):
        b = await body(request)
        s = db.get_slot(int(b["slot_id"]))
        if not s:
            return err("Слот не найден")
        if s["booked"] and not b.get("force"):
            return err("На слот есть записи — сначала отмените их")
        users = db.delete_slot(s["id"])
        for u in users:
            await core.notify_user(bot, u, f"⚠️ Тренировка {core.fmt_dt(s['start'])} отменена тренером. "
                                           "Занятие вернулось на абонемент.")
        return ok()

    async def a_clear_free(request):
        d = date.fromisoformat((await body(request))["date"])
        n = 0
        for s in db.slots_on(d):
            if s["booked"] == 0:
                db.delete_slot(s["id"])
                n += 1
        return ok(deleted=n)

    async def a_cancel_booking(request):
        b = db.cancel_booking(int((await body(request))["booking_id"]), request["uid"], core.now(), 0, by_owner=True)
        await core.notify_user(bot, b["user_id"], f"⚠️ Тренер отменил вашу запись на {core.fmt_dt(b['start'])}. "
                                                  "Занятие вернулось на абонемент.")
        return ok()

    async def a_orders(request):
        out = []
        for o in db.orders(30):
            out.append({"id": o["id"], "created": core.local_ts(o["created"]), "client": o["full_name"] or str(o["user_id"]),
                        "username": o["username"], "items": json.loads(o["items"]), "total": o["total"],
                        "delivery": o["delivery"], "address": o["address"], "phone": o["phone"],
                        "status": o["status"], "paid": bool(o["paid"])})
        return web.json_response({"orders": out, "statuses": ORDER_STATUSES})

    async def a_order_status(request):
        b = await body(request)
        oid = int(b["order_id"])
        o = db.get_order(oid)
        if not o:
            return err("Заказ не найден")
        if "status" in b and b["status"] != o["status"]:
            db.set_order_status(oid, b["status"])
            await core.notify_user(bot, o["user_id"], f"📦 Статус заказа №{oid}: <b>{ORDER_STATUSES[b['status']]}</b>")
        if "paid" in b:
            db.set_order_paid(oid, 1 if b["paid"] else 0)
        return ok()

    app = web.Application(middlewares=[auth_mw], client_max_size=64 * 1024)
    r = app.router
    r.add_get("/", index)
    r.add_get("/index.html", index)
    r.add_get("/healthz", lambda req: web.Response(text="ok"))
    r.add_get("/health", lambda req: web.Response(text="ok"))

    async def tg_js(request):
        # своя копия telegram-web-app.js: telegram.org может не открываться у пользователей в России
        resp = web.FileResponse(os.path.join(BASE, "webapp", "telegram-web-app.js"))
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp
    r.add_get("/telegram-web-app.js", tg_js)
    r.add_get("/api/me", me)
    r.add_get("/api/days", days)
    r.add_get("/api/slots", slots)
    r.add_post("/api/book", book)
    r.add_post("/api/cancel", cancel)
    r.add_get("/api/bookings", bookings)
    r.add_get("/api/plans", plans)
    r.add_post("/api/buy", buy)
    r.add_get("/api/products", products)
    r.add_get("/api/photo/{pid}", photo)
    r.add_get("/api/cart", cart)
    r.add_post("/api/cart", cart_change)
    r.add_post("/api/order", order)
    r.add_get("/api/orders", my_orders)
    r.add_get("/api/admin/days", a_days)
    r.add_get("/api/admin/day", a_day)
    r.add_post("/api/admin/slots", a_add_slots)
    r.add_post("/api/admin/slot/delete", a_delete_slot)
    r.add_post("/api/admin/clear_free", a_clear_free)
    r.add_post("/api/admin/booking/cancel", a_cancel_booking)
    r.add_get("/api/admin/orders", a_orders)
    r.add_post("/api/admin/order", a_order_status)
    return app


async def start(core, bot):
    app = make_app(core, bot)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    host = os.getenv("WEBAPP_HOST", "127.0.0.1")
    port = int(os.getenv("WEBAPP_PORT", "8080"))
    await web.TCPSite(runner, host, port).start()
    log.info("Mini App server on http://%s:%s", host, port)
    return runner
