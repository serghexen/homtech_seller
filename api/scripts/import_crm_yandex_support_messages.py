"""Импортирует из CRM только сообщения ручной выдачи Яндекс Маркета.

JSONL читается из заранее подготовленного read-only экспорта. Остатки, лимиты,
инструкции и другие поля снимка этот скрипт намеренно не изменяет.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg

from import_crm_yandex_settings import (
    catalog_external_ids,
    connection_for_campaign,
    read_source_rows,
)


def existing_support_settings(connection, connection_id: int) -> dict[str, tuple[str, bool]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT external_product_id, support_message, support_message_delivery_enabled
            FROM seller.yandex_product_settings_snapshot
            WHERE connection_id=%s
            """,
            (connection_id,),
        )
        rows = cursor.fetchall()
    return {
        str(external_product_id): (str(message or ""), bool(enabled))
        for external_product_id, message, enabled in rows
    }


def import_support_settings(connection, connection_id: int, rows) -> None:
    statement = """
        UPDATE seller.yandex_product_settings_snapshot
        SET support_message=%s,
            support_message_delivery_enabled=%s
        WHERE connection_id=%s
          AND external_product_id=%s
          AND (support_message, support_message_delivery_enabled)
              IS DISTINCT FROM (%s, %s)
    """
    values = [
        (
            row.support_message,
            row.support_message_delivery_enabled,
            connection_id,
            external_product_id,
            row.support_message,
            row.support_message_delivery_enabled,
        )
        for row, external_product_id in rows
    ]
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout TO '3s'")
            cursor.execute("SET LOCAL statement_timeout TO '30s'")
            cursor.executemany(statement, values)


def main() -> int:
    parser = argparse.ArgumentParser(description="Import only CRM Yandex support messages into Seller")
    parser.add_argument("--input", default="-", help="JSONL file or - for stdin")
    parser.add_argument("--source-store-code", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    args = parser.parse_args()

    if not args.database_url:
        raise RuntimeError("DATABASE_URL is required")
    stream = sys.stdin if args.input == "-" else Path(args.input).open("r", encoding="utf-8")
    try:
        source_rows = read_source_rows(stream)
    finally:
        if stream is not sys.stdin:
            stream.close()
    if args.expected_count is not None and len(source_rows) != args.expected_count:
        raise RuntimeError(f"Expected {args.expected_count} rows, received {len(source_rows)}")
    wrong_store = [row.offer_id for row in source_rows if row.source_store_code != args.source_store_code]
    if wrong_store:
        raise RuntimeError(f"Source contains {len(wrong_store)} rows for another store")

    with psycopg.connect(args.database_url) as connection:
        connection_id, display_name, status = connection_for_campaign(connection, str(args.campaign_id))
        catalog = catalog_external_ids(connection, connection_id)
        missing_catalog = [row.offer_id for row in source_rows if row.offer_id not in catalog]
        if missing_catalog:
            raise RuntimeError(f"{len(missing_catalog)} source offers are missing in Seller catalog")
        prepared = [(row, catalog[row.offer_id]) for row in source_rows]
        existing = existing_support_settings(connection, connection_id)
        missing_snapshot = [external_id for _, external_id in prepared if external_id not in existing]
        if missing_snapshot:
            raise RuntimeError(f"{len(missing_snapshot)} Seller settings snapshots are missing")
        changed = [
            (row, external_id)
            for row, external_id in prepared
            if existing[external_id] != (row.support_message, row.support_message_delivery_enabled)
        ]
        if args.apply:
            import_support_settings(connection, connection_id, changed)

    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "scope": "support-messages-only",
        "source_store_code": args.source_store_code,
        "campaign_id": str(args.campaign_id),
        "seller_connection_id": connection_id,
        "seller_connection_name": display_name,
        "seller_connection_status": status,
        "source_rows": len(source_rows),
        "filled_messages": sum(bool(row.support_message) for row in source_rows),
        "enabled_messages": sum(bool(row.support_message and row.support_message_delivery_enabled) for row in source_rows),
        "updated": len(changed),
        "unchanged": len(source_rows) - len(changed),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
