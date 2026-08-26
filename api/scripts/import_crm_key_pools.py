"""Переносит пулы одного магазина из CRM напрямую в Seller без файлов с открытыми ключами.

Источник всегда открывается только для чтения. По умолчанию скрипт выполняет dry-run;
запись в Seller включается отдельным флагом ``--apply``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import os
from typing import Any, Iterable

import psycopg


ALLOWED_STATUSES = {"free", "reserved", "sending", "delivered", "expired", "disabled"}

INSERT_IMPORTED_KEY_SQL = """
    INSERT INTO seller.marketplace_keys (
      pool_id, code_ciphertext, code_hash, code_suffix, status, expires_at,
      issued_order_ref, reserved_at, issued_at, source_system, source_key_id,
      key_origin, created_at, updated_at
    ) VALUES (
      %s, pgp_sym_encrypt(%s, %s, 'cipher-algo=aes256, compress-algo=0'), %s, %s, %s, %s,
      %s, %s, %s, 'crm', %s, 'pool', %s, %s
    )
    ON CONFLICT DO NOTHING
    RETURNING id
"""


@dataclass(frozen=True)
class SourceKey:
    product_key: str
    source_key_id: int
    code: str
    status: str
    expires_at: date | None
    issued_order_ref: str
    reserved_at: datetime | None
    issued_at: datetime | None
    created_at: datetime
    updated_at: datetime


def required_secret(name: str) -> str:
    # Не запускает перенос, если один из ключей расшифровки случайно не задан.
    value = str(os.getenv(name, "")).strip()
    if len(value) < 32:
        raise RuntimeError(f"{name} must contain at least 32 characters")
    return value


def normalized_source_key(row: tuple[Any, ...]) -> SourceKey:
    # Проверяет каждую строку CRM до любой записи в Seller.
    product_key = str(row[0] or "").strip()
    code = str(row[2] or "").strip()
    status = str(row[3] or "").strip()
    if not product_key or len(product_key) > 256:
        raise ValueError("CRM pool contains an invalid product_key")
    if not code or len(code) > 1024:
        raise ValueError("CRM pool contains an invalid key value")
    if status not in ALLOWED_STATUSES:
        raise ValueError(f"CRM pool contains unsupported status {status}")
    return SourceKey(
        product_key=product_key,
        source_key_id=int(row[1]),
        code=code,
        status=status,
        expires_at=row[4],
        issued_order_ref=str(row[5] or ""),
        reserved_at=row[6],
        issued_at=row[7],
        created_at=row[8],
        updated_at=row[9],
    )


def seller_key_hash(value: str) -> str:
    # Использует тот же отпечаток, что и API добавления ключей Seller.
    return sha256(f"seller-marketplace-key:v1:{value}".encode("utf-8")).hexdigest()


def target_connection(target, campaign_id: str) -> tuple[int, str]:
    # Связывает магазин по неизменяемому campaign_id, а не по отображаемому названию.
    with target.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, display_name
            FROM seller.marketplace_connections
            WHERE provider_code='yandex_market' AND campaign_id=%s
            ORDER BY id
            """,
            (campaign_id,),
        )
        rows = cursor.fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"campaign_id={campaign_id} matches {len(rows)} Seller connections")
    return int(rows[0][0]), str(rows[0][1])


def target_catalog(target, connection_id: int) -> dict[str, str]:
    # Строит точное соответствие CRM product_key к карточке уже синхронизированного каталога Seller.
    with target.cursor() as cursor:
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
        key = str(offer_id or "").strip()
        if key:
            result[key] = str(external_product_id)
    return result


def target_tables_ready(target) -> bool:
    # Даёт понятную ошибку до начала чтения секретов, если миграция Seller ещё не применена.
    with target.cursor() as cursor:
        cursor.execute(
            """
            SELECT to_regclass('seller.marketplace_key_pools') IS NOT NULL
               AND to_regclass('seller.marketplace_keys') IS NOT NULL
               AND to_regclass('seller.order_fulfillments') IS NOT NULL
               AND to_regclass('seller.fulfillment_key_reservations') IS NOT NULL
            """
        )
        return bool(cursor.fetchone()[0])


def source_inflight_count(source, *, marketplace: str, store_code: str) -> int:
    # Проверяет незавершённые CRM-резервы без чтения и расшифровки самих ключей.
    with source.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM app.marketplace_manual_keys AS manual_key
            JOIN app.marketplace_manual_key_pools AS pool ON pool.id=manual_key.pool_id
            WHERE pool.marketplace=%s AND lower(pool.store_code)=lower(%s)
              AND manual_key.status IN ('reserved', 'sending')
            """,
            (marketplace, store_code),
        )
        count = int(cursor.fetchone()[0] or 0)
    source.commit()
    return count


def ensure_target_import_writable(target, *, connection_id: int) -> None:
    # После начала выдач Seller повторный импорт не вправе перезаписывать состояния тех же ключей из CRM.
    with target.cursor() as cursor:
        cursor.execute(
            """
            SELECT marketplace_connection.fulfillment_reservation_enabled,
                   EXISTS (
                     SELECT 1
                     FROM seller.product_card_settings AS settings
                     WHERE settings.connection_id=marketplace_connection.id
                       AND settings.pool_issue_enabled=true
                   ),
                   EXISTS (
                     SELECT 1
                     FROM seller.order_fulfillments AS fulfillment
                     WHERE fulfillment.connection_id=marketplace_connection.id
                       AND fulfillment.status IN ('reserved', 'sending', 'submitted', 'unknown', 'delivered')
                   ),
                   EXISTS (
                     SELECT 1
                     FROM seller.fulfillment_key_reservations AS reservation
                     JOIN seller.order_fulfillments AS fulfillment
                       ON fulfillment.id=reservation.fulfillment_id
                     WHERE fulfillment.connection_id=marketplace_connection.id
                       AND reservation.state IN ('reserved', 'consumed')
                   )
            FROM seller.marketplace_connections AS marketplace_connection
            WHERE marketplace_connection.id=%s
            """,
            (connection_id,),
        )
        state = cursor.fetchone()
    if not state:
        raise RuntimeError(f"Seller connection id={connection_id} no longer exists")
    if any(bool(value) for value in state):
        raise RuntimeError(
            "Seller fulfillment ownership has already started for this store; "
            "CRM key-pool import is blocked to prevent two systems from changing the same key states"
        )


def read_source_batch(source, *, marketplace: str, store_code: str, secret: str, after_id: int, limit: int) -> list[SourceKey]:
    # Читает небольшой диапазон по первичному ключу, чтобы не держать долгую транзакцию и большие блоки памяти.
    with source.cursor() as cursor:
        cursor.execute(
            """
            SELECT pool.product_key, manual_key.id,
                   pgp_sym_decrypt(manual_key.code_ciphertext, %s),
                   manual_key.status, manual_key.expires_at, manual_key.issued_order_ref,
                   manual_key.reserved_at, manual_key.issued_at,
                   manual_key.created_at, manual_key.updated_at
            FROM app.marketplace_manual_keys AS manual_key
            JOIN app.marketplace_manual_key_pools AS pool ON pool.id=manual_key.pool_id
            WHERE pool.marketplace=%s AND lower(pool.store_code)=lower(%s)
              AND manual_key.id>%s
            ORDER BY manual_key.id
            LIMIT %s
            """,
            (secret, marketplace, store_code, after_id, limit),
        )
        rows = cursor.fetchall()
    source.commit()
    return [normalized_source_key(row) for row in rows]


def ensure_target_pool(cursor, connection_id: int, external_product_id: str) -> int:
    # Создаёт целевой пул только когда действительно требуется вставка или обновление ключа.
    cursor.execute(
        """
        INSERT INTO seller.marketplace_key_pools(connection_id, external_product_id)
        VALUES (%s, %s)
        ON CONFLICT (connection_id, external_product_id)
        DO UPDATE SET updated_at=now()
        RETURNING id
        """,
        (connection_id, external_product_id),
    )
    return int(cursor.fetchone()[0])


def import_batch(
    target,
    *,
    connection_id: int,
    catalog: dict[str, str],
    rows: Iterable[SourceKey],
    secret: str,
    apply: bool,
) -> tuple[int, int, int, int, int]:
    # Повторный запуск добавляет новые ключи и обновляет состояния ранее импортированных записей.
    added = 0
    updated = 0
    unchanged = 0
    duplicates = 0
    missing_products = 0
    with target.cursor() as cursor:
        for row in rows:
            external_product_id = catalog.get(row.product_key)
            if not external_product_id:
                missing_products += 1
                continue
            fingerprint = seller_key_hash(row.code)
            cursor.execute(
                """
                SELECT key.id, pool.connection_id, pool.external_product_id,
                       key.code_hash, key.status, key.expires_at, key.issued_order_ref,
                       key.reserved_at, key.issued_at, key.updated_at
                FROM seller.marketplace_keys AS key
                JOIN seller.marketplace_key_pools AS pool ON pool.id=key.pool_id
                WHERE key.source_system='crm' AND key.source_key_id=%s
                LIMIT 1
                """,
                (row.source_key_id,),
            )
            existing = cursor.fetchone()
            if existing:
                if str(existing[3]) != fingerprint:
                    raise RuntimeError(f"CRM key id={row.source_key_id} changed its secret value")
                next_signature = (
                    row.status, row.expires_at, row.issued_order_ref,
                    row.reserved_at, row.issued_at, row.updated_at,
                )
                current_signature = (
                    str(existing[4]), existing[5], str(existing[6] or ""),
                    existing[7], existing[8], existing[9],
                )
                needs_update = (
                    int(existing[1]) != connection_id
                    or str(existing[2]) != external_product_id
                    or current_signature != next_signature
                )
                if not needs_update:
                    unchanged += 1
                    continue
                updated += 1
                if apply:
                    pool_id = ensure_target_pool(cursor, connection_id, external_product_id)
                    cursor.execute(
                        """
                        UPDATE seller.marketplace_keys
                        SET pool_id=%s, status=%s, expires_at=%s, issued_order_ref=%s,
                            reserved_at=%s, issued_at=%s, updated_at=%s
                        WHERE id=%s
                        """,
                        (
                            pool_id, row.status, row.expires_at, row.issued_order_ref,
                            row.reserved_at, row.issued_at, row.updated_at, int(existing[0]),
                        ),
                    )
                continue
            cursor.execute("SELECT 1 FROM seller.marketplace_keys WHERE code_hash=%s LIMIT 1", (fingerprint,))
            if cursor.fetchone():
                duplicates += 1
                continue
            if not apply:
                added += 1
                continue
            pool_id = ensure_target_pool(cursor, connection_id, external_product_id)
            cursor.execute(
                INSERT_IMPORTED_KEY_SQL,
                (
                    pool_id, row.code, secret, fingerprint, row.code[-4:], row.status, row.expires_at,
                    row.issued_order_ref, row.reserved_at, row.issued_at, row.source_key_id,
                    row.created_at, row.updated_at,
                ),
            )
            if cursor.fetchone():
                added += 1
            else:
                duplicates += 1
    if apply:
        target.commit()
    else:
        target.rollback()
    return added, updated, unchanged, duplicates, missing_products


def main() -> int:
    parser = argparse.ArgumentParser(description="Import one CRM marketplace key pool into Seller")
    parser.add_argument("--source-store-code", required=True)
    parser.add_argument("--target-campaign-id", required=True)
    parser.add_argument("--marketplace", default="yandex_market", choices=["yandex_market", "ozon"])
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    source_dsn = str(os.getenv("CRM_DATABASE_URL", "")).strip()
    target_dsn = str(os.getenv("DATABASE_URL", "")).strip()
    if not source_dsn or not target_dsn:
        raise RuntimeError("CRM_DATABASE_URL and DATABASE_URL are required")
    if source_dsn == target_dsn:
        raise RuntimeError("CRM and Seller database URLs must be different")
    source_secret = required_secret("CRM_MARKETPLACE_KEY_POOL_SECRET")
    target_secret = required_secret("SELLER_KEY_POOL_SECRET")
    batch_size = min(max(args.batch_size, 10), 1000)

    totals = {
        "read": 0, "added": 0, "updated": 0, "unchanged": 0,
        "duplicates": 0, "missing_products": 0,
    }
    with psycopg.connect(source_dsn) as source, psycopg.connect(target_dsn) as target:
        with source.cursor() as cursor:
            cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            cursor.execute("SET statement_timeout='5s'")
            cursor.execute("SET lock_timeout='500ms'")
        source.commit()
        connection_id, display_name = target_connection(target, args.target_campaign_id)
        if not target_tables_ready(target):
            raise RuntimeError("Seller fulfillment tables are missing; apply database migrations first")
        catalog = target_catalog(target, connection_id)
        inflight_count = source_inflight_count(
            source,
            marketplace=args.marketplace,
            store_code=args.source_store_code,
        )
        totals["inflight"] = inflight_count
        if args.apply:
            if inflight_count:
                raise RuntimeError(
                    f"CRM still owns {inflight_count} reserved/sending keys; "
                    "finish or release them before the final import"
                )
            ensure_target_import_writable(target, connection_id=connection_id)
        after_id = 0
        while True:
            rows = read_source_batch(
                source,
                marketplace=args.marketplace,
                store_code=args.source_store_code,
                secret=source_secret,
                after_id=after_id,
                limit=batch_size,
            )
            if not rows:
                break
            after_id = rows[-1].source_key_id
            added, updated, unchanged, duplicates, missing_products = import_batch(
                target,
                connection_id=connection_id,
                catalog=catalog,
                rows=rows,
                secret=target_secret,
                apply=args.apply,
            )
            totals["read"] += len(rows)
            totals["added"] += added
            totals["updated"] += updated
            totals["unchanged"] += unchanged
            totals["duplicates"] += duplicates
            totals["missing_products"] += missing_products

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"{mode}: store={display_name!r}, read={totals['read']}, added={totals['added']}, "
        f"updated={totals['updated']}, unchanged={totals['unchanged']}, "
        f"duplicates={totals['duplicates']}, missing_products={totals['missing_products']}, "
        f"crm_inflight={totals['inflight']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
