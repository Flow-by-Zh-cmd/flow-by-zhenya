# Flow by Zhenya — Telegram-бот (@Flowbyzhenya_bot)

Python 3 + aiogram 3 (long polling) + SQLite (`bot.db`).

## Запуск / перезапуск
    ./start.sh     # (пере)запуск в фоне, PID в bot.pid, лог в bot.log
    ./stop.sh      # остановка
    venv/bin/python -m unittest test_db -v   # тесты логики БД

## Владелец
Отправьте боту `/claim_owner` — первый отправивший станет владельцем (сохраняется в БД), после этого команда отключена.
Либо пропишите ID в `.env` → `OWNER_IDS=123,456` (тогда /claim_owner не работает) и перезапустите. Узнать свой ID: `/myid`.
Владелец видит кнопку «⚙️ Админ» (/admin) и получает уведомления о записях, отменах, оплатах, заказах и заявках на абонемент.

## Настройки (.env)
- PAYMENT_PROVIDER_TOKEN — платёжный токен из @BotFather → Payments (ЮKassa и др.). Пусто → онлайн-оплата выключена:
  клиент оставляет заявку, владелец получает её с кнопкой «Начислить». Заказы магазина создаются неоплаченными,
  владелец отмечает оплату вручную.
- CANCEL_HOURS — за сколько часов можно отменить запись (по умолчанию 12).
- TZ_NAME, STUDIO_NAME, STUDIO_ADDRESS, CURRENCY.

## Пример данных
Тарифы, товары и слоты с пометкой «(пример)» созданы автоматически — цены условные, замените в админке.

## Файлы
- bot.py — обработчики Telegram; db.py — вся логика и схема БД; test_db.py — тесты.

## Telegram Mini App
- Фронтенд: `webapp/index.html` (одна страница, тема Telegram, светлая/тёмная). API: `webapp.py` (aiohttp, порт 8080,
  в том же процессе, что и бот, общая БД и логика `db.py`). Каждый запрос `/api/*` проверяется по подписи initData (`tgauth.py`).
- Публичный HTTPS: туннель **runlocal.eu** (WebSocket на 443, без регистрации). Cloudflare quick tunnel с этого
  компьютера не работает (исходящий порт 7844 закрыт), SSH-туннели тоже; можно включить `TUNNEL=cloudflared ./start.sh --fresh` в другой сети.
- `./start.sh` поднимает туннель (если он не жив), пишет URL в `webapp_url.txt`, перезапускает бота; бот при старте
  ставит кнопку меню «Открыть» (setChatMenuButton) и показывает кнопку «📱 Открыть приложение» в /start.
  `./start.sh --fresh` — новый туннель (новый URL). `./stop.sh` — остановить всё.
- `watchdog.sh` (запускается из start.sh) раз в минуту проверяет бота и публичный URL и при сбое перезапускает.
- Тесты: `venv/bin/python -m unittest test_db test_auth test_webapp`.

## Хостинг (вебхук) и PostgreSQL
- `WEBHOOK_BASE_URL=https://...` → режим вебхука: одно aiohttp-приложение на `0.0.0.0:$PORT` отдаёт Mini App, API и
  `POST /tg/webhook` (секрет в заголовке). Пусто → long polling + локальный сервер (как сейчас на box).
- `DATABASE_URL=postgresql://...` → PostgreSQL (Neon); пусто → SQLite `bot.db`. `DATABASE_URL_ENC` — то же, зашифрованное токеном (`secretbox.py`).
- Без `.env` (на хостинге) читается `config.env` из репозитория; переменные окружения важнее.
- `migrate_to_postgres.py` — перенос bot.db → Postgres. `Dockerfile`, `requirements.txt`, `render.yaml`, `main.py`.
- Пошаговая инструкция на русском: `DEPLOY_RU.md`.
- Тесты: `venv/bin/python -m unittest test_db test_auth test_webapp`; Postgres: `TEST_DATABASE_URL=postgresql://... venv/bin/python -m unittest test_db`;
  сквозной тест вебхука с поддельным Bot API: `venv/bin/python test_webhook_e2e.py [postgres-url]`.

## Владельцы
⚙️ Админ → 👑 Владельцы: список, добавить (ID, @username или «Выбрать из контактов»), убрать (с подтверждением).
Последнего владельца убрать нельзя; ID из `OWNER_IDS` помечены 🔒 и удаляются только в настройках.
Владельцы хранятся в БД (таблица settings) и переносятся миграцией.
