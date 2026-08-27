"""Транзакционная постановка повторной публикации целевого остатка Ozon."""

from __future__ import annotations

import os


def stock_republish_delay_seconds() -> int:
    return max(0, min(int(os.getenv("OZON_STOCK_REPUBLISH_DELAY_SECONDS", "3")), 60))


def enqueue_ozon_stock_publication(cursor, *, fulfillment_id: int) -> None:
    cursor.execute(
        """
        INSERT INTO seller.ozon_stock_outbound_jobs(fulfillment_id, next_attempt_at)
        VALUES (%s, now() + (%s * interval '1 second'))
        ON CONFLICT (fulfillment_id) WHERE job_kind='fulfillment' DO NOTHING
        """,
        (int(fulfillment_id), stock_republish_delay_seconds()),
    )

