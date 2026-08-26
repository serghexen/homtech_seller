"""Транзакционная постановка публикации остатка без сетевых зависимостей."""

from __future__ import annotations

import os


def stock_republish_delay_seconds() -> int:
    """Даёт Маркету закончить приём цифрового кода перед возвратом целевого остатка."""

    return max(0, min(int(os.getenv("YANDEX_MARKET_STOCK_REPUBLISH_DELAY_SECONDS", "3")), 60))


def enqueue_yandex_stock_publication(cursor, *, fulfillment_id: int) -> None:
    """Создаёт ровно одно задание в той же транзакции, что и подтверждённая отправка."""

    cursor.execute(
        """
        INSERT INTO seller.yandex_stock_outbound_jobs(fulfillment_id, next_attempt_at)
        VALUES (%s, now() + (%s * interval '1 second'))
        ON CONFLICT (fulfillment_id) DO NOTHING
        """,
        (int(fulfillment_id), stock_republish_delay_seconds()),
    )
