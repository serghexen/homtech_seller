"""Безопасно доимпортирует политику выдачи и связи Interhub одного магазина CRM.

CRM открывается в read-only режиме с коротким timeout. По умолчанию выполняется
только сверка; Seller изменяется лишь с ``--apply``. Скрипт не вызывает API
поставщика, не создаёт покупок и не включает магазинные переключатели.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_UP
from typing import Any

import psycopg


@dataclass(frozen=True)
class SourcePolicy:
    offer_id: str
    supplier_enabled: bool
    pool_enabled: bool
    support_enabled: bool
    service_id: int | None
    nominal_id: str
    params: dict[str, Any]
    mapping_enabled: bool
    quoted_amount: Decimal | None
    quoted_at: Any
    source_updated_at: Any


def read_source(source, store_code: str) -> tuple[list[SourcePolicy], int]:
    with source.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute("SET statement_timeout='5s'")
        cursor.execute("SET lock_timeout='1s'")
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM app.marketplace_yandex_digital_deliveries
            WHERE lower(store_code)=lower(%s)
              AND status IN ('supplier_processing','market_sending','market_submitted','market_unknown')
            """,
            (store_code,),
        )
        inflight = int(cursor.fetchone()[0] or 0)
        cursor.execute(
            """
            SELECT settings.offer_id, settings.auto_issue_enabled,
                   settings.pool_issue_enabled, settings.support_message_delivery_enabled,
                   supplier.service_id, supplier.nominal_id, supplier.params, supplier.enabled,
                   price.fixed_amount, price.calculated_at,
                   GREATEST(settings.updated_at, COALESCE(supplier.updated_at, settings.updated_at))
            FROM app.marketplace_yandex_stock_settings AS settings
            LEFT JOIN app.marketplace_yandex_digital_suppliers AS supplier
              ON supplier.store_code=settings.store_code AND supplier.offer_id=settings.offer_id
             AND supplier.provider_code='interhub' AND supplier.priority=1
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
            WHERE lower(settings.store_code)=lower(%s)
            ORDER BY settings.offer_id
            """,
            (store_code,),
        )
        rows = cursor.fetchall()
    source.commit()
    result = []
    for row in rows:
        params = row[6] if isinstance(row[6], dict) else {}
        result.append(SourcePolicy(
            offer_id=str(row[0]), supplier_enabled=bool(row[1]), pool_enabled=bool(row[2]),
            support_enabled=bool(row[3]), service_id=int(row[4]) if row[4] is not None else None,
            nominal_id=str(row[5] or ""), params=dict(params), mapping_enabled=bool(row[7]),
            quoted_amount=Decimal(str(row[8])) if row[8] is not None else None,
            quoted_at=row[9], source_updated_at=row[10],
        ))
    return result, inflight


def target_context(target, campaign_id: str) -> tuple[int, str, dict[str, str]]:
    with target.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, display_name, fulfillment_reservation_enabled,
                   fulfillment_outbound_enabled, supplier_fulfillment_enabled
            FROM seller.marketplace_connections
            WHERE provider_code='yandex_market' AND campaign_id=%s
            """,
            (campaign_id,),
        )
        rows = cursor.fetchall()
        if len(rows) != 1:
            raise RuntimeError(f"campaign_id={campaign_id} matches {len(rows)} Seller connections")
        row = rows[0]
        if any(bool(value) for value in row[2:5]):
            raise RuntimeError("Seller store fulfillment gates must stay disabled during CRM import")
        cursor.execute(
            """
            SELECT offer_id, external_product_id FROM seller.catalog_items
            WHERE connection_id=%s AND is_present=true
            """,
            (int(row[0]),),
        )
        catalog = {str(offer): str(external_id) for offer, external_id in cursor.fetchall() if str(offer or "").strip()}
    return int(row[0]), str(row[1]), catalog


def max_amount(quote: Decimal, buffer_percent: Decimal) -> Decimal:
    return (quote * (Decimal("1") + buffer_percent / Decimal("100"))).quantize(
        Decimal("0.01"), rounding=ROUND_UP,
    )


def apply_rows(target, connection_id: int, prepared, buffer_percent: Decimal) -> None:
    with target.transaction():
        with target.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout='3s'")
            cursor.execute("SET LOCAL statement_timeout='30s'")
            for source, external_id in prepared:
                cursor.execute(
                    """
                    INSERT INTO seller.product_fulfillment_policies(
                      connection_id, external_product_id, supplier_issue_enabled,
                      pool_issue_enabled, support_message_delivery_enabled,
                      source_system, source_updated_at
                    ) VALUES (%s,%s,%s,%s,%s,'crm',%s)
                    ON CONFLICT (connection_id, external_product_id) DO UPDATE SET
                      supplier_issue_enabled=EXCLUDED.supplier_issue_enabled,
                      pool_issue_enabled=EXCLUDED.pool_issue_enabled,
                      support_message_delivery_enabled=EXCLUDED.support_message_delivery_enabled,
                      source_updated_at=EXCLUDED.source_updated_at, updated_at=now()
                    WHERE seller.product_fulfillment_policies.source_system='crm'
                    """,
                    (
                        connection_id, external_id, source.supplier_enabled,
                        source.pool_enabled, source.support_enabled, source.source_updated_at,
                    ),
                )
                if not source.service_id or not source.quoted_amount:
                    continue
                cursor.execute(
                    """
                    INSERT INTO seller.product_supplier_mappings(
                      connection_id, external_product_id, provider_code, priority,
                      enabled, service_id, nominal_id, params, max_amount,
                      quoted_amount, quoted_at, source_system, source_updated_at
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
                        connection_id, external_id, source.mapping_enabled, source.service_id,
                        source.nominal_id, json.dumps(source.params, ensure_ascii=False),
                        max_amount(source.quoted_amount, buffer_percent), source.quoted_amount,
                        source.quoted_at, source.source_updated_at,
                    ),
                )


def main() -> int:
    parser = argparse.ArgumentParser(description="Import Yandex fulfillment policy from CRM into Seller")
    parser.add_argument("--source-store-code", required=True)
    parser.add_argument("--target-campaign-id", required=True)
    parser.add_argument("--expected-count", type=int)
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
        source_rows, inflight = read_source(source, args.source_store_code)
        if inflight:
            raise RuntimeError(f"CRM has {inflight} in-flight deliveries; import is blocked")
        if args.expected_count is not None and len(source_rows) != args.expected_count:
            raise RuntimeError(f"Expected {args.expected_count} rows, received {len(source_rows)}")
        connection_id, display_name, catalog = target_context(target, args.target_campaign_id)
        prepared = [(row, catalog[row.offer_id]) for row in source_rows if row.offer_id in catalog]
        missing = [row.offer_id for row in source_rows if row.offer_id not in catalog]
        invalid_supplier = [
            row.offer_id for row in source_rows
            if row.supplier_enabled and (
                not row.mapping_enabled or not row.service_id or not row.quoted_amount
            )
        ]
        if invalid_supplier:
            raise RuntimeError(
                f"{len(invalid_supplier)} supplier-enabled rows have no complete mapping/price: "
                f"{', '.join(invalid_supplier[:10])}"
            )
        with target.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) FROM seller.product_fulfillment_policies
                WHERE connection_id=%s AND source_system='seller'
                  AND external_product_id=ANY(%s)
                """,
                (connection_id, [external_id for _, external_id in prepared]),
            )
            conflicts = int(cursor.fetchone()[0] or 0)
        if conflicts:
            raise RuntimeError(f"{conflicts} cards were edited in Seller; CRM import will not overwrite them")
        if args.apply:
            apply_rows(target, connection_id, prepared, args.price_buffer_percent)

    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "source_store_code": args.source_store_code,
        "seller_connection_id": connection_id,
        "seller_connection_name": display_name,
        "source_rows": len(source_rows),
        "prepared": len(prepared),
        "missing_in_seller_catalog": len(missing),
        "missing_offer_ids": missing,
        "supplier_enabled": sum(row.supplier_enabled for row in source_rows),
        "pool_enabled": sum(row.pool_enabled for row in source_rows),
        "support_enabled": sum(row.support_enabled for row in source_rows),
        "supplier_prices_found": sum(row.quoted_amount is not None for row in source_rows),
        "price_buffer_percent": str(args.price_buffer_percent),
        "crm_inflight_deliveries": inflight,
        "seller_fulfillment_gates_changed": False,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
