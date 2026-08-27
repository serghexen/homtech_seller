"""Долговечная read-only обработка сохранённых уведомлений Яндекс Маркета."""

from __future__ import annotations

import os
from typing import Callable
import uuid

from domains.fulfillment_service import observe_order_fulfillments
from domains.marketplace_orders_service import fetch_yandex_market_order
from domains.marketplace_sync_service import load_active_connection, save_order_snapshots


def webhook_lease_seconds() -> int:
    # Аренда переживает обычный сетевой запрос, но освобождается после падения worker-а.
    return max(60, min(int(os.getenv("YANDEX_MARKET_WEBHOOK_LEASE_SECONDS", "600")), 3600))


def webhook_max_attempts() -> int:
    # После ограниченного числа попыток событие уходит в dead-letter и требует внимания оператора.
    return max(1, min(int(os.getenv("YANDEX_MARKET_WEBHOOK_MAX_ATTEMPTS", "8")), 50))


def webhook_retry_delay_seconds(attempt_count: int) -> int:
    # Экспоненциальная пауза защищает API Маркета при временном сбое.
    return min(15 * (2 ** max(0, min(int(attempt_count), 16) - 1)), 3600)


def webhook_error_text(error: Exception) -> str:
    # Сохраняет короткую причину без токена и полного ответа внешнего API.
    detail = getattr(error, "detail", None)
    return str(detail or error or error.__class__.__name__)[:1000]


def build_yandex_market_webhook_processor(
    *,
    database_url: Callable[[], str],
    psycopg,
    processing_enabled: Callable[[], bool],
) -> Callable[[int], None]:
    """Создаёт общий processor для точечного вызова и периодического DB-worker."""

    def claim_event(event_id: int | None = None):
        # Атомарно арендует одно готовое событие; paused-записи до переключения не затрагиваются.
        lock_token = str(uuid.uuid4())
        id_filter = "AND event.id=%s" if event_id is not None else ""
        with psycopg.connect(database_url()) as connection:
            query_params = (
                (event_id, lock_token, webhook_lease_seconds())
                if event_id is not None
                else (lock_token, webhook_lease_seconds())
            )
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    WITH candidate AS (
                      SELECT event.id
                      FROM seller.yandex_webhook_events AS event
                      JOIN seller.marketplace_connections AS marketplace_connection
                        ON marketplace_connection.id=event.connection_id
                      WHERE event.processing_enabled_at_receive=true
                        AND marketplace_connection.status='active'
                        AND marketplace_connection.webhook_processing_enabled=true
                        AND (
                          (
                            event.processing_state IN ('received', 'failed')
                            AND event.processing_attempts < {webhook_max_attempts()}
                            AND event.next_attempt_at <= now()
                            AND (event.processing_locked_until IS NULL OR event.processing_locked_until <= now())
                          )
                          OR (
                            event.processing_state='processing'
                            AND event.processing_locked_until <= now()
                          )
                        )
                        {id_filter}
                      ORDER BY event.next_attempt_at, event.id
                      FOR UPDATE SKIP LOCKED
                      LIMIT 1
                    )
                    UPDATE seller.yandex_webhook_events AS event
                    SET processing_state='processing',
                        processing_attempts=event.processing_attempts + 1,
                        processing_lock_token=%s::uuid,
                        processing_locked_until=now() + (%s * interval '1 second'),
                        last_attempt_at=now(), last_error='', updated_at=now()
                    FROM candidate
                    WHERE event.id=candidate.id
                    RETURNING event.id, event.connection_id, event.campaign_id, event.order_id,
                              event.notification_type, event.processing_attempts
                    """,
                    query_params,
                )
                row = cursor.fetchone()
            connection.commit()
        return (row, lock_token) if row else (None, lock_token)

    def finish_without_order(event_id: int, lock_token: str) -> None:
        # Сервисное событие без orderId безопасно завершается без обращения к Маркету.
        with psycopg.connect(database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.yandex_webhook_events
                    SET processing_state='ignored', processed_at=now(), last_error='',
                        processing_lock_token=NULL, processing_locked_until=NULL, updated_at=now()
                    WHERE id=%s AND processing_lock_token=%s::uuid
                    """,
                    (event_id, lock_token),
                )
            connection.commit()

    def fail_event(event_id: int, lock_token: str, attempts: int, error: Exception) -> None:
        # Повторяет временно неуспешное событие, а последнюю попытку переводит в dead-letter.
        terminal = int(attempts) >= webhook_max_attempts()
        state = "dead" if terminal else "failed"
        delay = 0 if terminal else webhook_retry_delay_seconds(attempts)
        with psycopg.connect(database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.yandex_webhook_events
                    SET processing_state=%s, last_error=%s,
                        next_attempt_at=now() + (%s * interval '1 second'),
                        processed_at=CASE WHEN %s='dead' THEN now() ELSE NULL END,
                        processing_lock_token=NULL, processing_locked_until=NULL, updated_at=now()
                    WHERE id=%s AND processing_lock_token=%s::uuid
                    """,
                    (state, webhook_error_text(error), delay, state, event_id, lock_token),
                )
            connection.commit()

    def process_claimed_event(event: tuple, lock_token: str) -> None:
        # Загружает один заказ и готовит локальную выдачу; раскрытие и отправка ключей здесь отсутствуют.
        event_id = int(event[0])
        connection_id = int(event[1]) if event[1] is not None else 0
        campaign_id = str(event[2] or "").strip()
        order_id = str(event[3] or "").strip()
        attempts = int(event[5] or 1)
        try:
            if not order_id:
                finish_without_order(event_id, lock_token)
                return
            if not connection_id or not campaign_id:
                raise ValueError("Для Yandex webhook не найдено подключение магазина")
            if not order_id.isdigit() or not campaign_id.isdigit():
                raise ValueError("Yandex webhook содержит некорректные идентификаторы заказа или магазина")

            # Токен живёт только в памяти worker-а и не сохраняется в событии.
            with psycopg.connect(database_url()) as connection:
                connection_row = load_active_connection(connection, connection_id)
            (
                row_connection_id, provider_code, _name, _client_id, business_id,
                stored_campaign_id, token, _last_sync, *_connection_runtime,
            ) = connection_row
            if str(provider_code) != "yandex_market" or str(stored_campaign_id or "") != campaign_id:
                raise ValueError("Yandex webhook не соответствует подключенному магазину")
            if not str(business_id or "").isdigit():
                raise ValueError("У подключенного магазина не указан businessId")

            order = fetch_yandex_market_order(
                business_id=int(business_id),
                campaign_id=int(campaign_id),
                order_id=int(order_id),
                token=str(token),
            )
            with psycopg.connect(database_url()) as connection:
                saved_items = save_order_snapshots(
                    connection,
                    connection_id=int(row_connection_id),
                    provider_code="yandex_market",
                    rows=[order],
                )
                if saved_items < 1:
                    raise ValueError(f"Заказ Яндекс Маркета {order_id} не содержит сохраняемых позиций")
                observe_order_fulfillments(
                    connection,
                    connection_id=int(row_connection_id),
                    external_order_id=order_id,
                )
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE seller.yandex_webhook_events
                        SET processing_state='processed', processed_at=now(), last_error='',
                            processing_lock_token=NULL, processing_locked_until=NULL, updated_at=now()
                        WHERE id=%s AND processing_lock_token=%s::uuid
                        """,
                        (event_id, lock_token),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("Yandex webhook lease was lost before snapshot commit")
                connection.commit()
        except Exception as error:
            # HTTP-ответ уже отдан; ошибка остаётся в очереди и не запускает внешнюю выдачу.
            fail_event(event_id, lock_token, attempts, error)

    def process_event(event_id: int) -> None:
        # Точечный путь пригодится для безопасного переигрывания события оператором.
        if not processing_enabled():
            return
        event, lock_token = claim_event(int(event_id))
        if event:
            process_claimed_event(event, lock_token)

    def process_pending_events(batch_size: int = 25) -> int:
        # Периодически подбирает готовые события, но kill switch останавливает новые claims.
        if not processing_enabled():
            return 0
        processed_count = 0
        for _index in range(max(1, min(100, int(batch_size or 25)))):
            event, lock_token = claim_event()
            if not event:
                break
            process_claimed_event(event, lock_token)
            processed_count += 1
        return processed_count

    process_event.process_pending_events = process_pending_events  # type: ignore[attr-defined]
    process_event.claim_event = claim_event  # type: ignore[attr-defined]
    process_event.process_claimed_event = process_claimed_event  # type: ignore[attr-defined]
    return process_event
