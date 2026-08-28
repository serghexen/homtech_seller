"""Проверки границы неопределённости внешней выдачи без рабочей БД и сети."""

from __future__ import annotations

import inspect
import unittest
import urllib.error
from unittest.mock import patch
from uuid import uuid4

from domains.yandex_market_outbound import (
    OutboundPayload,
    YandexOutboundError,
    YandexOutboundProcessor,
    send_yandex_digital_goods,
    yandex_outbound_enabled,
)
from domains.buyer_text import normalize_buyer_text


def payload() -> OutboundPayload:
    return OutboundPayload(1, uuid4(), 7, 10, 20, 30, "secret-token", ("CODE-1",), "Инструкция")


class YandexOutboundTests(unittest.TestCase):
    @patch.dict("os.environ", {"SELLER_YANDEX_OUTBOUND_ENABLED": "false"})
    def test_global_switch_is_disabled_by_default(self) -> None:
        self.assertFalse(yandex_outbound_enabled())

    @patch("domains.yandex_market_outbound.urllib.request.urlopen")
    def test_http_4xx_is_definite_rejection(self, urlopen) -> None:
        urlopen.side_effect = urllib.error.HTTPError("url", 400, "bad", {}, None)
        with self.assertRaises(YandexOutboundError) as raised:
            send_yandex_digital_goods(payload())
        self.assertTrue(raised.exception.definite)
        self.assertNotIn("CODE-1", str(raised.exception))

    @patch("domains.yandex_market_outbound.urllib.request.urlopen")
    def test_timeout_is_unknown_and_must_not_be_retried_blindly(self, urlopen) -> None:
        urlopen.side_effect = TimeoutError()
        with self.assertRaises(YandexOutboundError) as raised:
            send_yandex_digital_goods(payload())
        self.assertFalse(raised.exception.definite)

    def test_processor_has_no_retry_path_after_sending(self) -> None:
        source = inspect.getsource(YandexOutboundProcessor)
        self.assertIn("state='unknown'", source)
        self.assertIn("повтор запрещён", source)
        self.assertNotIn("retry_delay", source)

    def test_finish_casts_empty_event_message_for_postgres(self) -> None:
        source = inspect.getsource(YandexOutboundProcessor._finish)
        self.assertIn("jsonb_build_object('message', (%s)::text)", source)

    def test_success_enqueues_target_stock_republish(self) -> None:
        source = inspect.getsource(YandexOutboundProcessor._finish)
        self.assertIn("enqueue_yandex_stock_publication", source)

    def test_worker_rechecks_exact_provider_status_and_digital_subtype(self) -> None:
        source = inspect.getsource(YandexOutboundProcessor._claim_and_prepare)
        self.assertIn("item.provider_status", source)
        self.assertIn("digitalGoods,type", source)
        self.assertIn("marketplace_order_allows_fulfillment", source)

    def test_legacy_instruction_uses_real_line_breaks(self) -> None:
        self.assertEqual(
            normalize_buyer_text("Шаг 1\\nШаг 2\\r\\nШаг 3"),
            "Шаг 1\nШаг 2\nШаг 3",
        )


if __name__ == "__main__":
    unittest.main()
