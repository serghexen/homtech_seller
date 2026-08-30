"""Отдельный worker долговечной очереди синхронизации Seller."""

from __future__ import annotations

import os
import re
import signal
import time
import hashlib
from dataclasses import dataclass
from datetime import timedelta

import psycopg
from fastapi import HTTPException

from domains.marketplace_sync_service import execute_sync_job, record_connection_error
from domains.marketplace_dashboard_service import dashboard_insights_enabled
from domains.ozon_outbound import build_ozon_outbound_processor
from domains.ozon_stock_outbound import build_ozon_stock_outbound_processor
from domains.supplier_fulfillment import build_supplier_fulfillment_processor
from domains.yandex_market_webhook_processor import build_yandex_market_webhook_processor
from domains.yandex_market_webhooks_api import webhook_processing_enabled
from domains.yandex_market_outbound import build_yandex_outbound_processor
from domains.yandex_market_stock_outbound import build_yandex_stock_outbound_processor


SYNC_LOCK_NAMESPACE = 20_260_824


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    connection_id: int
    sync_kind: str
    attempt_count: int
    max_attempts: int


def database_url() -> str:
    value = str(os.getenv("DATABASE_URL", "")).strip()
    if not value:
        raise RuntimeError("DATABASE_URL is required")
    return value


def poll_seconds() -> float:
    return max(0.2, min(float(os.getenv("SYNC_WORKER_POLL_SECONDS", "2")), 30.0))


def stale_seconds() -> int:
    return max(60, min(int(os.getenv("SYNC_JOB_STALE_SECONDS", "300")), 86_400))


def webhook_batch_size() -> int:
    # Ограничивает число webhook между обычными заданиями, чтобы ни одна очередь не голодала.
    return max(1, min(int(os.getenv("YANDEX_MARKET_WEBHOOK_BATCH_SIZE", "10")), 100))


def outbound_batch_size() -> int:
    return max(1, min(int(os.getenv("YANDEX_MARKET_OUTBOUND_BATCH_SIZE", "5")), 50))


def fulfillment_batch_size() -> int:
    return max(1, min(int(os.getenv("SELLER_FULFILLMENT_BATCH_SIZE", "5")), 50))


def stock_outbound_batch_size() -> int:
    return max(1, min(int(os.getenv("YANDEX_MARKET_STOCK_OUTBOUND_BATCH_SIZE", "5")), 50))


def advisory_lock_key(connection_id: int) -> int:
    # PostgreSQL-вариант advisory lock принимает signed int32 во второй части ключа.
    return int(connection_id) % 2_147_483_647


def retry_delay_seconds(attempt_count: int) -> int:
    # Короткий exponential backoff ограничивает нагрузку и не оставляет пользователя ждать часами.
    return min(15 * (2 ** max(0, attempt_count - 1)), 300)


def dashboard_refresh_interval_seconds() -> int:
    return max(60, min(int(os.getenv("SELLER_DASHBOARD_REFRESH_SECONDS", "600")), 86_400))


def stable_dashboard_jitter_seconds(connection_id: int) -> int:
    # Разносит восстановление 20/100 магазинов по времени одинаково после каждого рестарта.
    window = max(1, dashboard_refresh_interval_seconds() // 4)
    digest = hashlib.sha256(f"dashboard:{int(connection_id)}".encode("ascii")).digest()
    return int.from_bytes(digest[:4], "big") % window


def enqueue_due_dashboard_jobs(connection, limit: int = 20) -> int:
    """Ставит read-only снимки Яндекса в общую очередь без отдельного polling-процесса."""

    if not dashboard_insights_enabled():
        return 0
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT marketplace.id, marketplace.workspace_id
            FROM seller.marketplace_connections AS marketplace
            LEFT JOIN seller.marketplace_dashboard_snapshots AS snapshot
              ON snapshot.connection_id=marketplace.id
            WHERE marketplace.status='active' AND marketplace.provider_code='yandex_market'
              AND COALESCE(snapshot.next_refresh_at, now()) <= now()
            ORDER BY COALESCE(snapshot.next_refresh_at, marketplace.created_at), marketplace.id
            FOR UPDATE OF marketplace SKIP LOCKED
            LIMIT %s
            """,
            (max(1, min(int(limit), 100)),),
        )
        rows = cursor.fetchall()
        queued = 0
        for connection_id, workspace_id in rows:
            delay_seconds = dashboard_refresh_interval_seconds() + stable_dashboard_jitter_seconds(int(connection_id))
            cursor.execute(
                """
                INSERT INTO seller.marketplace_dashboard_snapshots(
                    connection_id, workspace_id, next_refresh_at
                ) VALUES (%s, %s, now() + (%s * interval '1 second'))
                ON CONFLICT (connection_id) DO UPDATE SET
                    workspace_id=EXCLUDED.workspace_id,
                    next_refresh_at=EXCLUDED.next_refresh_at,
                    updated_at=now()
                """,
                (int(connection_id), int(workspace_id), delay_seconds),
            )
            cursor.execute(
                """
                INSERT INTO seller.marketplace_sync_jobs(workspace_id, connection_id, sync_kind)
                VALUES (%s, %s, 'dashboard')
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (int(workspace_id), int(connection_id)),
            )
            if cursor.fetchone():
                queued += 1
    connection.commit()
    return queued


def enqueue_due_marketplace_order_jobs(connection, limit: int = 20) -> int:
    """Ставит резервную синхронизацию заказов запущенных магазинов в общую очередь."""

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, workspace_id, orders_poll_interval_seconds
            FROM seller.marketplace_connections
            WHERE status='active' AND launch_state='running'
              AND orders_polling_enabled=true AND next_orders_poll_at <= now()
            ORDER BY next_orders_poll_at, id
            FOR UPDATE SKIP LOCKED
            LIMIT %s
            """,
            (max(1, min(int(limit), 100)),),
        )
        rows = cursor.fetchall()
        queued = 0
        for connection_id, workspace_id, interval_seconds in rows:
            cursor.execute(
                """
                UPDATE seller.marketplace_connections
                SET next_orders_poll_at=now() + (%s * interval '1 second'), updated_at=now()
                WHERE id=%s
                """,
                (int(interval_seconds), int(connection_id)),
            )
            cursor.execute(
                """
                INSERT INTO seller.marketplace_sync_jobs(workspace_id, connection_id, sync_kind)
                VALUES (%s,%s,'orders')
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (int(workspace_id), int(connection_id)),
            )
            if cursor.fetchone():
                queued += 1
    connection.commit()
    return queued


def enqueue_due_ozon_order_jobs(connection, limit: int = 20) -> int:
    """Совместимый псевдоним для старых локальных вызовов и тестов."""

    return enqueue_due_marketplace_order_jobs(connection, limit=limit)


def is_transient_sync_error(exc: Exception) -> bool:
    # Повторяем сетевые/серверные сбои, но не ошибки доступа и отключённые магазины.
    if isinstance(exc, HTTPException):
        detail = str(exc.detail or "")
        upstream_status = re.search(r"\bHTTP\s+(\d{3})\b", detail)
        if upstream_status:
            status_code = int(upstream_status.group(1))
            return status_code in {420, 429} or status_code >= 500
        return int(exc.status_code) in {429, 502, 503, 504}
    return isinstance(exc, (psycopg.Error, TimeoutError, ConnectionError, OSError))


def error_text(exc: Exception) -> str:
    detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
    return str(detail or exc.__class__.__name__)[:1000]


def try_connection_lock(connection, connection_id: int) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_try_advisory_lock(%s, %s)",
            (SYNC_LOCK_NAMESPACE, advisory_lock_key(connection_id)),
        )
        return bool(cursor.fetchone()[0])


def release_connection_lock(connection, connection_id: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_unlock(%s, %s)",
            (SYNC_LOCK_NAMESPACE, advisory_lock_key(connection_id)),
        )


def recover_stale_jobs(connection) -> int:
    # Возвращает задания после падения worker-а только если его session lock уже освобождён.
    recovered = 0
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, connection_id, attempt_count, max_attempts
            FROM seller.marketplace_sync_jobs
            WHERE status='running'
              AND heartbeat_at < now() - (%s * interval '1 second')
            ORDER BY heartbeat_at, id
            LIMIT 20
            """,
            (stale_seconds(),),
        )
        rows = cursor.fetchall()
    for job_id, connection_id, attempt_count, max_attempts in rows:
        if not try_connection_lock(connection, int(connection_id)):
            continue
        try:
            with connection.cursor() as cursor:
                if int(attempt_count) < int(max_attempts):
                    cursor.execute(
                        """
                        UPDATE seller.marketplace_sync_jobs
                        SET status='queued', available_at=now(), started_at=NULL, heartbeat_at=NULL,
                            error='Worker был перезапущен; задание поставлено повторно', updated_at=now()
                        WHERE id=%s AND status='running'
                        """,
                        (job_id,),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE seller.marketplace_sync_jobs
                        SET status='failed', finished_at=now(), heartbeat_at=NULL,
                            error='Worker был перезапущен; исчерпаны попытки', updated_at=now()
                        WHERE id=%s AND status='running'
                        """,
                        (job_id,),
                    )
                recovered += cursor.rowcount
            connection.commit()
        finally:
            release_connection_lock(connection, int(connection_id))
            connection.commit()
    return recovered


def claim_next_job(connection) -> ClaimedJob | None:
    # SKIP LOCKED позволяет нескольким worker-ам брать разные магазины без общей блокировки очереди.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, connection_id, sync_kind, attempt_count, max_attempts
            FROM seller.marketplace_sync_jobs
            WHERE status='queued' AND available_at <= now()
            ORDER BY available_at, id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        )
        row = cursor.fetchone()
    if not row:
        connection.commit()
        return None
    job_id, connection_id, sync_kind, attempt_count, max_attempts = row
    if not try_connection_lock(connection, int(connection_id)):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE seller.marketplace_sync_jobs
                SET available_at=now() + interval '2 seconds', updated_at=now()
                WHERE id=%s AND status='queued'
                """,
                (job_id,),
            )
        connection.commit()
        return None
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE seller.marketplace_sync_jobs
            SET status='running', attempt_count=attempt_count + 1,
                started_at=now(), heartbeat_at=now(), finished_at=NULL, error='', updated_at=now()
            WHERE id=%s AND status='queued'
            RETURNING attempt_count
            """,
            (job_id,),
        )
        claimed = cursor.fetchone()
    connection.commit()
    if not claimed:
        release_connection_lock(connection, int(connection_id))
        connection.commit()
        return None
    return ClaimedJob(
        id=int(job_id),
        connection_id=int(connection_id),
        sync_kind=str(sync_kind),
        attempt_count=int(claimed[0]),
        max_attempts=int(max_attempts),
    )


def complete_job(connection, job: ClaimedJob, synced_items: int) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE seller.marketplace_sync_jobs
            SET status='succeeded', synced_items=%s, error='', finished_at=now(),
                heartbeat_at=NULL, updated_at=now()
            WHERE id=%s AND status='running'
            """,
            (synced_items, job.id),
        )
    connection.commit()


def fail_job(connection, job: ClaimedJob, exc: Exception) -> str:
    message = error_text(exc)
    should_retry = is_transient_sync_error(exc) and job.attempt_count < job.max_attempts
    with connection.cursor() as cursor:
        if should_retry:
            cursor.execute(
                """
                UPDATE seller.marketplace_sync_jobs
                SET status='queued', available_at=now() + (%s * interval '1 second'),
                    started_at=NULL, heartbeat_at=NULL, error=%s, updated_at=now()
                WHERE id=%s AND status='running'
                """,
                (retry_delay_seconds(job.attempt_count), message, job.id),
            )
        else:
            cursor.execute(
                """
                UPDATE seller.marketplace_sync_jobs
                SET status='failed', error=%s, finished_at=now(), heartbeat_at=NULL, updated_at=now()
                WHERE id=%s AND status='running'
                """,
                (message, job.id),
            )
    connection.commit()
    return "queued" if should_retry else "failed"


def run_worker() -> int:
    stopping = False

    def stop(_signal, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    process_yandex_webhook = build_yandex_market_webhook_processor(
        database_url=database_url,
        psycopg=psycopg,
        processing_enabled=webhook_processing_enabled,
    )
    outbound = build_yandex_outbound_processor(database_url=database_url, psycopg=psycopg)
    ozon_outbound = build_ozon_outbound_processor(database_url=database_url, psycopg=psycopg)
    stock_outbound = build_yandex_stock_outbound_processor(database_url=database_url, psycopg=psycopg)
    ozon_stock_outbound = build_ozon_stock_outbound_processor(database_url=database_url, psycopg=psycopg)
    fulfillment = build_supplier_fulfillment_processor(database_url=database_url, psycopg=psycopg)
    print("Seller worker started", flush=True)
    while not stopping:
        try:
            recovered_fulfillments = fulfillment.recover_stale()
            if recovered_fulfillments:
                print(f"Recovered fulfillment leases: {recovered_fulfillments}", flush=True)
            processed_fulfillments = fulfillment.process_pending(fulfillment_batch_size())
            if processed_fulfillments:
                print(f"Processed fulfillment resolutions: {processed_fulfillments}", flush=True)
            requeued_outbound, unknown_outbound = outbound.recover_stale()
            if requeued_outbound or unknown_outbound:
                print(
                    f"Recovered outbound jobs: requeued={requeued_outbound}, unknown={unknown_outbound}",
                    flush=True,
                )
            processed_outbound = outbound.process_pending_jobs(outbound_batch_size())
            if processed_outbound:
                print(f"Processed Yandex outbound jobs: {processed_outbound}", flush=True)
            requeued_ozon, unknown_ozon = ozon_outbound.recover_stale()
            if requeued_ozon or unknown_ozon:
                print(f"Recovered Ozon outbound jobs: requeued={requeued_ozon}, unknown={unknown_ozon}", flush=True)
            processed_ozon_outbound = ozon_outbound.process_pending_jobs(outbound_batch_size())
            if processed_ozon_outbound:
                print(f"Processed Ozon outbound jobs: {processed_ozon_outbound}", flush=True)
            recovered_stock = stock_outbound.recover_stale()
            if recovered_stock:
                print(f"Recovered Yandex stock jobs: {recovered_stock}", flush=True)
            processed_stock = stock_outbound.process_pending_jobs(stock_outbound_batch_size())
            if processed_stock:
                print(f"Processed Yandex stock jobs: {processed_stock}", flush=True)
            recovered_ozon_stock = ozon_stock_outbound.recover_stale()
            if recovered_ozon_stock:
                print(f"Recovered Ozon stock jobs: {recovered_ozon_stock}", flush=True)
            processed_ozon_stock = ozon_stock_outbound.process_pending_jobs(stock_outbound_batch_size())
            if processed_ozon_stock:
                print(f"Processed Ozon stock jobs: {processed_ozon_stock}", flush=True)
            processed_webhooks = process_yandex_webhook.process_pending_events(webhook_batch_size())
            if processed_webhooks:
                print(f"Processed Yandex webhook events: {processed_webhooks}", flush=True)
            with psycopg.connect(database_url()) as lock_connection:
                scheduled_orders = enqueue_due_marketplace_order_jobs(lock_connection)
                if scheduled_orders:
                    print(f"Scheduled marketplace order polls: {scheduled_orders}", flush=True)
                scheduled_dashboard = enqueue_due_dashboard_jobs(lock_connection)
                if scheduled_dashboard:
                    print(f"Scheduled marketplace dashboard refreshes: {scheduled_dashboard}", flush=True)
                recovered = recover_stale_jobs(lock_connection)
                if recovered:
                    print(f"Recovered stale sync jobs: {recovered}", flush=True)
                job = claim_next_job(lock_connection)
                if not job:
                    if not any((
                        processed_fulfillments, processed_webhooks, processed_outbound, processed_stock,
                        processed_ozon_outbound, processed_ozon_stock, scheduled_orders, scheduled_dashboard,
                    )):
                        time.sleep(poll_seconds())
                    continue
                try:
                    synced_items = execute_sync_job(
                        database_url, psycopg, connection_id=job.connection_id, sync_kind=job.sync_kind,
                    )
                except Exception as exc:
                    message = error_text(exc)
                    try:
                        record_connection_error(
                            database_url, psycopg, job.connection_id, message, sync_kind=job.sync_kind,
                        )
                    except Exception as record_exc:
                        print(f"Could not record connection error for job {job.id}: {record_exc}", flush=True)
                    state = fail_job(lock_connection, job, exc)
                    print(f"Sync job {job.id} {state}: {message}", flush=True)
                else:
                    complete_job(lock_connection, job, synced_items)
                    print(f"Sync job {job.id} succeeded: {synced_items} items", flush=True)
                finally:
                    release_connection_lock(lock_connection, job.connection_id)
                    lock_connection.commit()
        except Exception as exc:
            print(f"Worker loop error: {exc}", flush=True)
            time.sleep(poll_seconds())
    print("Seller worker stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_worker())
