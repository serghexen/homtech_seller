"""Проверки безопасной очереди заданного остатка без рабочей БД и сети."""

from __future__ import annotations

import inspect
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch
from uuid import uuid4

from domains.yandex_market_stock_outbound import (
    StockOutboundPayload,
    YandexStockOutboundError,
    YandexStockOutboundProcessor,
    calculate_effective_stock,
    send_yandex_stock,
    yandex_stock_outbound_enabled,
)
from domains.yandex_market_stock_queue import stock_republish_delay_seconds


def payload() -> StockOutboundPayload:
    return StockOutboundPayload(1, uuid4(), 7, "MRKT-1", 10, "secret-token", 5)


class YandexStockOutboundTests(unittest.TestCase):
    @patch.dict("os.environ", {"SELLER_YANDEX_STOCK_OUTBOUND_ENABLED": "false"})
    def test_global_switch_is_disabled_by_default(self) -> None:
        self.assertFalse(yandex_stock_outbound_enabled())

    def test_target_stock_without_daily_limit_matches_configured_value(self) -> None:
        self.assertEqual(calculate_effective_stock(5, None, 0, 0, 0, 1, 0), 5)

    def test_target_stock_is_limited_by_remaining_daily_quota(self) -> None:
        self.assertEqual(calculate_effective_stock(5, 4, 1, 1, 0, 2, 1), 1)

    @patch("domains.yandex_market_stock_outbound.urllib.request.urlopen")
    def test_sends_exact_sku_and_target_stock(self, urlopen) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        urlopen.return_value = response

        send_yandex_stock(payload())

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.method, "PUT")
        self.assertEqual(body["skus"][0]["sku"], "MRKT-1")
        self.assertEqual(body["skus"][0]["items"][0]["count"], 5)
        self.assertNotIn("secret-token", json.dumps(body))

    @patch("domains.yandex_market_stock_outbound.urllib.request.urlopen")
    def test_rate_limit_is_retryable(self, urlopen) -> None:
        urlopen.side_effect = urllib.error.HTTPError("url", 429, "rate", {}, None)
        with self.assertRaises(YandexStockOutboundError) as raised:
            send_yandex_stock(payload())
        self.assertFalse(raised.exception.definite)

    def test_processor_requires_store_switch_and_confirmed_submission(self) -> None:
        source = inspect.getsource(YandexStockOutboundProcessor._claim_and_prepare)
        self.assertIn("market.stock_outbound_enabled=true", source)
        self.assertIn("{\"submitted\", \"delivered\"}", source)
        self.assertIn("FOR UPDATE OF job SKIP LOCKED", source)

    def test_processor_accepts_manual_jobs_without_fake_fulfillment(self) -> None:
        source = inspect.getsource(YandexStockOutboundProcessor._claim_and_prepare)
        self.assertIn("LEFT JOIN seller.order_fulfillments", source)
        self.assertIn("COALESCE(job.connection_id, fulfillment.connection_id)", source)
        self.assertIn('job_kind == "manual"', source)
        self.assertIn("job.requested_stock", source)

    def test_processor_recounts_free_pool_keys_before_send(self) -> None:
        source = inspect.getsource(YandexStockOutboundProcessor._claim_and_prepare)
        self.assertIn("key.key_origin='pool'", source)
        self.assertIn("key.status='free'", source)
        self.assertIn("stock_target_base", source)

    @patch.dict("os.environ", {"YANDEX_MARKET_STOCK_REPUBLISH_DELAY_SECONDS": "3"})
    def test_republish_delay_matches_crm_safety_window(self) -> None:
        self.assertEqual(stock_republish_delay_seconds(), 3)

    def test_stale_put_can_be_requeued_safely(self) -> None:
        source = inspect.getsource(YandexStockOutboundProcessor.recover_stale)
        self.assertIn("state IN ('preparing','sending')", source)
        self.assertIn("state='queued'", source)


if __name__ == "__main__":
    unittest.main()
