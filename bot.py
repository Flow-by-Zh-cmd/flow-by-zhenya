"""Flow by Zhenya — Telegram-бот для записи к тренеру, абонементов и магазина. aiogram 3, long polling."""
import asyncio
import html
import json
import logging
import os
import sys
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, Message, PreCheckoutQuery, LabeledPrice, InlineKeyboardMarkup,
                           InlineKeyboardButton as IB, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
                           BotCommand, WebAppInfo, MenuButtonWebApp,
                           KeyboardButtonRequestUsers)
from dotenv import load_dotenv

from db import DB, BookingError, ORDER_STATUSES, FMT

BASE = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(BASE, ".env")):
    load_dotenv(os.path.join(BASE, ".env"))      # локальный запуск: секреты и настройки (в git не попадает)
else:
    load_dotenv(os.path.join(BASE, "config.env"))  # хостинг: несекретные настройки из репозитория; env-переменные важнее

BOT_TOKEN = next((os.environ[k].strip() for k in ("BOT_TOKEN", "TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN", "API_TOKEN")
                  if os.getenv(k, "").strip()), None)
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN не задан")
ENV_OWNER_IDS = {int(i) for i in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if i}
PAY_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "").strip()
CURRENCY = os.getenv("CURRENCY", "RUB")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Yekaterinburg"))
CANCEL_HOURS = float(os.getenv("CANCEL_HOURS", "12"))
STUDIO_NAME = os.getenv("STUDIO_NAME", "Flow by Zhenya")
STUDIO_ADDRESS = os.getenv("STUDIO_ADDRESS", "адрес студии уточните у тренера")
DB_PATH = os.getenv("DB_PATH", os.path.join(BASE, "bot.db"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()          # postgres://... (Neon и т.п.); пусто = SQLite
if not DATABASE_URL and os.getenv("DATABASE_URL_ENC", "").strip():   # зашифрованный токеном бота (см. secretbox.py)
    from secretbox import decrypt
    DATABASE_URL = decrypt(os.environ["DATABASE_URL_ENC"].strip(), BOT_TOKEN)
# задан = режим вебхука (хостинг). На Render берётся автоматически из RENDER_EXTERNAL_URL
BOOKING_DAYS = int(os.getenv("BOOKING_DAYS", "30"))
WEBHOOK_BASE_URL = (os.getenv("WEBHOOK_BASE_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip()).rstrip("/")
PORT = int(os.getenv("PORT", "8080"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("trainer-bot")

db = DB(DATABASE_URL or DB_PATH)
router = Router()
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# ======================= helpers =======================

def now():
    return datetime.now(TZ).replace(tzinfo=None)


def is_owner(uid):
    return uid in ENV_OWNER_IDS or uid in db.db_owner_ids()


def owner_ids():
    return ENV_OWNER_IDS | db.db_owner_ids()


class IsOwner(Filter):
    async def __call__(self, event):
        return is_owner(event.from_user.id)


def e(s):
    return html.escape(str(s or ""))


def money(kop):
    rub = kop / 100
    s = f"{rub:,.0f}" if kop % 100 == 0 else f"{rub:,.2f}"
    return s.replace(",", " ") + " ₽"


def fmt_day(d):
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return f"{WD[d.weekday()]} {d.strftime('%d.%m')}"


def fmt_dt(s):
    dt = datetime.strptime(s, FMT)
    return f"{fmt_day(dt.date())} в {dt.strftime('%H:%M')}"


def uname(row_or_user):
    if row_or_user is None:
        return "клиент"
    r = row_or_user
    name = e(r["full_name"] or "")
    if r["username"]:
        name += f" (@{e(r['username'])})"
    return name or f"id {r['id']}"


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[IB(text=t, callback_data=d) for t, d in row] for row in rows])


BTN_SCHED, BTN_MY, BTN_SUB, BTN_BUY, BTN_SHOP, BTN_CART, BTN_ADMIN = (
    "📅 Расписание", "🗓 Мои записи", "🎫 Мой абонемент", "💳 Купить абонемент", "🛍 Магазин", "🛒 Корзина", "⚙️ Админ")


def main_menu(uid):
    rows = [[KeyboardButton(text=BTN_SCHED), KeyboardButton(text=BTN_MY)],
            [KeyboardButton(text=BTN_SUB), KeyboardButton(text=BTN_BUY)],
            [KeyboardButton(text=BTN_SHOP), KeyboardButton(text=BTN_CART)]]
    if is_owner(uid):
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


async def show(cb: CallbackQuery, text, markup=None):
    """Редактирует сообщение колбэка, а если нельзя (фото и т.п.) — отправляет новое."""
    try:
        if cb.message.text is not None:
            await cb.message.edit_text(text, reply_markup=markup)
            return
    except Exception as ex:
        if "not modified" in str(ex):
            return
    await cb.message.answer(text, reply_markup=markup)


async def notify_owners(bot: Bot, text, markup=None):
    for oid in owner_ids():
        try:
            await bot.send_message(oid, text, reply_markup=markup)
        except Exception as ex:
            log.warning("notify owner %s failed: %s", oid, ex)


async def notify_user(bot: Bot, uid, text, markup=None):
    try:
        await bot.send_message(uid, text, reply_markup=markup)
    except Exception as ex:
        log.warning("notify user %s failed: %s", uid, ex)


# ---- общие уведомления владельцу (используются ботом и Mini App) ----
async def notify_new_booking(bot: Bot, uid, sid, source=""):
    s = db.get_slot(sid)
    await notify_owners(bot, f"🆕 <b>Новая запись</b>{source}\n{fmt_dt(s['start'])} — {uname(db.get_user(uid))}\n"
                             f"Записано: {s['booked']}/{s['capacity']}", kb([[("Открыть слот", f"as:{sid}")]]))


async def notify_cancel(bot: Bot, uid, b, source=""):
    await notify_owners(bot, f"❌ <b>Отмена записи</b>{source}\n{fmt_dt(b['start'])} — {uname(db.get_user(uid))}")


async def notify_plan_request(bot: Bot, uid, p, source=""):
    await notify_owners(bot, f"💳 <b>Заявка на абонемент</b>{source}\n{uname(db.get_user(uid))}, id <code>{uid}</code>\n"
                             f"Тариф: {e(p['title'])} — {money(p['price'])}\n\n"
                             "После получения оплаты нажмите кнопку, чтобы начислить абонемент:",
                        kb([[(f"✅ Начислить «{p['title']}»", f"acr:{uid}:{p['id']}")]]))


async def notify_new_order(bot: Bot, oid, source=""):
    await notify_owners(bot, f"🛍 <b>Новый заказ</b>{source}\n" + order_text(db.get_order(oid)),
                        kb([[("Открыть заказ", f"ao:{oid}")]]))


def plan_invoice_kwargs(p):
    return dict(title=p["title"][:32], description=f"{p['sessions']} занятий, срок действия {p['days']} дн. — {STUDIO_NAME}"[:255],
                payload=f"plan:{p['id']}", provider_token=PAY_TOKEN, currency=CURRENCY,
                prices=[LabeledPrice(label=p["title"][:32], amount=p["price"])])


def order_invoice_kwargs(o):
    return dict(title=f"Заказ №{o['id']}", description=f"Товары {STUDIO_NAME}"[:255], payload=f"order:{o['id']}",
                provider_token=PAY_TOKEN, currency=CURRENCY,
                prices=[LabeledPrice(label=f"Заказ №{o['id']}", amount=o["total"])])


def webapp_url():
    """URL Mini App: WEBAPP_URL, иначе WEBHOOK_BASE_URL (хостинг), иначе webapp_url.txt (локальный туннель)."""
    env = os.getenv("WEBAPP_URL", "").strip() or WEBHOOK_BASE_URL
    if env.startswith("https://"):
        return env.rstrip("/") + "/"
    try:
        with open(os.path.join(BASE, "webapp_url.txt")) as f:
            u = f.read().strip()
            return u if u.startswith("https://") else None
    except OSError:
        return None


class UserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = getattr(event, "from_user", None)
        if u:
            db.upsert_user(u.id, u.username, u.full_name)
        return await handler(event, data)


# ======================= states =======================

class Checkout(StatesGroup):
    delivery = State()
    address = State()
    phone = State()
    confirm = State()


class AddSlot(StatesGroup):
    date = State()
    time = State()
    capacity = State()
    title = State()


class Tpl(StatesGroup):
    params = State()


class SlotCap(StatesGroup):
    value = State()


class NewPlan(StatesGroup):
    title = State()
    sessions = State()
    days = State()
    price = State()


class NewProduct(StatesGroup):
    title = State()
    description = State()
    price = State()
    photo = State()


class EditField(StatesGroup):
    value = State()


class FindUser(StatesGroup):
    query = State()


class CreditN(StatesGroup):
    value = State()


class AddOwner(StatesGroup):
    query = State()


# ======================= common =======================

@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    text = (f"Привет, {e(m.from_user.first_name)}! 👋\n\nЭто бот <b>{e(STUDIO_NAME)}</b>. Здесь можно:\n"
            "• посмотреть расписание и свободное время;\n• записаться на тренировку или отменить запись;\n"
            "• проверить остаток занятий по абонементу и купить новый;\n• заказать товары в магазине.\n\n"
            "Выберите раздел в меню ниже 👇")
    if is_owner(m.from_user.id):
        text += "\n\n⚙️ Вы владелец: панель управления — кнопка «Админ» или /admin."
    await m.answer(text, reply_markup=main_menu(m.from_user.id))
    url = webapp_url()
    if url:
        await m.answer("📱 Всё то же самое — в удобном приложении:", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[IB(text="📱 Открыть приложение", web_app=WebAppInfo(url=url))]]))


@router.message(Command("cancel"))
async def cmd_cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Действие отменено.", reply_markup=main_menu(m.from_user.id))


@router.message(Command("help"))
async def cmd_help(m: Message):
    t = ("/start — главное меню\n/cancel — отменить текущее действие\n")
    if is_owner(m.from_user.id):
        t += "/admin — панель владельца\n/myid — ваш Telegram ID\n"
    await m.answer(t)


@router.message(Command("myid"))
async def cmd_myid(m: Message):
    await m.answer(f"Ваш Telegram ID: <code>{m.from_user.id}</code>")


@router.message(Command("claim_owner"))
async def cmd_claim(m: Message):
    if is_owner(m.from_user.id):
        await m.answer("Вы уже владелец.", reply_markup=main_menu(m.from_user.id))
        return
    if ENV_OWNER_IDS or not db.claim_owner(m.from_user.id):
        await m.answer("Команда недоступна.")
        return
    log.info("Owner claimed by user id %s", m.from_user.id)
    await m.answer("✅ Готово! Теперь вы владелец бота. Вам будут приходить уведомления о записях, оплатах и заказах.\n"
                   "Панель управления — кнопка «⚙️ Админ» или /admin.", reply_markup=main_menu(m.from_user.id))


@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


# ======================= client: schedule & booking =======================

def days_kb(prefix, back=None, all_days=False):
    n = now()
    rows = []
    if all_days:
        stats = {r["d"]: r for r in db.days_with_slots(n.replace(hour=0, minute=0) - timedelta(minutes=1), BOOKING_DAYS + 1)}
        for i in range(BOOKING_DAYS):
            d = (n.date() + timedelta(days=i)).isoformat()
            r = stats.get(d)
            label = fmt_day(d) + (f" · слотов {r['total']}" if r else " · нет слотов")
            rows.append([(label, f"{prefix}:{d}")])
    else:
        for r in db.days_with_slots(n, BOOKING_DAYS):
            label = f"{fmt_day(r['d'])} · свободно {r['free']}" if r["free"] else f"{fmt_day(r['d'])} · всё занято"
            rows.append([(label, f"{prefix}:{r['d']}")])
    if back:
        rows.append([("⬅️ Назад", back)])
    return kb(rows), bool(rows)


@router.message(F.text == BTN_SCHED)
@router.message(Command("schedule"))
async def m_schedule(m: Message, state: FSMContext):
    await state.clear()
    markup, ok = days_kb("sd")
    if not ok:
        await m.answer("Пока нет доступного времени на ближайший месяц. Загляните позже 🙏")
        return
    await m.answer("📅 <b>Расписание на месяц</b>\nВыберите день:", reply_markup=markup)


@router.callback_query(F.data == "sch")
async def cb_schedule(cb: CallbackQuery):
    markup, ok = days_kb("sd")
    await show(cb, "📅 <b>Расписание на месяц</b>\nВыберите день:" if ok else "Свободного времени пока нет.", markup)
    await cb.answer()


@router.callback_query(F.data.startswith("sd:"))
async def cb_day(cb: CallbackQuery):
    d = date.fromisoformat(cb.data[3:])
    n = now()
    uid = cb.from_user.id
    mine = {r["start"] for r in db.user_upcoming(uid, n)}
    rows = []
    for s in db.slots_on(d):
        if datetime.strptime(s["start"], FMT) <= n:
            continue
        t = s["start"][11:]
        free = s["capacity"] - s["booked"]
        grp = f" · {s['title']}" if s["capacity"] > 1 else ""
        if s["start"] in mine:
            rows.append([(f"⭐ {t} — вы записаны{grp}", "noop")])
        elif free > 0:
            extra = f" (мест: {free})" if s["capacity"] > 1 else ""
            rows.append([(f"🟢 {t} свободно{extra}{grp}", f"sb:{s['id']}")])
        else:
            rows.append([(f"⚪️ {t} занято{grp}", "noop")])
    rows.append([("⬅️ К дням", "sch")])
    text = f"<b>{fmt_day(d)}</b>\n🟢 — свободно, ⚪️ — занято. Нажмите на свободное время, чтобы записаться."
    if len(rows) == 1:
        text = f"<b>{fmt_day(d)}</b>\nНа этот день свободного времени нет."
    await show(cb, text, kb(rows))
    await cb.answer()


@router.callback_query(F.data.startswith("sb:"))
async def cb_book_confirm(cb: CallbackQuery):
    sid = int(cb.data[3:])
    s = db.get_slot(sid)
    if not s:
        await cb.answer("Слот не найден", show_alert=True)
        return
    left, _ = db.subscription_summary(cb.from_user.id, datetime.strptime(s["start"], FMT).date())
    text = (f"Записаться: <b>{fmt_dt(s['start'])}</b>\n{e(s['title'])}, {s['duration']} мин.\n\n"
            f"Будет списано 1 занятие с абонемента (сейчас доступно: {left}).\n"
            f"Отменить запись можно не позднее чем за {CANCEL_HOURS:g} ч — занятие вернётся.")
    await show(cb, text, kb([[("✅ Записаться", f"sbok:{sid}")], [("⬅️ Назад", f"sd:{s['start'][:10]}")]]))
    await cb.answer()


@router.callback_query(F.data.startswith("sbok:"))
async def cb_book(cb: CallbackQuery, bot: Bot):
    sid = int(cb.data[5:])
    uid = cb.from_user.id
    try:
        bid = db.book(uid, sid, now())
    except BookingError as ex:
        if str(ex) == "NO_SUB":
            await show(cb, "😔 Для записи нужен действующий абонемент с оставшимися занятиями "
                           "(и сроком действия до даты тренировки).\n\nКупите абонемент — и записывайтесь!",
                       kb([[("💳 Купить абонемент", "plans")], [("⬅️ К расписанию", "sch")]]))
        else:
            await cb.answer(str(ex), show_alert=True)
        return
    s = db.get_slot(sid)
    left, _ = db.subscription_summary(uid, now().date())
    await show(cb, f"✅ Вы записаны на <b>{fmt_dt(s['start'])}</b>!\nОсталось занятий: {left}.\n\n"
                   "Посмотреть или отменить — «🗓 Мои записи».", kb([[("🗓 Мои записи", "my")]]))
    await cb.answer("Записано!")
    await notify_new_booking(bot, uid, sid)


def my_bookings_view(uid):
    rows = db.user_upcoming(uid, now())
    if not rows:
        return "У вас нет предстоящих записей.", kb([[("📅 Расписание", "sch")]])
    lines = ["🗓 <b>Ваши записи</b>"]
    btns = []
    for r in rows:
        lines.append(f"• {fmt_dt(r['start'])} — {e(r['title'])}")
        btns.append([(f"❌ Отменить {fmt_dt(r['start'])}", f"cx:{r['id']}")])
    lines.append(f"\nОтмена возможна не позднее чем за {CANCEL_HOURS:g} ч до начала.")
    return "\n".join(lines), kb(btns)


@router.message(F.text == BTN_MY)
async def m_my(m: Message, state: FSMContext):
    await state.clear()
    t, k = my_bookings_view(m.from_user.id)
    await m.answer(t, reply_markup=k)


@router.callback_query(F.data == "my")
async def cb_my(cb: CallbackQuery):
    t, k = my_bookings_view(cb.from_user.id)
    await show(cb, t, k)
    await cb.answer()


@router.callback_query(F.data.startswith("cx:"))
async def cb_cancel_confirm(cb: CallbackQuery):
    bid = int(cb.data[3:])
    b = db.get_booking(bid)
    if not b or b["user_id"] != cb.from_user.id or b["status"] != "active":
        await cb.answer("Запись не найдена", show_alert=True)
        return
    await show(cb, f"Отменить запись на <b>{fmt_dt(b['start'])}</b>?",
               kb([[("Да, отменить", f"cxok:{bid}"), ("Нет", "my")]]))
    await cb.answer()


@router.callback_query(F.data.startswith("cxok:"))
async def cb_cancel(cb: CallbackQuery, bot: Bot):
    bid = int(cb.data[5:])
    try:
        b = db.cancel_booking(bid, cb.from_user.id, now(), CANCEL_HOURS)
    except BookingError as ex:
        await cb.answer(str(ex), show_alert=True)
        return
    left, _ = db.subscription_summary(cb.from_user.id, now().date())
    await show(cb, f"Запись на {fmt_dt(b['start'])} отменена. Занятие вернулось на абонемент (доступно: {left}).",
               kb([[("🗓 Мои записи", "my"), ("📅 Расписание", "sch")]]))
    await cb.answer("Отменено")
    await notify_cancel(bot, cb.from_user.id, b)


# ======================= client: subscription & plans =======================

@router.message(F.text == BTN_SUB)
async def m_sub(m: Message, state: FSMContext):
    await state.clear()
    left, subs = db.subscription_summary(m.from_user.id, now().date())
    if not subs:
        await m.answer("🎫 У вас нет действующего абонемента.", reply_markup=kb([[("💳 Купить абонемент", "plans")]]))
        return
    lines = [f"🎫 <b>Осталось занятий: {left}</b>"]
    for s in subs:
        lines.append(f"• {e(s['title'])}: {s['sessions_left']} из {s['sessions_total']}, "
                     f"действует до {date.fromisoformat(s['expires']).strftime('%d.%m.%Y')}")
    up = db.user_upcoming(m.from_user.id, now())
    if up:
        lines.append(f"\nПредстоящих записей: {len(up)}")
    await m.answer("\n".join(lines), reply_markup=kb([[("📅 Записаться", "sch"), ("💳 Продлить", "plans")]]))


def plans_view():
    ps = db.plans()
    if not ps:
        return "Тарифы пока не настроены.", None
    lines = ["💳 <b>Абонементы</b>"]
    rows = []
    for p in ps:
        lines.append(f"• <b>{e(p['title'])}</b> — {money(p['price'])}, {p['sessions']} занятий, срок {p['days']} дн.")
        rows.append([(f"{p['title']} — {money(p['price'])}", f"buy:{p['id']}")])
    return "\n".join(lines), kb(rows)


@router.message(F.text == BTN_BUY)
async def m_plans(m: Message, state: FSMContext):
    await state.clear()
    t, k = plans_view()
    await m.answer(t, reply_markup=k)


@router.callback_query(F.data == "plans")
async def cb_plans(cb: CallbackQuery):
    t, k = plans_view()
    await show(cb, t, k)
    await cb.answer()


@router.callback_query(F.data.startswith("buy:"))
async def cb_buy(cb: CallbackQuery, bot: Bot):
    p = db.get_plan(int(cb.data[4:]))
    if not p or not p["active"]:
        await cb.answer("Тариф недоступен", show_alert=True)
        return
    if PAY_TOKEN:
        await bot.send_invoice(cb.from_user.id, **plan_invoice_kwargs(p))
        await cb.answer()
        return
    await show(cb, f"Вы выбрали: <b>{e(p['title'])}</b> — {money(p['price'])}.\n\n"
                   "💡 Онлайн-оплата в боте скоро будет подключена. Сейчас я передал заявку тренеру — "
                   "он свяжется с вами, а после оплаты абонемент появится в разделе «🎫 Мой абонемент».")
    await cb.answer("Заявка отправлена тренеру")
    uid = cb.from_user.id
    await notify_plan_request(bot, uid, p)


# ======================= payments =======================

@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    kind, _, rid = q.invoice_payload.partition(":")
    ok, err = False, "Не удалось проверить заказ, попробуйте снова."
    try:
        if kind == "plan":
            p = db.get_plan(int(rid))
            ok = bool(p and p["active"] and p["price"] == q.total_amount)
            err = "Тариф изменился или недоступен — откройте список тарифов заново."
        elif kind == "order":
            o = db.get_order(int(rid))
            ok = bool(o and not o["paid"] and o["status"] != "cancelled" and o["total"] == q.total_amount
                      and o["user_id"] == q.from_user.id)
            err = "Заказ уже оплачен, отменён или изменился."
    except Exception:
        log.exception("pre_checkout")
    await q.answer(ok=ok, error_message=None if ok else err)


@router.message(F.successful_payment)
async def on_paid(m: Message, bot: Bot):
    sp = m.successful_payment
    kind, _, rid = sp.invoice_payload.partition(":")
    rid = int(rid)
    if not db.record_payment(m.from_user.id, kind, rid, sp.total_amount, sp.currency,
                             sp.telegram_payment_charge_id, sp.provider_payment_charge_id):
        return
    who = uname(db.get_user(m.from_user.id))
    if kind == "plan":
        p = db.get_plan(rid)
        db.credit_subscription(m.from_user.id, p["sessions"], p["days"], p["title"], p["id"], "payment", now())
        left, _ = db.subscription_summary(m.from_user.id, now().date())
        await m.answer(f"✅ Оплата прошла! Абонемент «{e(p['title'])}» активирован. Доступно занятий: {left}.",
                       reply_markup=kb([[("📅 Записаться", "sch")]]))
        await notify_owners(bot, f"💰 <b>Оплата абонемента</b>\n{who}\n{e(p['title'])} — {money(sp.total_amount)}")
    elif kind == "order":
        db.set_order_paid(rid, 1)
        await m.answer(f"✅ Оплата заказа №{rid} прошла! Мы сообщим, когда он будет готов.")
        await notify_owners(bot, f"💰 <b>Оплачен заказ №{rid}</b>\n{who} — {money(sp.total_amount)}",
                            kb([[("Открыть заказ", f"ao:{rid}")]]))


# ======================= client: shop =======================

def shop_view():
    ps = db.products()
    if not ps:
        return "🛍 Магазин пока пуст.", None
    rows = [[(f"{p['title']} — {money(p['price'])}", f"p:{p['id']}")] for p in ps]
    rows.append([("🛒 Корзина", "cart")])
    return "🛍 <b>Магазин</b>\nВыберите товар:", kb(rows)


@router.message(F.text == BTN_SHOP)
async def m_shop(m: Message, state: FSMContext):
    await state.clear()
    t, k = shop_view()
    await m.answer(t, reply_markup=k)


@router.callback_query(F.data == "shop")
async def cb_shop(cb: CallbackQuery):
    t, k = shop_view()
    await show(cb, t, k)
    await cb.answer()


@router.callback_query(F.data.startswith("p:"))
async def cb_product(cb: CallbackQuery):
    p = db.get_product(int(cb.data[2:]))
    if not p or not p["active"]:
        await cb.answer("Товар недоступен", show_alert=True)
        return
    text = f"<b>{e(p['title'])}</b>\n\n{e(p['description'])}\n\nЦена: <b>{money(p['price'])}</b>"
    markup = kb([[("➕ В корзину", f"ca:{p['id']}")], [("🛒 Корзина", "cart"), ("⬅️ Каталог", "shop")]])
    if p["photo"]:
        try:
            await cb.message.answer_photo(p["photo"], caption=text, reply_markup=markup)
            await cb.answer()
            return
        except Exception as ex:
            log.warning("photo send failed: %s", ex)
    await show(cb, text, markup)
    await cb.answer()


@router.callback_query(F.data.startswith("ca:"))
async def cb_cart_add(cb: CallbackQuery):
    pid = int(cb.data[3:])
    p = db.get_product(pid)
    if not p or not p["active"]:
        await cb.answer("Товар недоступен", show_alert=True)
        return
    db.cart_add(cb.from_user.id, pid, 1)
    await cb.answer(f"Добавлено: {p['title']}")


def cart_view(uid):
    rows, total = db.cart(uid)
    if not rows:
        return "🛒 Корзина пуста.", kb([[("🛍 В магазин", "shop")]])
    lines = ["🛒 <b>Корзина</b>"]
    btns = []
    for r in rows:
        lines.append(f"• {e(r['title'])} × {r['qty']} = {money(r['price'] * r['qty'])}")
        btns.append([("➖", f"c-:{r['product_id']}"), (f"{r['title'][:20]} × {r['qty']}", "noop"),
                     ("➕", f"c+:{r['product_id']}")])
    lines.append(f"\nИтого: <b>{money(total)}</b>")
    btns.append([("✅ Оформить заказ", "checkout")])
    btns.append([("🗑 Очистить", "cclear"), ("🛍 Каталог", "shop")])
    return "\n".join(lines), kb(btns)


@router.message(F.text == BTN_CART)
async def m_cart(m: Message, state: FSMContext):
    await state.clear()
    t, k = cart_view(m.from_user.id)
    await m.answer(t, reply_markup=k)


@router.callback_query(F.data == "cart")
async def cb_cart(cb: CallbackQuery):
    t, k = cart_view(cb.from_user.id)
    await show(cb, t, k)
    await cb.answer()


@router.callback_query(F.data.startswith("c+:") | F.data.startswith("c-:"))
async def cb_cart_qty(cb: CallbackQuery):
    db.cart_add(cb.from_user.id, int(cb.data[3:]), 1 if cb.data[1] == "+" else -1)
    t, k = cart_view(cb.from_user.id)
    await show(cb, t, k)
    await cb.answer()


@router.callback_query(F.data == "cclear")
async def cb_cart_clear(cb: CallbackQuery):
    db.cart_clear(cb.from_user.id)
    t, k = cart_view(cb.from_user.id)
    await show(cb, t, k)
    await cb.answer("Корзина очищена")


@router.callback_query(F.data == "checkout")
async def cb_checkout(cb: CallbackQuery, state: FSMContext):
    rows, _ = db.cart(cb.from_user.id)
    if not rows:
        await cb.answer("Корзина пуста", show_alert=True)
        return
    await state.set_state(Checkout.delivery)
    await show(cb, "Как вы хотите получить заказ?",
               kb([[("🏠 Самовывоз из студии", "dl:pickup")], [("🚚 Доставка", "dl:delivery")], [("Отмена", "dl:cancel")]]))
    await cb.answer()


def phone_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
                               resize_keyboard=True, one_time_keyboard=True)


@router.callback_query(Checkout.delivery, F.data.startswith("dl:"))
async def cb_delivery(cb: CallbackQuery, state: FSMContext):
    v = cb.data[3:]
    if v == "cancel":
        await state.clear()
        await show(cb, "Оформление отменено. Товары остались в корзине.")
        await cb.answer()
        return
    await state.update_data(delivery=v)
    if v == "delivery":
        await state.set_state(Checkout.address)
        await show(cb, "🚚 Доставка. Напишите адрес: город, улица, дом, квартира (и комментарий для курьера, если нужно).")
    else:
        await state.set_state(Checkout.phone)
        await show(cb, f"🏠 Самовывоз из студии ({e(STUDIO_ADDRESS)}).")
        await cb.message.answer("Оставьте телефон для связи — нажмите кнопку или напишите номер:", reply_markup=phone_kb())
    await cb.answer()


@router.message(Checkout.address, F.text)
async def st_address(m: Message, state: FSMContext):
    if len(m.text.strip()) < 5:
        await m.answer("Адрес слишком короткий, напишите подробнее.")
        return
    await state.update_data(address=m.text.strip()[:500])
    await state.set_state(Checkout.phone)
    await m.answer("Телефон для связи — нажмите кнопку или напишите номер:", reply_markup=phone_kb())


@router.message(Checkout.phone, F.contact | F.text)
async def st_phone(m: Message, state: FSMContext):
    phone = m.contact.phone_number if m.contact else m.text.strip()
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 10:
        await m.answer("Похоже, номер неполный. Напишите телефон, например +7 900 123-45-67.")
        return
    db.set_phone(m.from_user.id, phone)
    await state.update_data(phone=phone)
    data = await state.get_data()
    rows, total = db.cart(m.from_user.id)
    lines = ["<b>Проверьте заказ</b>"] + [f"• {e(r['title'])} × {r['qty']} = {money(r['price'] * r['qty'])}" for r in rows]
    lines.append(f"Итого: <b>{money(total)}</b>")
    lines.append("Получение: " + ("доставка — " + e(data.get("address")) if data["delivery"] == "delivery"
                                  else "самовывоз из студии"))
    lines.append(f"Телефон: {e(phone)}")
    await state.set_state(Checkout.confirm)
    await m.answer("Спасибо!", reply_markup=main_menu(m.from_user.id))
    await m.answer("\n".join(lines), reply_markup=kb([[("✅ Подтвердить заказ", "cok")], [("Отмена", "cno")]]))


@router.callback_query(Checkout.confirm, F.data.in_({"cok", "cno"}))
async def cb_confirm_order(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    if cb.data == "cno":
        await show(cb, "Оформление отменено. Товары остались в корзине.")
        await cb.answer()
        return
    try:
        oid = db.create_order(cb.from_user.id, data["delivery"], data.get("address"), data["phone"])
    except BookingError as ex:
        await cb.answer(str(ex), show_alert=True)
        return
    o = db.get_order(oid)
    await cb.answer("Заказ оформлен")
    if PAY_TOKEN:
        await show(cb, f"✅ Заказ №{oid} оформлен на {money(o['total'])}. Ниже — счёт для оплаты.")
        await bot.send_invoice(cb.from_user.id, **order_invoice_kwargs(o))
    else:
        await show(cb, f"✅ Заказ №{oid} оформлен на {money(o['total'])}!\n\n"
                       "💡 Онлайн-оплата скоро появится в боте. Тренер свяжется с вами, чтобы подтвердить заказ и оплату.")
    await notify_new_order(bot, oid)


def local_ts(s):
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(TZ)
        return dt.strftime("%d.%m %H:%M")
    except Exception:
        return s


def order_text(o):
    items = json.loads(o["items"])
    lines = [f"Заказ №{o['id']} от {local_ts(o['created'])}", f"Клиент: {uname(o)}, id <code>{o['user_id']}</code>"]
    lines += [f"• {e(i['title'])} × {i['qty']} = {money(i['price'] * i['qty'])}" for i in items]
    lines.append(f"Итого: <b>{money(o['total'])}</b> · {'💰 оплачен' if o['paid'] else '⏳ не оплачен'}")
    lines.append("Получение: " + (f"доставка — {e(o['address'])}" if o["delivery"] == "delivery" else "самовывоз"))
    lines.append(f"Телефон: {e(o['phone'])}")
    lines.append(f"Статус: <b>{ORDER_STATUSES.get(o['status'], o['status'])}</b>")
    return "\n".join(lines)


# ======================= admin =======================

admin = Router()
admin.message.filter(IsOwner())
admin.callback_query.filter(IsOwner())


def admin_menu():
    return kb([[("📅 Расписание по дням", "adm_days")],
               [("➕ Добавить слот", "adm_addslot"), ("🗓 Шаблон недели", "adm_tpl")],
               [("🎫 Тарифы", "adm_plans"), ("🛍 Товары", "adm_products")],
               [("👤 Клиенты и начисления", "adm_users")],
               [("📦 Заказы", "adm_orders"), ("👑 Владельцы", "adm_owners")]])


ADMIN_TEXT = "⚙️ <b>Панель владельца</b>"


@admin.message(F.text == BTN_ADMIN)
@admin.message(Command("admin"))
async def m_admin(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(ADMIN_TEXT, reply_markup=admin_menu())


@admin.callback_query(F.data == "adm")
async def cb_admin(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, ADMIN_TEXT, admin_menu())
    await cb.answer()


# ---- schedule ----
@admin.callback_query(F.data == "adm_days")
async def cb_adm_days(cb: CallbackQuery):
    markup, _ = days_kb("ad", back="adm", all_days=True)
    await show(cb, "📅 Выберите день:", markup)
    await cb.answer()


def adm_day_view(d: str):
    slots = db.slots_on(date.fromisoformat(d))
    lines = [f"📅 <b>{fmt_day(d)}</b>"]
    rows = []
    total = 0
    for s in slots:
        bs = db.slot_bookings(s["id"])
        total += len(bs)
        who = ", ".join(uname(b) if b["full_name"] or b["username"] else f"id {b['user_id']}" for b in bs) or "—"
        lines.append(f"<b>{s['start'][11:]}</b> {e(s['title'])}: {s['booked']}/{s['capacity']} · {who}")
        rows.append([(f"{s['start'][11:]} · {s['booked']}/{s['capacity']}", f"as:{s['id']}")])
    if not slots:
        lines.append("Слотов нет.")
    else:
        lines.append(f"\nВсего записей: {total}")
    rows.append([("➕ Слот на этот день", f"aaddd:{d}"), ("🧹 Удалить свободные", f"aclr:{d}")])
    rows.append([("⬅️ К дням", "adm_days")])
    return "\n".join(lines), kb(rows)


@admin.callback_query(F.data.startswith("ad:"))
async def cb_adm_day(cb: CallbackQuery):
    t, k = adm_day_view(cb.data[3:])
    await show(cb, t, k)
    await cb.answer()


@admin.callback_query(F.data.startswith("aclr:"))
async def cb_adm_clear(cb: CallbackQuery):
    d = cb.data[5:]
    n = 0
    for s in db.slots_on(date.fromisoformat(d)):
        if s["booked"] == 0:
            db.delete_slot(s["id"])
            n += 1
    await cb.answer(f"Удалено свободных слотов: {n}", show_alert=True)
    t, k = adm_day_view(d)
    await show(cb, t, k)


def slot_view(sid):
    s = db.get_slot(sid)
    if not s:
        return None, None
    bs = db.slot_bookings(sid)
    lines = [f"<b>{fmt_dt(s['start'])}</b> — {e(s['title'])}", f"Записано: {s['booked']}/{s['capacity']}"]
    rows = []
    for b in bs:
        name = uname(b)
        lines.append(f"• {name}" + (f", {e(b['phone'])}" if b["phone"] else ""))
        rows.append([(f"❌ Отменить: {b['full_name'] or b['user_id']}"[:60], f"abx:{b['id']}")])
    rows.append([("👥 Изменить кол-во мест", f"acap:{sid}"), ("🗑 Удалить слот", f"adel:{sid}")])
    rows.append([("⬅️ К дню", f"ad:{s['start'][:10]}")])
    return "\n".join(lines), kb(rows)


@admin.callback_query(F.data.startswith("as:"))
async def cb_adm_slot(cb: CallbackQuery):
    t, k = slot_view(int(cb.data[3:]))
    if not t:
        await cb.answer("Слот удалён", show_alert=True)
        return
    await show(cb, t, k)
    await cb.answer()


@admin.callback_query(F.data.startswith("abx:"))
async def cb_adm_cancel_booking(cb: CallbackQuery, bot: Bot):
    try:
        b = db.cancel_booking(int(cb.data[4:]), cb.from_user.id, now(), 0, by_owner=True)
    except BookingError as ex:
        await cb.answer(str(ex), show_alert=True)
        return
    await notify_user(bot, b["user_id"], f"⚠️ Тренер отменил вашу запись на {fmt_dt(b['start'])}. "
                                         "Занятие вернулось на абонемент.")
    await cb.answer("Запись отменена, клиент уведомлён")
    t, k = slot_view(b["slot_id"])
    await show(cb, t, k)


@admin.callback_query(F.data.startswith("adel:"))
async def cb_adm_del_slot(cb: CallbackQuery):
    sid = int(cb.data[5:])
    s = db.get_slot(sid)
    if not s:
        await cb.answer("Уже удалён")
        return
    warn = f"\nНа слот записано {s['booked']} чел. — их записи будут отменены, занятия вернутся, клиенты получат уведомление." if s["booked"] else ""
    await show(cb, f"Удалить слот {fmt_dt(s['start'])}?{warn}", kb([[("🗑 Да, удалить", f"adelok:{sid}"), ("Нет", f"as:{sid}")]]))
    await cb.answer()


@admin.callback_query(F.data.startswith("adelok:"))
async def cb_adm_del_slot_ok(cb: CallbackQuery, bot: Bot):
    sid = int(cb.data[7:])
    s = db.get_slot(sid)
    if not s:
        await cb.answer("Уже удалён")
        return
    users = db.delete_slot(sid)
    for uid in users:
        await notify_user(bot, uid, f"⚠️ Тренировка {fmt_dt(s['start'])} отменена тренером. Занятие вернулось на абонемент.")
    await cb.answer("Слот удалён")
    t, k = adm_day_view(s["start"][:10])
    await show(cb, t, k)


@admin.callback_query(F.data.startswith("acap:"))
async def cb_adm_cap(cb: CallbackQuery, state: FSMContext):
    await state.set_state(SlotCap.value)
    await state.update_data(sid=int(cb.data[5:]))
    await cb.message.answer("Сколько мест в этом слоте? Отправьте число (1 — персональная). /cancel — отмена.")
    await cb.answer()


@admin.message(SlotCap.value, F.text)
async def st_cap(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or int(m.text) < 1:
        await m.answer("Нужно целое число от 1.")
        return
    sid = (await state.get_data())["sid"]
    await state.clear()
    db.set_slot_capacity(sid, int(m.text))
    t, k = slot_view(sid)
    await m.answer("Готово.\n\n" + (t or ""), reply_markup=k)


# ---- add slot ----
def parse_date(text):
    text = text.strip().lower()
    today = now().date()
    if text in ("сегодня",):
        return today
    if text in ("завтра",):
        return today + timedelta(days=1)
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        d = datetime.strptime(text, "%d.%m").date().replace(year=today.year)
        return d if d >= today else d.replace(year=today.year + 1)
    except ValueError:
        return None


@admin.callback_query(F.data == "adm_addslot")
async def cb_addslot(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddSlot.date)
    await cb.message.answer("На какую дату добавить слот? Например: <code>18.11</code>, <code>завтра</code>. /cancel — отмена.")
    await cb.answer()


@admin.callback_query(F.data.startswith("aaddd:"))
async def cb_addslot_day(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddSlot.time)
    await state.update_data(date=cb.data[6:])
    await cb.message.answer(f"Дата: {fmt_day(cb.data[6:])}. Время? Например <code>14:00</code> или несколько через запятую: "
                            "<code>9:00, 10:30, 18:00</code>")
    await cb.answer()


@admin.message(AddSlot.date, F.text)
async def st_slot_date(m: Message, state: FSMContext):
    d = parse_date(m.text)
    if not d:
        await m.answer("Не понял дату. Формат: 18.11 или 18.11.2026.")
        return
    await state.update_data(date=d.isoformat())
    await state.set_state(AddSlot.time)
    await m.answer(f"Дата: {fmt_day(d)}. Время? Например <code>14:00</code> или несколько через запятую: <code>9:00, 10:30</code>")


@admin.message(AddSlot.time, F.text)
async def st_slot_time(m: Message, state: FSMContext):
    times = []
    for part in m.text.replace(";", ",").split(","):
        part = part.strip().replace(".", ":")
        if not part:
            continue
        if ":" not in part:
            part += ":00"
        try:
            times.append(datetime.strptime(part, "%H:%M").strftime("%H:%M"))
        except ValueError:
            await m.answer(f"Не понял время «{e(part)}». Пример: 14:00")
            return
    if not times:
        await m.answer("Укажите время, например 14:00")
        return
    await state.update_data(times=times)
    await state.set_state(AddSlot.capacity)
    await m.answer("Сколько мест? <code>1</code> — персональная, больше — групповая.")


@admin.message(AddSlot.capacity, F.text)
async def st_slot_cap(m: Message, state: FSMContext):
    if not m.text.strip().isdigit() or int(m.text) < 1:
        await m.answer("Нужно целое число от 1.")
        return
    await state.update_data(capacity=int(m.text))
    await state.set_state(AddSlot.title)
    await m.answer("Название тренировки? Отправьте <code>-</code>, чтобы оставить «Персональная тренировка» "
                   "(для групповых по умолчанию «Групповая тренировка»).")


@admin.message(AddSlot.title, F.text)
async def st_slot_title(m: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    cap = data["capacity"]
    title = m.text.strip()
    if title == "-":
        title = "Персональная тренировка" if cap == 1 else "Групповая тренировка"
    d = date.fromisoformat(data["date"])
    added = []
    for t in data["times"]:
        hh, mm = map(int, t.split(":"))
        db.add_slot(datetime(d.year, d.month, d.day, hh, mm), cap, title[:100])
        added.append(t)
    tv, k = adm_day_view(d.isoformat())
    await m.answer(f"✅ Добавлено слотов: {len(added)} ({', '.join(added)}).\n\n" + tv, reply_markup=k)


# ---- template ----
@admin.callback_query(F.data == "adm_tpl")
async def cb_tpl(cb: CallbackQuery):
    await show(cb, "🗓 <b>Шаблон расписания</b>\nСоздаёт почасовые слоты, начиная с завтрашнего дня. "
                   "Уже существующие слоты на то же время пропускаются.",
               kb([[("Пн–Пт 9:00–20:00 · 1 неделя", "atpl:1")], [("Пн–Пт 9:00–20:00 · 2 недели", "atpl:2")],
                   [("Пн–Пт 9:00–20:00 · 4 недели", "atpl:4")],
                   [("✏️ Свои параметры", "atpl:custom")], [("⬅️ Назад", "adm")]]))
    await cb.answer()


@admin.callback_query(F.data.startswith("atpl:"))
async def cb_tpl_go(cb: CallbackQuery, state: FSMContext):
    v = cb.data[5:]
    if v == "custom":
        await state.set_state(Tpl.params)
        await cb.message.answer("Отправьте параметры одной строкой: <b>дни часы места недели</b>\n"
                                "Дни: 1=Пн … 7=Вс, диапазон или список. Часы — с какого по какой (последний час не включается).\n"
                                "Пример: <code>1-5 9-20 1 2</code> — Пн–Пт, слоты 9:00…19:00, 1 место, 2 недели.\n"
                                "Пример: <code>2,4,6 18-21 6 4</code> — Вт/Чт/Сб вечером, группы по 6, 4 недели.")
        await cb.answer()
        return
    n = db.generate_template(now().date() + timedelta(days=1), int(v), {0, 1, 2, 3, 4}, 9, 20, 1)
    await cb.answer(f"Создано слотов: {n}", show_alert=True)


def parse_days(s):
    out = set()
    for part in s.split(","):
        if "-" in part:
            a, b = map(int, part.split("-"))
            out |= set(range(a, b + 1))
        else:
            out.add(int(part))
    if not out or min(out) < 1 or max(out) > 7:
        raise ValueError
    return {d - 1 for d in out}


@admin.message(Tpl.params, F.text)
async def st_tpl(m: Message, state: FSMContext):
    try:
        days_s, hours_s, cap_s, weeks_s = m.text.split()
        days = parse_days(days_s)
        h1, h2 = map(int, hours_s.split("-"))
        cap, weeks = int(cap_s), int(weeks_s)
        assert 0 <= h1 < h2 <= 24 and cap >= 1 and 1 <= weeks <= 12
    except Exception:
        await m.answer("Не понял. Пример: <code>1-5 9-20 1 2</code>")
        return
    await state.clear()
    title = "Персональная тренировка" if cap == 1 else "Групповая тренировка"
    n = db.generate_template(now().date() + timedelta(days=1), weeks, days, h1, h2, cap, title)
    await m.answer(f"✅ Создано слотов: {n}.", reply_markup=admin_menu())


# ---- plans & products (generic) ----
FIELD_NAMES = {"title": "название", "sessions": "кол-во занятий", "days": "срок (дней)", "price": "цену (₽)",
               "description": "описание", "photo": "фото"}


def plans_admin_view():
    rows = [[(f"{'' if p['active'] else '🚫 '}{p['title']} — {money(p['price'])}", f"apl:{p['id']}")]
            for p in db.plans(only_active=False)]
    rows.append([("➕ Новый тариф", "apladd")])
    rows.append([("⬅️ Назад", "adm")])
    return "🎫 <b>Тарифы</b> (🚫 — скрыт от клиентов)", kb(rows)


def plan_card(pid):
    p = db.get_plan(pid)
    if not p:
        return None, None
    t = (f"🎫 <b>{e(p['title'])}</b>\nЗанятий: {p['sessions']}\nСрок: {p['days']} дн.\nЦена: {money(p['price'])}\n"
         f"Статус: {'виден клиентам' if p['active'] else 'скрыт'}")
    k = kb([[("✏️ Название", f"aed:plans:{pid}:title"), ("✏️ Занятия", f"aed:plans:{pid}:sessions")],
            [("✏️ Срок", f"aed:plans:{pid}:days"), ("✏️ Цена", f"aed:plans:{pid}:price")],
            [("👁 Скрыть/показать", f"atg:plans:{pid}"), ("🗑 Удалить", f"adp:plans:{pid}")],
            [("⬅️ К тарифам", "adm_plans")]])
    return t, k


def products_admin_view():
    rows = [[(f"{'' if p['active'] else '🚫 '}{p['title']} — {money(p['price'])}", f"apr:{p['id']}")]
            for p in db.products(only_active=False)]
    rows.append([("➕ Новый товар", "apradd")])
    rows.append([("⬅️ Назад", "adm")])
    return "🛍 <b>Товары</b> (🚫 — скрыт от клиентов)", kb(rows)


def product_card(pid):
    p = db.get_product(pid)
    if not p:
        return None, None
    t = (f"🛍 <b>{e(p['title'])}</b>\n{e(p['description'])}\nЦена: {money(p['price'])}\n"
         f"Фото: {'есть' if p['photo'] else 'нет'}\nСтатус: {'виден клиентам' if p['active'] else 'скрыт'}")
    k = kb([[("✏️ Название", f"aed:products:{pid}:title"), ("✏️ Описание", f"aed:products:{pid}:description")],
            [("✏️ Цена", f"aed:products:{pid}:price"), ("🖼 Фото", f"aed:products:{pid}:photo")],
            [("👁 Скрыть/показать", f"atg:products:{pid}"), ("🗑 Удалить", f"adp:products:{pid}")],
            [("⬅️ К товарам", "adm_products")]])
    return t, k


def card(table, rid):
    return plan_card(rid) if table == "plans" else product_card(rid)


@admin.callback_query(F.data == "adm_plans")
async def cb_adm_plans(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    t, k = plans_admin_view()
    await show(cb, t, k)
    await cb.answer()


@admin.callback_query(F.data == "adm_products")
async def cb_adm_products(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    t, k = products_admin_view()
    await show(cb, t, k)
    await cb.answer()


@admin.callback_query(F.data.startswith("apl:") | F.data.startswith("apr:"))
async def cb_adm_card(cb: CallbackQuery):
    t, k = card("plans" if cb.data.startswith("apl") else "products", int(cb.data[4:]))
    if not t:
        await cb.answer("Не найдено", show_alert=True)
        return
    await show(cb, t, k)
    await cb.answer()


@admin.callback_query(F.data.startswith("atg:"))
async def cb_toggle(cb: CallbackQuery):
    _, table, rid = cb.data.split(":")
    rid = int(rid)
    row = db.get_plan(rid) if table == "plans" else db.get_product(rid)
    db.update_field(table, rid, "active", 0 if row["active"] else 1)
    t, k = card(table, rid)
    await show(cb, t, k)
    await cb.answer("Готово")


@admin.callback_query(F.data.startswith("adp:"))
async def cb_del_item(cb: CallbackQuery):
    _, table, rid = cb.data.split(":")
    await show(cb, "Удалить безвозвратно? (Можно вместо этого скрыть.)",
               kb([[("🗑 Удалить", f"adpok:{table}:{rid}"), ("Нет", f"{'apl' if table == 'plans' else 'apr'}:{rid}")]]))
    await cb.answer()


@admin.callback_query(F.data.startswith("adpok:"))
async def cb_del_item_ok(cb: CallbackQuery):
    _, table, rid = cb.data.split(":")
    if table == "plans":
        db.delete_plan(int(rid))
        t, k = plans_admin_view()
    else:
        db.delete_product(int(rid))
        t, k = products_admin_view()
    await show(cb, t, k)
    await cb.answer("Удалено")


@admin.callback_query(F.data.startswith("aed:"))
async def cb_edit(cb: CallbackQuery, state: FSMContext):
    _, table, rid, field = cb.data.split(":")
    await state.set_state(EditField.value)
    await state.update_data(table=table, rid=int(rid), field=field)
    hint = {"price": "Число в рублях, например 2500", "photo": "Пришлите фото или <code>-</code>, чтобы убрать фото"}
    await cb.message.answer(f"Введите новое значение: {FIELD_NAMES[field]}.\n{hint.get(field, '')}\n/cancel — отмена.")
    await cb.answer()


def parse_value(field, m: Message):
    """Возвращает значение для БД или бросает ValueError с текстом ошибки."""
    if field == "photo":
        if m.photo:
            return m.photo[-1].file_id
        if m.text and m.text.strip() == "-":
            return None
        raise ValueError("Пришлите фото или «-».")
    text = (m.text or "").strip()
    if not text:
        raise ValueError("Пустое значение.")
    if field in ("sessions", "days"):
        if not text.isdigit() or int(text) < 1:
            raise ValueError("Нужно целое число больше 0.")
        return int(text)
    if field == "price":
        try:
            v = round(float(text.replace(" ", "").replace(",", ".").replace("₽", "")) * 100)
        except ValueError:
            raise ValueError("Нужна цена числом, например 2500.")
        if v <= 0:
            raise ValueError("Цена должна быть больше 0.")
        return v
    return text[:1000] if field == "description" else text[:100]


@admin.message(EditField.value)
async def st_edit(m: Message, state: FSMContext):
    data = await state.get_data()
    try:
        v = parse_value(data["field"], m)
    except ValueError as ex:
        await m.answer(str(ex))
        return
    await state.clear()
    db.update_field(data["table"], data["rid"], data["field"], v)
    t, k = card(data["table"], data["rid"])
    await m.answer("✅ Сохранено.\n\n" + (t or ""), reply_markup=k)


@admin.callback_query(F.data == "apladd")
async def cb_plan_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(NewPlan.title)
    await cb.message.answer("Новый тариф. Название? (например «8 занятий»). /cancel — отмена.")
    await cb.answer()


@admin.message(NewPlan.title, F.text)
async def st_np_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip()[:100])
    await state.set_state(NewPlan.sessions)
    await m.answer("Сколько занятий?")


@admin.message(NewPlan.sessions, F.text)
async def st_np_sessions(m: Message, state: FSMContext):
    try:
        await state.update_data(sessions=parse_value("sessions", m))
    except ValueError as ex:
        await m.answer(str(ex))
        return
    await state.set_state(NewPlan.days)
    await m.answer("Срок действия в днях? (например 30)")


@admin.message(NewPlan.days, F.text)
async def st_np_days(m: Message, state: FSMContext):
    try:
        await state.update_data(days=parse_value("days", m))
    except ValueError as ex:
        await m.answer(str(ex))
        return
    await state.set_state(NewPlan.price)
    await m.answer("Цена в рублях?")


@admin.message(NewPlan.price, F.text)
async def st_np_price(m: Message, state: FSMContext):
    try:
        price = parse_value("price", m)
    except ValueError as ex:
        await m.answer(str(ex))
        return
    d = await state.get_data()
    await state.clear()
    pid = db.add_plan(d["title"], d["sessions"], d["days"], price)
    t, k = plan_card(pid)
    await m.answer("✅ Тариф создан.\n\n" + t, reply_markup=k)


@admin.callback_query(F.data == "apradd")
async def cb_prod_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(NewProduct.title)
    await cb.message.answer("Новый товар. Название? /cancel — отмена.")
    await cb.answer()


@admin.message(NewProduct.title, F.text)
async def st_npr_title(m: Message, state: FSMContext):
    await state.update_data(title=m.text.strip()[:100])
    await state.set_state(NewProduct.description)
    await m.answer("Описание товара?")


@admin.message(NewProduct.description, F.text)
async def st_npr_desc(m: Message, state: FSMContext):
    await state.update_data(description=m.text.strip()[:1000])
    await state.set_state(NewProduct.price)
    await m.answer("Цена в рублях?")


@admin.message(NewProduct.price, F.text)
async def st_npr_price(m: Message, state: FSMContext):
    try:
        await state.update_data(price=parse_value("price", m))
    except ValueError as ex:
        await m.answer(str(ex))
        return
    await state.set_state(NewProduct.photo)
    await m.answer("Пришлите фото товара или <code>-</code>, если без фото.")


@admin.message(NewProduct.photo)
async def st_npr_photo(m: Message, state: FSMContext):
    try:
        photo = parse_value("photo", m)
    except ValueError as ex:
        await m.answer(str(ex))
        return
    d = await state.get_data()
    await state.clear()
    pid = db.add_product(d["title"], d["description"], d["price"], photo)
    t, k = product_card(pid)
    await m.answer("✅ Товар добавлен.\n\n" + t, reply_markup=k)


# ---- users & credits ----
@admin.callback_query(F.data == "adm_users")
async def cb_users(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [[(f"{u['full_name'] or ''} {('@' + u['username']) if u['username'] else ''}".strip()[:50] or str(u["id"]),
              f"au:{u['id']}")] for u in db.list_users(30)]
    rows.append([("🔎 Найти по @username или ID", "afind")])
    rows.append([("⬅️ Назад", "adm")])
    await show(cb, "👤 <b>Клиенты</b> (последние 30, кто запускал бота). Выберите клиента, чтобы начислить занятия:", kb(rows))
    await cb.answer()


@admin.callback_query(F.data == "afind")
async def cb_find(cb: CallbackQuery, state: FSMContext):
    await state.set_state(FindUser.query)
    await cb.message.answer("Отправьте @username или числовой ID клиента (клиент должен хотя бы раз запустить бота).")
    await cb.answer()


def user_card(uid):
    u = db.get_user(uid)
    if not u:
        return None, None
    left, subs = db.subscription_summary(uid, now().date())
    lines = [f"👤 <b>{uname(u)}</b>", f"ID: <code>{uid}</code>"]
    if u["phone"]:
        lines.append(f"Телефон: {e(u['phone'])}")
    lines.append(f"Доступно занятий: <b>{left}</b>")
    for s in subs:
        lines.append(f"• {e(s['title'])}: {s['sessions_left']}/{s['sessions_total']} до {s['expires']}")
    up = db.user_upcoming(uid, now())
    if up:
        lines.append("Записи: " + "; ".join(fmt_dt(r["start"]) for r in up))
    rows = [[(f"➕ {p['title']}", f"acr:{uid}:{p['id']}")] for p in db.plans()]
    rows.append([("➕ Произвольное кол-во занятий", f"acrn:{uid}")])
    rows.append([("⬅️ К клиентам", "adm_users")])
    return "\n".join(lines), kb(rows)


@admin.message(FindUser.query, F.text)
async def st_find(m: Message, state: FSMContext):
    u = db.find_user(m.text)
    if not u:
        await m.answer("Не нашёл. Клиент должен сначала нажать /start в боте. Попробуйте ещё раз или /cancel.")
        return
    await state.clear()
    t, k = user_card(u["id"])
    await m.answer(t, reply_markup=k)


@admin.callback_query(F.data.startswith("au:"))
async def cb_user(cb: CallbackQuery):
    t, k = user_card(int(cb.data[3:]))
    if not t:
        await cb.answer("Не найден", show_alert=True)
        return
    await show(cb, t, k)
    await cb.answer()


@admin.callback_query(F.data.startswith("acr:"))
async def cb_credit_plan(cb: CallbackQuery, bot: Bot):
    _, uid, pid = cb.data.split(":")
    uid, p = int(uid), db.get_plan(int(pid))
    if not p:
        await cb.answer("Тариф не найден", show_alert=True)
        return
    db.credit_subscription(uid, p["sessions"], p["days"], p["title"], p["id"], f"manual:{cb.from_user.id}", now())
    left, _ = db.subscription_summary(uid, now().date())
    await notify_user(bot, uid, f"🎫 Вам начислен абонемент «{e(p['title'])}». Доступно занятий: {left}.",
                      kb([[("📅 Записаться", "sch")]]))
    await cb.answer("Начислено, клиент уведомлён", show_alert=True)
    t, k = user_card(uid)
    if t:
        await cb.message.answer(t, reply_markup=k)


@admin.callback_query(F.data.startswith("acrn:"))
async def cb_credit_n(cb: CallbackQuery, state: FSMContext):
    await state.set_state(CreditN.value)
    await state.update_data(uid=int(cb.data[5:]))
    await cb.message.answer("Отправьте: <b>кол-во занятий</b> и <b>срок в днях</b> через пробел, например <code>5 30</code>.")
    await cb.answer()


@admin.message(CreditN.value, F.text)
async def st_credit_n(m: Message, state: FSMContext, bot: Bot):
    try:
        n, days = map(int, m.text.split())
        assert 1 <= n <= 500 and 1 <= days <= 3650
    except Exception:
        await m.answer("Формат: <code>5 30</code>")
        return
    uid = (await state.get_data())["uid"]
    await state.clear()
    db.credit_subscription(uid, n, days, f"Начислено тренером: {n} зан.", None, f"manual:{m.from_user.id}", now())
    left, _ = db.subscription_summary(uid, now().date())
    await notify_user(bot, uid, f"🎫 Тренер начислил вам {n} занятий (срок {days} дн.). Доступно: {left}.",
                      kb([[("📅 Записаться", "sch")]]))
    t, k = user_card(uid)
    await m.answer("✅ Начислено, клиент уведомлён.\n\n" + t, reply_markup=k)


# ---- orders ----
@admin.callback_query(F.data == "adm_orders")
async def cb_orders(cb: CallbackQuery):
    os_ = db.orders(20)
    rows = [[(f"№{o['id']} · {money(o['total'])} · {ORDER_STATUSES.get(o['status'])} · {'💰' if o['paid'] else '⏳'}",
              f"ao:{o['id']}")] for o in os_]
    rows.append([("⬅️ Назад", "adm")])
    await show(cb, "📦 <b>Заказы</b> (последние 20)" if os_ else "Заказов пока нет.", kb(rows))
    await cb.answer()


def order_card(oid):
    o = db.get_order(oid)
    if not o:
        return None, None
    st = [(("• " if o["status"] == k else "") + v, f"aos:{oid}:{k}") for k, v in ORDER_STATUSES.items()]
    rows = [st[i:i + 2] for i in range(0, len(st), 2)]
    rows.append([("💰 Отметить оплаченным" if not o["paid"] else "↩️ Снять отметку оплаты", f"aop:{oid}")])
    rows.append([("⬅️ К заказам", "adm_orders")])
    return order_text(o), kb(rows)


@admin.callback_query(F.data.startswith("ao:"))
async def cb_order(cb: CallbackQuery):
    t, k = order_card(int(cb.data[3:]))
    if not t:
        await cb.answer("Не найден", show_alert=True)
        return
    await show(cb, t, k)
    await cb.answer()


@admin.callback_query(F.data.startswith("aos:"))
async def cb_order_status(cb: CallbackQuery, bot: Bot):
    _, oid, status = cb.data.split(":")
    oid = int(oid)
    o = db.get_order(oid)
    if not o:
        await cb.answer("Не найден", show_alert=True)
        return
    if o["status"] != status:
        db.set_order_status(oid, status)
        await notify_user(bot, o["user_id"], f"📦 Статус заказа №{oid}: <b>{ORDER_STATUSES[status]}</b>")
    t, k = order_card(oid)
    await show(cb, t, k)
    await cb.answer("Статус обновлён")


@admin.callback_query(F.data.startswith("aop:"))
async def cb_order_paid(cb: CallbackQuery):
    oid = int(cb.data[4:])
    o = db.get_order(oid)
    db.set_order_paid(oid, 0 if o["paid"] else 1)
    t, k = order_card(oid)
    await show(cb, t, k)
    await cb.answer("Готово")


# ---- owners ----
def owners_view():
    lines = ["👑 <b>Владельцы бота</b>", "Владельцы видят панель «⚙️ Админ», вкладку «Тренер» в приложении и получают все уведомления."]
    rows = []
    for oid in sorted(owner_ids()):
        u = db.get_user(oid)
        name = uname(u) if u else "ещё не запускал бота"
        perm = oid in ENV_OWNER_IDS
        lines.append(f"• {name} — <code>{oid}</code>" + (" 🔒 (OWNER_IDS)" if perm else ""))
        if not perm:
            label = (u["full_name"] if u and u["full_name"] else str(oid))[:40]
            rows.append([(f"❌ Убрать: {label}", f"aowdel:{oid}")])
    rows.append([("➕ Добавить владельца", "aowadd")])
    rows.append([("⬅️ Назад", "adm")])
    return "\n".join(lines), kb(rows)


@admin.callback_query(F.data == "adm_owners")
async def cb_owners(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    t, k = owners_view()
    await show(cb, t, k)
    await cb.answer()


def share_user_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="👤 Выбрать из контактов", request_users=KeyboardButtonRequestUsers(
        request_id=1, user_is_bot=False, max_quantity=1, request_name=True, request_username=True))],
        [KeyboardButton(text="Отмена")]], resize_keyboard=True, one_time_keyboard=True)


@admin.callback_query(F.data == "aowadd")
async def cb_owner_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddOwner.query)
    await cb.message.answer("Кого сделать владельцем? Нажмите «👤 Выбрать из контактов» или отправьте числовой Telegram ID "
                            "либо @username (по @username найдётся только тот, кто уже запускал бота). /cancel — отмена.",
                            reply_markup=share_user_kb())
    await cb.answer()


async def grant_owner(m: Message, bot: Bot, uid: int):
    if uid in owner_ids():
        await m.answer("Этот пользователь уже владелец.", reply_markup=main_menu(m.from_user.id))
        return
    db.add_owner(uid)
    log.info("Owner %s added by %s", uid, m.from_user.id)
    u = db.get_user(uid)
    await notify_user(bot, uid, f"👑 {uname(db.get_user(m.from_user.id))} сделал(а) вас владельцем бота <b>{e(STUDIO_NAME)}</b>.\n"
                                "Теперь вам доступна панель «⚙️ Админ» (/admin) и уведомления о записях, оплатах и заказах.",
                      main_menu(uid))
    await m.answer(f"✅ {uname(u) if u else uid} теперь владелец. Если он ещё не запускал бота, пусть нажмёт /start.",
                   reply_markup=main_menu(m.from_user.id))
    t, k = owners_view()
    await m.answer(t, reply_markup=k)


@admin.message(AddOwner.query, F.users_shared)
async def st_owner_shared(m: Message, state: FSMContext, bot: Bot):
    su = m.users_shared.users[0]
    await state.clear()
    if not db.get_user(su.user_id):
        full = " ".join(x for x in (su.first_name, su.last_name) if x)
        db.upsert_user(su.user_id, su.username, full)
    await grant_owner(m, bot, su.user_id)


@admin.message(AddOwner.query, F.text)
async def st_owner_text(m: Message, state: FSMContext, bot: Bot):
    if m.text.strip().lower() == "отмена":
        await state.clear()
        await m.answer("Отменено.", reply_markup=main_menu(m.from_user.id))
        return
    text = m.text.strip()
    u = db.find_user(text)
    if u:
        uid = u["id"]
    elif text.isdigit():
        uid = int(text)
    else:
        await m.answer("Не нашёл такого пользователя. Попросите его нажать /start в боте, или пришлите числовой ID, "
                       "или выберите через «👤 Выбрать из контактов».")
        return
    await state.clear()
    await grant_owner(m, bot, uid)


@admin.callback_query(F.data.startswith("aowdel:"))
async def cb_owner_del(cb: CallbackQuery):
    uid = int(cb.data.split(":")[1])
    u = db.get_user(uid)
    await show(cb, f"Убрать права владельца у {uname(u) if u else uid}?",
               kb([[("Да, убрать", f"aowdelok:{uid}"), ("Нет", "adm_owners")]]))
    await cb.answer()


@admin.callback_query(F.data.startswith("aowdelok:"))
async def cb_owner_del_ok(cb: CallbackQuery, bot: Bot):
    uid = int(cb.data.split(":")[1])
    try:
        db.remove_owner(uid, ENV_OWNER_IDS)
    except BookingError as ex:
        await cb.answer(str(ex), show_alert=True)
        return
    log.info("Owner %s removed by %s", uid, cb.from_user.id)
    if uid != cb.from_user.id:
        await notify_user(bot, uid, f"Права владельца бота {e(STUDIO_NAME)} сняты.", main_menu(uid))
    await cb.answer("Готово")
    if uid == cb.from_user.id:
        await show(cb, "Вы больше не владелец.")
        await cb.message.answer("Меню обновлено.", reply_markup=main_menu(uid))
        return
    t, k = owners_view()
    await show(cb, t, k)


# ======================= fallback =======================

fallback = Router()


@fallback.message()
async def any_message(m: Message, state: FSMContext):
    if await state.get_state():
        await m.answer("Не понял ответ. Попробуйте ещё раз или отправьте /cancel.")
        return
    await m.answer("Выберите раздел в меню 👇", reply_markup=main_menu(m.from_user.id))


@fallback.callback_query()
async def any_cb(cb: CallbackQuery):
    await cb.answer("Кнопка устарела или недоступна", show_alert=False)


# ======================= main =======================

WEBHOOK_PATH = "/tg/webhook"


def make_bot():
    """TELEGRAM_API_BASE — свой сервер/зеркало Bot API (например, если api.telegram.org недоступен с хостинга)."""
    base = os.getenv("TELEGRAM_API_BASE", "").strip()
    session = None
    if base:
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.telegram import TelegramAPIServer
        session = AiohttpSession(api=TelegramAPIServer.from_base(base))
    return Bot(BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode="HTML"))


def webhook_secret():
    import hashlib
    return os.getenv("WEBHOOK_SECRET") or hashlib.sha256(("wh:" + BOT_TOKEN).encode()).hexdigest()[:48]


def build_dispatcher():
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(UserMiddleware())
    dp.callback_query.outer_middleware(UserMiddleware())
    dp.include_router(router)   # общие команды/меню (/cancel, кнопки) раньше админских FSM-шагов
    dp.include_router(admin)
    dp.include_router(fallback)
    return dp


async def setup_bot_ui(bot: Bot):
    await bot.set_my_commands([BotCommand(command="start", description="Главное меню"),
                               BotCommand(command="schedule", description="Расписание"),
                               BotCommand(command="cancel", description="Отменить действие"),
                               BotCommand(command="help", description="Помощь")])
    url = webapp_url()
    if url:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Открыть", web_app=WebAppInfo(url=url)))
            log.info("Menu button set to web app %s", url)
        except Exception as ex:
            log.error("setChatMenuButton failed: %s", ex)
    else:
        log.warning("Mini App URL unknown — menu button not set")


def build_webhook_app(bot: Bot, dp: Dispatcher):
    """Одно aiohttp-приложение: Mini App + API + вебхук Telegram."""
    import webapp
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    app = webapp.make_app(sys.modules[__name__], bot)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=webhook_secret()).register(app, path=WEBHOOK_PATH)

    async def on_startup(bot: Bot):
        await setup_bot_ui(bot)
        await bot.set_webhook(WEBHOOK_BASE_URL + WEBHOOK_PATH, secret_token=webhook_secret(),
                              allowed_updates=dp.resolve_used_update_types(), drop_pending_updates=False)
        log.info("Webhook set to %s%s", WEBHOOK_BASE_URL, WEBHOOK_PATH)

    dp.startup.register(on_startup)
    setup_application(app, dp, bot=bot)
    return app


async def main():
    if db.seed_if_empty(now().date()):
        log.info("Seeded example data")
    bot = make_bot()
    dp = build_dispatcher()
    me = await bot.get_me()
    log.info("Bot @%s; db: %s; owners configured: %s; payments: %s", me.username,
             "postgres" if db.pg else "sqlite", "yes" if owner_ids() else "no (use /claim_owner)",
             "on" if PAY_TOKEN else "off")
    if WEBHOOK_BASE_URL:
        from aiohttp import web
        app = build_webhook_app(bot, dp)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("Webhook mode: listening on 0.0.0.0:%s", PORT)
        await asyncio.Event().wait()
    else:
        await setup_bot_ui(bot)
        import webapp
        await webapp.start(sys.modules[__name__], bot)
        log.info("Polling mode")
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
