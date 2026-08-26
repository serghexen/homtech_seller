"""Идемпотентная публикация заданного остатка после подтверждённой выдачи."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
import urllib.error
import urllib.request
from uuid import UUID

from domains.marketplace_connection_verification import YANDEX_MARKET_BASE_URL, _ssl_context
from domains.marketplace_sync_service import credentials_secret


@dataclass(frozen=True)
class StockOutboundPayload:
    job_id: int
    lock_token: UUID
    connection_id: int
    external_product_id: str
    campaign_id: int
    token: str
    target_stock: int


class YandexStockOutboundError(RuntimeError):
    def __init__(self, message: str, *, definite: bool) -> None:
        super().__init__(message)
        self.definite = definite


def yandex_stock_outbound_enabled() -> bool:
    return str(os.getenv("SELLER_YANDEX_STOCK_OUTBOUND_ENABLED", "false")).strip().lower() in {
        "1", "true", "yes",
    }


def stock_outbound_timeout_seconds() -> int:
    return max(3, min(int(os.getenv("YANDEX_MARKET_STOCK_OUTBOUND_TIMEOUT_SECONDS", "20")), 60))


def stock_retry_delay_seconds(attempt_count: int) -> int:
    return min(15 * (2 ** max(0, min(int(attempt_count), 10) - 1)), 3600)


def calculate_effective_stock(
    manual_stock: int,
    sales_limit: int | None,
    daily_extra: int,
    baseline_used: int,
    baseline_reserved: int,
    seller_used: int,
    seller_reserved: int,
) -> int:
    """Повторяет CRM: заданный остаток ограничивается оставшейся дневной квотой."""

    target = max(0, int(manual_stock or 0))
    if sales_limit is None:
        return target
    remaining = max(
        0,
        int(sales_limit) + max(0, int(daily_extra or 0))
        - max(0, int(baseline_used or 0))
        - max(0, int(baseline_reserved or 0))
        - max(0, int(seller_used or 0))
        - max(0, int(seller_reserved or 0)),
    )
    return min(target, remaining)


def send_yandex_stock(payload: StockOutboundPayload) -> None:
    updated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    body = json.dumps({
        "skus": [{
            "sku": payload.external_product_id,
            "items": [{"count": payload.target_stock, "updatedAt": updated_at}],
        }],
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{YANDEX_MARKET_BASE_URL}/v2/campaigns/{payload.campaign_id}/offers/stocks",
        data=body,
        method="PUT",
        headers={"Api-Key": payload.token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=stock_outbound_timeout_seconds(), context=_ssl_context(),
        ) as response:
            response.read(1024)
    except urllib.error.HTTPError as exc:
        definite = 400 <= int(exc.code) < 500 and int(exc.code) != 429
        raise YandexStockOutboundError(
            f"Яндекс Маркет отклонил остаток: HTTP {exc.code}", definite=definite,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # PUT остатка идемпотентен: неизвестный исход можно безопасно повторить тем же актуальным значением.
        raise YandexStockOutboundError("Не удалось подтвердить публикацию остатка", definite=False) from exc


class YandexStockOutboundProcessor:
    def __init__(self, *, database_url, psycopg, sender=send_yandex_stock) -> None:
        self._database_url = database_url
        self._psycopg = psycopg
        self._sender = sender

    def recover_stale(self) -> int:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.yandex_stock_outbound_jobs
                    SET state='queued', lock_token=NULL, locked_until=NULL,
                        next_attempt_at=now(), last_error='Worker был перезапущен; остаток будет опубликован повторно',
                        updated_at=now()
                    WHERE state IN ('preparing','sending') AND locked_until < now()
                    """
                )
                recovered = cursor.rowcount
            connection.commit()
        return int(recovered)

    def process_pending_jobs(self, limit: int = 5) -> int:
        if not yandex_stock_outbound_enabled():
            return 0
        processed = 0
        for _index in range(max(1, min(int(limit), 50))):
            payload = self._claim_and_prepare()
            if payload is None:
                break
            processed += 1
            try:
                self._sender(payload)
            except YandexStockOutboundError as exc:
                self._finish_failure(payload, str(exc), definite=exc.definite)
            except Exception:
                self._finish_failure(payload, "Не удалось подтвердить публикацию остатка", definite=False)
            else:
                self._finish_success(payload)
        return processed

    def _claim_and_prepare(self) -> StockOutboundPayload | None:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT job.id
                    FROM seller.yandex_stock_outbound_jobs AS job
                    LEFT JOIN seller.order_fulfillments AS fulfillment ON fulfillment.id=job.fulfillment_id
                    JOIN seller.marketplace_connections AS market
                      ON market.id=COALESCE(job.connection_id, fulfillment.connection_id)
                    WHERE job.state='queued' AND job.next_attempt_at <= now()
                      AND job.attempt_count < job.max_attempts
                      AND market.status='active' AND market.provider_code='yandex_market'
                      AND market.stock_outbound_enabled=true
                    ORDER BY job.next_attempt_at, job.id
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
                    UPDATE seller.yandex_stock_outbound_jobs
                    SET state='preparing', attempt_count=attempt_count + 1,
                        lock_token=gen_random_uuid(), locked_until=now() + interval '120 seconds', updated_at=now()
                    WHERE id=%s AND state='queued'
                    RETURNING lock_token
                    """,
                    (job_id,),
                )
                lock_token = cursor.fetchone()[0]
            try:
                secret = credentials_secret()
            except RuntimeError as exc:
                self._finish_failure_before_send(connection, job_id, lock_token, str(exc))
                return None
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COALESCE(job.connection_id, fulfillment.connection_id),
                           COALESCE(job.external_product_id, fulfillment.offer_id),
                           job.job_kind, fulfillment.status,
                           market.campaign_id, market.status, market.stock_outbound_enabled,
                           pgp_sym_decrypt(market.token_ciphertext, %s),
                           job.requested_stock,
                           local_settings.connection_id IS NOT NULL,
                           COALESCE(local_settings.manual_stock_limit, imported.manual_stock_limit),
                           CASE WHEN local_settings.connection_id IS NOT NULL
                             THEN local_settings.sales_limit ELSE imported.sales_limit END,
                           CASE WHEN local_settings.connection_id IS NOT NULL
                                      AND local_settings.sales_limit_day=(now() AT TIME ZONE 'Europe/Moscow')::date
                             THEN local_settings.sales_limit_daily_extra
                             WHEN local_settings.connection_id IS NULL
                                      AND imported.sales_limit_day=(now() AT TIME ZONE 'Europe/Moscow')::date
                             THEN imported.sales_limit_daily_extra ELSE 0 END,
                           CASE WHEN imported.sales_limit_day=(now() AT TIME ZONE 'Europe/Moscow')::date
                             THEN imported.sales_limit_used ELSE 0 END,
                           CASE WHEN imported.sales_limit_day=(now() AT TIME ZONE 'Europe/Moscow')::date
                             THEN imported.sales_limit_reserved ELSE 0 END,
                           CASE WHEN imported.sales_limit_day=(now() AT TIME ZONE 'Europe/Moscow')::date
                             THEN imported.imported_at
                             ELSE ((now() AT TIME ZONE 'Europe/Moscow')::date::timestamp AT TIME ZONE 'Europe/Moscow') END
                    FROM seller.yandex_stock_outbound_jobs AS job
                    LEFT JOIN seller.order_fulfillments AS fulfillment ON fulfillment.id=job.fulfillment_id
                    JOIN seller.marketplace_connections AS market
                      ON market.id=COALESCE(job.connection_id, fulfillment.connection_id)
                    LEFT JOIN seller.product_card_settings AS local_settings
                      ON local_settings.connection_id=COALESCE(job.connection_id, fulfillment.connection_id)
                     AND local_settings.external_product_id=COALESCE(job.external_product_id, fulfillment.offer_id)
                    LEFT JOIN seller.yandex_product_settings_snapshot AS imported
                      ON imported.connection_id=COALESCE(job.connection_id, fulfillment.connection_id)
                     AND imported.external_product_id=COALESCE(job.external_product_id, fulfillment.offer_id)
                    WHERE job.id=%s AND job.state='preparing' AND job.lock_token=%s
                    FOR UPDATE OF job
                    """,
                    (secret, job_id, lock_token),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                connection_id, product_id = int(row[0]), str(row[1] or "").strip()
                job_kind = str(row[2] or "fulfillment")
                configured_stock = row[8] if job_kind == "manual" else row[10]
                validation_error = ""
                if not yandex_stock_outbound_enabled() or str(row[5]) != "active" or not bool(row[6]):
                    validation_error = "Публикация остатков выключена"
                elif job_kind == "fulfillment" and str(row[3]) not in {"submitted", "delivered"}:
                    validation_error = "Остаток публикуется только после подтверждённой отправки"
                elif not product_id or configured_stock is None:
                    validation_error = "Для товара не указан заданный остаток"
                try:
                    campaign_id = int(str(row[4]))
                except (TypeError, ValueError):
                    campaign_id = 0
                if campaign_id <= 0:
                    validation_error = "У магазина не указан campaignId"
                if validation_error:
                    self._finish_failure_before_send(connection, job_id, lock_token, validation_error)
                    return None
                cutoff = row[15]
                cursor.execute(
                    """
                    SELECT
                      COALESCE(SUM(requested_quantity) FILTER (
                        WHERE status='delivered' AND delivered_at >= %s
                      ), 0),
                      COALESCE(SUM(requested_quantity) FILTER (
                        WHERE status IN ('reserved','sending','submitted','unknown')
                          AND created_at >= %s
                      ), 0)
                    FROM seller.order_fulfillments
                    WHERE connection_id=%s AND offer_id=%s
                    """,
                    (cutoff, cutoff, connection_id, product_id),
                )
                seller_used, seller_reserved = (int(value or 0) for value in cursor.fetchone())
                target_stock = calculate_effective_stock(
                    int(configured_stock), int(row[11]) if row[11] is not None else None, int(row[12] or 0),
                    int(row[13] or 0), int(row[14] or 0), seller_used, seller_reserved,
                )
                cursor.execute(
                    """
                    UPDATE seller.yandex_stock_outbound_jobs
                    SET state='sending', target_stock=%s, sending_at=now(), updated_at=now()
                    WHERE id=%s AND state='preparing' AND lock_token=%s
                    """,
                    (target_stock, job_id, lock_token),
                )
            connection.commit()
            return StockOutboundPayload(
                job_id=job_id, lock_token=lock_token, connection_id=connection_id,
                external_product_id=product_id, campaign_id=campaign_id,
                token=str(row[7]), target_stock=target_stock,
            )

    @staticmethod
    def _finish_failure_before_send(connection, job_id: int, lock_token: UUID, message: str) -> None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE seller.yandex_stock_outbound_jobs
                SET state='failed', failed_at=now(), last_error=%s,
                    lock_token=NULL, locked_until=NULL, updated_at=now()
                WHERE id=%s AND state='preparing' AND lock_token=%s
                """,
                (message[:1000], job_id, lock_token),
            )
        connection.commit()

    def _finish_failure(self, payload: StockOutboundPayload, message: str, *, definite: bool) -> None:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT attempt_count, max_attempts FROM seller.yandex_stock_outbound_jobs
                    WHERE id=%s AND state='sending' AND lock_token=%s FOR UPDATE
                    """,
                    (payload.job_id, payload.lock_token),
                )
                row = cursor.fetchone()
                if not row:
                    return
                terminal = bool(definite) or int(row[0]) >= int(row[1])
                if terminal:
                    cursor.execute(
                        """
                        UPDATE seller.yandex_stock_outbound_jobs
                        SET state='failed', failed_at=now(), last_error=%s,
                            lock_token=NULL, locked_until=NULL, updated_at=now()
                        WHERE id=%s
                        """,
                        (message[:1000], payload.job_id),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE seller.yandex_stock_outbound_jobs
                        SET state='queued', next_attempt_at=now() + (%s * interval '1 second'),
                            last_error=%s, lock_token=NULL, locked_until=NULL, updated_at=now()
                        WHERE id=%s
                        """,
                        (stock_retry_delay_seconds(int(row[0])), message[:1000], payload.job_id),
                    )
                cursor.execute(
                    """
                    UPDATE seller.product_card_settings
                    SET last_stock_sync_error=%s, updated_at=now()
                    WHERE connection_id=%s AND external_product_id=%s
                    """,
                    (message[:1000], payload.connection_id, payload.external_product_id),
                )
            connection.commit()

    def _finish_success(self, payload: StockOutboundPayload) -> None:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.yandex_stock_outbound_jobs
                    SET state='succeeded', succeeded_at=now(), last_error='',
                        lock_token=NULL, locked_until=NULL, updated_at=now()
                    WHERE id=%s AND state='sending' AND lock_token=%s
                    """,
                    (payload.job_id, payload.lock_token),
                )
                if cursor.rowcount != 1:
                    return
                cursor.execute(
                    """
                    UPDATE seller.product_card_settings
                    SET published_stock=%s, last_stock_sync_at=now(), last_stock_sync_error='', updated_at=now()
                    WHERE connection_id=%s AND external_product_id=%s
                    """,
                    (payload.target_stock, payload.connection_id, payload.external_product_id),
                )
                cursor.execute(
                    """
                    UPDATE seller.yandex_product_settings_snapshot
                    SET published_stock=%s, last_stock_sync_at=now(), imported_at=imported_at
                    WHERE connection_id=%s AND external_product_id=%s
                    """,
                    (payload.target_stock, payload.connection_id, payload.external_product_id),
                )
            connection.commit()


def build_yandex_stock_outbound_processor(*, database_url, psycopg) -> YandexStockOutboundProcessor:
    return YandexStockOutboundProcessor(database_url=database_url, psycopg=psycopg)
