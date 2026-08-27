"""Проверки lease, retry и read-only обработки Yandex webhook."""

from __future__ import annotations

import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, Mock, call, patch

from fastapi import HTTPException

from domains.yandex_market_webhook_processor import (
    build_yandex_market_webhook_processor,
    webhook_retry_delay_seconds,
)


def connection_with_cursor(*, row=None, rowcount: int = 1):
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    cursor.rowcount = rowcount
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    return connection, cursor


class YandexMarketWebhookProcessorTests(unittest.TestCase):
    def test_kill_switch_does_not_claim_events(self) -> None:
        # processing_enabled=false оставляет CRM единственным обработчиком до переключения.
        fake_psycopg = Mock()
        processor = build_yandex_market_webhook_processor(
            database_url=lambda: "postgresql://test",
            psycopg=fake_psycopg,
            processing_enabled=lambda: False,
        )

        self.assertEqual(processor.process_pending_events(), 0)
        fake_psycopg.connect.assert_not_called()

    @patch("domains.yandex_market_webhook_processor.observe_order_fulfillments", return_value=[81])
    @patch("domains.yandex_market_webhook_processor.save_order_snapshots", return_value=1)
    @patch("domains.yandex_market_webhook_processor.fetch_yandex_market_order")
    @patch("domains.yandex_market_webhook_processor.load_active_connection")
    def test_processes_one_order_without_delivery_calls(
        self, load_connection, fetch_order, save_snapshots, observe_fulfillments,
    ) -> None:
        # Processor читает заказ и создаёт локальную запись, но выключенный резерв не затрагивает ключи.
        claim_connection, claim_cursor = connection_with_cursor(
            row=(41, 7, "149196813", "123", "ORDER_CREATED", 1),
        )
        load_db_connection, _load_cursor = connection_with_cursor()
        save_connection, save_cursor = connection_with_cursor(rowcount=1)
        fake_psycopg = Mock()
        fake_psycopg.connect.side_effect = [
            nullcontext(claim_connection),
            nullcontext(load_db_connection),
            nullcontext(save_connection),
        ]
        load_connection.return_value = (
            7, "yandex_market", "JoyCards", "", "216926720", "149196813", "token", None, None,
        )
        fetch_order.return_value = {
            "orderId": 123,
            "campaignId": 149196813,
            "items": [{"id": 9, "offerId": "SKU-1"}],
        }
        processor = build_yandex_market_webhook_processor(
            database_url=lambda: "postgresql://test",
            psycopg=fake_psycopg,
            processing_enabled=lambda: True,
        )

        handled = processor.process_pending_events(batch_size=1)

        self.assertEqual(handled, 1)
        claim_sql = claim_cursor.execute.call_args.args[0]
        self.assertIn("processing_enabled_at_receive=true", claim_sql)
        self.assertIn("JOIN seller.marketplace_connections", claim_sql)
        self.assertIn("marketplace_connection.status='active'", claim_sql)
        self.assertIn("marketplace_connection.webhook_processing_enabled=true", claim_sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", claim_sql)
        self.assertNotIn("'paused'", claim_sql)
        fetch_order.assert_called_once_with(
            business_id=216926720,
            campaign_id=149196813,
            order_id=123,
            token="token",
        )
        save_snapshots.assert_called_once_with(
            save_connection,
            connection_id=7,
            provider_code="yandex_market",
            rows=[fetch_order.return_value],
        )
        observe_fulfillments.assert_called_once_with(
            save_connection,
            connection_id=7,
            external_order_id="123",
        )
        finish_sql = save_cursor.execute.call_args.args[0]
        self.assertIn("processing_state='processed'", finish_sql)

    @patch("domains.yandex_market_webhook_processor.observe_order_fulfillments", return_value=[81, 82])
    @patch("domains.yandex_market_webhook_processor.save_order_snapshots", return_value=2)
    @patch("domains.yandex_market_webhook_processor.fetch_yandex_market_order")
    @patch("domains.yandex_market_webhook_processor.load_active_connection")
    def test_global_reservation_switch_calls_safe_reservation_for_each_item(
        self, load_connection, fetch_order, _save_snapshots, _observe,
    ) -> None:
        claim_connection, _claim_cursor = connection_with_cursor(
            row=(41, 7, "149196813", "123", "ORDER_CREATED", 1),
        )
        load_db_connection, _load_cursor = connection_with_cursor()
        save_connection, _save_cursor = connection_with_cursor(rowcount=1)
        fake_psycopg = Mock()
        fake_psycopg.connect.side_effect = [
            nullcontext(claim_connection), nullcontext(load_db_connection), nullcontext(save_connection),
        ]
        load_connection.return_value = (
            7, "yandex_market", "JoyCards", "", "216926720", "149196813", "token", None, None,
        )
        fetch_order.return_value = {"orderId": 123, "items": [{"id": 9}, {"id": 10}]}
        processor = build_yandex_market_webhook_processor(
            database_url=lambda: "postgresql://test", psycopg=fake_psycopg, processing_enabled=lambda: True,
        )

        processor.process_pending_events(batch_size=1)

        _observe.assert_called_once_with(save_connection, connection_id=7, external_order_id="123")

    @patch("domains.yandex_market_webhook_processor.fetch_yandex_market_order")
    @patch("domains.yandex_market_webhook_processor.load_active_connection")
    def test_failure_is_retried_with_backoff(self, load_connection, fetch_order) -> None:
        claim_connection, _claim_cursor = connection_with_cursor(
            row=(41, 7, "149196813", "123", "ORDER_CREATED", 2),
        )
        load_db_connection, _load_cursor = connection_with_cursor()
        fail_connection, fail_cursor = connection_with_cursor()
        fake_psycopg = Mock()
        fake_psycopg.connect.side_effect = [
            nullcontext(claim_connection),
            nullcontext(load_db_connection),
            nullcontext(fail_connection),
        ]
        load_connection.return_value = (
            7, "yandex_market", "JoyCards", "", "216926720", "149196813", "token", None, None,
        )
        fetch_order.side_effect = HTTPException(status_code=404, detail="order is not visible yet")
        processor = build_yandex_market_webhook_processor(
            database_url=lambda: "postgresql://test",
            psycopg=fake_psycopg,
            processing_enabled=lambda: True,
        )

        self.assertEqual(processor.process_pending_events(batch_size=1), 1)

        fail_params = fail_cursor.execute.call_args.args[1]
        self.assertEqual(fail_params[0], "failed")
        self.assertEqual(fail_params[2], webhook_retry_delay_seconds(2))
        self.assertNotIn("token", fail_params[1])

    @patch.dict("os.environ", {"YANDEX_MARKET_WEBHOOK_MAX_ATTEMPTS": "3"})
    @patch("domains.yandex_market_webhook_processor.fetch_yandex_market_order")
    @patch("domains.yandex_market_webhook_processor.load_active_connection")
    def test_last_failure_moves_event_to_dead_letter(self, load_connection, fetch_order) -> None:
        claim_connection, _claim_cursor = connection_with_cursor(
            row=(41, 7, "149196813", "123", "ORDER_CREATED", 3),
        )
        load_db_connection, _load_cursor = connection_with_cursor()
        fail_connection, fail_cursor = connection_with_cursor()
        fake_psycopg = Mock()
        fake_psycopg.connect.side_effect = [
            nullcontext(claim_connection),
            nullcontext(load_db_connection),
            nullcontext(fail_connection),
        ]
        load_connection.return_value = (
            7, "yandex_market", "JoyCards", "", "216926720", "149196813", "token", None, None,
        )
        fetch_order.side_effect = RuntimeError("provider unavailable")
        processor = build_yandex_market_webhook_processor(
            database_url=lambda: "postgresql://test",
            psycopg=fake_psycopg,
            processing_enabled=lambda: True,
        )

        processor.process_pending_events(batch_size=1)

        fail_params = fail_cursor.execute.call_args.args[1]
        self.assertEqual(fail_params[0], "dead")
        self.assertEqual(fail_params[2], 0)


if __name__ == "__main__":
    unittest.main()
