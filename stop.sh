#!/usr/bin/env bash
# ./stop.sh — остановить бота, туннель и сторожа
cd "$(dirname "$0")"
for f in watchdog bot tunnel; do [ -f $f.pid ] && kill "$(cat $f.pid)" 2>/dev/null && echo "$f stopped"; rm -f $f.pid; done
rm -f webapp_url.txt
