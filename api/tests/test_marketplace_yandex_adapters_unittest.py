"""Контрактные проверки read-only адаптеров Яндекс Маркета."""

from __future__ import annotations

import unittest
import urllib.parse
from unittest.mock import patch

from domains.marketplace_catalog_service import _fetch_yandex_catalog
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
        ]

        rows = _fetch_yandex_catalog(business_id=77, token="test-api-key")

        self.assertEqual([row["offer"]["offerId"] for row in rows], ["one", "two"])
        self.assertEqual(request_json.call_count, 2)
        first_query = urllib.parse.parse_qs(urllib.parse.urlparse(request_json.call_args_list[0].args[0]).query)
        second_query = urllib.parse.parse_qs(urllib.parse.urlparse(request_json.call_args_list[1].args[0]).query)
        self.assertEqual(first_query, {"limit": ["100"]})
        self.assertEqual(second_query, {"limit": ["100"], "pageToken": ["catalog /+="]})


if __name__ == "__main__":
    unittest.main()
