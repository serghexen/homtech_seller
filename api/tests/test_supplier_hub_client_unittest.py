"""Проверки безопасной read-only границы Seller → Supplier Hub."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from domains.supplier_hub_client import (
    SupplierHubClient,
    SupplierHubError,
    SupplierHubSettings,
    load_supplier_hub_settings,
    supplier_hub_status,
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.payload


def settings(**overrides) -> SupplierHubSettings:
    values = {
        "base_url": "http://127.0.0.1:18010",
        "client_id": "seller",
        "client_key": "s" * 48,
        "timeout_seconds": 10,
        "fulfillment_enabled": False,
    }
    values.update(overrides)
    return SupplierHubSettings(**values)


class SupplierHubClientTests(unittest.TestCase):
    @patch.dict("os.environ", {}, clear=True)
    def test_fulfillment_is_disabled_by_default(self) -> None:
        self.assertFalse(load_supplier_hub_settings().fulfillment_enabled)

    def test_plain_http_public_address_is_rejected(self) -> None:
        with self.assertRaises(SupplierHubError):
            SupplierHubClient(settings(base_url="http://89.110.88.203:18010"))

    @patch("domains.supplier_hub_client.urllib.request.urlopen")
    def test_authenticated_reads_use_dedicated_headers(self, urlopen) -> None:
        urlopen.return_value = FakeResponse({"items": [{"id": 11125}]})
        client = SupplierHubClient(settings())
        self.assertEqual(client.services(), [{"id": 11125}])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-hub-client"), "seller")
        self.assertEqual(request.get_header("X-hub-key"), "s" * 48)

    @patch("domains.supplier_hub_client.SupplierHubClient.ready")
    @patch("domains.supplier_hub_client.SupplierHubClient.live")
    @patch.dict(
        "os.environ",
        {
            "SUPPLIER_HUB_URL": "http://127.0.0.1:18010",
            "SUPPLIER_HUB_CLIENT_ID": "seller",
            "SUPPLIER_HUB_CLIENT_KEY": "s" * 48,
            "SELLER_SUPPLIER_HUB_FULFILLMENT_ENABLED": "false",
        },
        clear=True,
    )
    def test_status_does_not_enable_fulfillment(self, live, ready) -> None:
        live.return_value = {"status": "ok", "version": "0.2.0"}
        ready.return_value = {"status": "ready", "purchases_enabled": False}
        status = supplier_hub_status()
        self.assertTrue(status["reachable"])
        self.assertFalse(status["fulfillment_enabled"])
        self.assertFalse(status["hub_purchases_enabled"])


if __name__ == "__main__":
    unittest.main()
