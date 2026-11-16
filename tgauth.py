"""Проверка Telegram WebApp initData (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app)."""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


class AuthError(Exception):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400, now: float | None = None) -> dict:
    """Возвращает dict с полями initData (user — уже распарсен) или бросает AuthError."""
    if not init_data:
        raise AuthError("no initData")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise AuthError("malformed initData")
    data = dict(pairs)
    if len(data) != len(pairs):
        raise AuthError("duplicate keys")
    received = data.pop("hash", None)
    if not received:
        raise AuthError("no hash")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise AuthError("bad hash")
    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        raise AuthError("bad auth_date")
    now = time.time() if now is None else now
    if max_age and (now - auth_date > max_age or auth_date - now > 300):
        raise AuthError("initData expired")
    try:
        user = json.loads(data.get("user", "null"))
    except ValueError:
        raise AuthError("bad user")
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        raise AuthError("no user")
    data["user"] = user
    return data


def sign_init_data(fields: dict, bot_token: str) -> str:
    """Для тестов: формирует корректную строку initData."""
    from urllib.parse import urlencode
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    h = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": h})
