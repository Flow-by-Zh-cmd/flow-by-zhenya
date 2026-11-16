import json, time, unittest
from tgauth import validate_init_data, sign_init_data, AuthError

TOKEN = "123456:TEST-token"
USER = json.dumps({"id": 42, "first_name": "Иван", "username": "ivan"}, ensure_ascii=False, separators=(",", ":"))


def fields(**kw):
    f = {"query_id": "AAH", "user": USER, "auth_date": str(int(time.time()))}
    f.update(kw)
    return f


class T(unittest.TestCase):
    def test_valid(self):
        d = validate_init_data(sign_init_data(fields(), TOKEN), TOKEN)
        self.assertEqual(d["user"]["id"], 42)
        self.assertEqual(d["user"]["first_name"], "Иван")

    def test_wrong_token(self):
        with self.assertRaises(AuthError):
            validate_init_data(sign_init_data(fields(), "999:other"), TOKEN)

    def test_tampered(self):
        s = sign_init_data(fields(), TOKEN).replace("42", "43")
        with self.assertRaises(AuthError):
            validate_init_data(s, TOKEN)

    def test_missing(self):
        for s in ("", "user=x&auth_date=1", "garbage"):
            with self.assertRaises(AuthError):
                validate_init_data(s, TOKEN)

    def test_expired(self):
        s = sign_init_data(fields(auth_date=str(int(time.time()) - 2 * 86400)), TOKEN)
        with self.assertRaises(AuthError):
            validate_init_data(s, TOKEN)

    def test_no_user(self):
        f = fields(); f.pop("user")
        with self.assertRaises(AuthError):
            validate_init_data(sign_init_data(f, TOKEN), TOKEN)

    def test_known_vector(self):
        # независимый расчёт по алгоритму из документации Telegram
        import hmac, hashlib
        from urllib.parse import urlencode
        f = {"auth_date": "1700000000", "user": USER}
        dcs = f"auth_date=1700000000\nuser={USER}"
        key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        h = hmac.new(key, dcs.encode(), hashlib.sha256).hexdigest()
        d = validate_init_data(urlencode({**f, "hash": h}), TOKEN, now=1700000100)
        self.assertEqual(d["user"]["username"], "ivan")


if __name__ == "__main__":
    unittest.main()
