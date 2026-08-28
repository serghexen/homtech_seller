"""Идемпотентная публикация заданного остатка цифрового товара Ozon."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import urllib.error
import urllib.request
from uuid import UUID

from domains.marketplace_connection_verification import OZON_SELLER_BASE_URL, _ssl_context
from domains.marketplace_sync_service import credentials_secret
from domains.stock_target_policy import stock_target_base


@dataclass(frozen=True)
class OzonStockPayload:
    job_id: int
    lock_token: UUID
    connection_id: int
    external_product_id: str
    offer_id: str
    client_id: str
    token: str
    target_stock: int


class OzonStockError(RuntimeError):
    def __init__(self, message: str, *, definite: bool) -> None:
        super().__init__(message)
        self.definite = definite


def ozon_stock_outbound_enabled() -> bool:
    return str(os.getenv("SELLER_OZON_STOCK_OUTBOUND_ENABLED", "false")).strip().lower() in {"1", "true", "yes"}


def retry_delay_seconds(attempt: int) -> int:
    return min(15 * (2 ** max(0, min(int(attempt), 10) - 1)), 3600)


def send_ozon_stock(payload: OzonStockPayload) -> None:
    request = urllib.request.Request(
        f"{OZON_SELLER_BASE_URL}/v1/product/digital/stocks/import",
        data=json.dumps({"stocks": [{"offer_id": payload.offer_id, "stock": payload.target_stock}]}).encode(),
        method="POST",
        headers={"Client-Id": payload.client_id, "Api-Key": payload.token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20, context=_ssl_context()) as response:
            value = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise OzonStockError(
            f"Ozon отклонил остаток: HTTP {exc.code}",
            definite=400 <= int(exc.code) < 500 and int(exc.code) != 429,
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OzonStockError("Не удалось подтвердить остаток Ozon", definite=False) from exc
    statuses = value.get("status") if isinstance(value, dict) and isinstance(value.get("status"), list) else []
    if not any(isinstance(item, dict) and bool(item.get("updated")) for item in statuses):
        raise OzonStockError("Ozon не подтвердил обновление остатка", definite=True)


class OzonStockOutboundProcessor:
    def __init__(self, *, database_url, psycopg, sender=send_ozon_stock) -> None:
        self._database_url = database_url
        self._psycopg = psycopg
        self._sender = sender

    def recover_stale(self) -> int:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE seller.ozon_stock_outbound_jobs
                       SET state='queued',lock_token=NULL,locked_until=NULL,next_attempt_at=now(),
                           last_error='Worker был перезапущен; остаток будет опубликован повторно',updated_at=now()
                       WHERE state IN ('preparing','sending') AND locked_until < now()"""
                )
                count = cursor.rowcount
            connection.commit()
        return int(count)

    def process_pending_jobs(self, limit: int = 5) -> int:
        if not ozon_stock_outbound_enabled():
            return 0
        processed = 0
        for _ in range(max(1, min(int(limit), 50))):
            payload = self._claim_and_prepare()
            if payload is None:
                break
            processed += 1
            try:
                self._sender(payload)
            except OzonStockError as exc:
                self._finish_failure(payload, str(exc), definite=exc.definite)
            except Exception:
                self._finish_failure(payload, "Не удалось подтвердить остаток Ozon", definite=False)
            else:
                self._finish_success(payload)
        return processed

    def _claim_and_prepare(self) -> OzonStockPayload | None:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT job.id
                    FROM seller.ozon_stock_outbound_jobs AS job
                    LEFT JOIN seller.order_fulfillments AS fulfillment ON fulfillment.id=job.fulfillment_id
                    JOIN seller.marketplace_connections AS market
                      ON market.id=COALESCE(job.connection_id,fulfillment.connection_id)
                    WHERE job.state='queued' AND job.next_attempt_at<=now() AND job.attempt_count<job.max_attempts
                      AND market.status='active' AND market.provider_code='ozon' AND market.stock_outbound_enabled=true
                    ORDER BY job.next_attempt_at,job.id FOR UPDATE OF job SKIP LOCKED LIMIT 1
                    """
                )
                row = cursor.fetchone()
                if not row:
                    return None
                job_id = int(row[0])
                cursor.execute(
                    """UPDATE seller.ozon_stock_outbound_jobs
                       SET state='preparing',attempt_count=attempt_count+1,lock_token=gen_random_uuid(),
                           locked_until=now()+interval '2 minutes',updated_at=now()
                       WHERE id=%s AND state='queued' RETURNING lock_token""",
                    (job_id,),
                )
                lock_token = cursor.fetchone()[0]
            try:
                secret = credentials_secret()
            except RuntimeError as exc:
                self._fail_before_send(connection, job_id, lock_token, str(exc))
                return None
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COALESCE(job.connection_id,fulfillment.connection_id),
                           COALESCE(job.external_product_id,fulfillment.offer_id),job.job_kind,
                           fulfillment.status,market.client_id,market.status,market.stock_outbound_enabled,
                           pgp_sym_decrypt(market.token_ciphertext,%s),job.requested_stock,
                           settings.manual_stock_limit,item.offer_id,item.is_archived,
                           COALESCE(policy.supplier_issue_enabled, false),
                           COALESCE(policy.pool_issue_enabled, settings.pool_issue_enabled, false),
                           COALESCE(pool_stock.free_count, 0)
                    FROM seller.ozon_stock_outbound_jobs AS job
                    LEFT JOIN seller.order_fulfillments AS fulfillment ON fulfillment.id=job.fulfillment_id
                    JOIN seller.marketplace_connections AS market
                      ON market.id=COALESCE(job.connection_id,fulfillment.connection_id)
                    LEFT JOIN seller.product_card_settings AS settings
                      ON settings.connection_id=COALESCE(job.connection_id,fulfillment.connection_id)
                     AND settings.external_product_id=COALESCE(job.external_product_id,fulfillment.offer_id)
                    LEFT JOIN seller.product_fulfillment_policies AS policy
                      ON policy.connection_id=COALESCE(job.connection_id,fulfillment.connection_id)
                     AND policy.external_product_id=COALESCE(job.external_product_id,fulfillment.offer_id)
                    JOIN seller.catalog_items AS item
                      ON item.connection_id=COALESCE(job.connection_id,fulfillment.connection_id)
                     AND item.external_product_id=COALESCE(job.external_product_id,fulfillment.offer_id)
                    LEFT JOIN LATERAL (
                      SELECT COUNT(*) FILTER (
                        WHERE key.key_origin='pool' AND key.status='free'
                          AND (key.expires_at IS NULL OR key.expires_at >= current_date)
                      ) AS free_count
                      FROM seller.marketplace_key_pools AS pool
                      LEFT JOIN seller.marketplace_keys AS key ON key.pool_id=pool.id
                      WHERE pool.connection_id=COALESCE(job.connection_id,fulfillment.connection_id)
                        AND pool.external_product_id=COALESCE(job.external_product_id,fulfillment.offer_id)
                    ) AS pool_stock ON true
                    WHERE job.id=%s AND job.state='preparing' AND job.lock_token=%s
                    FOR UPDATE OF job
                    """,
                    (secret, job_id, lock_token),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                configured_manual_stock = row[8] if str(row[2]) == "manual" else row[9]
                target = stock_target_base(
                    manual_stock=int(configured_manual_stock) if configured_manual_stock is not None else None,
                    supplier_issue_enabled=bool(row[12]),
                    pool_issue_enabled=bool(row[13]),
                    pool_free_count=int(row[14] or 0),
                )
                error = ""
                if not ozon_stock_outbound_enabled() or str(row[5]) != "active" or not bool(row[6]):
                    error = "Публикация остатков Ozon выключена"
                elif str(row[2]) == "fulfillment" and str(row[3]) not in {"submitted", "delivered"}:
                    error = "Остаток публикуется только после подтверждённой отправки"
                elif target is None:
                    error = "Для товара не указан заданный остаток"
                elif bool(row[11]):
                    error = "Нельзя публиковать остаток архивной карточки"
                elif not str(row[10] or "").strip():
                    error = "У карточки Ozon отсутствует offer_id"
                if error:
                    self._fail_before_send(connection, job_id, lock_token, error)
                    return None
                cursor.execute(
                    "UPDATE seller.ozon_stock_outbound_jobs SET state='sending',target_stock=%s,sending_at=now(),updated_at=now() WHERE id=%s AND state='preparing' AND lock_token=%s",
                    (int(target), job_id, lock_token),
                )
            connection.commit()
            return OzonStockPayload(job_id, lock_token, int(row[0]), str(row[1]), str(row[10]), str(row[4]), str(row[7]), int(target))

    @staticmethod
    def _fail_before_send(connection, job_id: int, lock_token: UUID, message: str) -> None:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE seller.ozon_stock_outbound_jobs SET state='failed',failed_at=now(),last_error=%s,lock_token=NULL,locked_until=NULL,updated_at=now() WHERE id=%s AND state='preparing' AND lock_token=%s", (message[:1000], job_id, lock_token))
        connection.commit()

    def _finish_failure(self, payload: OzonStockPayload, message: str, *, definite: bool) -> None:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT attempt_count,max_attempts FROM seller.ozon_stock_outbound_jobs WHERE id=%s AND state='sending' AND lock_token=%s FOR UPDATE", (payload.job_id, payload.lock_token))
                row = cursor.fetchone()
                if not row:
                    return
                terminal = definite or int(row[0]) >= int(row[1])
                if terminal:
                    cursor.execute("UPDATE seller.ozon_stock_outbound_jobs SET state='failed',failed_at=now(),last_error=%s,lock_token=NULL,locked_until=NULL,updated_at=now() WHERE id=%s", (message[:1000], payload.job_id))
                else:
                    cursor.execute("UPDATE seller.ozon_stock_outbound_jobs SET state='queued',next_attempt_at=now()+(%s*interval '1 second'),last_error=%s,lock_token=NULL,locked_until=NULL,updated_at=now() WHERE id=%s", (retry_delay_seconds(int(row[0])), message[:1000], payload.job_id))
                cursor.execute("UPDATE seller.product_card_settings SET last_stock_sync_error=%s,updated_at=now() WHERE connection_id=%s AND external_product_id=%s", (message[:1000], payload.connection_id, payload.external_product_id))
            connection.commit()

    def _finish_success(self, payload: OzonStockPayload) -> None:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE seller.ozon_stock_outbound_jobs SET state='succeeded',succeeded_at=now(),last_error='',lock_token=NULL,locked_until=NULL,updated_at=now() WHERE id=%s AND state='sending' AND lock_token=%s", (payload.job_id, payload.lock_token))
                if cursor.rowcount != 1:
                    return
                cursor.execute("UPDATE seller.product_card_settings SET published_stock=%s,last_stock_sync_at=now(),last_stock_sync_error='',updated_at=now() WHERE connection_id=%s AND external_product_id=%s", (payload.target_stock, payload.connection_id, payload.external_product_id))
            connection.commit()


def build_ozon_stock_outbound_processor(*, database_url, psycopg) -> OzonStockOutboundProcessor:
    return OzonStockOutboundProcessor(database_url=database_url, psycopg=psycopg)
