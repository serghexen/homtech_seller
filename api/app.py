"""Минимальная точка входа независимого API HomTech Seller."""

from __future__ import annotations

import os

import psycopg
from fastapi import FastAPI, HTTPException


app = FastAPI(title="HomTech Seller API", version="0.1.0")


def check_database() -> None:
    # Проверяет доступность отдельной БД без изменения схемы или данных.
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    with psycopg.connect(database_url, connect_timeout=3) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")


@app.get("/health")
def health() -> dict[str, str]:
    # Отдаёт готовность API только после проверки соединения с его собственной БД.
    try:
        check_database()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database is unavailable") from exc
    return {"status": "ok", "service": "homtech-seller-api"}
