"""Перепроверяет цены настроенных карточек через безопасный quote Supplier Hub.

Quote вызывает только Interhub calculate: check/pay и покупка не выполняются.
По умолчанию изменения БД не сохраняются; ``--apply`` обновляет цену и лимит.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from decimal import Decimal, ROUND_UP

import psycopg

# Позволяет одинаково запускать скрипт как модуль и как ``python scripts/...``
# внутри production-контейнера с WORKDIR=/app.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domains.supplier_hub_client import SupplierHubClient, load_supplier_hub_settings
from domains.workspace_entitlements import SUPPLIER_MAPPING_MANAGE, workspace_allows


def price_limit(amount: Decimal, percent: Decimal) -> Decimal:
    return (amount * (Decimal("1") + percent / Decimal("100"))).quantize(
        Decimal("0.01"), rounding=ROUND_UP,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh Seller Interhub quotes without purchases")
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--price-buffer-percent", type=Decimal, default=Decimal("5"))
    parser.add_argument("--delay-ms", type=int, default=200)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.price_buffer_percent < 0 or args.price_buffer_percent > 50:
        raise RuntimeError("price buffer must be between 0 and 50 percent")
    database_url = str(os.getenv("DATABASE_URL", "")).strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, fulfillment_reservation_enabled, fulfillment_outbound_enabled,
                       supplier_fulfillment_enabled, workspace_id
                FROM seller.marketplace_connections
                WHERE provider_code='yandex_market' AND campaign_id=%s
                """,
                (args.campaign_id,),
            )
            rows = cursor.fetchall()
            if len(rows) != 1:
                raise RuntimeError(f"campaign_id={args.campaign_id} matches {len(rows)} Seller connections")
            connection_id = int(rows[0][0])
            if any(bool(value) for value in rows[0][1:4]):
                raise RuntimeError("Store fulfillment gates must stay disabled while quotes are prepared")
            if not workspace_allows(cursor, int(rows[0][4]), SUPPLIER_MAPPING_MANAGE):
                raise RuntimeError("Supplier Hub mappings require the Pro plan")
            cursor.execute(
                """
                SELECT id, external_product_id, service_id, nominal_id, params,
                       quoted_amount, max_amount
                FROM seller.product_supplier_mappings
                WHERE connection_id=%s AND enabled=true
                ORDER BY external_product_id, priority, id
                """,
                (connection_id,),
            )
            mappings = list(cursor.fetchall())
    client = SupplierHubClient(load_supplier_hub_settings())
    if args.limit is not None:
        mappings = mappings[:max(0, args.limit)]

    refreshed = []
    errors = []
    for index, row in enumerate(mappings):
        mapping_id, external_id, service_id, nominal_id, params, previous_quote, previous_limit = row
        try:
            result = client.quote(
                service_id=int(service_id), nominal_id=str(nominal_id or ""),
                params=params if isinstance(params, dict) else {},
            )
            if not bool(result.get("success")):
                raise RuntimeError(str(result.get("message") or "quote failed"))
            amount = Decimal(str(result.get("fixed_amount") or "0"))
            if amount <= 0:
                raise RuntimeError("quote returned a non-positive amount")
            refreshed.append((int(mapping_id), str(external_id), amount, price_limit(amount, args.price_buffer_percent)))
        except Exception as exc:
            errors.append({"external_product_id": str(external_id), "error": str(exc)[:300]})
        if index + 1 < len(mappings) and args.delay_ms > 0:
            time.sleep(min(args.delay_ms, 5000) / 1000)

    if args.apply:
        if not errors:
            with psycopg.connect(database_url) as connection:
                with connection.transaction():
                    with connection.cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout='3s'")
                        cursor.execute("SET LOCAL statement_timeout='30s'")
                        cursor.executemany(
                            """
                            UPDATE seller.product_supplier_mappings
                            SET quoted_amount=%s, quoted_at=now(), max_amount=%s, updated_at=now()
                            WHERE id=%s
                            """,
                            [(amount, limit, mapping_id) for mapping_id, _, amount, limit in refreshed],
                        )

    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "operation": "calculate-only-no-purchase",
        "campaign_id": args.campaign_id,
        "mappings": len(mappings),
        "refreshed": len(refreshed),
        "errors": errors,
        "price_buffer_percent": str(args.price_buffer_percent),
        "sample": [
            {"offer_id": external_id, "quote": str(amount), "max_amount": str(limit)}
            for _, external_id, amount, limit in refreshed[:10]
        ],
    }, ensure_ascii=False, indent=2))
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
