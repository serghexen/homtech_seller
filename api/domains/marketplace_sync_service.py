"""Исполнение одного read-only задания синхронизации вне HTTP-процесса."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import HTTPException

from domains.fulfillment_service import (
    automatic_pool_reservation_enabled,
    observe_order_fulfillments,
    reserve_pool_keys,
)
from domains.marketplace_catalog_service import fetch_marketplace_catalog, fetch_marketplace_stocks
from domains.marketplace_orders_service import fetch_marketplace_orders
from domains.marketplace_read_api import catalog_payload_with_stock, normalize_catalog_item, normalize_order_items


def credentials_secret() -> str:
    # Worker использует тот же отдельный ключ Seller и никогда не передаёт расшифрованный токен в очередь.
    value = str(os.getenv("MARKETPLACE_CREDENTIALS_SECRET", "")).strip()
    if len(value) < 32:
        raise RuntimeError("MARKETPLACE_CREDENTIALS_SECRET is not configured")
    return value


def load_active_connection(connection, connection_id: int) -> tuple[Any, ...]:
    # Расшифровывает токен только внутри worker и только для активного подключения из задания.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, provider_code, display_name, client_id, business_id, campaign_id,
                   pgp_sym_decrypt(token_ciphertext, %s), last_successful_sync_at
            FROM seller.marketplace_connections
            WHERE id=%s AND status='active'
            """,
            (credentials_secret(), connection_id),
        )
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=409, detail="Подключенный магазин больше не активен")
    return row


def sync_catalog_connection(connection, connection_row: tuple[Any, ...]) -> int:
    # Атомарно обновляет полный снимок и отличает исчезнувшие карточки от штатного архива маркетплейса.
    connection_id, provider_code, _name, client_id, business_id, campaign_id, token, _last_sync = connection_row
    rows = fetch_marketplace_catalog(
        provider_code=str(provider_code),
        token=str(token),
        client_id=str(client_id or ""),
        business_id=int(business_id) if str(business_id or "").isdigit() else None,
        campaign_id=int(campaign_id) if str(campaign_id or "").isdigit() else None,
    )
    normalized_candidates = [(normalize_catalog_item(str(provider_code), item), item) for item in rows if isinstance(item, dict)]
    if len(normalized_candidates) != len(rows) or any(item is None for item, _payload in normalized_candidates):
        # При изменении внешнего контракта безопаснее откатить снимок, чем ошибочно скрыть рабочий каталог.
        raise RuntimeError("Маркетплейс вернул неполный или неизвестный формат каталога")
    normalized_rows = [(item, payload) for item, payload in normalized_candidates if item is not None]
    current_product_ids = [str(item["external_product_id"]) for item, _payload in normalized_rows]
    stock_checked_at = datetime.now(timezone.utc)
    stocks_by_offer = fetch_marketplace_stocks(
        provider_code=str(provider_code),
        token=str(token),
        campaign_id=int(campaign_id) if str(campaign_id or "").isdigit() else None,
        offer_ids=[str(item["offer_id"]) for item, _payload in normalized_rows if not item["is_archived"]],
    )
    with connection.cursor() as cursor:
        for item, raw_payload in normalized_rows:
            persisted_payload = dict(raw_payload)
            if str(provider_code) == "yandex_market":
                stock = stocks_by_offer.get(str(item["offer_id"]), {})
                if stock.get("found") and stock.get("available_stock") is not None:
                    persisted_payload = catalog_payload_with_stock(
                        raw_payload,
                        available_stock=int(stock["available_stock"]),
                        checked_at=stock_checked_at,
                        provider_updated_at=str(stock.get("updated_at") or ""),
                    )
            cursor.execute(
                """
                INSERT INTO seller.catalog_items(
                    connection_id, external_product_id, offer_id, sku, title, raw_payload,
                    is_present, is_archived, archived_at, synced_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s::jsonb, true, %s,
                    CASE WHEN %s THEN now() ELSE NULL END, now()
                )
                ON CONFLICT (connection_id, external_product_id) DO UPDATE SET
                    offer_id=EXCLUDED.offer_id, sku=EXCLUDED.sku, title=EXCLUDED.title,
                    raw_payload=EXCLUDED.raw_payload, is_present=true,
                    is_archived=EXCLUDED.is_archived, archived_at=EXCLUDED.archived_at, synced_at=now()
                """,
                (
                    connection_id,
                    item["external_product_id"],
                    item["offer_id"],
                    item["sku"],
                    item["title"],
                    json.dumps(persisted_payload, ensure_ascii=False),
                    item["is_archived"],
                    item["is_archived"],
                ),
            )
        cursor.execute(
            """
            UPDATE seller.catalog_items
            SET is_present=false, archived_at=now()
            WHERE connection_id=%s AND is_present=true
              AND NOT (external_product_id = ANY(%s::text[]))
            """,
            (connection_id, current_product_ids),
        )
    return len(normalized_rows)


def sync_orders_connection(
    connection, connection_row: tuple[Any, ...], *, sync_started_at: datetime,
) -> int:
    # Использует сохранённый watermark и записывает позиции идемпотентно до его продвижения.
    connection_id, provider_code, _name, client_id, business_id, campaign_id, token, last_successful_sync_at = connection_row
    rows = fetch_marketplace_orders(
        provider_code=str(provider_code),
        token=str(token),
        client_id=str(client_id or ""),
        business_id=int(business_id) if str(business_id or "").isdigit() else None,
        campaign_id=int(campaign_id) if str(campaign_id or "").isdigit() else None,
        synced_after=last_successful_sync_at if isinstance(last_successful_sync_at, datetime) else None,
        synced_before=sync_started_at,
    )
    saved_items = save_order_snapshots(
        connection,
        connection_id=int(connection_id),
        provider_code=str(provider_code),
        rows=rows,
    )
    if str(provider_code) == "yandex_market":
        # Polling остаётся страховочной сеткой: пропущенный webhook не должен оставить резерв у отменённого заказа.
        seen_order_ids: set[str] = set()
        for row in rows:
            order_id = str(row.get("orderId") or row.get("id") or "").strip() if isinstance(row, dict) else ""
            if not order_id or order_id in seen_order_ids:
                continue
            seen_order_ids.add(order_id)
            fulfillment_ids = observe_order_fulfillments(
                connection,
                connection_id=int(connection_id),
                external_order_id=order_id,
            )
            if automatic_pool_reservation_enabled():
                for fulfillment_id in fulfillment_ids:
                    reserve_pool_keys(connection, fulfillment_id=fulfillment_id)
    return saved_items


def save_order_snapshots(
    connection,
    *,
    connection_id: int,
    provider_code: str,
    rows: list[dict[str, Any]],
) -> int:
    # Единообразно сохраняет полную синхронизацию и точечное webhook-обновление без продвижения watermark.
    normalized_rows = [
        (item, raw_payload)
        for raw_payload in rows if isinstance(raw_payload, dict)
        for item in normalize_order_items(provider_code, raw_payload)
    ]
    with connection.cursor() as cursor:
        for item, raw_payload in normalized_rows:
            cursor.execute(
                """
                INSERT INTO seller.order_items(
                    connection_id, external_order_id, external_item_id, offer_id, sku, title, quantity,
                    provider_status, provider_substatus, normalized_status, delivery_type,
                    created_at, updated_at, raw_payload, synced_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                ON CONFLICT (connection_id, external_order_id, external_item_id) DO UPDATE SET
                    offer_id=EXCLUDED.offer_id, sku=EXCLUDED.sku, title=EXCLUDED.title,
                    quantity=EXCLUDED.quantity, provider_status=EXCLUDED.provider_status,
                    provider_substatus=EXCLUDED.provider_substatus, normalized_status=EXCLUDED.normalized_status,
                    delivery_type=EXCLUDED.delivery_type,
                    created_at=COALESCE(EXCLUDED.created_at, seller.order_items.created_at),
                    updated_at=COALESCE(EXCLUDED.updated_at, seller.order_items.updated_at),
                    raw_payload=EXCLUDED.raw_payload, synced_at=now()
                """,
                (
                    connection_id,
                    item["external_order_id"],
                    item["external_item_id"],
                    item["offer_id"],
                    item["sku"],
                    item["title"],
                    item["quantity"],
                    item["provider_status"],
                    item["provider_substatus"],
                    item["normalized_status"],
                    item["delivery_type"],
                    item["created_at"],
                    item["updated_at"],
                    json.dumps(raw_payload, ensure_ascii=False),
                ),
            )
    return len(normalized_rows)


def mark_connection_success(
    connection, connection_id: int, *, sync_kind: str, sync_started_at: datetime,
) -> None:
    # Watermark заказов продвигается в той же транзакции, что и сохранённый снимок.
    with connection.cursor() as cursor:
        cursor.execute(
            """
            UPDATE seller.marketplace_connections
            SET last_error='', last_checked_at=now(),
                last_successful_sync_at=CASE
                    WHEN %s='orders' THEN %s
                    ELSE last_successful_sync_at
                END,
                updated_at=now()
            WHERE id=%s
            """,
            (sync_kind, sync_started_at, connection_id),
        )


def record_connection_error(database_url: Callable[[], str], psycopg, connection_id: int, message: str) -> None:
    # Ошибка задания сохраняется отдельно: неуспешная транзакция снимка уже откатилась.
    with psycopg.connect(database_url()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE seller.marketplace_connections
                SET last_error=%s, last_checked_at=now(), updated_at=now()
                WHERE id=%s
                """,
                (message[:1000], connection_id),
            )


def execute_sync_job(
    database_url: Callable[[], str], psycopg, *, connection_id: int, sync_kind: str,
) -> int:
    # Выполняет один магазин атомарно; исключение откатывает и позиции, и watermark.
    sync_started_at = datetime.now(timezone.utc)
    with psycopg.connect(database_url()) as connection:
        connection_row = load_active_connection(connection, connection_id)
        if sync_kind == "catalog":
            synced_items = sync_catalog_connection(connection, connection_row)
        elif sync_kind == "orders":
            synced_items = sync_orders_connection(connection, connection_row, sync_started_at=sync_started_at)
        else:
            raise RuntimeError(f"Unsupported sync kind: {sync_kind}")
        mark_connection_success(
            connection, connection_id, sync_kind=sync_kind, sync_started_at=sync_started_at,
        )
    return synced_items
