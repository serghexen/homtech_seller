"""Проверки идемпотентной очереди остатков Ozon."""

from __future__ import annotations

import inspect
import json
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from domains.ozon_stock_outbound import (
    OzonStockOutboundProcessor,
    OzonStockPayload,
    ozon_stock_outbound_enabled,
    send_ozon_stock,
)


class OzonStockOutboundTests(unittest.TestCase):
    @patch.dict("os.environ", {"SELLER_OZON_STOCK_OUTBOUND_ENABLED": "false"})
    def test_global_switch_is_disabled_by_default(self) -> None:
        self.assertFalse(ozon_stock_outbound_enabled())

    @patch("domains.ozon_stock_outbound.urllib.request.urlopen")
    def test_sends_exact_saved_target(self, urlopen) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"status":[{"updated":true}]}'
        urlopen.return_value = response
        payload = OzonStockPayload(1, uuid4(), 3, "77", "OFFER-1", "client", "token", 6)

        send_ozon_stock(payload)

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode())
        self.assertEqual(request.full_url, "https://api-seller.ozon.ru/v1/product/digital/stocks/import")
        self.assertEqual(body, {"stocks": [{"offer_id": "OFFER-1", "stock": 6}]})

    def test_processor_requires_ozon_store_switch_and_confirmed_fulfillment(self) -> None:
        source = inspect.getsource(OzonStockOutboundProcessor._claim_and_prepare)
        self.assertIn("market.provider_code='ozon'", source)
        self.assertIn("market.stock_outbound_enabled=true", source)
        self.assertIn('{"submitted", "delivered"}', source)


if __name__ == "__main__":
    unittest.main()

