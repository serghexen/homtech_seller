"""Переносит настройки карточек, связки и недостающие заказы Ozon из CRM.

CRM всегда открывается только для чтения. Seller изменяется лишь с ``--apply``.
Скрипт не вызывает Ozon или Supplier Hub и требует выключенных исполнительных
флагов целевого магазина.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal, ROUND_UP
import json
import os
from typing import Any

import psycopg


@dataclass(frozen=True)
class SourceSetting:
    external_product_id: str
    offer_id: str
    manual_stock_limit: int
    published_stock: int
    activation_instruction: str
    support_message: str
    supplier_enabled: bool
    pool_enabled: bool
    last_stock_sync_at: Any
    last_stock_sync_error: str
    updated_at: Any


@dataclass(frozen=True)
class SourceMapping:
    external_product_id: str
    enabled: bool
    service_id: int
    nominal_id: str
    params: dict[str, Any]
    quoted_amount: Decimal
    quoted_at: Any
    updated_at: Any


@dataclass(frozen=True)
class SourceOrder:
    source_id: int
    external_product_id: str
    posting_number: str
    order_number: str
    product_name: str
    sku: str
    required_qty: int
    status: str
    ozon_status: str
    created_at: Any
    delivered_at: Any
    updated_at: Any


def guarded_max_amount(quote: Decimal, buffer_percent: Decimal) -> Decimal:
    return (quote * (Decimal("1") + buffer_percent / Decimal("100"))).quantize(
        Decimal("0.01"), rounding=ROUND_UP,
    )


def read_source(source, store_code: str) -> tuple[list[SourceSetting], list[SourceMapping], list[SourceOrder], int]:
    with source.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute("SET statement_timeout='10s'")
        cursor.execute("SET lock_timeout='1s'")
        cursor.execute(
            """
            SELECT count(*)
            FROM app.marketplace_ozon_digital_orders
            WHERE lower(store_code)=lower(%s)
              AND status NOT IN ('delivered','cancelled')
            """,
            (store_code,),
        )
        inflight = int(cursor.fetchone()[0] or 0)
        cursor.execute(
            """
            SELECT external_product_id, offer_id, manual_stock_limit, published_stock,
                   activation_instruction, support_error_message, auto_issue_enabled,
                   pool_issue_enabled, last_stock_sync_at, last_stock_sync_error, updated_at
            FROM app.marketplace_ozon_digital_settings
            WHERE lower(store_code)=lower(%s)
            ORDER BY external_product_id
            """,
            (store_code,),
        )
        settings = [
            SourceSetting(
                external_product_id=str(row[0]), offer_id=str(row[1] or "").strip(),
                manual_stock_limit=int(row[2] or 0), published_stock=int(row[3] or 0),
                activation_instruction=str(row[4] or "").strip(),
                support_message=str(row[5] or "").strip(), supplier_enabled=bool(row[6]),
                pool_enabled=bool(row[7]), last_stock_sync_at=row[8],
                last_stock_sync_error=str(row[9] or ""), updated_at=row[10],
            )
            for row in cursor.fetchall()
        ]
        cursor.execute(
            """
            SELECT supplier.external_product_id, supplier.enabled, supplier.service_id,
                   supplier.nominal_id, supplier.params, price.fixed_amount,
                   price.calculated_at, supplier.updated_at
            FROM app.marketplace_ozon_digital_suppliers AS supplier
            LEFT JOIN LATERAL (
              SELECT calculation.fixed_amount, calculation.calculated_at
              FROM app.interhub_price_calculations AS calculation
              WHERE calculation.service_id=supplier.service_id
                AND calculation.nominal_id=CASE
                  WHEN supplier.nominal_id ~ '^[0-9]+$' THEN supplier.nominal_id::integer ELSE 0 END
                AND calculation.success=true AND calculation.fixed_amount>0
              ORDER BY calculation.calculated_at DESC, calculation.id DESC
              LIMIT 1
            ) AS price ON true
            WHERE lower(supplier.store_code)=lower(%s)
              AND supplier.provider_code='interhub' AND supplier.priority=1
            ORDER BY supplier.external_product_id
            """,
            (store_code,),
        )
        mappings: list[SourceMapping] = []
        for row in cursor.fetchall():
            if row[2] is None or row[5] is None:
                raise RuntimeError(f"Ozon product {row[0]} has no complete supplier mapping or price")
            mappings.append(SourceMapping(
                external_product_id=str(row[0]), enabled=bool(row[1]), service_id=int(row[2]),
                nominal_id=str(row[3] or ""), params=dict(row[4]) if isinstance(row[4], dict) else {},
                quoted_amount=Decimal(str(row[5])), quoted_at=row[6], updated_at=row[7],
            ))
        cursor.execute(
            """
            SELECT id, external_product_id, posting_number, order_number, product_name,
                   sku, required_qty, status, ozon_status, created_at, delivered_at, updated_at
            FROM app.marketplace_ozon_digital_orders
            WHERE lower(store_code)=lower(%s)
            ORDER BY id
            """,
            (store_code,),
        )
        orders = [
            SourceOrder(
                source_id=int(row[0]), external_product_id=str(row[1]),
                posting_number=str(row[2] or "").strip(), order_number=str(row[3] or "").strip(),
                product_name=str(row[4] or "").strip(), sku=str(row[5] or "").strip(),
                required_qty=int(row[6] or 0), status=str(row[7] or "").strip().lower(),
                ozon_status=str(row[8] or "").strip(), created_at=row[9], delivered_at=row[10],
                updated_at=row[11],
            )
            for row in cursor.fetchall()
        ]
    source.commit()
    return settings, mappings, orders, inflight


def target_context(target, connection_id: int) -> tuple[str, dict[str, tuple[str, str]]]:
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
        cursor.execute(
            """
            SELECT external_product_id, offer_id, title
            FROM seller.catalog_items
            WHERE connection_id=%s AND is_present=true
            """,
            (connection_id,),
        )
        catalog = {str(item[0]): (str(item[1] or ""), str(item[2] or "")) for item in cursor.fetchall()}
    return str(row[0]), catalog


def existing_order_keys(target, connection_id: int) -> dict[tuple[str, str], tuple[str, int]]:
    with target.cursor() as cursor:
        cursor.execute(
            """
            SELECT external_order_id, external_item_id, offer_id, quantity
            FROM seller.order_items WHERE connection_id=%s
            """,
            (connection_id,),
        )
        return {(str(row[0]), str(row[2] or "")): (str(row[1]), int(row[3] or 0)) for row in cursor.fetchall()}


def apply_snapshot(
    target,
    *,
    connection_id: int,
    settings: list[SourceSetting],
    mappings: list[SourceMapping],
    orders: list[SourceOrder],
    catalog: dict[str, tuple[str, str]],
    buffer_percent: Decimal,
) -> None:
    with target.transaction(), target.cursor() as cursor:
        cursor.execute("SET LOCAL lock_timeout='3s'")
        cursor.execute("SET LOCAL statement_timeout='30s'")
        for setting in settings:
            external_id = setting.external_product_id
            cursor.execute(
                """
                INSERT INTO seller.product_card_settings(
                  connection_id, external_product_id, manual_stock_limit, activation_instruction,
                  pool_issue_enabled, support_message, support_message_delivery_enabled,
                  support_message_overridden, published_stock, last_stock_sync_at,
                  last_stock_sync_error, updated_by_user_id, created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,false,false,%s,%s,%s,NULL,%s,%s)
                ON CONFLICT (connection_id, external_product_id) DO UPDATE SET
                  manual_stock_limit=EXCLUDED.manual_stock_limit,
                  activation_instruction=EXCLUDED.activation_instruction,
                  pool_issue_enabled=EXCLUDED.pool_issue_enabled,
                  support_message=EXCLUDED.support_message,
                  support_message_delivery_enabled=false,
                  support_message_overridden=false,
                  published_stock=EXCLUDED.published_stock,
                  last_stock_sync_at=EXCLUDED.last_stock_sync_at,
                  last_stock_sync_error=EXCLUDED.last_stock_sync_error,
                  updated_at=EXCLUDED.updated_at
                WHERE seller.product_card_settings.updated_by_user_id IS NULL
                """,
                (
                    connection_id, external_id, setting.manual_stock_limit,
                    setting.activation_instruction, setting.pool_enabled, setting.support_message,
                    setting.published_stock, setting.last_stock_sync_at,
                    setting.last_stock_sync_error, setting.updated_at, setting.updated_at,
                ),
            )
            cursor.execute(
                """
                INSERT INTO seller.product_fulfillment_policies(
                  connection_id, external_product_id, supplier_issue_enabled,
                  pool_issue_enabled, support_message_delivery_enabled,
                  source_system, source_updated_at
                ) VALUES (%s,%s,%s,%s,false,'crm',%s)
                ON CONFLICT (connection_id, external_product_id) DO UPDATE SET
                  supplier_issue_enabled=EXCLUDED.supplier_issue_enabled,
                  pool_issue_enabled=EXCLUDED.pool_issue_enabled,
                  support_message_delivery_enabled=false,
                  source_updated_at=EXCLUDED.source_updated_at, updated_at=now()
                WHERE seller.product_fulfillment_policies.source_system='crm'
                """,
                (connection_id, external_id, setting.supplier_enabled, setting.pool_enabled, setting.updated_at),
            )
        for mapping in mappings:
            cursor.execute(
                """
                INSERT INTO seller.product_supplier_mappings(
                  connection_id, external_product_id, provider_code, priority, enabled,
                  service_id, nominal_id, params, max_amount, quoted_amount, quoted_at,
                  source_system, source_updated_at
                ) VALUES (%s,%s,'interhub',1,%s,%s,%s,%s::jsonb,%s,%s,%s,'crm',%s)
                ON CONFLICT (connection_id, external_product_id, provider_code, priority) DO UPDATE SET
                  enabled=EXCLUDED.enabled, service_id=EXCLUDED.service_id,
                  nominal_id=EXCLUDED.nominal_id, params=EXCLUDED.params,
                  max_amount=EXCLUDED.max_amount, quoted_amount=EXCLUDED.quoted_amount,
                  quoted_at=EXCLUDED.quoted_at, source_updated_at=EXCLUDED.source_updated_at,
                  updated_at=now()
                WHERE seller.product_supplier_mappings.source_system='crm'
                """,
                (
                    connection_id, mapping.external_product_id, mapping.enabled,
                    mapping.service_id, mapping.nominal_id,
                    json.dumps(mapping.params, ensure_ascii=False),
                    guarded_max_amount(mapping.quoted_amount, buffer_percent),
                    mapping.quoted_amount, mapping.quoted_at, mapping.updated_at,
                ),
            )
        for order in orders:
            offer_id, catalog_title = catalog[order.external_product_id]
            provider_status = "cancelled" if order.status == "cancelled" else "delivered"
            raw_payload = {
                "__migration_source": "crm_ozon",
                "__crm_ozon_order_id": order.source_id,
                "__marketplace_source": "DIGITAL",
                "posting_number": order.posting_number,
                "order_id": order.order_number,
                "status": provider_status,
                "substatus": order.ozon_status,
                "products": [{
                    "product_id": int(order.external_product_id),
                    "offer_id": offer_id,
                    "sku": int(order.sku) if order.sku.isdigit() else order.sku,
                    "name": order.product_name or catalog_title,
                    "quantity": order.required_qty,
                }],
            }
            cursor.execute(
                """
                INSERT INTO seller.order_items(
                  connection_id, external_order_id, external_item_id, offer_id, sku, title,
                  quantity, provider_status, provider_substatus, normalized_status,
                  delivery_type, created_at, updated_at, raw_payload, synced_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'DIGITAL',%s,%s,%s::jsonb,now())
                ON CONFLICT (connection_id, external_order_id, external_item_id) DO NOTHING
                """,
                (
                    connection_id, order.posting_number, order.sku,
                    offer_id, order.sku, order.product_name or catalog_title,
                    order.required_qty, provider_status, order.ozon_status,
                    provider_status, order.created_at, order.updated_at,
                    json.dumps(raw_payload, ensure_ascii=False),
                ),
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Import CRM Ozon settings, mappings and order history")
    parser.add_argument("--source-store-code", required=True)
    parser.add_argument("--target-connection-id", type=int, required=True)
    parser.add_argument("--expected-settings", type=int)
    parser.add_argument("--expected-mappings", type=int)
    parser.add_argument("--expected-orders", type=int)
    parser.add_argument("--price-buffer-percent", type=Decimal, default=Decimal("5"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.price_buffer_percent < 0 or args.price_buffer_percent > 50:
        raise RuntimeError("price buffer must be between 0 and 50 percent")
    source_dsn = str(os.getenv("CRM_DATABASE_URL", "")).strip()
    target_dsn = str(os.getenv("DATABASE_URL", "")).strip()
    if not source_dsn or not target_dsn or source_dsn == target_dsn:
        raise RuntimeError("Distinct CRM_DATABASE_URL and DATABASE_URL are required")

    with psycopg.connect(source_dsn) as source, psycopg.connect(target_dsn) as target:
        settings, mappings, orders, inflight = read_source(source, args.source_store_code)
        for expected, rows, label in (
            (args.expected_settings, settings, "settings"),
            (args.expected_mappings, mappings, "mappings"),
            (args.expected_orders, orders, "orders"),
        ):
            if expected is not None and len(rows) != expected:
                raise RuntimeError(f"Expected {expected} {label}, received {len(rows)}")
        if inflight:
            raise RuntimeError(f"CRM has {inflight} in-flight Ozon orders; import is blocked")
        display_name, catalog = target_context(target, args.target_connection_id)
        source_product_ids = {
            *(row.external_product_id for row in settings),
            *(row.external_product_id for row in mappings),
            *(row.external_product_id for row in orders),
        }
        missing_products = sorted(source_product_ids - set(catalog))
        if missing_products:
            raise RuntimeError(f"{len(missing_products)} CRM Ozon products are missing in Seller catalog")
        with target.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*) FROM seller.product_card_settings
                WHERE connection_id=%s AND updated_by_user_id IS NOT NULL
                  AND external_product_id=ANY(%s)
                """,
                (args.target_connection_id, [row.external_product_id for row in settings]),
            )
            local_conflicts = int(cursor.fetchone()[0] or 0)
            cursor.execute(
                """
                SELECT count(*) FROM seller.product_fulfillment_policies
                WHERE connection_id=%s AND source_system='seller'
                  AND external_product_id=ANY(%s)
                """,
                (args.target_connection_id, [row.external_product_id for row in settings]),
            )
            policy_conflicts = int(cursor.fetchone()[0] or 0)
            cursor.execute(
                """
                SELECT count(*) FROM seller.product_supplier_mappings
                WHERE connection_id=%s AND source_system='seller'
                  AND external_product_id=ANY(%s)
                """,
                (args.target_connection_id, [row.external_product_id for row in mappings]),
            )
            mapping_conflicts = int(cursor.fetchone()[0] or 0)
        if local_conflicts or policy_conflicts or mapping_conflicts:
            raise RuntimeError(
                f"Seller has local edits: settings={local_conflicts}, policies={policy_conflicts}, mappings={mapping_conflicts}"
            )
        existing_orders = existing_order_keys(target, args.target_connection_id)
        missing_orders = [
            row for row in orders
            if (row.posting_number, catalog[row.external_product_id][0]) not in existing_orders
        ]
        delivered_mismatches = [
            row.source_id for row in orders
            if row.status == "delivered"
            and (row.posting_number, catalog[row.external_product_id][0]) in existing_orders
            and existing_orders[(row.posting_number, catalog[row.external_product_id][0])][1]
                != row.required_qty
        ]
        if delivered_mismatches:
            raise RuntimeError(f"{len(delivered_mismatches)} delivered orders differ between CRM and Seller")
        apply_snapshot(
            target,
            connection_id=args.target_connection_id,
            settings=settings,
            mappings=mappings,
            orders=orders,
            catalog=catalog,
            buffer_percent=args.price_buffer_percent,
        )
        if args.apply:
            target.commit()
        else:
            target.rollback()

    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "source_store_code": args.source_store_code,
        "seller_connection_id": args.target_connection_id,
        "seller_connection_name": display_name,
        "settings": len(settings),
        "supplier_enabled": sum(row.supplier_enabled for row in settings),
        "pool_enabled": sum(row.pool_enabled for row in settings),
        "supplier_mappings": len(mappings),
        "source_orders": len(orders),
        "existing_orders": len(orders) - len(missing_orders),
        "orders_to_insert": len(missing_orders),
        "crm_inflight_orders": inflight,
        "external_api_calls": 0,
        "seller_execution_flags_changed": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
