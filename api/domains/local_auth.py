"""Примитивы независимой локальной авторизации HomTech Seller."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt


PASSWORD_ITERATIONS = 310_000
PASSWORD_SCHEME = "seller_pbkdf2_sha256"
PASSWORD_SALT_BYTES = 16
JWT_AUDIENCE = "homtech-seller"
JWT_ISSUER = "homtech-seller"
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@dataclass(frozen=True)
class AuthenticatedUser:
    """Идентичность пользователя после проверки локальной сессии."""

    user_id: int
    email: str


def normalize_email(value: str) -> str:
    # Приводит email к стабильному ключу аккаунта и отсеивает явно некорректный ввод.
    email = str(value or "").strip().lower()
    if not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Invalid email")
    return email


def hash_password(password: str) -> str:
    # Хеширует пароль PBKDF2 с индивидуальной солью, чтобы пароль никогда не хранился в базе открыто.
    salt = secrets.token_bytes(PASSWORD_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return "$".join(
        (
            PASSWORD_SCHEME,
            str(PASSWORD_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )
    )


def verify_password(password: str, stored_hash: str) -> bool:
    # Сверяет пароль с сохранённым хешем constant-time сравнением и безопасно отклоняет старый/битый формат.
    try:
        scheme, iterations_text, salt_text, digest_text = str(stored_hash or "").split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        iterations = int(iterations_text)
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
    except (TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def create_access_token(*, user_id: int, email: str, secret: str, ttl_minutes: int) -> str:
    # Выпускает короткую сессию только для Seller с обязательными issuer и audience.
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    return jwt.encode(
        {
            "sub": str(user_id),
            "email": email,
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "exp": expires_at,
        },
        secret,
        algorithm="HS256",
    )


def decode_access_token(token: str, *, secret: str) -> AuthenticatedUser:
    # Проверяет подпись, срок, issuer и audience, не принимая токены других приложений HomTech.
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"], audience=JWT_AUDIENCE, issuer=JWT_ISSUER)
        return AuthenticatedUser(user_id=int(payload["sub"]), email=normalize_email(str(payload["email"])))
    except Exception as exc:
        raise ValueError("Invalid token") from exc
