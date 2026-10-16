import base64
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.password = "test-only-password-83"
        self.auth = patch.object(main, "_app_password", self.password)
        self.username = patch.object(main, "_app_username", "openshorts")
        self.auth.start()
        self.username.start()
        self.client = TestClient(main.app, base_url="https://testserver", follow_redirects=False)

    def tearDown(self):
        self.client.close()
        self.username.stop()
        self.auth.stop()

    def login(self):
        return self.client.post("/api/login", json={"username": "openshorts", "password": self.password})

    def test_browser_uses_login_page_without_native_auth_popup(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")
        self.assertNotIn("www-authenticate", response.headers)
        self.assertEqual(self.client.get("/login").status_code, 200)
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/api/config").status_code, 401)

    def test_bad_credentials_including_unicode_return_clear_error(self):
        response = self.client.post("/api/login", json={"username": "openshorts", "password": "كلمة خاطئة"})
        self.assertEqual(response.status_code, 401)
        self.assertIn("غير صحيحة", response.json()["detail"])
        self.assertNotIn("www-authenticate", response.headers)
        self.assertNotIn(main.SESSION_COOKIE, self.client.cookies)

    def test_session_unlocks_site_even_if_browser_sends_stale_basic_credentials(self):
        response = self.login()
        self.assertEqual(response.status_code, 200)
        cookie = response.headers["set-cookie"].lower()
        for flag in ["httponly", "secure", "samesite=lax", "path=/"]:
            self.assertIn(flag, cookie)
        self.assertNotIn(self.password, self.client.cookies[main.SESSION_COOKIE])
        stale = base64.b64encode(b"openshorts:outdated-password").decode()
        self.assertEqual(self.client.get("/", headers={"Authorization": "Basic " + stale}).status_code, 200)
        self.assertEqual(self.client.get("/api/config").status_code, 200)
        self.assertEqual(self.client.post("/logout").status_code, 303)
        self.assertEqual(self.client.get("/api/config").status_code, 401)

    def test_tampering_expiry_and_password_changes_invalidate_sessions(self):
        self.login()
        with patch("main.time.time", return_value=time.time() + main.SESSION_SECONDS + 2):
            self.assertEqual(self.client.get("/api/config").status_code, 401)
        self.login()
        with patch.object(main, "_app_password", "rotated-password"):
            self.assertEqual(self.client.get("/api/config").status_code, 401)
        token = self.client.cookies[main.SESSION_COOKIE]
        corrupted = token[:-1] + ("0" if token[-1] != "0" else "1")
        self.client.cookies.clear()
        self.client.cookies.set(main.SESSION_COOKIE, corrupted, domain="testserver.local", path="/")
        self.assertEqual(self.client.get("/api/config").status_code, 401)

    def test_api_clients_can_still_use_basic_auth(self):
        encoded = base64.b64encode(("openshorts:" + self.password).encode()).decode()
        response = self.client.get("/api/config", headers={"Authorization": "Basic " + encoded})
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
