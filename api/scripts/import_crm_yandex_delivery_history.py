"""Переносит уже доставленные цифровые ключи Яндекс Маркета из CRM в историю Seller.

CRM открывается только для чтения. По умолчанию изменения Seller откатываются;
запись включается флагом ``--apply``. Скрипт не вызывает API Яндекса или поставщика
и связывает код только с точной позицией заказа, уже существующей в Seller.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import os
from typing import Any

import psycopg

try:
    from .import_crm_key_pools import required_secret, seller_key_hash, target_connection
except ImportError:  # pragma: no cover - прямой запуск ``python scripts/...``
    from import_crm_key_pools import required_secret, seller_key_hash, target_connection


@dataclass(frozen=True)
class SourceDelivery:
    source_id: int
    order_id: str
    item_id: str
    offer_id: str
    required_qty: int
    codes: tuple[str, ...]
    delivery_source: str
    market_submitted_at: datetime | None
    delivered_at: datetime | None
    created_at: datetime
    updated_at: datetime


def normalized_codes(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("CRM delivery contains invalid delivered_codes JSON") from exc
    if not isinstance(value, list):
        raise ValueError("CRM delivery delivered_codes must be a JSON array")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        code = str(raw or "").strip()
        if not code or len(code) > 1024:
            raise ValueError("CRM delivery contains an invalid key value")
        if code in seen:
            raise ValueError("CRM delivery contains the same key more than once")
        seen.add(code)
        result.append(code)
    return tuple(result)


def normalized_source_delivery(row: tuple[Any, ...]) -> SourceDelivery:
    codes = normalized_codes(row[5])
    required_qty = int(row[4] or 0)
    if required_qty <= 0 or len(codes) != required_qty:
        raise ValueError(
            f"CRM delivery id={row[0]} has {len(codes)} codes for required_qty={required_qty}"
        )
    offer_id = str(row[3] or "").strip()
    if not offer_id or len(offer_id) > 256:
        raise ValueError(f"CRM delivery id={row[0]} contains an invalid offer_id")
    return SourceDelivery(
        source_id=int(row[0]),
        order_id=str(row[1]),
        item_id=str(row[2]),
        offer_id=offer_id,
        required_qty=required_qty,
        codes=codes,
        delivery_source=str(row[6] or "").strip().lower(),
        market_submitted_at=row[7],
        delivered_at=row[8],
        created_at=row[9],
        updated_at=row[10],
    )


def seller_delivery_source(value: str) -> str:
    return {
        "pool": "pool",
        "manual": "manual",
        "interhub": "supplier",
        "supplier": "supplier",
    }.get(str(value or "").strip().lower(), "external")


def read_source(source, store_code: str) -> list[SourceDelivery]:
    with source.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute("SET statement_timeout='10s'")
        cursor.execute("SET lock_timeout='1s'")
        cursor.execute(
            """
            SELECT id, order_id, item_id, offer_id, required_qty, delivered_codes,
                   delivery_source, market_submitted_at, delivered_at, created_at, updated_at
            FROM app.marketplace_yandex_digital_deliveries
            WHERE lower(store_code)=lower(%s)
              AND status='market_delivered'
              AND delivery_source<>'support_message'
              AND jsonb_array_length(delivered_codes)>0
            ORDER BY id
            """,
            (store_code,),
        )
        rows = cursor.fetchall()
    source.commit()
    return [normalized_source_delivery(row) for row in rows]


def existing_fulfillment(cursor, connection_id: int, delivery: SourceDelivery):
    cursor.execute(
        """
        SELECT item.offer_id, item.quantity, fulfillment.id, fulfillment.status,
               fulfillment.delivery_source, fulfillment.reservation_ref
        FROM seller.order_items AS item
        LEFT JOIN seller.order_fulfillments AS fulfillment
          ON fulfillment.connection_id=item.connection_id
         AND fulfillment.external_order_id=item.external_order_id
         AND fulfillment.external_item_id=item.external_item_id
        WHERE item.connection_id=%s AND item.external_order_id=%s AND item.external_item_id=%s
        """,
        (connection_id, delivery.order_id, delivery.item_id),
    )
    return cursor.fetchone()


def ensure_pool(cursor, connection_id: int, offer_id: str) -> int:
    cursor.execute(
        """
        INSERT INTO seller.marketplace_key_pools(connection_id, external_product_id)
        VALUES (%s,%s)
        ON CONFLICT (connection_id, external_product_id) DO UPDATE SET updated_at=now()
        RETURNING id
        """,
        (connection_id, offer_id),
    )
    return int(cursor.fetchone()[0])


def ensure_fulfillment(cursor, connection_id: int, delivery: SourceDelivery, existing) -> tuple[int, str, bool]:
    reservation_ref = f"crm:yandex_market:{connection_id}:{delivery.order_id}:{delivery.item_id}"
    if existing[2] is not None:
        fulfillment_id = int(existing[2])
        cursor.execute(
            """
            SELECT key.code_hash
            FROM seller.fulfillment_key_reservations AS reservation
            JOIN seller.marketplace_keys AS key ON key.id=reservation.key_id
            WHERE reservation.fulfillment_id=%s AND reservation.state IN ('reserved','consumed')
            ORDER BY key.code_hash
            """,
            (fulfillment_id,),
        )
        linked_hashes = sorted(str(row[0]) for row in cursor.fetchall())
        expected_hashes = sorted(seller_key_hash(code) for code in delivery.codes)
        if linked_hashes:
            if linked_hashes != expected_hashes:
                raise RuntimeError(
                    f"Seller fulfillment for order={delivery.order_id} item={delivery.item_id} already has another key set"
                )
            return fulfillment_id, str(existing[5] or reservation_ref), False
        if str(existing[3]) not in {"pending", "manual_required", "closed_external", "delivered"}:
            raise RuntimeError(
                f"Seller fulfillment for order={delivery.order_id} item={delivery.item_id} is active ({existing[3]})"
            )
        return fulfillment_id, str(existing[5] or reservation_ref), False
    cursor.execute(
        """
        INSERT INTO seller.order_fulfillments(
          connection_id, external_order_id, external_item_id, offer_id,
          requested_quantity, status, delivery_source, reservation_ref,
          reserved_at, submitted_at, delivered_at, created_at, updated_at
        ) VALUES (
          %s,%s,%s,%s,%s,'delivered',%s,%s,
          COALESCE(%s,%s),%s,%s,%s,%s
        )
        RETURNING id
        """,
        (
            connection_id, delivery.order_id, delivery.item_id, delivery.offer_id,
            delivery.required_qty, seller_delivery_source(delivery.delivery_source), reservation_ref,
            delivery.market_submitted_at, delivery.delivered_at, delivery.market_submitted_at,
            delivery.delivered_at or delivery.updated_at, delivery.created_at, delivery.updated_at,
        ),
    )
    return int(cursor.fetchone()[0]), reservation_ref, True


def link_delivery(
    cursor,
    *,
    connection_id: int,
    delivery: SourceDelivery,
    target_secret: str,
) -> tuple[int, int, int, int]:
    existing = existing_fulfillment(cursor, connection_id, delivery)
    if not existing:
        return 0, 0, 0, 1
    if str(existing[0]) != delivery.offer_id or int(existing[1] or 0) != delivery.required_qty:
        raise RuntimeError(
            f"Seller order={delivery.order_id} item={delivery.item_id} does not match CRM offer or quantity"
        )
    fulfillment_id, reservation_ref, fulfillment_created = ensure_fulfillment(
        cursor, connection_id, delivery, existing,
    )
    pool_id = ensure_pool(cursor, connection_id, delivery.offer_id)
    created_keys = 0
    linked_keys = 0
    for code in delivery.codes:
        fingerprint = seller_key_hash(code)
        cursor.execute(
            """
            SELECT key.id, pool.connection_id, pool.external_product_id
            FROM seller.marketplace_keys AS key
            JOIN seller.marketplace_key_pools AS pool ON pool.id=key.pool_id
            WHERE key.code_hash=%s
            """,
            (fingerprint,),
        )
        key_row = cursor.fetchone()
        if key_row:
            if int(key_row[1]) != connection_id or str(key_row[2]) != delivery.offer_id:
                raise RuntimeError(f"CRM delivery id={delivery.source_id} reuses a key from another Seller product")
            key_id = int(key_row[0])
        else:
            cursor.execute(
                """
                INSERT INTO seller.marketplace_keys(
                  pool_id, code_ciphertext, code_hash, code_suffix, status,
                  key_origin, issued_order_ref, issued_at, source_system,
                  created_at, updated_at
                ) VALUES (
                  %s,pgp_sym_encrypt(%s,%s,'cipher-algo=aes256, compress-algo=0'),
                  %s,%s,'delivered','order',%s,%s,'crm',%s,%s
                )
                RETURNING id
                """,
                (
                    pool_id, code, target_secret, fingerprint, code[-4:], reservation_ref,
                    delivery.delivered_at or delivery.updated_at, delivery.created_at, delivery.updated_at,
                ),
            )
            key_id = int(cursor.fetchone()[0])
            created_keys += 1
        cursor.execute(
            """
            SELECT fulfillment.id
            FROM seller.fulfillment_key_reservations AS reservation
            JOIN seller.order_fulfillments AS fulfillment ON fulfillment.id=reservation.fulfillment_id
            WHERE reservation.key_id=%s AND reservation.state IN ('reserved','consumed')
            """,
            (key_id,),
        )
        owner = cursor.fetchone()
        if owner and int(owner[0]) != fulfillment_id:
            raise RuntimeError(f"Seller key id={key_id} is already linked to another fulfillment")
        cursor.execute(
            """
            UPDATE seller.marketplace_keys
            SET status='delivered', issued_order_ref=%s,
                issued_at=COALESCE(issued_at,%s), updated_at=GREATEST(updated_at,%s)
            WHERE id=%s
            """,
            (reservation_ref, delivery.delivered_at or delivery.updated_at, delivery.updated_at, key_id),
        )
        if not owner:
            cursor.execute(
                """
                INSERT INTO seller.fulfillment_key_reservations(
                  fulfillment_id, key_id, state, order_ref, reserved_at,
                  consumed_at, created_at, updated_at
                ) VALUES (%s,%s,'consumed',%s,COALESCE(%s,%s),%s,%s,%s)
                """,
                (
                    fulfillment_id, key_id, reservation_ref,
                    delivery.market_submitted_at, delivery.delivered_at,
                    delivery.delivered_at or delivery.updated_at,
                    delivery.created_at, delivery.updated_at,
                ),
            )
            linked_keys += 1
    cursor.execute(
        """
        UPDATE seller.order_fulfillments
        SET status='delivered', delivery_source=%s, reservation_ref=%s,
            reserved_at=COALESCE(reserved_at,%s,%s),
            submitted_at=COALESCE(submitted_at,%s),
            delivered_at=COALESCE(delivered_at,%s), last_error='',
            updated_at=GREATEST(updated_at,%s)
        WHERE id=%s
        """,
        (
            seller_delivery_source(delivery.delivery_source), reservation_ref,
            delivery.market_submitted_at, delivery.delivered_at,
            delivery.market_submitted_at, delivery.delivered_at or delivery.updated_at,
            delivery.updated_at, fulfillment_id,
        ),
    )
    cursor.execute(
        """
        INSERT INTO seller.fulfillment_events(fulfillment_id, event_type, from_status, to_status, details, created_at)
        SELECT %s,'crm_delivery_history_imported',%s,'delivered',
               jsonb_build_object('crm_delivery_id',%s,'delivery_source',%s),%s
        WHERE NOT EXISTS (
          SELECT 1 FROM seller.fulfillment_events
          WHERE fulfillment_id=%s AND event_type='crm_delivery_history_imported'
            AND details->>'crm_delivery_id'=%s
        )
        """,
        (
            fulfillment_id, str(existing[3] or ""), delivery.source_id, delivery.delivery_source,
            delivery.delivered_at or delivery.updated_at, fulfillment_id, str(delivery.source_id),
        ),
    )
    return int(fulfillment_created), created_keys, linked_keys, 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Import delivered Yandex keys from CRM into Seller history")
    parser.add_argument("--source-store-code", required=True)
    parser.add_argument("--target-campaign-id", required=True)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    source_dsn = str(os.getenv("CRM_DATABASE_URL", "")).strip()
    target_dsn = str(os.getenv("DATABASE_URL", "")).strip()
    if not source_dsn or not target_dsn or source_dsn == target_dsn:
        raise RuntimeError("Distinct CRM_DATABASE_URL and DATABASE_URL are required")
    target_secret = required_secret("SELLER_KEY_POOL_SECRET")

    totals = {"fulfillments_created": 0, "keys_created": 0, "keys_linked": 0, "missing_orders": 0}
    with psycopg.connect(source_dsn) as source, psycopg.connect(target_dsn) as target:
        deliveries = read_source(source, args.source_store_code)
        if args.expected_count is not None and len(deliveries) != args.expected_count:
            raise RuntimeError(f"Expected {args.expected_count} CRM deliveries, received {len(deliveries)}")
        connection_id, display_name = target_connection(target, args.target_campaign_id)
        with target.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout='3s'")
            cursor.execute("SET LOCAL statement_timeout='30s'")
            for delivery in deliveries:
                result = link_delivery(
                    cursor,
                    connection_id=connection_id,
                    delivery=delivery,
                    target_secret=target_secret,
                )
                for key, value in zip(totals, result, strict=True):
                    totals[key] += value
        if args.apply:
            target.commit()
        else:
            target.rollback()

    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "source_store_code": args.source_store_code,
        "seller_connection_id": connection_id,
        "seller_connection_name": display_name,
        "source_deliveries": len(deliveries),
        **totals,
        "external_api_calls": 0,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
