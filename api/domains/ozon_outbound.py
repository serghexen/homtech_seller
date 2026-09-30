"""Долговечная отправка цифровых кодов в Ozon без слепых повторов."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import os
import urllib.error
import urllib.request
from uuid import UUID, uuid4

from domains.marketplace_connection_verification import OZON_SELLER_BASE_URL, _ssl_context
from domains.marketplace_sync_service import credentials_secret
from domains.ozon_stock_queue import enqueue_ozon_stock_publication


@dataclass(frozen=True)
class OzonOutboundPayload:
    job_id: int
    lock_token: UUID
    fulfillment_id: int
    posting_number: str
    sku: int
    client_id: str
    token: str = field(repr=False)
    codes: tuple[str, ...] = field(repr=False)
    siblings: tuple[OzonOutboundPayload, ...] = field(default=(), repr=False)
    workspace_id: int = 0
    connection_id: int = 0


class OzonOutboundError(RuntimeError):
    def __init__(self, message: str, *, definite: bool, accepted: bool = False) -> None:
        super().__init__(message)
        self.definite = definite
        self.accepted = accepted


def ozon_outbound_enabled() -> bool:
    return str(os.getenv("SELLER_OZON_OUTBOUND_ENABLED", "false")).strip().lower() in {"1", "true", "yes"}


def key_pool_secret() -> str:
    value = str(os.getenv("SELLER_KEY_POOL_SECRET", "")).strip()
    if len(value) < 32:
        raise RuntimeError("SELLER_KEY_POOL_SECRET is not configured")
    return value


def outbound_timeout_seconds() -> int:
    return max(3, min(int(os.getenv("OZON_OUTBOUND_TIMEOUT_SECONDS", "20")), 60))


def _safe_ozon_error(detail: str, payload: OzonOutboundPayload) -> str:
    # Ozon can echo request values: never persist keys, credentials or raw details.
    try:
        value = json.loads(detail)
    except (ValueError, TypeError):
        return ""
    if not isinstance(value, dict) or not isinstance(value.get("message"), str):
        return ""
    message = value["message"]
    secrets = [payload.token, payload.client_id,
               *(code for part in (payload, *payload.siblings) for code in part.codes)]
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        message = message.replace(secret, "[скрыто]")
    return " ".join(message.split())[:500]


def send_ozon_digital_codes(payload: OzonOutboundPayload) -> None:
    parts = (payload, *payload.siblings)
    body = json.dumps({
        "posting_number": payload.posting_number,
        "exemplars_by_sku": [{
            "sku": part.sku,
            "exemplar_qty": len(part.codes),
            "not_available_exemplar_qty": 0,
            "exemplar_keys": list(part.codes),
        } for part in parts],
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{OZON_SELLER_BASE_URL}/v1/posting/digital/codes/upload",
        data=body, method="POST",
        headers={"Client-Id": payload.client_id, "Api-Key": payload.token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=outbound_timeout_seconds(), context=_ssl_context()) as response:
            value = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = _safe_ozon_error(exc.read().decode("utf-8", errors="replace"), payload)
        # A completed posting does not prove that these particular keys were accepted.
        definite = 400 <= int(exc.code) < 500 and int(exc.code) not in {408, 409, 429}
        if "done" in detail.lower():
            definite = False
        raise OzonOutboundError(
            f"Ozon отклонил выдачу: HTTP {exc.code}" + (f" — {detail}" if detail else ""),
            definite=definite,
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise OzonOutboundError("Результат отправки в Ozon неизвестен", definite=False) from None
    results = value.get("exemplars_by_sku") if isinstance(value, dict) else None
    try:
        if not isinstance(results, list) or len(results) != len(parts):
            raise ValueError()
        by_sku = {int(item["sku"]): item for item in results}
        if len(by_sku) != len(parts):
            raise ValueError()
        for part in parts:
            result = by_sku[part.sku]
            if int(result["received_qty"]) != len(part.codes) or int(result["rejected_qty"]) != 0:
                raise ValueError()
    except (KeyError, TypeError, ValueError):
        # Some codes may already have reached Ozon; retain every reservation for reconciliation.
        raise OzonOutboundError("Ozon не подтвердил полный комплект отправления; требуется сверка", definite=False) from None


class OzonOutboundProcessor:
    def __init__(self, *, database_url, psycopg, sender=send_ozon_digital_codes) -> None:
        self._database_url = database_url
        self._psycopg = psycopg
        self._sender = sender

    def recover_stale(self) -> tuple[int, int]:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE seller.fulfillment_outbound_jobs AS job
                    SET state='queued', lock_token=NULL, locked_until=NULL,
                        last_error='Worker был перезапущен до внешней отправки', updated_at=now()
                    FROM seller.order_fulfillments AS fulfillment
                    JOIN seller.marketplace_connections AS market ON market.id=fulfillment.connection_id
                    WHERE job.fulfillment_id=fulfillment.id AND market.provider_code='ozon'
                      AND job.state='preparing' AND job.locked_until < now()
                      AND pg_try_advisory_xact_lock(20260824, (market.id % 2147483647)::integer)
                    """
                )
                requeued = cursor.rowcount
                cursor.execute(
                    """
                    WITH stale AS (
                      UPDATE seller.fulfillment_outbound_jobs AS job
                      SET state='unknown', unknown_at=now(), lock_token=NULL, locked_until=NULL,
                          last_error='Worker остановился после начала отправки; повтор запрещён', updated_at=now()
                      FROM seller.order_fulfillments AS fulfillment
                      JOIN seller.marketplace_connections AS market ON market.id=fulfillment.connection_id
                      WHERE job.fulfillment_id=fulfillment.id AND market.provider_code='ozon'
                        AND job.state='sending' AND job.locked_until < now()
                        AND pg_try_advisory_xact_lock(20260824, (market.id % 2147483647)::integer)
                      RETURNING job.fulfillment_id
                    )
                    UPDATE seller.order_fulfillments
                    SET status='unknown', last_error='Результат отправки в Ozon неизвестен; требуется сверка', updated_at=now()
                    WHERE id IN (SELECT fulfillment_id FROM stale) AND status='sending'
                    """
                )
                unknown = cursor.rowcount
            connection.commit()
        return int(requeued), int(unknown)

    def process_pending_jobs(self, limit: int = 5) -> int:
        if not ozon_outbound_enabled():
            return 0
        processed = 0
        for _ in range(max(1, min(int(limit), 50))):
            # Session advisory lock remains held through the network call and final commit.
            with self._psycopg.connect(self._database_url()) as lock_connection:
                payload = self._claim_and_prepare(lock_connection)
                if payload is None:
                    continue
                processed += 1
                try:
                    self._sender(payload)
                except OzonOutboundError as exc:
                    self._finish(payload, "failed" if exc.definite else "unknown", str(exc))
                except Exception:
                    self._finish(payload, "unknown", "Результат отправки в Ozon неизвестен")
                else:
                    self._finish(payload, "submitted", "")
        return processed

    def _claim_and_prepare(self, connection) -> OzonOutboundPayload | None:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT market.workspace_id, market.id, fulfillment.external_order_id
                FROM seller.fulfillment_outbound_jobs job
                JOIN seller.order_fulfillments fulfillment ON fulfillment.id=job.fulfillment_id
                JOIN seller.marketplace_connections market ON market.id=fulfillment.connection_id
                WHERE job.state='queued' AND market.status='active'
                  AND market.provider_code='ozon' AND market.fulfillment_outbound_enabled=true
                  AND NOT EXISTS (
                    SELECT 1 FROM seller.fulfillment_outbound_jobs busy
                    JOIN seller.order_fulfillments bf ON bf.id=busy.fulfillment_id
                    WHERE bf.connection_id=market.id AND busy.state IN ('preparing','sending'))
                  AND NOT EXISTS (
                    SELECT 1 FROM seller.order_items item
                    LEFT JOIN seller.order_fulfillments f
                      ON f.connection_id=item.connection_id AND f.external_order_id=item.external_order_id
                     AND f.external_item_id=item.external_item_id
                    LEFT JOIN seller.fulfillment_outbound_jobs j ON j.fulfillment_id=f.id
                    WHERE item.connection_id=market.id AND item.external_order_id=fulfillment.external_order_id
                      AND (f.status IS DISTINCT FROM 'reserved' OR j.state IS DISTINCT FROM 'queued'
                        OR item.normalized_status<>'processing' OR item.delivery_type<>'DIGITAL'))
                  AND pg_try_advisory_xact_lock(20260824, (market.id % 2147483647)::integer)
                ORDER BY job.queued_at, job.id
                FOR UPDATE OF market, job SKIP LOCKED LIMIT 1
                """
            )
            candidate = cursor.fetchone()
            if not candidate:
                return None
            workspace_id, connection_id, posting = candidate
            cursor.execute("SELECT pg_try_advisory_lock(20260824, %s)", (connection_id % 2147483647,))
            if not cursor.fetchone()[0]:
                return None
            # Lock every item and job before decrypting anything or changing states.
            cursor.execute(
                """
                SELECT job.id, f.id, item.sku, item.quantity, f.requested_quantity,
                       f.reservation_ref, item.raw_payload, item.external_item_id, f.offer_id
                FROM seller.order_items item
                JOIN seller.marketplace_connections market ON market.id=item.connection_id
                JOIN seller.order_fulfillments f
                  ON f.connection_id=item.connection_id AND f.external_order_id=item.external_order_id
                 AND f.external_item_id=item.external_item_id
                JOIN seller.fulfillment_outbound_jobs job ON job.fulfillment_id=f.id
                WHERE market.workspace_id=%s AND market.id=%s AND item.external_order_id=%s
                  AND job.state='queued' AND f.status='reserved'
                  AND item.normalized_status='processing' AND item.delivery_type='DIGITAL'
                ORDER BY f.id FOR UPDATE OF item, f, job
                """, (workspace_id, connection_id, posting),
            )
            rows = cursor.fetchall()
            cursor.execute("SELECT count(*) FROM seller.order_items WHERE connection_id=%s AND external_order_id=%s", (connection_id, posting))
            if not rows or len(rows) != cursor.fetchone()[0]:
                return None
            lock_token = uuid4()
            job_ids = [row[0] for row in rows]
            cursor.execute("""UPDATE seller.fulfillment_outbound_jobs
                SET state='preparing',attempt_count=attempt_count+1,lock_token=%s,
                    locked_until=now()+interval '2 minutes',updated_at=now()
                WHERE id=ANY(%s) AND state='queued'""", (lock_token, job_ids))
            try:
                credential_key, material_key = credentials_secret(), key_pool_secret()
                expected = {int(row[2]): int(row[3]) for row in rows}
                if len(expected) != len(rows) or any(row[3] != row[4] or row[3] < 1 for row in rows):
                    raise RuntimeError("Состав выдачи не совпадает с позициями отправления Ozon")
                # Each normalized row retains the complete provider posting snapshot.
                for row in rows:
                    products = row[6].get("products") if isinstance(row[6], dict) else None
                    snapshot = {int(p["sku"]): int(p.get("required_qty_for_digital_code", p.get("quantity", 0))) for p in products or []}
                    if snapshot != expected:
                        raise RuntimeError("Неполный состав отправления Ozon; обновите заказ и подготовьте все позиции")
                cursor.execute("""SELECT client_id,pgp_sym_decrypt(token_ciphertext,%s)
                    FROM seller.marketplace_connections WHERE workspace_id=%s AND id=%s
                      AND status='active' AND fulfillment_outbound_enabled=true""",
                    (credential_key, workspace_id, connection_id))
                credentials = cursor.fetchone()
                if not ozon_outbound_enabled() or not credentials:
                    raise RuntimeError("Внешняя отправка Ozon выключена")
                parts, key_ids, hashes = [], [], []
                for job_id, fulfillment_id, sku, quantity, _, reservation_ref, _, _, offer_id in rows:
                    cursor.execute("""
                        SELECT key.id,pgp_sym_decrypt(key.code_ciphertext,%s),key.code_hash
                        FROM seller.fulfillment_key_reservations reservation
                        JOIN seller.marketplace_keys key ON key.id=reservation.key_id
                        JOIN seller.marketplace_key_pools pool ON pool.id=key.pool_id
                        WHERE reservation.fulfillment_id=%s AND reservation.state='reserved'
                          AND key.status='reserved' AND key.issued_order_ref=%s
                          AND pool.connection_id=%s AND pool.external_product_id=%s
                        ORDER BY reservation.id FOR UPDATE OF reservation,key
                        """, (material_key, fulfillment_id, reservation_ref, connection_id, offer_id))
                    keys = cursor.fetchall()
                    if len(keys) != quantity:
                        raise RuntimeError("Зарезервирован неполный комплект ключей отправления")
                    key_ids.extend(k[0] for k in keys)
                    hashes.append((int(sku), [str(k[2]) for k in keys]))
                    parts.append(OzonOutboundPayload(job_id, lock_token, fulfillment_id, posting, int(sku),
                        str(credentials[0]), str(credentials[1]), tuple(str(k[1]) for k in keys),
                        workspace_id=workspace_id, connection_id=connection_id))
            except (RuntimeError, ValueError, TypeError, KeyError) as exc:
                message = str(exc) if isinstance(exc, RuntimeError) else "Некорректный состав отправления Ozon"
                for job_id in job_ids:
                    cursor.execute("""UPDATE seller.fulfillment_outbound_jobs SET state='failed',failed_at=now(),
                        last_error=%s,lock_token=NULL,locked_until=NULL,updated_at=now() WHERE id=%s""", (message, job_id))
                connection.commit()
                return None
            fingerprint = hashlib.sha256(json.dumps([workspace_id, connection_id, posting, hashes]).encode()).hexdigest()
            cursor.execute("UPDATE seller.marketplace_keys SET status='sending',updated_at=now() WHERE id=ANY(%s) AND status='reserved'", (key_ids,))
            if cursor.rowcount != len(key_ids):
                raise RuntimeError("Не удалось зафиксировать полный комплект Ozon")
            for part in parts:
                cursor.execute("UPDATE seller.order_fulfillments SET status='sending',last_error='',updated_at=now() WHERE id=%s AND status='reserved'", (part.fulfillment_id,))
                cursor.execute("""UPDATE seller.fulfillment_outbound_jobs SET state='sending',request_fingerprint=%s,
                    sending_at=now(),updated_at=now() WHERE id=%s AND state='preparing' AND lock_token=%s""",
                    (fingerprint, part.job_id, lock_token))
                cursor.execute("INSERT INTO seller.fulfillment_events(fulfillment_id,event_type,from_status,to_status) VALUES (%s,'outbound_started','reserved','sending')", (part.fulfillment_id,))
        connection.commit()
        return replace(parts[0], siblings=tuple(parts[1:]))

    def _finish(self, payload: OzonOutboundPayload, state: str, message: str) -> None:
        with self._psycopg.connect(self._database_url()) as connection:
            with connection.cursor() as cursor:
                parts = (payload, *payload.siblings)
                for payload in parts:
                    cursor.execute("""SELECT job.id FROM seller.fulfillment_outbound_jobs job
                        JOIN seller.order_fulfillments f ON f.id=job.fulfillment_id
                        JOIN seller.marketplace_connections market ON market.id=f.connection_id
                        WHERE job.id=%s AND job.state='sending' AND job.lock_token=%s
                          AND f.id=%s AND market.workspace_id=%s AND market.id=%s
                        FOR UPDATE OF job""", (payload.job_id, payload.lock_token,
                            payload.fulfillment_id, payload.workspace_id, payload.connection_id))
                    if not cursor.fetchone():
                        raise RuntimeError("Состояние групповой отправки Ozon изменилось; требуется сверка")
                    if state == "submitted":
                        cursor.execute("UPDATE seller.fulfillment_outbound_jobs SET state='submitted',submitted_at=now(),last_error='',lock_token=NULL,locked_until=NULL,updated_at=now() WHERE id=%s", (payload.job_id,))
                        cursor.execute("UPDATE seller.order_fulfillments SET status='submitted',submitted_at=now(),last_error='',updated_at=now() WHERE id=%s AND status='sending'", (payload.fulfillment_id,))
                        enqueue_ozon_stock_publication(cursor, fulfillment_id=payload.fulfillment_id)
                        event_type, target = "outbound_submitted", "submitted"
                    elif state == "failed":
                        cursor.execute("UPDATE seller.fulfillment_outbound_jobs SET state='failed',failed_at=now(),last_error=%s,lock_token=NULL,locked_until=NULL,updated_at=now() WHERE id=%s", (message[:1000], payload.job_id))
                        cursor.execute("""UPDATE seller.marketplace_keys AS key SET status='reserved',updated_at=now()
                                          WHERE key.id IN (SELECT key_id FROM seller.fulfillment_key_reservations WHERE fulfillment_id=%s AND state='reserved') AND key.status='sending'""", (payload.fulfillment_id,))
                        cursor.execute("UPDATE seller.order_fulfillments SET status='reserved',last_error=%s,updated_at=now() WHERE id=%s AND status='sending'", (message[:1000], payload.fulfillment_id))
                        event_type, target = "outbound_rejected", "reserved"
                    else:
                        cursor.execute("UPDATE seller.fulfillment_outbound_jobs SET state='unknown',unknown_at=now(),last_error=%s,lock_token=NULL,locked_until=NULL,updated_at=now() WHERE id=%s", (message[:1000], payload.job_id))
                        cursor.execute("UPDATE seller.order_fulfillments SET status='unknown',last_error=%s,updated_at=now() WHERE id=%s AND status='sending'", (message[:1000], payload.fulfillment_id))
                        event_type, target = "outbound_unknown", "unknown"
                    cursor.execute("INSERT INTO seller.fulfillment_events(fulfillment_id,event_type,from_status,to_status,details) VALUES (%s,%s,'sending',%s,jsonb_build_object('message',(%s)::text))", (payload.fulfillment_id, event_type, target, message[:1000]))
            connection.commit()


def build_ozon_outbound_processor(*, database_url, psycopg) -> OzonOutboundProcessor:
    return OzonOutboundProcessor(database_url=database_url, psycopg=psycopg)
