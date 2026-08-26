"""Долговечная и не допускающая слепых повторов отправка цифровых товаров в Яндекс Маркет."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import urllib.error
import urllib.request
from uuid import UUID

from domains.marketplace_connection_verification import YANDEX_MARKET_BASE_URL, _ssl_context
from domains.marketplace_sync_service import credentials_secret


@dataclass(frozen=True)
class OutboundPayload:
    job_id: int
    lock_token: UUID
    fulfillment_id: int
    campaign_id: int
    order_id: int
    item_id: int
    token: str
    codes: tuple[str, ...]
    instruction: str


class YandexOutboundError(RuntimeError):
    def __init__(self, message: str, *, definite: bool) -> None:
        super().__init__(message)
        self.definite = definite


def yandex_outbound_enabled() -> bool:
    return str(os.getenv("SELLER_YANDEX_OUTBOUND_ENABLED", "false")).strip().lower() in {"1", "true", "yes"}


def key_pool_secret() -> str:
    value = str(os.getenv("SELLER_KEY_POOL_SECRET", "")).strip()
    if len(value) < 32:
        raise RuntimeError("SELLER_KEY_POOL_SECRET is not configured")
    return value


def outbound_timeout_seconds() -> int:
    return max(3, min(int(os.getenv("YANDEX_MARKET_OUTBOUND_TIMEOUT_SECONDS", "20")), 60))


def send_yandex_digital_goods(payload: OutboundPayload) -> None:
    """Единственная сетевая граница; коды существуют только в памяти worker-а."""

    body = json.dumps(
        {
            "items": [{
                "id": payload.item_id,
                "codes": list(payload.codes),
                "slip": payload.instruction,
                "activate_till": "2099-12-31",
            }]
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{YANDEX_MARKET_BASE_URL}/v2/campaigns/{payload.campaign_id}/orders/{payload.order_id}/deliverDigitalGoods",
        data=body,
        method="POST",
        headers={"Api-Key": payload.token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=outbound_timeout_seconds(), context=_ssl_context(),
        ) as response:
            response.read(1024)
    except urllib.error.HTTPError as exc:
        # 4xx означает однозначный отказ до принятия запроса; 5xx мог возникнуть уже после обработки.
        raise YandexOutboundError(
            f"Яндекс Маркет отклонил выдачу: HTTP {exc.code}", definite=400 <= int(exc.code) < 500,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise YandexOutboundError("Результат отправки в Яндекс Маркет неизвестен", definite=False) from exc


class YandexOutboundProcessor:
    def __init__(self, *, database_url, psycopg, sender=send_yandex_digital_goods) -> None:
        self._database_url = database_url
        self._psycopg = psycopg
        self._sender = sender

    def recover_stale(self) -> tuple[int, int]:
        """До сети preparing безопасно возвращается в очередь, после sending повтор запрещён."""

        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.fulfillment_outbound_jobs
                    SET state='queued', lock_token=NULL, locked_until=NULL,
                        last_error='Worker был перезапущен до внешней отправки', updated_at=now()
                    WHERE state='preparing' AND locked_until < now()
                    """
                )
                requeued = cursor.rowcount
                cursor.execute(
                    """
                    WITH stale AS (
                      UPDATE seller.fulfillment_outbound_jobs
                      SET state='unknown', unknown_at=now(), lock_token=NULL, locked_until=NULL,
                          last_error='Worker был перезапущен после начала отправки; повтор запрещён', updated_at=now()
                      WHERE state='sending' AND locked_until < now()
                      RETURNING fulfillment_id
                    )
                    UPDATE seller.order_fulfillments AS fulfillment
                    SET status='unknown', last_error='Результат внешней отправки неизвестен; требуется сверка', updated_at=now()
                    WHERE fulfillment.id IN (SELECT fulfillment_id FROM stale) AND fulfillment.status='sending'
                    """
                )
                unknown = cursor.rowcount
            return int(requeued), int(unknown)

    def process_pending_jobs(self, limit: int = 5) -> int:
        if not yandex_outbound_enabled():
            return 0
        processed = 0
        for _ in range(max(1, min(int(limit), 50))):
            payload = self._claim_and_prepare()
            if payload is None:
                break
            processed += 1
            try:
                self._sender(payload)
            except YandexOutboundError as exc:
                self._finish(payload, "failed" if exc.definite else "unknown", str(exc))
            except Exception:
                # Любое исключение после фиксации sending является неопределённым исходом.
                self._finish(payload, "unknown", "Результат отправки в Яндекс Маркет неизвестен")
            else:
                self._finish(payload, "submitted", "")
        return processed

    def _claim_and_prepare(self) -> OutboundPayload | None:
        lock_seconds = 120
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT job.id
                    FROM seller.fulfillment_outbound_jobs AS job
                    JOIN seller.order_fulfillments AS fulfillment ON fulfillment.id=job.fulfillment_id
                    JOIN seller.marketplace_connections AS market ON market.id=fulfillment.connection_id
                    WHERE job.state='queued' AND market.status='active'
                      AND market.provider_code='yandex_market' AND market.fulfillment_outbound_enabled=true
                    ORDER BY job.queued_at, job.id
                    FOR UPDATE OF job SKIP LOCKED
                    LIMIT 1
                    """
                )
                row = cursor.fetchone()
                if not row:
                    return None
                job_id = int(row[0])
                cursor.execute(
                    """
                    UPDATE seller.fulfillment_outbound_jobs
                    SET state='preparing', attempt_count=attempt_count + 1,
                        lock_token=gen_random_uuid(), locked_until=now() + (%s * interval '1 second'), updated_at=now()
                    WHERE id=%s AND state='queued'
                    RETURNING lock_token
                    """,
                    (lock_seconds, job_id),
                )
                lock_token = cursor.fetchone()[0]

            try:
                credential_key = credentials_secret()
            except RuntimeError as exc:
                self._fail_before_send(connection, job_id, lock_token, str(exc))
                return None

            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT fulfillment.id, fulfillment.external_order_id, fulfillment.external_item_id,
                           fulfillment.requested_quantity, fulfillment.status, fulfillment.reservation_ref,
                           fulfillment.delivery_source, fulfillment.support_message_snapshot,
                           market.campaign_id, market.status, market.fulfillment_outbound_enabled,
                           pgp_sym_decrypt(market.token_ciphertext, %s),
                           CASE WHEN settings.connection_id IS NOT NULL
                             THEN settings.activation_instruction
                             ELSE COALESCE(imported_settings.activation_instruction, '') END,
                           item.normalized_status, item.delivery_type
                    FROM seller.fulfillment_outbound_jobs AS job
                    JOIN seller.order_fulfillments AS fulfillment ON fulfillment.id=job.fulfillment_id
                    JOIN seller.marketplace_connections AS market ON market.id=fulfillment.connection_id
                    JOIN seller.order_items AS item
                      ON item.connection_id=fulfillment.connection_id
                     AND item.external_order_id=fulfillment.external_order_id
                     AND item.external_item_id=fulfillment.external_item_id
                    LEFT JOIN seller.product_card_settings AS settings
                      ON settings.connection_id=fulfillment.connection_id
                     AND settings.external_product_id=fulfillment.offer_id
                    LEFT JOIN seller.yandex_product_settings_snapshot AS imported_settings
                      ON imported_settings.connection_id=fulfillment.connection_id
                     AND imported_settings.external_product_id=fulfillment.offer_id
                    WHERE job.id=%s AND job.state='preparing' AND job.lock_token=%s
                    FOR UPDATE OF job, fulfillment
                    """,
                    (credential_key, job_id, lock_token),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                fulfillment_id = int(row[0])
                try:
                    order_id, item_id = int(str(row[1])), int(str(row[2]))
                    quantity, campaign_id = int(row[3]), int(str(row[8]))
                except (TypeError, ValueError):
                    self._fail_before_send(connection, job_id, lock_token, "Яндекс вернул неподдерживаемый идентификатор заказа")
                    return None
                delivery_source = str(row[6] or "")
                support_message = str(row[7] or "").strip()
                instruction = str(row[12] or "").strip()
                validation_error = ""
                if not yandex_outbound_enabled() or str(row[9]) != "active" or not bool(row[10]):
                    validation_error = "Внешняя отправка выключена"
                elif str(row[4]) != "reserved":
                    validation_error = f"Статус {row[4]} не допускает отправку"
                elif str(row[13]) != "processing" or str(row[14] or "").strip().upper() != "DIGITAL":
                    validation_error = "Заказ уже не является обрабатываемым цифровым заказом"
                elif not instruction:
                    validation_error = "Не заполнена инструкция покупателю"
                if validation_error:
                    self._fail_before_send(connection, job_id, lock_token, validation_error)
                    return None
                key_ids: list[int] = []
                if delivery_source == "support_message":
                    if not support_message:
                        self._fail_before_send(connection, job_id, lock_token, "Не найден подготовленный снимок сообщения поддержки")
                        return None
                    codes = tuple(support_message for _ in range(quantity))
                    material_hashes = [hashlib.sha256(support_message.encode()).hexdigest()] * quantity
                else:
                    try:
                        secret = key_pool_secret()
                    except RuntimeError as exc:
                        self._fail_before_send(connection, job_id, lock_token, str(exc))
                        return None
                    cursor.execute(
                        """
                        SELECT key.id, pgp_sym_decrypt(key.code_ciphertext, %s), key.code_hash
                        FROM seller.fulfillment_key_reservations AS reservation
                        JOIN seller.marketplace_keys AS key ON key.id=reservation.key_id
                        WHERE reservation.fulfillment_id=%s AND reservation.state='reserved'
                          AND key.status='reserved' AND key.issued_order_ref=%s
                        ORDER BY reservation.id
                        FOR UPDATE OF reservation, key
                        """,
                        (secret, fulfillment_id, str(row[5])),
                    )
                    key_rows = cursor.fetchall()
                    if len(key_rows) != quantity:
                        self._fail_before_send(connection, job_id, lock_token, "Зарезервирован неполный комплект ключей")
                        return None
                    key_ids = [int(key[0]) for key in key_rows]
                    codes = tuple(str(key[1]) for key in key_rows)
                    material_hashes = [str(key[2]) for key in key_rows]
                fingerprint = hashlib.sha256(
                    f"{campaign_id}:{order_id}:{item_id}:{delivery_source}:{'|'.join(material_hashes)}:{hashlib.sha256(instruction.encode()).hexdigest()}".encode()
                ).hexdigest()
                if key_ids:
                    cursor.execute(
                        """
                        UPDATE seller.marketplace_keys SET status='sending', updated_at=now()
                        WHERE id=ANY(%s) AND status='reserved'
                        """,
                        (key_ids,),
                    )
                    if cursor.rowcount != quantity:
                        raise RuntimeError("Не удалось зафиксировать полный комплект перед отправкой")
                cursor.execute(
                    """
                    UPDATE seller.order_fulfillments
                    SET status='sending', last_error='', updated_at=now()
                    WHERE id=%s AND status='reserved'
                    """,
                    (fulfillment_id,),
                )
                cursor.execute(
                    """
                    UPDATE seller.fulfillment_outbound_jobs
                    SET state='sending', request_fingerprint=%s, sending_at=now(), updated_at=now()
                    WHERE id=%s AND state='preparing' AND lock_token=%s
                    """,
                    (fingerprint, job_id, lock_token),
                )
                cursor.execute(
                    """
                    INSERT INTO seller.fulfillment_events(fulfillment_id, event_type, from_status, to_status)
                    VALUES (%s,'outbound_started','reserved','sending')
                    """,
                    (fulfillment_id,),
                )
            connection.commit()  # Граница неопределённости фиксируется строго до HTTP-вызова.
            return OutboundPayload(
                job_id, lock_token, fulfillment_id, campaign_id, order_id, item_id,
                str(row[11]), codes, instruction,
            )

    @staticmethod
    def _fail_before_send(connection, job_id: int, lock_token: UUID, message: str) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE seller.fulfillment_outbound_jobs
                SET state='failed', failed_at=now(), last_error=%s,
                    lock_token=NULL, locked_until=NULL, updated_at=now()
                WHERE id=%s AND state='preparing' AND lock_token=%s
                """,
                (message[:1000], job_id, lock_token),
            )
        connection.commit()

    def _finish(self, payload: OutboundPayload, state: str, message: str) -> None:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id FROM seller.fulfillment_outbound_jobs
                    WHERE id=%s AND state='sending' AND lock_token=%s FOR UPDATE
                    """,
                    (payload.job_id, payload.lock_token),
                )
                if not cursor.fetchone():
                    return
                if state == "submitted":
                    cursor.execute(
                        """
                        UPDATE seller.fulfillment_outbound_jobs
                        SET state='submitted', submitted_at=now(), last_error='', lock_token=NULL, locked_until=NULL, updated_at=now()
                        WHERE id=%s
                        """,
                        (payload.job_id,),
                    )
                    cursor.execute(
                        """
                        UPDATE seller.order_fulfillments
                        SET status='submitted', submitted_at=now(), last_error='', updated_at=now()
                        WHERE id=%s AND status='sending'
                        """,
                        (payload.fulfillment_id,),
                    )
                    event_type, to_status = "outbound_submitted", "submitted"
                elif state == "failed":
                    cursor.execute(
                        """
                        UPDATE seller.fulfillment_outbound_jobs
                        SET state='failed', failed_at=now(), last_error=%s, lock_token=NULL, locked_until=NULL, updated_at=now()
                        WHERE id=%s
                        """,
                        (message[:1000], payload.job_id),
                    )
                    cursor.execute(
                        """
                        UPDATE seller.marketplace_keys AS key SET status='reserved', updated_at=now()
                        WHERE key.id IN (
                          SELECT key_id FROM seller.fulfillment_key_reservations
                          WHERE fulfillment_id=%s AND state='reserved'
                        ) AND key.status='sending'
                        """,
                        (payload.fulfillment_id,),
                    )
                    cursor.execute(
                        """
                        UPDATE seller.order_fulfillments SET status='reserved', last_error=%s, updated_at=now()
                        WHERE id=%s AND status='sending'
                        """,
                        (message[:1000], payload.fulfillment_id),
                    )
                    event_type, to_status = "outbound_rejected", "reserved"
                else:
                    cursor.execute(
                        """
                        UPDATE seller.fulfillment_outbound_jobs
                        SET state='unknown', unknown_at=now(), last_error=%s, lock_token=NULL, locked_until=NULL, updated_at=now()
                        WHERE id=%s
                        """,
                        (message[:1000], payload.job_id),
                    )
                    cursor.execute(
                        """
                        UPDATE seller.order_fulfillments SET status='unknown', last_error=%s, updated_at=now()
                        WHERE id=%s AND status='sending'
                        """,
                        (message[:1000], payload.fulfillment_id),
                    )
                    event_type, to_status = "outbound_unknown", "unknown"
                cursor.execute(
                    """
                    INSERT INTO seller.fulfillment_events(fulfillment_id, event_type, from_status, to_status, details)
                    VALUES (%s,%s,'sending',%s,jsonb_build_object('message', (%s)::text))
                    """,
                    (payload.fulfillment_id, event_type, to_status, message[:1000]),
                )


def build_yandex_outbound_processor(*, database_url, psycopg) -> YandexOutboundProcessor:
    return YandexOutboundProcessor(database_url=database_url, psycopg=psycopg)
