"""Проверки безопасной нормализации и выбора read-only адаптеров заказов."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from domains.marketplace_orders_service import (
    _fetch_ozon_orders,
    _fetch_yandex_market_orders,
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
        # Первый запуск явно читает историю, а не только заказы текущего дня.
        _fetch_yandex_market_orders(
            business_id=10,
            campaign_id=20,
            token="secret",
            synced_before=datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
        )

        dates = request_json.call_args.kwargs["payload"]["dates"]
        self.assertEqual(dates, {"creationDateFrom": "2026-07-26", "creationDateTo": "2026-08-25"})

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

        self.assertEqual(request_json.call_count, 2)
        first_dates = request_json.call_args_list[0].kwargs["payload"]["dates"]
        second_dates = request_json.call_args_list[1].kwargs["payload"]["dates"]
        self.assertEqual(first_dates["updateDateFrom"], "2026-07-01T11:55:00Z")
        self.assertEqual(first_dates["updateDateTo"], second_dates["updateDateFrom"])
        self.assertEqual(second_dates["updateDateTo"], "2026-08-15T12:00:00Z")

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
