"""Переносит доставленные Ozon-коды из CRM в историю Seller.

По умолчанию выполняет полный dry-run с откатом Seller. Внешние API не
вызываются; CRM открывается только для чтения.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from typing import Any

import psycopg

try:
    from .import_crm_key_pools import required_secret, seller_key_hash
except ImportError:  # pragma: no cover
    from import_crm_key_pools import required_secret, seller_key_hash


@dataclass(frozen=True)
class SourceDelivery:
    source_id: int
    external_product_id: str
    posting_number: str
    item_id: str
    offer_id: str
    required_qty: int
    codes: tuple[str, ...]
    delivery_source: str
    created_at: Any
    delivered_at: Any
    updated_at: Any


def normalized_codes(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("CRM Ozon delivered_codes must be a JSON array")
    codes = tuple(str(code or "").strip() for code in value)
    if any(not code or len(code) > 1024 for code in codes):
        raise ValueError("CRM Ozon delivery contains an invalid code")
    if len(codes) != len(set(codes)):
        raise ValueError("CRM Ozon delivery repeats the same code")
    return codes


def read_source(source, store_code: str) -> list[SourceDelivery]:
    with source.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute("SET statement_timeout='10s'")
        cursor.execute("SET lock_timeout='1s'")
        cursor.execute(
            """
            SELECT orders.id, orders.external_product_id, orders.posting_number, orders.sku,
                   catalog.offer_id, orders.required_qty, orders.delivered_codes,
                   CASE
                     WHEN EXISTS (
                       SELECT 1 FROM app.marketplace_ozon_digital_supplier_attempts AS attempt
                       WHERE attempt.order_id=orders.id AND attempt.state='paid'
                     ) THEN 'supplier'
                     WHEN EXISTS (
                       SELECT 1
                       FROM app.marketplace_manual_keys AS manual_key
                       JOIN app.marketplace_manual_key_pools AS pool ON pool.id=manual_key.pool_id
                       WHERE pool.marketplace='ozon'
                         AND lower(pool.store_code)=lower(orders.store_code)
                         AND manual_key.issued_order_ref=orders.posting_number
                     ) THEN 'pool'
                     ELSE 'manual'
                   END,
                   orders.created_at, orders.delivered_at, orders.updated_at
            FROM app.marketplace_ozon_digital_orders AS orders
            JOIN app.marketplace_ozon_catalog_items AS catalog
              ON catalog.store_code=orders.store_code
             AND catalog.external_product_id=orders.external_product_id
            WHERE lower(orders.store_code)=lower(%s)
              AND orders.status='delivered'
              AND jsonb_array_length(orders.delivered_codes)>0
            ORDER BY orders.id
            """,
            (store_code,),
        )
        rows = cursor.fetchall()
    source.commit()
    deliveries: list[SourceDelivery] = []
    for row in rows:
        codes = normalized_codes(row[6])
        required_qty = int(row[5] or 0)
        if required_qty <= 0 or len(codes) != required_qty:
            raise RuntimeError(
                f"CRM Ozon order id={row[0]} has {len(codes)} codes for quantity={required_qty}"
            )
        deliveries.append(SourceDelivery(
            source_id=int(row[0]), external_product_id=str(row[1]),
            posting_number=str(row[2] or "").strip(), item_id=str(row[3] or "").strip(),
            offer_id=str(row[4] or "").strip(), required_qty=required_qty,
            codes=codes, delivery_source=str(row[7]),
            created_at=row[8], delivered_at=row[9], updated_at=row[10],
        ))
    return deliveries


def target_context(target, connection_id: int) -> str:
    with target.cursor() as cursor:
        cursor.execute(
            """
            SELECT display_name, webhook_processing_enabled, fulfillment_reservation_enabled,
                   fulfillment_outbound_enabled, supplier_fulfillment_enabled, stock_outbound_enabled
            FROM seller.marketplace_connections
            WHERE id=%s AND provider_code='ozon' AND status='active'
            """,
            (connection_id,),
        )
        row = cursor.fetchone()
    if not row:
        raise RuntimeError(f"Active Seller Ozon connection id={connection_id} was not found")
    if any(bool(value) for value in row[1:]):
        raise RuntimeError("All Seller Ozon execution flags must stay disabled during CRM import")
    return str(row[0])


def ensure_pool(cursor, connection_id: int, external_product_id: str) -> int:
    cursor.execute(
        """
        INSERT INTO seller.marketplace_key_pools(connection_id, external_product_id)
        VALUES (%s,%s)
        ON CONFLICT (connection_id, external_product_id) DO UPDATE SET updated_at=now()
        RETURNING id
        """,
        (connection_id, external_product_id),
    )
    return int(cursor.fetchone()[0])


def order_and_fulfillment(cursor, connection_id: int, delivery: SourceDelivery):
    cursor.execute(
        """
        SELECT item.offer_id, item.quantity, fulfillment.id, fulfillment.status,
               fulfillment.reservation_ref
        FROM seller.order_items AS item
        LEFT JOIN seller.order_fulfillments AS fulfillment
          ON fulfillment.connection_id=item.connection_id
         AND fulfillment.external_order_id=item.external_order_id
         AND fulfillment.external_item_id=item.external_item_id
        WHERE item.connection_id=%s AND item.external_order_id=%s AND item.external_item_id=%s
        """,
        (connection_id, delivery.posting_number, delivery.item_id),
    )
    return cursor.fetchone()


def ensure_fulfillment(cursor, connection_id: int, delivery: SourceDelivery, existing) -> tuple[int, str, bool]:
    reservation_ref = f"crm:ozon:{connection_id}:{delivery.posting_number}:{delivery.item_id}"
    if existing[2] is not None:
        fulfillment_id = int(existing[2])
        if str(existing[3]) not in {"pending", "manual_required", "closed_external", "delivered"}:
            raise RuntimeError(f"Seller fulfillment id={fulfillment_id} is active ({existing[3]})")
        return fulfillment_id, str(existing[4] or reservation_ref), False
    cursor.execute(
        """
        INSERT INTO seller.order_fulfillments(
          connection_id, external_order_id, external_item_id, offer_id,
          requested_quantity, status, delivery_source, reservation_ref,
          reserved_at, submitted_at, delivered_at, created_at, updated_at
        ) VALUES (%s,%s,%s,%s,%s,'delivered',%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
        """,
        (
            connection_id, delivery.posting_number, delivery.item_id,
            delivery.offer_id, delivery.required_qty, delivery.delivery_source,
            reservation_ref, delivery.delivered_at or delivery.updated_at,
            delivery.delivered_at or delivery.updated_at,
            delivery.delivered_at or delivery.updated_at,
            delivery.created_at, delivery.updated_at,
        ),
    )
    return int(cursor.fetchone()[0]), reservation_ref, True


def import_delivery(
    cursor,
    *,
    connection_id: int,
    delivery: SourceDelivery,
    target_secret: str,
) -> tuple[int, int, int, int]:
    existing = order_and_fulfillment(cursor, connection_id, delivery)
    if not existing:
        return 0, 0, 0, 1
    if str(existing[0]) != delivery.offer_id or int(existing[1] or 0) != delivery.required_qty:
        raise RuntimeError(f"Seller order {delivery.posting_number} does not match CRM offer or quantity")
    fulfillment_id, reservation_ref, fulfillment_created = ensure_fulfillment(
        cursor, connection_id, delivery, existing,
    )
    pool_id = ensure_pool(cursor, connection_id, delivery.external_product_id)
    created_keys = 0
    linked_keys = 0
    expected_hashes = sorted(seller_key_hash(code) for code in delivery.codes)
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
    if linked_hashes and linked_hashes != expected_hashes:
        raise RuntimeError(f"Seller fulfillment id={fulfillment_id} already has another key set")
    for index, code in enumerate(delivery.codes, start=1):
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
            if int(key_row[1]) != connection_id or str(key_row[2]) != delivery.external_product_id:
                raise RuntimeError(f"CRM Ozon order id={delivery.source_id} reuses another product key")
            key_id = int(key_row[0])
        else:
            cursor.execute(
                """
                INSERT INTO seller.marketplace_keys(
                  pool_id, code_ciphertext, code_hash, code_suffix, status, key_origin,
                  issued_order_ref, issued_at, source_system, source_reference,
                  created_at, updated_at
                ) VALUES (
                  %s,pgp_sym_encrypt(%s,%s,'cipher-algo=aes256, compress-algo=0'),
                  %s,%s,'delivered','order',%s,%s,'crm',%s,%s,%s
                ) RETURNING id
                """,
                (
                    pool_id, code, target_secret, fingerprint, code[-4:], reservation_ref,
                    delivery.delivered_at or delivery.updated_at,
                    f"ozon_delivery:{delivery.source_id}:{index}",
                    delivery.created_at, delivery.updated_at,
                ),
            )
            key_id = int(cursor.fetchone()[0])
            created_keys += 1
        cursor.execute(
            """
            SELECT fulfillment_id
            FROM seller.fulfillment_key_reservations
            WHERE key_id=%s AND state IN ('reserved','consumed')
            """,
            (key_id,),
        )
        owner = cursor.fetchone()
        if owner and int(owner[0]) != fulfillment_id:
            raise RuntimeError(f"Seller key id={key_id} already belongs to another fulfillment")
        cursor.execute(
            """
            UPDATE seller.marketplace_keys
            SET status='delivered',
                issued_order_ref=CASE
                  WHEN key_origin='pool' AND issued_order_ref<>'' THEN issued_order_ref
                  ELSE %s
                END,
                issued_at=COALESCE(issued_at,%s),
                updated_at=CASE
                  WHEN key_origin='pool' THEN updated_at
                  ELSE GREATEST(updated_at,%s)
                END
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
                ) VALUES (%s,%s,'consumed',%s,%s,%s,%s,%s)
                """,
                (
                    fulfillment_id, key_id, reservation_ref,
                    delivery.delivered_at or delivery.updated_at,
                    delivery.delivered_at or delivery.updated_at,
                    delivery.created_at, delivery.updated_at,
                ),
            )
            linked_keys += 1
    cursor.execute(
        """
        UPDATE seller.order_fulfillments
        SET status='delivered', delivery_source=%s, reservation_ref=%s,
            reserved_at=COALESCE(reserved_at,%s), submitted_at=COALESCE(submitted_at,%s),
            delivered_at=COALESCE(delivered_at,%s), last_error='', updated_at=GREATEST(updated_at,%s)
        WHERE id=%s
        """,
        (
            delivery.delivery_source, reservation_ref,
            delivery.delivered_at or delivery.updated_at,
            delivery.delivered_at or delivery.updated_at,
            delivery.delivered_at or delivery.updated_at,
            delivery.updated_at, fulfillment_id,
        ),
    )
    cursor.execute(
        """
        INSERT INTO seller.fulfillment_events(
          fulfillment_id, event_type, from_status, to_status, details, created_at
        )
        SELECT %s,'crm_ozon_delivery_history_imported',%s,'delivered',
               jsonb_build_object('crm_order_id',%s::bigint,'delivery_source',%s::text),%s
        WHERE NOT EXISTS (
          SELECT 1 FROM seller.fulfillment_events
          WHERE fulfillment_id=%s AND event_type='crm_ozon_delivery_history_imported'
            AND details->>'crm_order_id'=%s::text
        )
        """,
        (
            fulfillment_id, str(existing[3] or ""), delivery.source_id,
            delivery.delivery_source, delivery.delivered_at or delivery.updated_at,
            fulfillment_id, str(delivery.source_id),
        ),
    )
    return int(fulfillment_created), created_keys, linked_keys, 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Import delivered CRM Ozon keys into Seller history")
    parser.add_argument("--source-store-code", required=True)
    parser.add_argument("--target-connection-id", type=int, required=True)
    parser.add_argument("--expected-deliveries", type=int)
    parser.add_argument("--expected-keys", type=int)
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
        source_key_count = sum(len(row.codes) for row in deliveries)
        if args.expected_deliveries is not None and len(deliveries) != args.expected_deliveries:
            raise RuntimeError(f"Expected {args.expected_deliveries} deliveries, received {len(deliveries)}")
        if args.expected_keys is not None and source_key_count != args.expected_keys:
            raise RuntimeError(f"Expected {args.expected_keys} keys, received {source_key_count}")
        display_name = target_context(target, args.target_connection_id)
        with target.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout='3s'")
            cursor.execute("SET LOCAL statement_timeout='30s'")
            for delivery in deliveries:
                values = import_delivery(
                    cursor,
                    connection_id=args.target_connection_id,
                    delivery=delivery,
                    target_secret=target_secret,
                )
                for name, value in zip(totals, values, strict=True):
                    totals[name] += value
        if args.apply:
            target.commit()
        else:
            target.rollback()
    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "source_store_code": args.source_store_code,
        "seller_connection_id": args.target_connection_id,
        "seller_connection_name": display_name,
        "source_deliveries": len(deliveries),
        "source_keys": source_key_count,
        "supplier_deliveries": sum(row.delivery_source == "supplier" for row in deliveries),
        "pool_deliveries": sum(row.delivery_source == "pool" for row in deliveries),
        "manual_deliveries": sum(row.delivery_source == "manual" for row in deliveries),
        **totals,
        "external_api_calls": 0,
        "seller_execution_flags_changed": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
