"""Проверки нормализации read-only снимков Seller."""

from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import FastAPI
from pydantic import ValidationError

from domains.marketplace_catalog_actions_api import mount_marketplace_catalog_action_routes
from domains.marketplace_read_api import (
    CATALOG_SEARCH_EXPRESSIONS,
    ORDER_SEARCH_EXPRESSIONS,
    YANDEX_STARTED_ORDER_VISIBILITY_SQL,
    MarketplaceCatalogItemOut,
    MarketplaceCatalogStockPublishIn,
    MarketplaceCatalogSettingsIn,
    marketplace_order_from_row,
    catalog_card_details,
    catalog_marketplace_url,
    catalog_payload_with_stock,
    catalog_primary_image,
    ilike_search_condition,
    mount_marketplace_read_routes,
    normalize_catalog_item,
    normalize_order_items,
    order_fulfillment_action,
    supplier_price_guard,
)


class MarketplaceReadApiTests(unittest.TestCase):
    def test_yandex_working_order_visibility_uses_fulfillment_identity(self) -> None:
        self.assertIn("connection.provider_code <> 'yandex_market'", YANDEX_STARTED_ORDER_VISIBILITY_SQL)
        self.assertIn("FROM seller.order_fulfillments AS visible_fulfillment", YANDEX_STARTED_ORDER_VISIBILITY_SQL)
        self.assertIn("visible_fulfillment.connection_id=item.connection_id", YANDEX_STARTED_ORDER_VISIBILITY_SQL)
        self.assertIn("visible_fulfillment.external_order_id=item.external_order_id", YANDEX_STARTED_ORDER_VISIBILITY_SQL)
        self.assertIn("visible_fulfillment.external_item_id=item.external_item_id", YANDEX_STARTED_ORDER_VISIBILITY_SQL)

        route_source = inspect.getsource(mount_marketplace_read_routes)
        self.assertGreaterEqual(route_source.count("YANDEX_STARTED_ORDER_VISIBILITY_SQL"), 3)
        self.assertIn(
            'conditions = ["connection.workspace_id=%s", YANDEX_STARTED_ORDER_VISIBILITY_SQL]',
            route_source,
        )

    def test_order_list_exposes_stored_fulfillment_result(self) -> None:
        result = marketplace_order_from_row((
            3, "ozon", "ASAT", "04259716-0136-1", "5196324554", "17162", "5196324554",
            "PUBG", 1, "delivered", "delivered", "FBO", None, None,
            datetime(2026, 8, 27, tzinfo=timezone.utc), True, True,
        ))

        self.assertEqual(result.delivery_type, "FBO")
        self.assertTrue(result.has_fulfillment_keys)
        self.assertTrue(result.has_fulfillment_result)

    def test_order_list_marks_support_message_without_claiming_it_has_keys(self) -> None:
        result = marketplace_order_from_row((
            4, "yandex_market", "ASAT GAMES", "60940029440", "60940029440", "MRKT-GL4ZAXEY",
            "MRKT-GL4ZAXEY", "PSN CHF", 1, "delivered", "DELIVERED", "DIGITAL", None, None,
            datetime(2026, 8, 27, tzinfo=timezone.utc), False, True,
        ))

        self.assertFalse(result.has_fulfillment_keys)
        self.assertTrue(result.has_fulfillment_result)

    def test_order_list_marks_automatic_fulfillment_as_non_operator_action(self) -> None:
        result = marketplace_order_from_row((
            7, "yandex_market", "MIC DIGITAL SHOP", "60976906051", "60976906051", "MRKT-9E6P74A1",
            "MRKT-9E6P74A1", "Crash Bandicoot", 1, "processing", "PROCESSING", "DIGITAL", None, None,
            datetime(2026, 8, 28, tzinfo=timezone.utc), False, True,
            "sending", "automatic", "sending", True, False,
        ))

        self.assertEqual(result.fulfillment_status, "sending")
        self.assertEqual(result.fulfillment_handling_mode, "automatic")
        self.assertEqual(result.fulfillment_action, "automatic")

    def test_fulfillment_action_distinguishes_operator_view_and_attention(self) -> None:
        base = {
            "provider_code": "yandex_market",
            "order_status": "processing",
            "delivery_type": "DIGITAL",
            "has_result": False,
        }
        self.assertEqual(order_fulfillment_action(
            **base, fulfillment_status="manual_required", handling_mode="manual", resolver_enabled=True,
        ), "operator")
        self.assertEqual(order_fulfillment_action(
            **base, fulfillment_status="unknown", handling_mode="automatic", resolver_enabled=True,
        ), "attention")
        self.assertEqual(order_fulfillment_action(
            **{**base, "order_status": "in_delivery", "has_result": True},
            fulfillment_status="submitted", handling_mode="automatic", resolver_enabled=True,
        ), "view")

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

    def test_ozon_digital_order_keeps_digital_delivery_type(self) -> None:
        items = normalize_order_items("ozon", {
            "posting_number": "04259716-0133-1",
            "status": "awaiting_packaging",
            "__marketplace_source": "DIGITAL",
            "products": [{
                "product_id": 5639743995,
                "sku": 5196324554,
                "offer_id": "17162",
                "required_qty_for_digital_code": 1,
            }],
        })

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["delivery_type"], "DIGITAL")
        self.assertEqual(items[0]["quantity"], 1)

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

    def test_yandex_catalog_uses_exact_b2c_showcase_url(self) -> None:
        url = catalog_marketplace_url(
            "yandex_market",
            {"showcaseUrls": [
                {"showcaseType": "B2B", "showcaseUrl": "https://market.yandex.ru/card/business/1"},
                {"showcaseType": "B2C", "showcaseUrl": "https://market.yandex.ru/card/product/6100345993"},
            ]},
        )
        self.assertEqual(url, "https://market.yandex.ru/card/product/6100345993")

    def test_catalog_marketplace_url_rejects_untrusted_host(self) -> None:
        url = catalog_marketplace_url(
            "yandex_market",
            {"showcaseUrls": [{"showcaseType": "B2C", "showcaseUrl": "https://example.com/not-market"}]},
        )
        self.assertEqual(url, "")

    def test_ozon_catalog_builds_product_url_from_numeric_sku(self) -> None:
        self.assertEqual(
            catalog_marketplace_url("ozon", {}, sku="5204479032"),
            "https://www.ozon.ru/product/5204479032/",
        )

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

    def test_ozon_catalog_card_reads_embedded_stock(self) -> None:
        details = catalog_card_details(
            "ozon",
            {
                "price": "133.00",
                "currency_code": "RUB",
                "stocks": {"has_stock": True, "stocks": [
                    {"sku": 5196324554, "source": "fbo", "present": 5, "reserved": 0},
                ]},
            },
        )

        self.assertEqual(details["available_stock"], 5)
        self.assertIsNone(details["stock_synced_at"])

    def test_ozon_catalog_card_prefers_checked_snapshot(self) -> None:
        details = catalog_card_details(
            "ozon",
            {
                "stocks": {"has_stock": True, "stocks": [{"present": 5}]},
                "_sellerSnapshot": {
                    "availableStock": 4,
                    "stockCheckedAt": "2026-08-27T16:30:00+00:00",
                },
            },
        )

        self.assertEqual(details["available_stock"], 4)
        self.assertEqual(details["stock_synced_at"], "2026-08-27T16:30:00+00:00")

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

    def test_mounts_durable_manual_stock_publication(self) -> None:
        app = FastAPI()
        mount_marketplace_read_routes(
            app,
            database_url=lambda: "",
            psycopg=None,
            current_user=lambda: None,
            user_with_workspace=lambda *_args: None,
        )
        publish = next(route for route in app.routes if route.path == "/marketplaces/catalog/stock/publications")
        status = next(route for route in app.routes if route.path == "/marketplaces/catalog/stock/publications/{job_id}")
        self.assertEqual(publish.methods, {"POST"})
        self.assertEqual(status.methods, {"GET"})

    def test_manual_stock_publication_validates_explicit_target(self) -> None:
        payload = MarketplaceCatalogStockPublishIn(
            connection_id=1,
            external_product_id="MRKT-1",
            target_stock=5,
        )
        self.assertEqual(payload.target_stock, 5)
        with self.assertRaises(ValidationError):
            MarketplaceCatalogStockPublishIn(
                connection_id=1,
                external_product_id="MRKT-1",
                target_stock=-1,
            )

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

    def test_catalog_source_enablement_uses_store_scoped_capabilities(self) -> None:
        source = inspect.getsource(mount_marketplace_read_routes)

        self.assertIn("pool_being_enabled", source)
        self.assertIn("support_being_enabled", source)
        self.assertIn("current_access.allows(FULFILLMENT_POOL)", source)
        self.assertIn("current_access.allows(FULFILLMENT_MANUAL)", source)

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

    def test_ozon_digital_order_uses_required_code_quantity_and_deadline(self) -> None:
        items = normalize_order_items(
            "ozon",
            {
                "posting_number": "0201103974-0044-1",
                "status": "awaiting_packaging",
                "__marketplace_source": "DIGITAL",
                "waiting_deadline_for_digital_code": "2026-08-27T17:30:00Z",
                "products": [{
                    "product_id": 17162,
                    "sku": 5204479032,
                    "offer_id": "PSN-250",
                    "quantity": 1,
                    "required_qty_for_digital_code": 3,
                }],
            },
        )
        self.assertEqual(items[0]["quantity"], 3)
        self.assertEqual(items[0]["normalized_status"], "processing")
        self.assertEqual(items[0]["fulfillment_deadline_at"].isoformat(), "2026-08-27T17:30:00+00:00")

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
