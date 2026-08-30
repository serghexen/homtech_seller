"""Проверяет понятные пользователю сообщения локальной авторизации."""

from __future__ import annotations

import unittest

from fastapi import HTTPException, Response

from app import AuthOut, LoginIn, RegisterIn, login


class AuthMessageTests(unittest.TestCase):
    def test_registration_does_not_require_workspace_name(self) -> None:
        payload = RegisterIn(email="user@example.com", password="1", display_name="Сергей")

        self.assertEqual(payload.workspace_name, "")
        self.assertEqual(payload.password, "1")

    def test_auth_contract_does_not_expose_workspace_wide_tariff(self) -> None:
        self.assertNotIn("access", AuthOut.model_fields)

    def test_invalid_login_email_has_russian_message(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            login(LoginIn(email="bad", password="wrong"), Response())

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(raised.exception.detail, "Неверный email или пароль")


if __name__ == "__main__":
    unittest.main()
