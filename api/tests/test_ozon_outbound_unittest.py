"""Проверки безопасной границы отправки цифровых кодов Ozon."""

from __future__ import annotations

import inspect
import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch
from uuid import uuid4

from domains.ozon_outbound import (
    OzonOutboundError,
    OzonOutboundPayload,
    OzonOutboundProcessor,
    ozon_outbound_enabled,
    send_ozon_digital_codes,
)


def payload() -> OzonOutboundPayload:
    return OzonOutboundPayload(1, uuid4(), 7, "123-1", 9911, "client", "token", ("CODE-1", "CODE-2"))


class OzonOutboundTests(unittest.TestCase):
    @patch.dict("os.environ", {"SELLER_OZON_OUTBOUND_ENABLED": "false"})
    def test_global_switch_is_disabled_by_default(self) -> None:
        self.assertFalse(ozon_outbound_enabled())

    @patch("domains.ozon_outbound.urllib.request.urlopen")
    def test_sends_complete_exemplar_set(self, urlopen) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "exemplars_by_sku": [{"sku": 9911, "received_qty": 2, "rejected_qty": 0}],
        }).encode()
        urlopen.return_value = response

        send_ozon_digital_codes(payload())

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode())
        self.assertEqual(request.full_url, "https://api-seller.ozon.ru/v1/posting/digital/codes/upload")
        self.assertEqual(body["posting_number"], "123-1")
        self.assertEqual(body["exemplars_by_sku"][0]["exemplar_keys"], ["CODE-1", "CODE-2"])

    @patch("domains.ozon_outbound.urllib.request.urlopen")
    def test_timeout_is_unknown_without_blind_retry(self, urlopen) -> None:
        urlopen.side_effect = TimeoutError()
        with self.assertRaises(OzonOutboundError) as raised:
            send_ozon_digital_codes(payload())
        self.assertFalse(raised.exception.definite)

    @patch("domains.ozon_outbound.urllib.request.urlopen")
    def test_done_response_is_treated_as_already_accepted(self, urlopen) -> None:
        urlopen.side_effect = urllib.error.HTTPError(
            "url", 409, "done", {}, io.BytesIO(b'{"message":"posting is done"}'),
        )
        with self.assertRaises(OzonOutboundError) as raised:
            send_ozon_digital_codes(payload())
        self.assertTrue(raised.exception.accepted)

    def test_processor_records_sending_before_http_and_never_blindly_retries(self) -> None:
        source = inspect.getsource(OzonOutboundProcessor)
        self.assertIn("connection.commit()", source)
        self.assertIn("state='unknown'", source)
        self.assertIn("повтор запрещён", source)
        self.assertIn("enqueue_ozon_stock_publication", source)


if __name__ == "__main__":
    unittest.main()

