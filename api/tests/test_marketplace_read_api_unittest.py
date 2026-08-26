"""Проверки нормализации read-only снимков Seller."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import FastAPI
from pydantic import ValidationError

from domains.marketplace_catalog_actions_api import mount_marketplace_catalog_action_routes
from domains.marketplace_read_api import (
    CATALOG_SEARCH_EXPRESSIONS,
    ORDER_SEARCH_EXPRESSIONS,
    MarketplaceCatalogItemOut,
    MarketplaceCatalogSettingsIn,
    catalog_card_details,
    catalog_payload_with_stock,
    catalog_primary_image,
    ilike_search_condition,
    mount_marketplace_read_routes,
    normalize_catalog_item,
    normalize_order_items,
    supplier_price_guard,
)


class MarketplaceReadApiTests(unittest.TestCase):
    def test_supplier_price_guard_is_internal_five_percent_ceiling(self) -> None:
        self.assertEqual(supplier_price_guard(Decimal("464.53")), Decimal("487.76"))

    def test_catalog_search_includes_visible_market_sku(self) -> None:
        condition, params = ilike_search_condition("6099375668", CATALOG_SEARCH_EXPRESSIONS)
        self.assertIn("mapping,marketSku", condition)
        self.assertEqual(params, ["%6099375668%"] * 4)

    def test_order_search_includes_offer_id(self) -> None:
        condition, params = ilike_search_condition("MRKT-3ETEAI6X", ORDER_SEARCH_EXPRESSIONS)
        self.assertIn("item.offer_id ILIKE %s", condition)
        self.assertEqual(params, ["%MRKT-3ETEAI6X%"] * 4)

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
            "is_archived": False,
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
        self.assertFalse(result["is_archived"])

    def test_yandex_catalog_preserves_archive_state(self) -> None:
        result = normalize_catalog_item(
            "yandex_market",
            {"offer": {"offerId": "OLD-SKU", "name": "Архивный товар", "archived": True}},
        )
        self.assertTrue(result["is_archived"])

    def test_yandex_catalog_uses_saved_first_picture(self) -> None:
        # Изображение берётся из локального raw_payload, поэтому открытие карточки не вызывает API Маркета повторно.
        image = catalog_primary_image(
            "yandex_market",
            {"offer": {"pictures": ["https://avatars.mds.yandex.net/example/1", "https://avatars.mds.yandex.net/example/2"]}},
        )
        self.assertEqual(image, "https://avatars.mds.yandex.net/example/1")

    def test_yandex_catalog_card_uses_saved_market_sku_and_price(self) -> None:
        details = catalog_card_details(
            "yandex_market",
            {
                "offer": {"basicPrice": {"value": 1284.0, "currencyId": "RUR"}},
                "mapping": {"marketSku": 6099375668},
                "_sellerSnapshot": {"availableStock": 5, "stockCheckedAt": "2026-08-24T16:25:00+00:00"},
            },
        )
        self.assertEqual(details, {
            "market_sku": "6099375668",
            "price": "1284.0",
            "currency_code": "RUR",
            "available_stock": 5,
            "stock_synced_at": "2026-08-24T16:25:00+00:00",
        })

    def test_ozon_catalog_uses_saved_primary_image(self) -> None:
        image = catalog_primary_image("ozon", {"primary_image": "https://cdn1.ozone.ru/example.jpg"})
        self.assertEqual(image, "https://cdn1.ozone.ru/example.jpg")

    def test_stock_snapshot_preserves_marketplace_payload(self) -> None:
        checked_at = datetime(2026, 8, 24, 16, 25, tzinfo=timezone.utc)
        payload = catalog_payload_with_stock(
            {"offer": {"offerId": "MRKT-1"}},
            available_stock=5,
            checked_at=checked_at,
            provider_updated_at="2026-08-24T16:24:00Z",
        )
        self.assertEqual(payload["offer"]["offerId"], "MRKT-1")
        self.assertEqual(payload["_sellerSnapshot"]["availableStock"], 5)
        self.assertEqual(payload["_sellerSnapshot"]["stockCheckedAt"], checked_at.isoformat())

    def test_catalog_contract_distinguishes_live_and_imported_stock(self) -> None:
        item = MarketplaceCatalogItemOut(
            connection_id=1,
            provider_code="yandex_market",
            store_name="JoyCards",
            external_product_id="MRKT-1",
            offer_id="MRKT-1",
            available_stock=3,
            stock_settings_available=True,
            manual_stock_limit=5,
            published_stock=5,
            activation_instruction="Активируйте код в магазине.",
            pool_issue_enabled=True,
            sales_limit=None,
            sales_limit_used=0,
            sales_limit_reserved=0,
            sales_limit_remaining=None,
            synced_at=datetime(2026, 8, 24, 16, 25, tzinfo=timezone.utc),
        )
        self.assertEqual(item.available_stock, 3)
        self.assertEqual(item.manual_stock_limit, 5)
        self.assertTrue(item.stock_settings_available)
        self.assertTrue(item.pool_issue_enabled)
        self.assertIsNone(item.sales_limit)

    def test_mounts_interactive_readonly_stock_refresh(self) -> None:
        app = FastAPI()
        mount_marketplace_read_routes(
            app,
            database_url=lambda: "",
            psycopg=None,
            current_user=lambda: None,
            user_with_workspace=lambda *_args: None,
        )
        route = next(route for route in app.routes if route.path == "/marketplaces/catalog/stock/refresh")
        self.assertEqual(route.methods, {"POST"})

    def test_mounts_explicit_catalog_archive_action(self) -> None:
        app = FastAPI()
        mount_marketplace_catalog_action_routes(
            app,
            database_url=lambda: "",
            psycopg=None,
            current_user=lambda: None,
            user_with_workspace=lambda *_args: None,
        )
        route = next(route for route in app.routes if route.path == "/marketplaces/catalog/archive")
        self.assertEqual(route.methods, {"POST"})

    def test_mounts_local_catalog_settings_save_without_marketplace_action(self) -> None:
        app = FastAPI()
        mount_marketplace_read_routes(
            app,
            database_url=lambda: "",
            psycopg=None,
            current_user=lambda: None,
            user_with_workspace=lambda *_args: None,
        )
        route = next(route for route in app.routes if route.path == "/marketplaces/catalog/settings")
        self.assertEqual(route.methods, {"POST"})

    def test_catalog_settings_validate_local_limits(self) -> None:
        valid = MarketplaceCatalogSettingsIn(
            connection_id=1,
            external_product_id="MRKT-1",
            manual_stock_limit=5,
            sales_limit=10,
            sales_limit_daily_extra=2,
            activation_instruction="Первая строка\nВторая строка",
            pool_issue_enabled=True,
        )
        self.assertEqual(valid.sales_limit, 10)
        self.assertTrue(valid.pool_issue_enabled)
        with self.assertRaises(ValidationError):
            MarketplaceCatalogSettingsIn(
                connection_id=1,
                external_product_id="MRKT-1",
                manual_stock_limit=-1,
            )

    def test_mounts_readonly_catalog_orders_route(self) -> None:
        app = FastAPI()
        mount_marketplace_read_routes(
            app,
            database_url=lambda: "",
            psycopg=None,
            current_user=lambda: None,
            user_with_workspace=lambda *_args: None,
        )
        route = next(route for route in app.routes if route.path == "/marketplaces/catalog/orders")
        self.assertEqual(route.methods, {"GET"})

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
                "updateDate": "2026-08-02T17:26:30+03:00",
                "items": [{"id": 2, "offerId": "MRKT-14UDFN97", "offerName": "App Store 2 USD", "count": 1}],
            },
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["external_order_id"], "59817480451")
        self.assertEqual(items[0]["status"] if "status" in items[0] else items[0]["normalized_status"], "processing")
        self.assertEqual(items[0]["sku"], "MRKT-14UDFN97")
        self.assertEqual(items[0]["updated_at"].isoformat(), "2026-08-02T17:26:30+03:00")


if __name__ == "__main__":
    unittest.main()
