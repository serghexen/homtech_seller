"""Контрактные проверки безопасного polling-контура Ozon."""

from __future__ import annotations

import inspect
import unittest

from domains import marketplace_sync_service
from domains import yandex_market_outbound
import worker


class OzonPollingTests(unittest.TestCase):
    def test_scheduler_requires_database_switch_and_due_time(self) -> None:
        source = inspect.getsource(worker.enqueue_due_ozon_order_jobs)
        self.assertIn("provider_code='ozon'", source)
        self.assertIn("orders_polling_enabled=true", source)
        self.assertIn("next_orders_poll_at <= now()", source)
        self.assertIn("ON CONFLICT DO NOTHING", source)

    def test_ozon_orders_use_separate_watermark(self) -> None:
        source = inspect.getsource(marketplace_sync_service.sync_orders_connection)
        self.assertIn('last_orders_poll_at if str(provider_code) == "ozon"', source)
        self.assertIn('str(provider_code) in {"yandex_market", "ozon"}', source)

    def test_yandex_recovery_cannot_touch_ozon_jobs(self) -> None:
        source = inspect.getsource(yandex_market_outbound.YandexOutboundProcessor.recover_stale)
        self.assertGreaterEqual(source.count("market.provider_code='yandex_market'"), 2)


if __name__ == "__main__":
    unittest.main()
