"""Проверки безопасной границы отправки цифровых кодов Ozon."""

from __future__ import annotations

import inspect
from dataclasses import replace
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
    def test_done_response_requires_reconciliation(self, urlopen) -> None:
        urlopen.side_effect = urllib.error.HTTPError(
            "url", 409, "done", {}, io.BytesIO(b'{"message":"posting is done"}'),
        )
        with self.assertRaises(OzonOutboundError) as raised:
            send_ozon_digital_codes(payload())
        self.assertFalse(raised.exception.accepted)
        self.assertFalse(raised.exception.definite)

    @patch("domains.ozon_outbound.urllib.request.urlopen")
    def test_two_skus_are_uploaded_together(self, urlopen):
        second = replace(payload(), job_id=2, fulfillment_id=8, sku=9922, codes=("CODE-3",))
        group = replace(payload(), siblings=(second,))
        urlopen.return_value.__enter__.return_value.read.return_value = json.dumps({
            "exemplars_by_sku": [
                {"sku": 9922, "received_qty": 1, "rejected_qty": 0},
                {"sku": 9911, "received_qty": 2, "rejected_qty": 0},
            ]}).encode()
        send_ozon_digital_codes(group)
        entries = json.loads(urlopen.call_args.args[0].data)["exemplars_by_sku"]
        self.assertEqual([(e["sku"], e["exemplar_qty"]) for e in entries], [(9911, 2), (9922, 1)])
        self.assertEqual(urlopen.call_count, 1)

    @patch("domains.ozon_outbound.urllib.request.urlopen")
    def test_partial_or_malformed_acceptance_never_releases_keys(self, urlopen):
        for result in [
            {}, {"exemplars_by_sku": [{"sku": 9911, "received_qty": 1, "rejected_qty": 1}]},
            {"exemplars_by_sku": [{"sku": 9911, "received_qty": 2}]},
            {"exemplars_by_sku": [None]},
        ]:
            with self.subTest(result=result):
                urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(result).encode()
                with self.assertRaises(OzonOutboundError) as raised:
                    send_ozon_digital_codes(payload())
                self.assertFalse(raised.exception.definite)

    @patch("domains.ozon_outbound.urllib.request.urlopen")
    def test_400_diagnostics_redact_all_codes_and_credentials(self, urlopen):
        second = replace(payload(), codes=("CODE-3",))
        urlopen.side_effect = urllib.error.HTTPError("url", 400, "bad request", {}, io.BytesIO(
            json.dumps({"message": "invalid CODE-1 CODE-2 CODE-3 token", "details": "secret"}).encode()))
        with self.assertRaises(OzonOutboundError) as raised:
            send_ozon_digital_codes(replace(payload(), siblings=(second,)))
        self.assertTrue(raised.exception.definite)
        self.assertIn("invalid", str(raised.exception))
        for secret in ("CODE-1", "CODE-2", "CODE-3", "token", "secret"):
            self.assertNotIn(secret, str(raised.exception))

    def test_payload_repr_does_not_disclose_keys_or_credentials(self):
        self.assertNotIn("CODE-1", repr(payload()))
        self.assertNotIn("'token'", repr(payload()))

    def test_processor_records_sending_before_http_and_never_blindly_retries(self) -> None:
        source = inspect.getsource(OzonOutboundProcessor)
        self.assertIn("connection.commit()", source)
        self.assertIn("state='unknown'", source)
        self.assertIn("повтор запрещён", source)
        self.assertIn("enqueue_ozon_stock_publication", source)


if __name__ == "__main__":
    unittest.main()

