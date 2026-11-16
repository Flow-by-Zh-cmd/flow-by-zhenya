#!/usr/bin/env bash
# Раз в минуту: если бот упал — ./start.sh; если туннель упал или публичный URL не отвечает 2 раза подряд — ./start.sh --fresh
cd "$(dirname "$0")"
fails=0
while sleep 60; do
  if ! { [ -f tunnel.pid ] && kill -0 "$(cat tunnel.pid)" 2>/dev/null; }; then
    echo "$(date '+%F %T') tunnel dead -> restart"; ./start.sh --fresh >/dev/null; fails=0; continue
  fi
  if ! { [ -f bot.pid ] && kill -0 "$(cat bot.pid)" 2>/dev/null; }; then
    echo "$(date '+%F %T') bot dead -> restart"; ./start.sh >/dev/null; continue
  fi
  code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$(cat webapp_url.txt 2>/dev/null)/healthz")
  if [ "$code" = "200" ]; then fails=0; else fails=$((fails+1)); echo "$(date '+%F %T') healthz=$code ($fails)"; fi
  if [ "$fails" -ge 2 ]; then echo "$(date '+%F %T') public URL down -> fresh tunnel"; ./start.sh --fresh >/dev/null; fails=0; fi
done
