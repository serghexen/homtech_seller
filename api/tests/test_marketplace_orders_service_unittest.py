"""Проверки безопасной нормализации и выбора read-only адаптеров заказов."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import HTTPException

from domains.marketplace_orders_service import (
    MarketplacePaginationError,
    _fetch_ozon_fbo_orders,
    _fetch_ozon_orders,
    _fetch_yandex_market_orders,
    fetch_yandex_market_order,
    fetch_marketplace_orders,
    normalize_marketplace_order_status,
)


class MarketplaceOrdersServiceTests(unittest.TestCase):
    def test_normalizes_yandex_statuses_to_russian_domain_states(self) -> None:
        # Фиксирует единый справочник Seller, чтобы UI не зависел от статусов конкретного маркета.
        self.assertEqual(normalize_marketplace_order_status(provider_code="yandex_market", status="PROCESSING"), "processing")
        self.assertEqual(normalize_marketplace_order_status(provider_code="yandex_market", status="DELIVERY"), "in_delivery")
        self.assertEqual(normalize_marketplace_order_status(provider_code="yandex_market", status="DELIVERED"), "delivered")
        self.assertEqual(normalize_marketplace_order_status(provider_code="yandex_market", status="CANCELLED"), "cancelled")
        self.assertEqual(normalize_marketplace_order_status(provider_code="yandex_market", status="UNEXPECTED"), "problem")

    def test_normalizes_ozon_statuses_to_russian_domain_states(self) -> None:
        # Проверяет важные статусы Ozon, включая отмену с различными техническими суффиксами.
        self.assertEqual(normalize_marketplace_order_status(provider_code="ozon", status="awaiting_delivery"), "processing")
        self.assertEqual(normalize_marketplace_order_status(provider_code="ozon", status="awaiting_packaging"), "processing")
        self.assertEqual(normalize_marketplace_order_status(provider_code="ozon", status="delivering"), "in_delivery")
        self.assertEqual(normalize_marketplace_order_status(provider_code="ozon", status="done"), "delivered")
        self.assertEqual(normalize_marketplace_order_status(provider_code="ozon", status="cancelled_by_seller"), "cancelled")
        self.assertEqual(normalize_marketplace_order_status(provider_code="ozon", status="unknown"), "problem")

    @patch("domains.marketplace_orders_service._fetch_yandex_market_orders", return_value=[{"orderId": "1"}])
    def test_selects_yandex_reader_without_delivery_actions(self, fetch_orders) -> None:
        # Убеждается, что публичная функция выбирает только читатель заказов Яндекс Маркета.
        result = fetch_marketplace_orders(
            provider_code="yandex_market",
            token="secret",
            client_id="",
            business_id=10,
            campaign_id=20,
        )
        self.assertEqual(result, [{"orderId": "1"}])
        fetch_orders.assert_called_once_with(
            business_id=10, campaign_id=20, token="secret", synced_after=None, synced_before=None,
        )

    @patch("domains.marketplace_orders_service._fetch_ozon_orders", return_value=[{"posting_number": "1"}])
    def test_selects_ozon_reader_without_delivery_actions(self, fetch_orders) -> None:
        # Убеждается, что Ozon получает только пару реквизитов для read-only запроса.
        result = fetch_marketplace_orders(
            provider_code="ozon",
            token="secret",
            client_id="123",
            business_id=None,
            campaign_id=None,
        )
        self.assertEqual(result, [{"posting_number": "1"}])
        fetch_orders.assert_called_once_with(
            client_id="123", token="secret", synced_after=None, synced_before=None,
        )

    @patch("domains.marketplace_orders_service._request_json", return_value={"result": {"orders": [], "paging": {}}})
    def test_yandex_first_sync_backfills_thirty_calendar_days(self, request_json) -> None:
        # Первый запуск читает историю посуточно, чтобы большой магазин не упирался в число страниц за месяц.
        _fetch_yandex_market_orders(
            business_id=10,
            campaign_id=20,
            token="secret",
            synced_before=datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(request_json.call_count, 30)
        first_dates = request_json.call_args_list[0].kwargs["payload"]["dates"]
        last_dates = request_json.call_args_list[-1].kwargs["payload"]["dates"]
        self.assertEqual(first_dates, {"creationDateFrom": "2026-07-26", "creationDateTo": "2026-07-27"})
        self.assertEqual(last_dates, {"creationDateFrom": "2026-08-24", "creationDateTo": "2026-08-25"})

    @patch("domains.marketplace_orders_service._request_json", return_value={"result": {"orders": [], "paging": {}}})
    def test_yandex_incremental_sync_uses_update_watermark_and_splits_downtime(self, request_json) -> None:
        # Интервал больше лимита Яндекса дробится без пропуска и начинается с пятиминутного overlap.
        _fetch_yandex_market_orders(
            business_id=10,
            campaign_id=20,
            token="secret",
            synced_after=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
            synced_before=datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(request_json.call_count, 46)
        first_dates = request_json.call_args_list[0].kwargs["payload"]["dates"]
        second_dates = request_json.call_args_list[1].kwargs["payload"]["dates"]
        last_dates = request_json.call_args_list[-1].kwargs["payload"]["dates"]
        self.assertEqual(first_dates["updateDateFrom"], "2026-07-01T11:55:00Z")
        self.assertEqual(first_dates["updateDateTo"], second_dates["updateDateFrom"])
        self.assertEqual(last_dates["updateDateTo"], "2026-08-15T12:00:00Z")

    @patch("domains.marketplace_orders_service._request_json")
    def test_yandex_pagination_continues_beyond_one_hundred_pages(self, request_json) -> None:
        # nextPageToken является единственным признаком конца выдачи; внутренний предел не должен обрезать магазин.
        request_json.side_effect = [
            {
                "result": {
                    "orders": [{"campaignId": 20, "orderId": str(page_number)}],
                    "paging": {"nextPageToken": f"page-{page_number + 1}"} if page_number < 100 else {},
                }
            }
            for page_number in range(101)
        ]

        result = _fetch_yandex_market_orders(
            business_id=10,
            campaign_id=20,
            token="secret",
            synced_after=datetime(2026, 8, 24, 0, 5, tzinfo=timezone.utc),
            synced_before=datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(request_json.call_count, 101)
        self.assertEqual(len(result), 101)
        self.assertIn("pageToken=page-100", request_json.call_args.args[0])

    @patch("domains.marketplace_orders_service._request_json")
    def test_yandex_pagination_rejects_repeated_token(self, request_json) -> None:
        request_json.side_effect = [
            {"result": {"orders": [], "paging": {"nextPageToken": "same-page"}}},
            {"result": {"orders": [], "paging": {"nextPageToken": "same-page"}}},
        ]

        with self.assertRaisesRegex(MarketplacePaginationError, "зациклил"):
            _fetch_yandex_market_orders(
                business_id=10,
                campaign_id=20,
                token="secret",
                synced_after=datetime(2026, 8, 24, 0, 5, tzinfo=timezone.utc),
                synced_before=datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
            )

    @patch("domains.marketplace_orders_service._request_json")
    def test_yandex_webhook_reads_exactly_one_order(self, request_json) -> None:
        # Webhook использует orderIds и не перечитывает историю либо соседние заказы магазина.
        request_json.return_value = {
            "result": {
                "orders": [
                    {"orderId": 123, "campaignId": 20, "items": [{"id": 1, "offerId": "SKU-1"}]},
                ]
            }
        }

        order = fetch_yandex_market_order(
            business_id=10,
            campaign_id=20,
            order_id=123,
            token="secret",
        )

        self.assertEqual(order["orderId"], 123)
        self.assertTrue(request_json.call_args.args[0].endswith("/v1/businesses/10/orders?limit=1"))
        self.assertEqual(
            request_json.call_args.kwargs["payload"],
            {"campaignIds": [20], "orderIds": [123], "programTypes": ["DBS"]},
        )

    @patch("domains.marketplace_orders_service._request_json", return_value={"result": {"orders": []}})
    def test_yandex_webhook_retries_when_order_is_not_visible_yet(self, _request_json) -> None:
        # HTTP 404 попадёт в retry очереди: уведомление может прийти немного раньше доступности заказа в API.
        with self.assertRaises(HTTPException) as raised:
            fetch_yandex_market_order(
                business_id=10,
                campaign_id=20,
                order_id=123,
                token="secret",
            )

        self.assertEqual(raised.exception.status_code, 404)

    @patch("domains.marketplace_orders_service._fetch_ozon_fbo_orders", return_value=[])
    @patch("domains.marketplace_orders_service._fetch_ozon_digital_orders", return_value=[])
    def test_ozon_sync_keeps_rolling_window_and_extends_after_downtime(self, digital_orders, fbo_orders) -> None:
        # После долгого простоя Ozon получает весь разрыв, разбитый на безопасные интервалы.
        _fetch_ozon_orders(
            client_id="123",
            token="secret",
            synced_after=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
            synced_before=datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(digital_orders.call_count, 3)
        self.assertEqual(fbo_orders.call_count, 3)
        first_period = digital_orders.call_args_list[0].kwargs
        last_period = digital_orders.call_args_list[-1].kwargs
        self.assertEqual(first_period["period_from"], datetime(2026, 5, 31, 23, 55, tzinfo=timezone.utc))
        self.assertEqual(last_period["period_to"], datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc))

    @patch("domains.marketplace_orders_service._fetch_ozon_fbo_orders")
    @patch("domains.marketplace_orders_service._fetch_ozon_digital_orders")
    def test_ozon_digital_snapshot_wins_when_posting_is_also_in_fbo(self, digital_orders, fbo_orders) -> None:
        digital_orders.return_value = [{
            "posting_number": "04259716-0133-1",
            "__marketplace_source": "DIGITAL",
            "products": [{"sku": 5196324554, "required_qty_for_digital_code": 1}],
        }]
        fbo_orders.return_value = [{
            "posting_number": "04259716-0133-1",
            "__marketplace_source": "FBO",
            "products": [{"sku": 5196324554, "quantity": 1}],
        }]

        rows = _fetch_ozon_orders(
            client_id="123",
            token="secret",
            synced_after=datetime(2026, 8, 27, 15, 50, tzinfo=timezone.utc),
            synced_before=datetime(2026, 8, 27, 15, 55, tzinfo=timezone.utc),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["__marketplace_source"], "DIGITAL")
        self.assertEqual(rows[0]["products"][0]["required_qty_for_digital_code"], 1)

    @patch("domains.marketplace_orders_service._request_json")
    def test_ozon_fbo_v3_uses_cursor_and_does_not_request_financial_data(self, request_json) -> None:
        request_json.side_effect = [
            {"postings": [{"posting_number": "1"}], "has_next": True, "cursor": "next"},
            {"postings": [{"posting_number": "2"}], "has_next": False, "cursor": "done"},
        ]

        rows = _fetch_ozon_fbo_orders(
            client_id="client",
            token="secret",
            period_from=datetime(2026, 8, 29, tzinfo=timezone.utc),
            period_to=datetime(2026, 8, 30, tzinfo=timezone.utc),
        )

        self.assertEqual([row["posting_number"] for row in rows], ["1", "2"])
        first_call = request_json.call_args_list[0]
        second_call = request_json.call_args_list[1]
        self.assertTrue(first_call.args[0].endswith("/v3/posting/fbo/list"))
        self.assertEqual(first_call.kwargs["payload"]["filter"]["status"], [
            "awaiting_packaging", "awaiting_deliver", "delivering", "delivered", "cancelled",
        ])
        self.assertFalse(first_call.kwargs["payload"]["with"]["financial_data"])
        self.assertEqual(second_call.kwargs["payload"]["cursor"], "next")
