"""Проверки очереди, retry/backoff и атомарного продвижения watermark."""

from __future__ import annotations

import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock, call, patch

from fastapi import HTTPException

from domains.marketplace_orders_service import MarketplacePaginationError
from domains.marketplace_sync_jobs_api import list_jobs_scope, parse_job_ids
from domains.marketplace_sync_service import (
    execute_sync_job,
    save_order_snapshots,
    sync_catalog_connection,
    sync_orders_connection,
)
from worker import advisory_lock_key, is_transient_sync_error, retry_delay_seconds


class MarketplaceSyncJobsTests(unittest.TestCase):
    @patch("domains.marketplace_sync_service.fetch_marketplace_stocks", return_value={})
    @patch("domains.marketplace_sync_service.fetch_marketplace_catalog")
    def test_catalog_sync_restores_current_items_and_archives_missing(self, fetch_catalog, _fetch_stocks) -> None:
        fetch_catalog.return_value = [
            {"product_id": 10, "offer_id": "SKU-10", "sku": "SKU-10", "name": "Товар 10"},
            {"product_id": 11, "offer_id": "SKU-11", "sku": "SKU-11", "name": "Товар 11"},
        ]
        cursor = MagicMock()
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor

        synced = sync_catalog_connection(connection, (7, "ozon", "Store", "2", "", "", "token", None))

        self.assertEqual(synced, 2)
        statements = [" ".join(call.args[0].split()) for call in cursor.execute.call_args_list]
        self.assertTrue(any("is_archived=EXCLUDED.is_archived" in statement for statement in statements))
        archive_call = next(call for call in cursor.execute.call_args_list if "SET is_present=false" in call.args[0])
        self.assertEqual(archive_call.args[1], (7, ["10", "11"]))

    @patch("domains.marketplace_sync_service.fetch_marketplace_stocks", return_value={})
    @patch("domains.marketplace_sync_service.fetch_marketplace_catalog")
    def test_catalog_sync_keeps_archived_items_but_does_not_request_their_stock(self, fetch_catalog, fetch_stocks) -> None:
        fetch_catalog.return_value = [
            {"offer": {"offerId": "LIVE", "name": "Активный", "archived": False}},
            {"offer": {"offerId": "OLD", "name": "Архивный", "archived": True}},
        ]
        cursor = MagicMock()
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor

        synced = sync_catalog_connection(connection, (7, "yandex_market", "Store", "", 77, 202, "token", None))

        self.assertEqual(synced, 2)
        self.assertEqual(fetch_stocks.call_args.kwargs["offer_ids"], ["LIVE"])
        upserts = [call for call in cursor.execute.call_args_list if "INSERT INTO seller.catalog_items" in call.args[0]]
        self.assertEqual([call.args[1][-2:] for call in upserts], [(False, False), (True, True)])

    @patch("domains.marketplace_sync_service.fetch_marketplace_catalog", return_value=[{"unexpected": "payload"}])
    def test_catalog_sync_does_not_archive_on_unknown_payload(self, _fetch_catalog) -> None:
        connection = MagicMock()
        with self.assertRaisesRegex(RuntimeError, "неполный или неизвестный формат"):
            sync_catalog_connection(connection, (7, "ozon", "Store", "2", "", "", "token", None))
        connection.cursor.assert_not_called()

    @patch("domains.marketplace_sync_service.fetch_marketplace_stocks", return_value={})
    @patch("domains.marketplace_sync_service.fetch_marketplace_catalog", return_value=[])
    def test_empty_successful_catalog_archives_previous_snapshot(self, _fetch_catalog, _fetch_stocks) -> None:
        cursor = MagicMock()
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor

        synced = sync_catalog_connection(connection, (7, "ozon", "Store", "2", "", "", "token", None))

        self.assertEqual(synced, 0)
        archive_call = next(call for call in cursor.execute.call_args_list if "SET is_present=false" in call.args[0])
        self.assertEqual(archive_call.args[1], (7, []))

    def test_targeted_order_snapshot_uses_same_idempotent_upsert(self) -> None:
        # Полная синхронизация и webhook не расходятся в структуре сохранённых позиций.
        cursor = MagicMock()
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor

        saved = save_order_snapshots(
            connection,
            connection_id=7,
            provider_code="yandex_market",
            rows=[{
                "orderId": 123,
                "campaignId": 20,
                "status": "PROCESSING",
                "items": [{"id": 9, "offerId": "SKU-1", "offerName": "Товар", "count": 1}],
            }],
        )

        self.assertEqual(saved, 1)
        upsert = cursor.execute.call_args
        self.assertIn("ON CONFLICT (connection_id, external_order_id, external_item_id) DO UPDATE", upsert.args[0])
        self.assertEqual(upsert.args[1][0:4], (7, "123", "9", "SKU-1"))

    @patch("domains.marketplace_sync_service.observe_order_fulfillments", return_value=[81])
    @patch("domains.marketplace_sync_service.save_order_snapshots", return_value=1)
    @patch("domains.marketplace_sync_service.fetch_marketplace_orders")
    def test_yandex_polling_reconciles_each_order_without_reserving_when_global_switch_is_off(
        self, fetch_orders, save_snapshots, observe_fulfillments,
    ) -> None:
        rows = [{"orderId": 123}, {"orderId": 123}]
        fetch_orders.return_value = rows
        connection = Mock()
        started_at = datetime.now(timezone.utc)

        saved = sync_orders_connection(
            connection,
            (7, "yandex_market", "Store", "", "216", "149", "token", None),
            sync_started_at=started_at,
        )

        self.assertEqual(saved, 1)
        save_snapshots.assert_called_once_with(
            connection, connection_id=7, provider_code="yandex_market", rows=rows,
        )
        observe_fulfillments.assert_called_once_with(connection, connection_id=7, external_order_id="123")

    @patch("domains.marketplace_sync_service.observe_order_fulfillments", return_value=[81, 82])
    @patch("domains.marketplace_sync_service.save_order_snapshots", return_value=2)
    @patch("domains.marketplace_sync_service.fetch_marketplace_orders", return_value=[{"orderId": 123}])
    def test_yandex_polling_applies_safe_reservation_gates_when_enabled(
        self, _fetch_orders, _save_snapshots, _observe,
    ) -> None:
        connection = Mock()

        sync_orders_connection(
            connection,
            (7, "yandex_market", "Store", "", "216", "149", "token", None),
            sync_started_at=datetime.now(timezone.utc),
        )

        _observe.assert_called_once_with(connection, connection_id=7, external_order_id="123")

    def test_parses_and_deduplicates_polling_job_ids(self) -> None:
        self.assertEqual(parse_job_ids("7, 8,7,9"), [7, 8, 9])

    def test_rejects_invalid_polling_job_ids(self) -> None:
        for value in ("0", "-1", "one", "1,,2"):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                parse_job_ids(value)

    def test_sync_restore_excludes_automatic_background_jobs(self) -> None:
        query, params = list_jobs_scope([], requested_by_user_id=41)
        self.assertEqual(query, "AND job.requested_by_user_id=%s")
        self.assertEqual(params, [41])

        targeted_query, targeted_params = list_jobs_scope([7, 8], requested_by_user_id=41)
        self.assertEqual(targeted_query, "AND job.id=ANY(%s)")
        self.assertEqual(targeted_params, [[7, 8]])

    def test_retry_backoff_is_exponential_and_capped(self) -> None:
        self.assertEqual([retry_delay_seconds(attempt) for attempt in (1, 2, 3, 4)], [15, 30, 60, 120])
        self.assertEqual(retry_delay_seconds(20), 300)

    def test_retries_only_temporary_http_failures(self) -> None:
        self.assertTrue(is_transient_sync_error(HTTPException(status_code=502, detail="upstream")))
        self.assertTrue(is_transient_sync_error(HTTPException(status_code=429, detail="rate limit")))
        self.assertTrue(is_transient_sync_error(HTTPException(status_code=502, detail="Маркетплейс: HTTP 503")))
        self.assertFalse(is_transient_sync_error(HTTPException(status_code=502, detail="Маркетплейс: HTTP 400")))
        self.assertFalse(is_transient_sync_error(HTTPException(status_code=409, detail="disabled")))
        self.assertFalse(is_transient_sync_error(HTTPException(status_code=400, detail="credentials")))
        self.assertFalse(is_transient_sync_error(MarketplacePaginationError("token loop")))

    def test_advisory_lock_key_stays_inside_postgresql_int32(self) -> None:
        self.assertEqual(advisory_lock_key(1), 1)
        self.assertGreaterEqual(advisory_lock_key(9_999_999_999), 0)
        self.assertLess(advisory_lock_key(9_999_999_999), 2_147_483_647)

    @patch("domains.marketplace_sync_service.mark_connection_success")
    @patch("domains.marketplace_sync_service.sync_orders_connection", return_value=12)
    @patch("domains.marketplace_sync_service.load_active_connection", return_value=(1, "ozon", "Store", "2", "", "", "token", None))
    def test_successful_job_marks_watermark_after_snapshot(self, load_connection, sync_orders, mark_success) -> None:
        connection = Mock()
        fake_psycopg = Mock(connect=Mock(return_value=nullcontext(connection)))

        synced_items = execute_sync_job(lambda: "postgresql://test", fake_psycopg, connection_id=1, sync_kind="orders")

        self.assertEqual(synced_items, 12)
        load_connection.assert_called_once_with(connection, 1)
        sync_started_at = sync_orders.call_args.kwargs["sync_started_at"]
        self.assertIsInstance(sync_started_at, datetime)
        self.assertEqual(sync_started_at.tzinfo, timezone.utc)
        mark_success.assert_called_once_with(
            connection, 1, sync_kind="orders", sync_started_at=sync_started_at,
        )

    @patch("domains.marketplace_sync_service.mark_connection_success")
    @patch("domains.marketplace_sync_service.sync_orders_connection", side_effect=HTTPException(status_code=502, detail="temporary"))
    @patch("domains.marketplace_sync_service.load_active_connection", return_value=(1, "ozon", "Store", "2", "", "", "token", None))
    def test_failed_job_does_not_advance_watermark(self, _load_connection, _sync_orders, mark_success) -> None:
        fake_psycopg = Mock(connect=Mock(return_value=nullcontext(Mock())))

        with self.assertRaises(HTTPException):
            execute_sync_job(lambda: "postgresql://test", fake_psycopg, connection_id=1, sync_kind="orders")

        mark_success.assert_not_called()


if __name__ == "__main__":
    unittest.main()
