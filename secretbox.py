"""Шифрование секретов (DATABASE_URL) ключом, производным от токена бота.

Нужно для хостингов без переменных окружения на бесплатном тарифе: в публичный репозиторий кладётся только
зашифрованная строка (DATABASE_URL_ENC в config.env), а расшифровать её может лишь тот, у кого есть токен бота.

    venv/bin/python secretbox.py encrypt 'postgresql://...'   # токен берётся из BOT_TOKEN / .env
"""
import base64
import hashlib
import os
import sys


def _fernet(bot_token):
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(("secretbox:" + bot_token).encode()).digest())
    return Fernet(key)


def encrypt(value, bot_token):
    return _fernet(bot_token).encrypt(value.encode()).decode()


def decrypt(token_str, bot_token):
    return _fernet(bot_token).decrypt(token_str.encode()).decode()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    if len(sys.argv) != 3 or sys.argv[1] != "encrypt":
        raise SystemExit(__doc__)
    print(encrypt(sys.argv[2], os.environ["BOT_TOKEN"]))
