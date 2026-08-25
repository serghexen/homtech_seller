"""Контрактные проверки read-only адаптеров Яндекс Маркета."""

from __future__ import annotations

import unittest
import urllib.parse
from unittest.mock import patch

from domains.marketplace_catalog_service import _fetch_yandex_catalog, _fetch_yandex_stocks, update_yandex_catalog_archive
from domains.marketplace_connection_verification import discover_yandex_market_stores


class YandexMarketplaceAdaptersTests(unittest.TestCase):
    @patch("domains.marketplace_connection_verification._read_json")
    def test_store_discovery_reads_all_pages_and_encodes_token(self, read_json) -> None:
        # Фиксирует актуальную forward-пагинацию, чтобы магазины после первой сотни не терялись.
        read_json.side_effect = [
            {
                "campaigns": [{"id": 101, "domain": "first", "business": {"id": 11, "name": "First"}}],
                "paging": {"nextPageToken": "next /+="},
            },
            {
                "campaigns": [{"id": 202, "domain": "second", "business": {"id": 22, "name": "Second"}}],
                "paging": {},
            },
        ]

        stores = discover_yandex_market_stores(token="test-api-key")

        self.assertEqual([store["campaign_id"] for store in stores], [101, 202])
        self.assertEqual(read_json.call_count, 2)
        first_query = urllib.parse.parse_qs(urllib.parse.urlparse(read_json.call_args_list[0].args[0].full_url).query)
        second_query = urllib.parse.parse_qs(urllib.parse.urlparse(read_json.call_args_list[1].args[0].full_url).query)
        self.assertEqual(first_query, {"limit": ["100"]})
        self.assertEqual(second_query, {"limit": ["100"], "pageToken": ["next /+="]})

    @patch("domains.marketplace_catalog_service._request_json")
    def test_catalog_uses_supported_limit_and_encodes_token(self, request_json) -> None:
        # Не допускает возврата к превышающему контракт API limit=200 и ручной склейке pageToken.
        request_json.side_effect = [
            {
                "result": {
                    "offerMappings": [{"offer": {"offerId": "one"}}],
                    "paging": {"nextPageToken": "catalog /+="},
                }
            },
            {"result": {"offerMappings": [{"offer": {"offerId": "two"}}], "paging": {}}},
            {"result": {"offerMappings": [{"offer": {"offerId": "old"}}], "paging": {}}},
        ]

        rows = _fetch_yandex_catalog(business_id=77, campaign_id=202, token="test-api-key")

        self.assertEqual([row["offer"]["offerId"] for row in rows], ["one", "two", "old"])
        self.assertEqual([row["offer"]["archived"] for row in rows], [False, False, True])
        self.assertEqual(request_json.call_count, 3)
        first_query = urllib.parse.parse_qs(urllib.parse.urlparse(request_json.call_args_list[0].args[0]).query)
        second_query = urllib.parse.parse_qs(urllib.parse.urlparse(request_json.call_args_list[1].args[0]).query)
        self.assertEqual(first_query, {"limit": ["100"]})
        self.assertEqual(second_query, {"limit": ["100"], "pageToken": ["catalog /+="]})
        self.assertEqual([call.kwargs["payload"] for call in request_json.call_args_list], [
            {"archived": False}, {"archived": False}, {"archived": True},
        ])

    @patch("domains.marketplace_catalog_service._request_json")
    def test_catalog_keeps_only_selected_campaign_when_yandex_returns_campaigns(self, request_json) -> None:
        request_json.side_effect = [
            {"result": {"offerMappings": [
                {"offer": {"offerId": "ours", "campaigns": [{"campaignId": 202}]}},
                {"offer": {"offerId": "other", "campaigns": [{"campaignId": 303}]}},
            ], "paging": {}}},
            {"result": {"offerMappings": [], "paging": {}}},
        ]

        rows = _fetch_yandex_catalog(business_id=77, campaign_id=202, token="test-api-key")

        self.assertEqual([row["offer"]["offerId"] for row in rows], ["ours"])

    @patch("domains.marketplace_catalog_service._request_json", return_value={"status": "OK"})
    def test_archive_action_uses_offer_id_without_stock_payload(self, request_json) -> None:
        update_yandex_catalog_archive(
            business_id=77,
            token="test-api-key",
            offer_id="MRKT-1",
            archived=True,
        )

        self.assertTrue(request_json.call_args.args[0].endswith("/v2/businesses/77/offer-mappings/archive"))
        self.assertEqual(request_json.call_args.kwargs["payload"], {"offerIds": ["MRKT-1"]})

        update_yandex_catalog_archive(
            business_id=77,
            token="test-api-key",
            offer_id="MRKT-1",
            archived=False,
        )
        self.assertTrue(request_json.call_args.args[0].endswith("/v2/businesses/77/offer-mappings/unarchive"))

    @patch("domains.marketplace_catalog_service._request_json")
    def test_stocks_are_read_in_batches_and_sum_only_available(self, request_json) -> None:
        # Фиксирует read-only POST, лимит 500 SKU и сумму AVAILABLE по складам без вызова обновления остатков.
        request_json.side_effect = [
            {"result": {"warehouses": [
                {"offers": [{"offerId": "sku-0", "updatedAt": "2026-08-24T12:00:00Z", "stocks": [
                    {"type": "AVAILABLE", "count": 3}, {"type": "FREEZE", "count": 9},
                ]}]},
                {"offers": [{"offerId": "sku-0", "updatedAt": "2026-08-24T12:01:00Z", "stocks": [
                    {"type": "AVAILABLE", "count": 2},
                ]}]},
            ]}},
            {"result": {"warehouses": []}},
        ]

        stocks = _fetch_yandex_stocks(
            campaign_id=77,
            token="test-api-key",
            offer_ids=[f"sku-{index}" for index in range(501)],
        )

        self.assertEqual(request_json.call_count, 2)
        self.assertTrue(all(call.kwargs["method"] == "POST" for call in request_json.call_args_list))
        self.assertEqual(len(request_json.call_args_list[0].kwargs["payload"]["offerIds"]), 500)
        self.assertEqual(len(request_json.call_args_list[1].kwargs["payload"]["offerIds"]), 1)
        self.assertEqual(stocks["sku-0"]["available_stock"], 5)
        self.assertEqual(stocks["sku-0"]["updated_at"], "2026-08-24T12:01:00Z")
        self.assertFalse(stocks["sku-500"]["found"])


if __name__ == "__main__":
    unittest.main()
