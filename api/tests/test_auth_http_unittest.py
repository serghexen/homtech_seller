"""HTTP-контракт локальной авторизации Seller без подключения к БД."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app import app


class AuthHttpTests(unittest.TestCase):
    def test_logout_clears_cookie_and_returns_no_content(self) -> None:
        # Не допускает возврата служебного Response без HTTP-статуса и повторного входа после обновления страницы.
        response = TestClient(app).post("/auth/logout")

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        cookie = response.headers["set-cookie"]
        self.assertIn('seller_session=""', cookie)
        self.assertIn("Max-Age=0", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn("HttpOnly", cookie)


if __name__ == "__main__":
    unittest.main()
