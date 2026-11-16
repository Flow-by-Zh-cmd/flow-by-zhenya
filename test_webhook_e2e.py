"""Сквозной тест режима вебхука: bot.py запускается как отдельный процесс с фиктивным токеном и поддельным
сервером Bot API (TELEGRAM_API_BASE), поэтому настоящий бот и реальные пользователи не затрагиваются.
Запуск: venv/bin/python test_webhook_e2e.py [postgres-url]"""
import asyncio, json, os, socket, subprocess, sys, tempfile, time
from aiohttp import web, ClientSession

BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN = "123456:FAKE-token-for-tests"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


async def main(db_url=None):
    calls = []

    async def api(request):
        method = request.match_info["method"]
        data = dict(await request.post()) if request.can_read_body else {}
        calls.append((method, data))
        results = {"getMe": {"id": 123456, "is_bot": True, "first_name": "Test", "username": "test_bot"},
                   "sendMessage": {"message_id": 1, "date": int(time.time()), "chat": {"id": 42, "type": "private"}, "text": "ok"}}
        return web.json_response({"ok": True, "result": results.get(method, True)})

    fake = web.Application(); fake.router.add_post("/bot{token}/{method}", api)
    fr = web.AppRunner(fake); await fr.setup()
    fport = free_port(); await web.TCPSite(fr, "127.0.0.1", fport).start()

    port = free_port()
    env = dict(os.environ, BOT_TOKEN=TOKEN, TELEGRAM_API_BASE=f"http://127.0.0.1:{fport}", PORT=str(port),
               WEBHOOK_BASE_URL="https://example.test", OWNER_IDS="", PAYMENT_PROVIDER_TOKEN="",
               DB_PATH=os.path.join(tempfile.mkdtemp(), "wh.db"), DATABASE_URL=db_url or "")
    if db_url and os.getenv("E2E_ENC"):   # проверка DATABASE_URL_ENC (строка БД, зашифрованная токеном)
        sys.path.insert(0, BASE)
        from secretbox import encrypt
        env.update(DATABASE_URL="", DATABASE_URL_ENC=encrypt(db_url, TOKEN))
    entry = os.getenv("E2E_ENTRY", "bot.py")
    proc = subprocess.Popen([sys.executable, "-u", os.path.join(BASE, entry)], env=env, cwd=tempfile.mkdtemp(),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    ok = True
    try:
        async with ClientSession() as s:
            for _ in range(60):
                try:
                    async with s.get(f"http://127.0.0.1:{port}/health") as r:
                        if r.status == 200: break
                except Exception: pass
                await asyncio.sleep(0.5)
            else:
                raise RuntimeError("server did not start")
            await asyncio.sleep(1)
            wh = [d for m, d in calls if m == "setWebhook"]
            print("setWebhook:", wh[0]["url"] if wh else None)
            ok &= bool(wh) and wh[0]["url"] == "https://example.test/tg/webhook"
            mb = [d for m, d in calls if m == "setChatMenuButton"]
            print("menu button:", json.loads(mb[0]["menu_button"])["web_app"]["url"] if mb else None)
            ok &= bool(mb)
            secret = wh[0]["secret_token"]
            update = {"update_id": 1, "message": {"message_id": 1, "date": int(time.time()), "text": "/start",
                      "chat": {"id": 42, "type": "private"}, "from": {"id": 42, "is_bot": False, "first_name": "Иван"},
                      "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}}
            async with s.post(f"http://127.0.0.1:{port}/tg/webhook", json=update,
                              headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}) as r:
                print("wrong secret ->", r.status); ok &= r.status in (401, 403)
            async with s.post(f"http://127.0.0.1:{port}/tg/webhook", json=update,
                              headers={"X-Telegram-Bot-Api-Secret-Token": secret}) as r:
                print("valid update ->", r.status); ok &= r.status == 200
            await asyncio.sleep(1.5)
            sent = [d for m, d in calls if m == "sendMessage"]
            print("bot replied:", len(sent), "messages;", (sent[0]["text"][:40] + "…") if sent else "")
            marks = json.dumps([json.loads(d["reply_markup"]) for d in sent if d.get("reply_markup")], ensure_ascii=False)
            print("web_app button in /start:", "https://example.test/" in marks)
            ok &= len(sent) >= 2 and "Открыть приложение" in marks and "web_app" in marks
            # владельцы: /claim_owner → «👑 Владельцы» → добавить по ID → новому владельцу уходит уведомление
            uid_n = [10]
            async def send(upd_body):
                uid_n[0] += 1
                upd = {"update_id": uid_n[0], **upd_body}
                async with s.post(f"http://127.0.0.1:{port}/tg/webhook", json=upd,
                                  headers={"X-Telegram-Bot-Api-Secret-Token": secret}) as r:
                    assert r.status == 200
                await asyncio.sleep(0.4)
            frm = {"id": 42, "is_bot": False, "first_name": "Иван"}
            chat = {"id": 42, "type": "private"}
            def msg(text):
                ent = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else []
                return {"message": {"message_id": 5, "date": int(time.time()), "text": text, "chat": chat, "from": frm, "entities": ent}}
            def cbq(data):
                return {"callback_query": {"id": "q1", "from": frm, "chat_instance": "ci", "data": data,
                        "message": {"message_id": 7, "date": int(time.time()), "chat": chat, "text": "x",
                                    "from": {"id": 123456, "is_bot": True, "first_name": "Test"}}}}
            await send(msg("/claim_owner"))
            await send(cbq("adm_owners"))
            await send(cbq("aowadd"))
            await send(msg("555000111"))
            to555 = [d for m, d in calls if m == "sendMessage" and d.get("chat_id") == "555000111"]
            print("new owner notified:", bool(to555)); ok &= bool(to555) and "владельцем" in to555[0]["text"]
            await send(cbq("aowdelok:555000111"))
            await send(cbq("aowdelok:42"))   # последний владелец — должен быть отказ
            alerts = [d for m, d in calls if m == "answerCallbackQuery" and "последнего" in d.get("text", "")]
            print("last owner protected:", bool(alerts)); ok &= bool(alerts)
            async with s.get(f"http://127.0.0.1:{port}/") as r:
                print("mini app page ->", r.status); ok &= r.status == 200
            async with s.get(f"http://127.0.0.1:{port}/telegram-web-app.js") as r:
                print("telegram-web-app.js ->", r.status); ok &= r.status == 200
            async with s.get(f"http://127.0.0.1:{port}/api/me") as r:
                print("api without initData ->", r.status); ok &= r.status == 401
    finally:
        proc.terminate()
        out = proc.communicate(timeout=10)[0]
        await fr.cleanup()
    if not ok or "Traceback" in out:
        print(out); print("FAILED"); sys.exit(1)
    if db_url and "db: postgres" not in out:
        print(out); print("FAILED: not postgres"); sys.exit(1)
    print("WEBHOOK E2E OK", "(postgres)" if db_url else "(sqlite)", "enc" if os.getenv("E2E_ENC") else "", os.getenv("E2E_ENTRY", "bot.py"))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
