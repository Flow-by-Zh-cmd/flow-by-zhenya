"""Перенос данных из SQLite (bot.db) в PostgreSQL (например, Neon).

    DATABASE_URL='postgresql://...' venv/bin/python migrate_to_postgres.py [путь к bot.db] [--force]

Копирует все таблицы с сохранением id, затем выставляет счётчики id. По умолчанию отказывается писать
в непустую базу; --force сначала очищает таблицы в Postgres."""
import os
import sqlite3
import sys

from dotenv import load_dotenv

from db import DB, TABLES, is_pg_url


def migrate(sqlite_path, pg_url, force=False, out=print):
    if not is_pg_url(pg_url):
        raise SystemExit("DATABASE_URL должен начинаться с postgres:// или postgresql://")
    if not os.path.exists(sqlite_path):
        raise SystemExit(f"Нет файла {sqlite_path}")
    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row
    dst = DB(pg_url)  # создаёт схему, если её нет
    existing = {t: dst.q1(f"SELECT COUNT(*) AS c FROM {t}")["c"] for t in TABLES}
    if any(existing.values()) and not force:
        raise SystemExit(f"В Postgres уже есть данные {existing}. Запустите с --force, чтобы перезаписать.")
    dst.x("BEGIN")
    try:
        for t in TABLES:
            dst.x(f"DELETE FROM {t}")
        for t in TABLES:
            rows = src.execute(f"SELECT * FROM {t}").fetchall()
            if not rows:
                out(f"{t}: 0")
                continue
            cols = rows[0].keys()
            sql = f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
            for r in rows:
                dst.x(sql, *[r[c] for c in cols])
            out(f"{t}: {len(rows)}")
        for t in TABLES:
            if t in ("settings", "users", "cart"):
                continue
            dst.x(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), COALESCE((SELECT MAX(id) FROM {t}), 0) + 1, false)")
        dst.x("COMMIT")
    except Exception:
        dst.x("ROLLBACK")
        raise
    for t in TABLES:
        a = src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        b = dst.q1(f"SELECT COUNT(*) AS c FROM {t}")["c"]
        if a != b:
            raise SystemExit(f"Проверка не прошла: {t} sqlite={a} postgres={b}")
    out("Готово: данные перенесены и проверены.")


if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(base, ".env"))
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    migrate(args[0] if args else os.path.join(base, "bot.db"), os.getenv("DATABASE_URL", ""), "--force" in sys.argv)
