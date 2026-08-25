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


if __name__ == "__main__":
    unittest.main()
