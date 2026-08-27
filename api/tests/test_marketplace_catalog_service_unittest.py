"""Проверки полного read-only снимка каталога маркетплейса."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from domains.marketplace_catalog_service import _fetch_ozon_catalog


class MarketplaceCatalogServiceTests(unittest.TestCase):
    @patch("domains.marketplace_catalog_service._request_json")
    def test_ozon_catalog_combines_active_and_archived_snapshots(self, request_json) -> None:
        def response(url, *, method, headers, payload=None):
            self.assertEqual(method, "POST")
            self.assertEqual(headers, {"Client-Id": "client", "Api-Key": "token"})
            if url.endswith("/v3/product/info/list"):
                self.assertEqual(payload["product_id"], [10, 20])
                return {
                    "items": [
                        {"id": 10, "offer_id": "LIVE", "name": "Активный товар"},
                        {"id": 20, "offer_id": "OLD", "name": "Архивный товар"},
                    ],
                }
            visibility = payload["filter"]["visibility"]
            if payload["last_id"]:
                return {"result": {"items": [], "last_id": ""}}
            if visibility == "ALL":
                return {"result": {"items": [{"product_id": 10, "offer_id": "LIVE"}], "last_id": "live-next"}}
            if visibility == "ARCHIVED":
                return {"result": {"items": [{"product_id": 20, "offer_id": "OLD"}], "last_id": "archive-next"}}
            self.fail(f"Unexpected Ozon visibility: {visibility}")

        request_json.side_effect = response

        rows = _fetch_ozon_catalog(client_id="client", token="token")

        self.assertEqual(len(rows), 2)
        self.assertEqual([(row["offer_id"], row["name"], row["archived"]) for row in rows], [
            ("LIVE", "Активный товар", False),
            ("OLD", "Архивный товар", True),
        ])
        list_calls = [call.kwargs["payload"] for call in request_json.call_args_list if call.args[0].endswith("/v3/product/list")]
        self.assertEqual([payload["filter"]["visibility"] for payload in list_calls], [
            "ALL", "ALL", "ARCHIVED", "ARCHIVED",
        ])


if __name__ == "__main__":
    unittest.main()
