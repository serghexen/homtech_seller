"""Проверки безопасной нормализации и выбора read-only адаптеров заказов."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from domains.marketplace_orders_service import fetch_marketplace_orders, normalize_marketplace_order_status


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
        fetch_orders.assert_called_once_with(business_id=10, campaign_id=20, token="secret")

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
        fetch_orders.assert_called_once_with(client_id="123", token="secret")
