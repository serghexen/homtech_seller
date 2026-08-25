"""Проверяет и импортирует JSONL-снимок настроек Яндекс Маркета из CRM.

Скрипт никогда не подключается к CRM и не может изменить её базу. Источник передаётся
готовым JSONL-потоком, а запись разрешается только явным флагом ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, TextIO
from zoneinfo import ZoneInfo

import psycopg


MAX_OFFER_ID_LENGTH = 256
MAX_INSTRUCTION_LENGTH = 5000
MAX_SUPPORT_MESSAGE_LENGTH = 2000
MOSCOW_TZ = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True)
class SourceSettings:
    source_store_code: str
    offer_id: str
    manual_stock_limit: int
    published_stock: int
    activation_instruction: str
    support_message: str
    support_message_delivery_enabled: bool
    sales_limit: int | None
    sales_limit_daily_extra: int
    sales_limit_day: date | None
    sales_limit_revision: int
    sales_limit_used: int
    sales_limit_reserved: int
    sales_limit_remaining: int | None
    sales_limit_exhausted_at: datetime | None
    archived_by_sales_limit: bool
    last_stock_sync_at: datetime | None
    source_updated_at: datetime


def required_text(value: Any, *, field: str, max_length: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    if len(text) > max_length:
        raise ValueError(f"{field} is longer than {max_length} characters")
    return text


def nonnegative_int(value: Any, *, field: str, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if number < 0:
        raise ValueError(f"{field} must be nonnegative")
    return number


def optional_positive_int(value: Any, *, field: str) -> int | None:
    if value is None or value == "":
        return None
    number = nonnegative_int(value, field=field)
    if number <= 0:
        raise ValueError(f"{field} must be positive or null")
    return number


def optional_date(value: Any, *, field: str) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc


def optional_datetime(value: Any, *, field: str, required: bool = False) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        if required:
            raise ValueError(f"{field} is required")
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO datetime") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include timezone")
    return parsed


def boolean_value(value: Any, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"", "0", "false", "no", "off"}:
        return False
    raise ValueError(f"{field} must be boolean")


def normalize_source_row(payload: dict[str, Any]) -> SourceSettings:
    instruction = str(payload.get("activation_instruction") or "").strip()
    if len(instruction) > MAX_INSTRUCTION_LENGTH:
        raise ValueError(f"activation_instruction is longer than {MAX_INSTRUCTION_LENGTH} characters")
    support_message = str(payload.get("support_message") or "").strip()
    if len(support_message) > MAX_SUPPORT_MESSAGE_LENGTH:
        raise ValueError(f"support_message is longer than {MAX_SUPPORT_MESSAGE_LENGTH} characters")
    sales_limit = optional_positive_int(payload.get("sales_limit"), field="sales_limit")
    remaining_value = payload.get("sales_limit_remaining")
    remaining = None if sales_limit is None else nonnegative_int(remaining_value, field="sales_limit_remaining")
    source_updated_at = optional_datetime(payload.get("source_updated_at"), field="source_updated_at", required=True)
    assert source_updated_at is not None
    return SourceSettings(
        source_store_code=required_text(payload.get("source_store_code"), field="source_store_code", max_length=64),
        offer_id=required_text(payload.get("offer_id"), field="offer_id", max_length=MAX_OFFER_ID_LENGTH),
        manual_stock_limit=nonnegative_int(payload.get("manual_stock_limit"), field="manual_stock_limit"),
        published_stock=nonnegative_int(payload.get("published_stock"), field="published_stock"),
        activation_instruction=instruction,
        support_message=support_message,
        support_message_delivery_enabled=boolean_value(
            payload.get("support_message_delivery_enabled", False), field="support_message_delivery_enabled",
        ),
        sales_limit=sales_limit,
        sales_limit_daily_extra=nonnegative_int(payload.get("sales_limit_daily_extra"), field="sales_limit_daily_extra"),
        sales_limit_day=optional_date(payload.get("sales_limit_day"), field="sales_limit_day"),
        sales_limit_revision=nonnegative_int(payload.get("sales_limit_revision"), field="sales_limit_revision"),
        sales_limit_used=nonnegative_int(payload.get("sales_limit_used"), field="sales_limit_used"),
        sales_limit_reserved=nonnegative_int(payload.get("sales_limit_reserved"), field="sales_limit_reserved"),
        sales_limit_remaining=remaining,
        sales_limit_exhausted_at=optional_datetime(
            payload.get("sales_limit_exhausted_at"), field="sales_limit_exhausted_at",
        ),
        archived_by_sales_limit=boolean_value(
            payload.get("archived_by_sales_limit", False), field="archived_by_sales_limit",
        ),
        last_stock_sync_at=optional_datetime(payload.get("last_stock_sync_at"), field="last_stock_sync_at"),
        source_updated_at=source_updated_at,
    )


def read_source_rows(stream: TextIO) -> list[SourceSettings]:
    rows: list[SourceSettings] = []
    offer_ids: set[str] = set()
    for line_number, raw_line in enumerate(stream, start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"line {line_number}: invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"line {line_number}: JSON object expected")
        try:
            row = normalize_source_row(payload)
        except ValueError as exc:
            raise ValueError(f"line {line_number}: {exc}") from exc
        if row.offer_id in offer_ids:
            raise ValueError(f"line {line_number}: duplicate offer_id {row.offer_id}")
        offer_ids.add(row.offer_id)
        rows.append(row)
    return rows


def settings_signature(row: SourceSettings) -> tuple[Any, ...]:
    values = asdict(row)
    return tuple(values[field] for field in SourceSettings.__dataclass_fields__)


def target_signature(row: tuple[Any, ...]) -> tuple[Any, ...]:
    return tuple(row)


def connection_for_campaign(connection, campaign_id: str) -> tuple[int, str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, display_name, status
            FROM seller.marketplace_connections
            WHERE provider_code='yandex_market' AND campaign_id=%s
            ORDER BY id
            """,
            (campaign_id,),
        )
        rows = cursor.fetchall()
    if not rows:
        raise RuntimeError(f"Yandex connection with campaign_id={campaign_id} was not found")
    if len(rows) != 1:
        raise RuntimeError(f"campaign_id={campaign_id} matches {len(rows)} Seller connections")
    connection_id, display_name, status = rows[0]
    if str(status) != "active":
        raise RuntimeError(f"Seller connection {connection_id} is not active")
    return int(connection_id), str(display_name), str(status)


def catalog_external_ids(connection, connection_id: int) -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT offer_id, external_product_id
            FROM seller.catalog_items
            WHERE connection_id=%s
            """,
            (connection_id,),
        )
        rows = cursor.fetchall()
    result: dict[str, str] = {}
    for offer_id, external_product_id in rows:
        normalized_offer = str(offer_id or "").strip()
        if not normalized_offer:
            continue
        if normalized_offer in result and result[normalized_offer] != str(external_product_id):
            raise RuntimeError(f"Seller catalog contains duplicate offer_id {normalized_offer}")
        result[normalized_offer] = str(external_product_id)
    return result


def prepare_catalog_rows(
    source_rows: Iterable[SourceSettings],
    catalog: dict[str, str],
    *,
    skip_missing: bool,
) -> tuple[list[tuple[SourceSettings, str]], list[str]]:
    rows = list(source_rows)
    missing = [row.offer_id for row in rows if row.offer_id not in catalog]
    if missing and not skip_missing:
        raise RuntimeError(
            f"{len(missing)} source offers are missing in Seller catalog: {', '.join(missing[:10])}"
        )
    prepared = [(row, catalog[row.offer_id]) for row in rows if row.offer_id in catalog]
    return prepared, missing


def existing_settings(connection, connection_id: int) -> dict[str, tuple[Any, ...]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT external_product_id, source_store_code, external_product_id,
                   manual_stock_limit, published_stock, activation_instruction,
                   support_message, support_message_delivery_enabled,
                   sales_limit, sales_limit_daily_extra, sales_limit_day, sales_limit_revision,
                   sales_limit_used, sales_limit_reserved, sales_limit_remaining,
                   sales_limit_exhausted_at, archived_by_sales_limit, last_stock_sync_at,
                   source_updated_at
            FROM seller.yandex_product_settings_snapshot
            WHERE connection_id=%s
            """,
            (connection_id,),
        )
        rows = cursor.fetchall()
    return {str(row[0]): target_signature(row[1:]) for row in rows}


def snapshot_table_exists(connection) -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('seller.yandex_product_settings_snapshot') IS NOT NULL")
        return bool(cursor.fetchone()[0])


def db_values(row: SourceSettings, external_product_id: str) -> tuple[Any, ...]:
    return (
        row.source_store_code,
        external_product_id,
        row.manual_stock_limit,
        row.published_stock,
        row.activation_instruction,
        row.support_message,
        row.support_message_delivery_enabled,
        row.sales_limit,
        row.sales_limit_daily_extra,
        row.sales_limit_day,
        row.sales_limit_revision,
        row.sales_limit_used,
        row.sales_limit_reserved,
        row.sales_limit_remaining,
        row.sales_limit_exhausted_at,
        row.archived_by_sales_limit,
        row.last_stock_sync_at,
        row.source_updated_at,
    )


def import_rows(connection, connection_id: int, prepared_rows: Iterable[tuple[SourceSettings, str]]) -> None:
    statement = """
        INSERT INTO seller.yandex_product_settings_snapshot(
          connection_id, external_product_id, source_store_code,
          manual_stock_limit, published_stock, activation_instruction,
          support_message, support_message_delivery_enabled,
          sales_limit, sales_limit_daily_extra, sales_limit_day, sales_limit_revision,
          sales_limit_used, sales_limit_reserved, sales_limit_remaining,
          sales_limit_exhausted_at, archived_by_sales_limit, last_stock_sync_at,
          source_updated_at, imported_at
        ) VALUES (
          %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
          %s, %s, %s, %s, %s, %s, %s, %s, %s, now()
        )
        ON CONFLICT (connection_id, external_product_id) DO UPDATE SET
          source_store_code=EXCLUDED.source_store_code,
          manual_stock_limit=EXCLUDED.manual_stock_limit,
          published_stock=EXCLUDED.published_stock,
          activation_instruction=EXCLUDED.activation_instruction,
          support_message=EXCLUDED.support_message,
          support_message_delivery_enabled=EXCLUDED.support_message_delivery_enabled,
          sales_limit=EXCLUDED.sales_limit,
          sales_limit_daily_extra=EXCLUDED.sales_limit_daily_extra,
          sales_limit_day=EXCLUDED.sales_limit_day,
          sales_limit_revision=EXCLUDED.sales_limit_revision,
          sales_limit_used=EXCLUDED.sales_limit_used,
          sales_limit_reserved=EXCLUDED.sales_limit_reserved,
          sales_limit_remaining=EXCLUDED.sales_limit_remaining,
          sales_limit_exhausted_at=EXCLUDED.sales_limit_exhausted_at,
          archived_by_sales_limit=EXCLUDED.archived_by_sales_limit,
          last_stock_sync_at=EXCLUDED.last_stock_sync_at,
          source_updated_at=EXCLUDED.source_updated_at,
          imported_at=now()
        WHERE (
          seller.yandex_product_settings_snapshot.source_store_code,
          seller.yandex_product_settings_snapshot.manual_stock_limit,
          seller.yandex_product_settings_snapshot.published_stock,
          seller.yandex_product_settings_snapshot.activation_instruction,
          seller.yandex_product_settings_snapshot.support_message,
          seller.yandex_product_settings_snapshot.support_message_delivery_enabled,
          seller.yandex_product_settings_snapshot.sales_limit,
          seller.yandex_product_settings_snapshot.sales_limit_daily_extra,
          seller.yandex_product_settings_snapshot.sales_limit_day,
          seller.yandex_product_settings_snapshot.sales_limit_revision,
          seller.yandex_product_settings_snapshot.sales_limit_used,
          seller.yandex_product_settings_snapshot.sales_limit_reserved,
          seller.yandex_product_settings_snapshot.sales_limit_remaining,
          seller.yandex_product_settings_snapshot.sales_limit_exhausted_at,
          seller.yandex_product_settings_snapshot.archived_by_sales_limit,
          seller.yandex_product_settings_snapshot.last_stock_sync_at,
          seller.yandex_product_settings_snapshot.source_updated_at
        ) IS DISTINCT FROM (
          EXCLUDED.source_store_code, EXCLUDED.manual_stock_limit, EXCLUDED.published_stock,
          EXCLUDED.activation_instruction, EXCLUDED.support_message,
          EXCLUDED.support_message_delivery_enabled, EXCLUDED.sales_limit, EXCLUDED.sales_limit_daily_extra,
          EXCLUDED.sales_limit_day, EXCLUDED.sales_limit_revision, EXCLUDED.sales_limit_used,
          EXCLUDED.sales_limit_reserved, EXCLUDED.sales_limit_remaining,
          EXCLUDED.sales_limit_exhausted_at, EXCLUDED.archived_by_sales_limit,
          EXCLUDED.last_stock_sync_at, EXCLUDED.source_updated_at
        )
    """
    values = []
    for row, external_product_id in prepared_rows:
        signature = db_values(row, external_product_id)
        values.append((connection_id, external_product_id, signature[0], *signature[2:]))
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout TO '3s'")
            cursor.execute("SET LOCAL statement_timeout TO '30s'")
            cursor.executemany(statement, values)


def open_input(path: str) -> TextIO:
    return sys.stdin if path == "-" else Path(path).open("r", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import read-only CRM Yandex settings snapshot into Seller")
    parser.add_argument("--input", default="-", help="JSONL file or - for stdin")
    parser.add_argument("--source-store-code", required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--expected-count", type=int)
    parser.add_argument(
        "--skip-missing",
        action="store_true",
        help="Import only offers currently present in Seller catalog and report skipped source rows",
    )
    parser.add_argument("--apply", action="store_true", help="Write to Seller; without this flag only validates")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", ""))
    args = parser.parse_args()

    if not args.database_url:
        raise RuntimeError("DATABASE_URL is required")
    stream = open_input(args.input)
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
    today_moscow = datetime.now(MOSCOW_TZ).date()
    stale_limits = [
        row.offer_id for row in source_rows
        if row.sales_limit is not None and row.sales_limit_day is not None and row.sales_limit_day < today_moscow
    ]
    if stale_limits:
        raise RuntimeError(
            f"{len(stale_limits)} active sales limits belong to an earlier Moscow day; "
            "refresh CRM limit state before import"
        )

    with psycopg.connect(args.database_url) as connection:
        connection_id, display_name, status = connection_for_campaign(connection, str(args.campaign_id))
        catalog = catalog_external_ids(connection, connection_id)
        prepared, missing = prepare_catalog_rows(
            source_rows,
            catalog,
            skip_missing=args.skip_missing,
        )
        table_ready = snapshot_table_exists(connection)
        if args.apply and not table_ready:
            raise RuntimeError("Seller snapshot table is missing; apply database migrations first")
        current = existing_settings(connection, connection_id) if table_ready else {}
        inserted = 0
        updated = 0
        unchanged = 0
        for row, external_product_id in prepared:
            previous = current.get(external_product_id)
            signature = db_values(row, external_product_id)
            if previous is None:
                inserted += 1
            elif previous == signature:
                unchanged += 1
            else:
                updated += 1
        if args.apply:
            import_rows(connection, connection_id, prepared)

    print(json.dumps({
        "mode": "apply" if args.apply else "dry-run",
        "source_store_code": args.source_store_code,
        "campaign_id": str(args.campaign_id),
        "seller_connection_id": connection_id,
        "seller_connection_name": display_name,
        "seller_connection_status": status,
        "source_rows": len(source_rows),
        "matched_catalog_rows": len(prepared),
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "missing": len(missing),
        "missing_offer_ids_preview": missing[:10],
        "target_table_ready": table_ready,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
