"""Read-only аудит готовности JoyCards перед финальным переключением.

Скрипт не обновляет ни Seller, ни CRM, не вызывает calculate/check/pay и не
раскрывает ключи. Ненулевой exit code означает, что переключаться ещё нельзя.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import timedelta
from pathlib import Path
import sys
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domains.supplier_hub_client import SupplierHubClient, load_supplier_hub_settings


FALSE_FLAGS = (
    "YANDEX_MARKET_WEBHOOK_PROCESSING_ENABLED",
    "SELLER_FULFILLMENT_RESOLVER_ENABLED",
    "SELLER_SUPPLIER_HUB_FULFILLMENT_ENABLED",
    "SELLER_POOL_RESERVATION_ENABLED",
    "SELLER_YANDEX_OUTBOUND_ENABLED",
    "SELLER_YANDEX_STOCK_OUTBOUND_ENABLED",
)


def enabled(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def add_check(
    checks: list[dict[str, Any]],
    name: str,
    ok: bool,
    value: Any,
    *,
    final_only: bool = False,
    warning_only: bool = False,
) -> None:
    checks.append({
        "name": name,
        "ok": bool(ok),
        "value": value,
        "final_only": final_only,
        "warning_only": warning_only,
    })


def seller_checks(target, campaign_id: str, expected_count: int | None, quote_max_age_hours: int):
    checks: list[dict[str, Any]] = []
    with target.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, display_name, webhook_processing_enabled,
                   fulfillment_reservation_enabled, fulfillment_outbound_enabled,
                   supplier_fulfillment_enabled, stock_outbound_enabled
            FROM seller.marketplace_connections
            WHERE provider_code='yandex_market' AND campaign_id=%s
            """,
            (campaign_id,),
        )
        rows = cursor.fetchall()
        add_check(checks, "seller_connection_unique", len(rows) == 1, len(rows))
        if len(rows) != 1:
            return checks, None
        row = rows[0]
        connection_id = int(row[0])
        add_check(checks, "seller_store_gates_disabled", not any(bool(value) for value in row[2:]), {
            "webhook": bool(row[2]), "pool": bool(row[3]),
            "outbound": bool(row[4]), "supplier": bool(row[5]), "stock": bool(row[6]),
        })
        cursor.execute(
            """
            SELECT count(*) FILTER (WHERE item.is_present),
                   count(policy.external_product_id),
                   count(*) FILTER (WHERE COALESCE(policy.supplier_issue_enabled, false)),
                   count(*) FILTER (
                     WHERE COALESCE(policy.supplier_issue_enabled, false)
                       AND (mapping.id IS NULL OR mapping.enabled=false OR mapping.service_id IS NULL
                            OR mapping.max_amount IS NULL OR mapping.quoted_amount IS NULL
                            OR mapping.quoted_at IS NULL)
                   ),
                   count(*) FILTER (
                     WHERE COALESCE(policy.supplier_issue_enabled, false)
                       AND mapping.quoted_at < now() - %s::interval
                   )
            FROM seller.catalog_items AS item
            LEFT JOIN seller.product_fulfillment_policies AS policy
              ON policy.connection_id=item.connection_id
             AND policy.external_product_id=item.external_product_id
            LEFT JOIN LATERAL (
              SELECT candidate.id, candidate.enabled, candidate.service_id,
                     candidate.max_amount, candidate.quoted_amount, candidate.quoted_at
              FROM seller.product_supplier_mappings AS candidate
              WHERE candidate.connection_id=item.connection_id
                AND candidate.external_product_id=item.external_product_id
                AND candidate.provider_code='interhub' AND candidate.priority=1
              LIMIT 1
            ) AS mapping ON true
            WHERE item.connection_id=%s
            """,
            (timedelta(hours=quote_max_age_hours), connection_id),
        )
        catalog, policies, supplier_enabled, incomplete, stale = (int(value or 0) for value in cursor.fetchone())
        add_check(checks, "seller_catalog_present", catalog > 0, catalog)
        add_check(checks, "seller_policy_expected_count", expected_count is None or policies == expected_count, policies)
        add_check(checks, "seller_supplier_mappings_complete", incomplete == 0, {
            "supplier_enabled": supplier_enabled, "incomplete": incomplete,
        })
        # CRM prices are confirmed manually. Their age remains visible for an
        # operator, while completeness of quote/max_amount above is the blocker.
        add_check(checks, "seller_supplier_quotes_fresh", stale == 0, {
            "max_age_hours": quote_max_age_hours, "stale": stale,
        }, warning_only=True)
        cursor.execute(
            """
            SELECT
              (SELECT count(*) FROM seller.order_fulfillments AS fulfillment
               WHERE fulfillment.connection_id=%s AND fulfillment.status IN (
                 'pending','reserved','supplier_required','sending','submitted','unknown')),
              (SELECT count(*)
               FROM seller.supplier_purchase_attempts AS attempt
               JOIN seller.order_fulfillments AS fulfillment ON fulfillment.id=attempt.fulfillment_id
               WHERE fulfillment.connection_id=%s AND attempt.state IN (
                 'created','checked','payment_started','processing','requires_attention')),
              (SELECT count(*)
               FROM seller.marketplace_keys AS key
               JOIN seller.marketplace_key_pools AS pool ON pool.id=key.pool_id
               WHERE pool.connection_id=%s AND key.status IN ('reserved','sending'))
            """,
            (connection_id, connection_id, connection_id),
        )
        active_fulfillments, active_attempts, locked_keys = (int(value or 0) for value in cursor.fetchone())
        add_check(checks, "seller_no_inflight_fulfillments", active_fulfillments == 0, active_fulfillments, final_only=True)
        add_check(checks, "seller_no_inflight_supplier_attempts", active_attempts == 0, active_attempts, final_only=True)
        add_check(checks, "seller_no_reserved_or_sending_keys", locked_keys == 0, locked_keys, final_only=True)
    target.rollback()
    return checks, connection_id


def crm_checks(source, store_code: str, expected_count: int | None) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    with source.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute("SET statement_timeout='5s'")
        cursor.execute("SET lock_timeout='500ms'")
        cursor.execute(
            "SELECT count(*) FROM app.marketplace_yandex_stock_settings WHERE lower(store_code)=lower(%s)",
            (store_code,),
        )
        settings_count = int(cursor.fetchone()[0] or 0)
        add_check(checks, "crm_policy_expected_count", expected_count is None or settings_count == expected_count, settings_count)
        cursor.execute(
            """
            SELECT count(*) FROM app.marketplace_yandex_digital_deliveries
            WHERE lower(store_code)=lower(%s)
              AND status IN ('supplier_processing','market_sending','market_submitted','market_unknown')
            """,
            (store_code,),
        )
        inflight = int(cursor.fetchone()[0] or 0)
        add_check(checks, "crm_no_inflight_deliveries", inflight == 0, inflight, final_only=True)
        cursor.execute(
            """
            SELECT count(*)
            FROM app.marketplace_manual_keys AS key
            JOIN app.marketplace_manual_key_pools AS pool ON pool.id=key.pool_id
            WHERE pool.marketplace='yandex_market' AND lower(pool.store_code)=lower(%s)
              AND key.status IN ('reserved','sending')
            """,
            (store_code,),
        )
        locked_keys = int(cursor.fetchone()[0] or 0)
        add_check(checks, "crm_no_reserved_or_sending_keys", locked_keys == 0, locked_keys, final_only=True)
    source.rollback()
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only JoyCards cutover readiness audit")
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--source-store-code", default="joycards")
    parser.add_argument("--expected-count", type=int)
    parser.add_argument("--quote-max-age-hours", type=int, default=24)
    args = parser.parse_args()
    database_url = str(os.getenv("DATABASE_URL", "")).strip()
    crm_database_url = str(os.getenv("CRM_DATABASE_URL", "")).strip()
    if not database_url or not crm_database_url or database_url == crm_database_url:
        raise RuntimeError("Distinct DATABASE_URL and CRM_DATABASE_URL are required")

    checks: list[dict[str, Any]] = []
    for name in FALSE_FLAGS:
        add_check(checks, f"env_{name.lower()}_disabled", not enabled(os.getenv(name)), os.getenv(name, "false"))
    with psycopg.connect(database_url) as target:
        target_result, _connection_id = seller_checks(
            target, args.campaign_id, args.expected_count, max(1, args.quote_max_age_hours),
        )
        checks.extend(target_result)
    with psycopg.connect(crm_database_url) as source:
        checks.extend(crm_checks(source, args.source_store_code, args.expected_count))

    try:
        summary = SupplierHubClient(load_supplier_hub_settings()).observability_summary()
        in_flight = int(summary.get("in_flight") or 0)
        attention = int(summary.get("requires_attention") or 0)
        add_check(checks, "hub_reachable_and_ready_for_audit", True, True)
        add_check(checks, "hub_no_inflight_purchases", in_flight == 0, in_flight, final_only=True)
        add_check(checks, "hub_no_requires_attention", attention == 0, attention, final_only=True)
    except Exception as exc:
        add_check(checks, "hub_reachable_and_ready_for_audit", False, str(exc)[:300])

    preparation_blockers = [
        item["name"] for item in checks
        if not item["ok"] and not item["final_only"] and not item["warning_only"]
    ]
    final_blockers = [item["name"] for item in checks if not item["ok"] and not item["warning_only"]]
    warnings = [item["name"] for item in checks if not item["ok"] and item["warning_only"]]
    print(json.dumps({
        "operation": "read-only-no-purchase",
        "prepared_for_cutover_window": not preparation_blockers,
        "ready_to_switch_now": not final_blockers,
        "preparation_blockers": preparation_blockers,
        "final_blockers": final_blockers,
        "warnings": warnings,
        "checks": checks,
    }, ensure_ascii=False, indent=2))
    return 0 if not final_blockers else 2


if __name__ == "__main__":
    raise SystemExit(main())
