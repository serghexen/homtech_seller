"""Проверки локальной авторизации Seller без подключения к БД."""

from __future__ import annotations

import unittest

from domains.local_auth import create_access_token, decode_access_token, hash_password, normalize_email, verify_password


class LocalAuthTests(unittest.TestCase):
    def test_normalizes_email(self) -> None:
        # Фиксирует единый ключ пользователя, чтобы регистрация и вход находили один аккаунт.
        self.assertEqual(normalize_email("  User@Example.COM "), "user@example.com")

    def test_rejects_invalid_email(self) -> None:
        # Не даёт создать локальный аккаунт с некорректным идентификатором.
        with self.assertRaises(ValueError):
            normalize_email("not-an-email")

    def test_hashes_and_verifies_password(self) -> None:
        # Проверяет, что хеш подходит только к исходному паролю и не совпадает с открытым значением.
        password_hash = hash_password("Strong-local-password")
        self.assertNotEqual(password_hash, "Strong-local-password")
        self.assertTrue(verify_password("Strong-local-password", password_hash))
        self.assertFalse(verify_password("another-password", password_hash))

    def test_accepts_only_seller_token(self) -> None:
        # Проверяет обязательные ограничения токена, чтобы не принять JWT из другого приложения.
        token = create_access_token(user_id=11, email="owner@example.com", secret="x" * 32, ttl_minutes=30)
        user = decode_access_token(token, secret="x" * 32)
        self.assertEqual(user.user_id, 11)
        self.assertEqual(user.email, "owner@example.com")
        with self.assertRaises(ValueError):
            decode_access_token(token, secret="y" * 32)
