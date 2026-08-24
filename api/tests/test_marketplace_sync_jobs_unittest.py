"""Проверки очереди, retry/backoff и атомарного продвижения watermark."""

from __future__ import annotations

import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from fastapi import HTTPException

from domains.marketplace_sync_jobs_api import parse_job_ids
from domains.marketplace_sync_service import execute_sync_job
from worker import advisory_lock_key, is_transient_sync_error, retry_delay_seconds


class MarketplaceSyncJobsTests(unittest.TestCase):
    def test_parses_and_deduplicates_polling_job_ids(self) -> None:
        self.assertEqual(parse_job_ids("7, 8,7,9"), [7, 8, 9])

    def test_rejects_invalid_polling_job_ids(self) -> None:
        for value in ("0", "-1", "one", "1,,2"):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                parse_job_ids(value)

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
