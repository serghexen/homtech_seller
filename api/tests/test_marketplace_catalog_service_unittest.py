"""Проверки полного read-only снимка каталога маркетплейса."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from domains.marketplace_catalog_service import _fetch_ozon_catalog, _fetch_ozon_stocks, ozon_stock_snapshot


class MarketplaceCatalogServiceTests(unittest.TestCase):
    def test_ozon_stock_snapshot_sums_present_sources(self) -> None:
        stock = ozon_stock_snapshot({
            "updated_at": "2026-08-27T15:28:20Z",
            "stocks": {"has_stock": True, "stocks": [
                {"source": "fbo", "present": 5, "reserved": 0},
                {"source": "fbs", "present": 2, "reserved": 1},
            ]},
        })

        self.assertEqual(stock, {
            "found": True,
            "available_stock": 7,
            "updated_at": "2026-08-27T15:28:20Z",
        })

    def test_ozon_stock_snapshot_treats_explicit_empty_stock_as_zero(self) -> None:
        self.assertEqual(ozon_stock_snapshot({"stocks": {"has_stock": False, "stocks": []}})["available_stock"], 0)
        self.assertFalse(ozon_stock_snapshot({})["found"])

    @patch("domains.marketplace_catalog_service._request_json")
    def test_ozon_stock_refresh_reads_only_requested_offers(self, request_json) -> None:
        request_json.return_value = {
            "items": [{
                "offer_id": "17162",
                "updated_at": "2026-08-27T15:28:20Z",
                "stocks": {"has_stock": True, "stocks": [{"source": "fbo", "present": 5, "reserved": 0}]},
            }],
        }

        stocks = _fetch_ozon_stocks(client_id="client", token="token", offer_ids=["17162"])

        self.assertEqual(stocks["17162"]["available_stock"], 5)
        request_json.assert_called_once_with(
            "https://api-seller.ozon.ru/v3/product/info/list",
            method="POST",
            headers={"Client-Id": "client", "Api-Key": "token"},
            payload={"offer_id": ["17162"], "product_id": [], "sku": []},
        )

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
