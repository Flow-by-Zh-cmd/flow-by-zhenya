#!/usr/bin/env bash
# (Пере)запуск: публичный HTTPS-туннель (если не запущен) + бот с Mini App сервером (порт 8080).
#   ./start.sh          — перезапустить бота; живой туннель сохраняется (URL не меняется)
#   ./start.sh --fresh  — перезапустить и туннель (будет НОВЫЙ URL)
# Туннель: TUNNEL=runlocal (по умолчанию; WebSocket на 443, без регистрации) или TUNNEL=cloudflared
# (quick tunnel; с этого компьютера не работает — порт 7844 закрыт).
# Бот при старте читает webapp_url.txt и сам вызывает setChatMenuButton (web_app, «Открыть»).
cd "$(dirname "$0")"
PORT="${WEBAPP_PORT:-8080}"
TUNNEL="${TUNNEL:-runlocal}"

tunnel_alive() { [ -f tunnel.pid ] && kill -0 "$(cat tunnel.pid)" 2>/dev/null; }

if [ "$1" = "--fresh" ] && tunnel_alive; then kill "$(cat tunnel.pid)"; sleep 1; fi

if ! tunnel_alive || [ ! -s webapp_url.txt ]; then
  tunnel_alive && kill "$(cat tunnel.pid)" && sleep 1
  : > tunnel.log; rm -f webapp_url.txt
  if [ "$TUNNEL" = "cloudflared" ]; then
    nohup ./bin/cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >> tunnel.log 2>&1 &
    RE='https://[a-z0-9-]*\.trycloudflare\.com'
  else
    NO_COLOR=1 nohup ./tunnel-npm/node_modules/.bin/runlocal "$PORT" >> tunnel.log 2>&1 &
    RE='https://[a-z0-9-]*\.runlocal\.eu'
  fi
  echo $! > tunnel.pid
  URL=""
  for i in $(seq 1 40); do
    URL=$(sed 's/\x1b\[[0-9;]*m//g' tunnel.log | grep -o "$RE" | head -1)
    [ -n "$URL" ] && break; sleep 1
  done
  if [ -z "$URL" ]; then echo "ERROR: tunnel URL not obtained, see tunnel.log"; else echo "$URL" > webapp_url.txt; fi
fi
echo "Mini App URL: $(cat webapp_url.txt 2>/dev/null)"

if [ -f bot.pid ] && kill -0 "$(cat bot.pid)" 2>/dev/null; then kill "$(cat bot.pid)"; sleep 2; fi
pkill -f "$PWD/venv/bin/python -u $PWD/bot.py" 2>/dev/null; sleep 1
nohup "$PWD/venv/bin/python" -u "$PWD/bot.py" >> bot.log 2>&1 &
echo $! > bot.pid
echo "Bot started, PID $(cat bot.pid) (tunnel PID $(cat tunnel.pid 2>/dev/null)). Logs: bot.log, tunnel.log"

# сторож: раз в минуту проверяет бота и туннель, при сбое перезапускает
if ! { [ -f watchdog.pid ] && kill -0 "$(cat watchdog.pid)" 2>/dev/null; }; then
  nohup ./watchdog.sh >> watchdog.log 2>&1 &
  echo $! > watchdog.pid
fi
