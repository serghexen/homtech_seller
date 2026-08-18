"""Проверки нормализации read-only снимков Seller."""

from __future__ import annotations

import unittest

from domains.marketplace_read_api import normalize_catalog_item, normalize_order_items


class MarketplaceReadApiTests(unittest.TestCase):
    def test_ozon_catalog_keeps_numeric_sku_and_title(self) -> None:
        # Фиксирует приоритет SKU Ozon, чтобы карточки не превращались в безымянные offerId.
        result = normalize_catalog_item(
            "ozon",
            {"product_id": 16987, "offer_id": "PSN-250-TRY", "sku": 5204479032, "name": "PSN 250 TRY"},
        )
        self.assertEqual(result, {
            "external_product_id": "16987",
            "offer_id": "PSN-250-TRY",
            "sku": "5204479032",
            "title": "PSN 250 TRY",
        })

    def test_yandex_catalog_uses_seller_offer_as_sku(self) -> None:
        # Фиксирует видимый продавцу SKU Яндекс Маркета вместо технического marketSku.
        result = normalize_catalog_item(
            "yandex_market",
            {"offer": {"offerId": "MRKT-SKI3HKAA", "name": "Apex Legends"}, "mapping": {"marketSku": 12345}},
        )
        self.assertEqual(result["external_product_id"], "MRKT-SKI3HKAA")
        self.assertEqual(result["sku"], "MRKT-SKI3HKAA")
        self.assertEqual(result["title"], "Apex Legends")

    def test_ozon_order_expands_products_without_delivery_action(self) -> None:
        # Проверяет, что снимок Ozon содержит позицию, но не добавляет никаких команд выдачи.
        items = normalize_order_items(
            "ozon",
            {
                "posting_number": "0201103974-0033-1",
                "status": "delivered",
                "in_process_at": "2026-08-02T10:31:00Z",
                "products": [{"product_id": 17162, "sku": 5204479032, "offer_id": "PSN-250", "name": "PSN 250 TRY", "quantity": 1}],
            },
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["normalized_status"], "delivered")
        self.assertEqual(items[0]["sku"], "5204479032")

    def test_yandex_order_expands_items_and_normalizes_status(self) -> None:
        # Проверяет единый статус Seller для карточки Яндекс Маркета с несколькими полями ответа.
        items = normalize_order_items(
            "yandex_market",
            {
                "orderId": 59817480451,
                "status": "PROCESSING",
                "creationDate": "2026-08-02T16:25:00Z",
                "items": [{"id": 2, "offerId": "MRKT-14UDFN97", "offerName": "App Store 2 USD", "count": 1}],
            },
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["external_order_id"], "59817480451")
        self.assertEqual(items[0]["status"] if "status" in items[0] else items[0]["normalized_status"], "processing")
        self.assertEqual(items[0]["sku"], "MRKT-14UDFN97")


if __name__ == "__main__":
    unittest.main()
