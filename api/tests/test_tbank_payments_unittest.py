"""Контрактные проверки пополнения общего баланса через СБП Т-Банка."""

from __future__ import annotations

import base64
import hashlib
import unittest
from datetime import datetime, timezone
from inspect import getsource
from unittest.mock import patch

from fastapi import FastAPI

from domains.tbank_payments import (
    TBankClient,
    TBankError,
    TBankSettings,
    WorkspaceTopupCreateIn,
    _ssl_context,
    make_token,
    mount_tbank_payment_routes,
    notification_token_is_valid,
    provider_state,
    qr_data_url,
    should_apply_provider_state,
    topup_receipt,
)


class TBankPaymentsTests(unittest.TestCase):
    def test_tbank_ca_bundle_can_extend_the_container_trust_store(self) -> None:
        expected_context = object()
        with patch.dict("os.environ", {"TBANK_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt"}), patch(
            "domains.tbank_payments.ssl.create_default_context", return_value=expected_context
        ) as create_context:
            actual_context = _ssl_context()

        self.assertIs(actual_context, expected_context)
        create_context.assert_called_once_with(cafile="/etc/ssl/certs/ca-certificates.crt")

    def test_token_uses_sorted_root_scalars_and_ignores_nested_values(self) -> None:
        payload = {
            "TerminalKey": "DemoTerminal",
            "Amount": 100000,
            "OrderId": "seller_order_1",
            "DATA": {"ignored": "secret"},
        }
        expected_source = "100000" + "seller_order_1" + "password" + "DemoTerminal"

        self.assertEqual(make_token(payload, "password"), hashlib.sha256(expected_source.encode()).hexdigest())

    def test_notification_token_comparison_accepts_boolean_values(self) -> None:
        payload = {"TerminalKey": "Demo", "Success": True, "Status": "CONFIRMED"}
        payload["Token"] = make_token(payload, "password")

        self.assertTrue(notification_token_is_valid(payload, "password"))
        self.assertFalse(notification_token_is_valid({**payload, "Status": "REJECTED"}, "password"))

    def test_qr_svg_is_wrapped_as_image_data_url_and_rejects_script(self) -> None:
        svg = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10"/></svg>'
        result = qr_data_url(svg)

        self.assertTrue(result.startswith("data:image/svg+xml;base64,"))
        self.assertEqual(base64.b64decode(result.split(",", 1)[1]).decode(), svg)
        with self.assertRaises(RuntimeError):
            qr_data_url('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')

    def test_provider_statuses_only_credit_confirmed(self) -> None:
        self.assertEqual(provider_state("CONFIRMED"), "confirmed")
        self.assertEqual(provider_state("AUTHORIZED"), "pending")
        self.assertEqual(provider_state("REJECTED"), "rejected")
        self.assertEqual(provider_state("DEADLINE_EXPIRED"), "expired")

    def test_completed_topup_cannot_regress_to_pending(self) -> None:
        self.assertFalse(should_apply_provider_state("confirmed", "pending"))
        self.assertFalse(should_apply_provider_state("rejected", "pending"))
        self.assertTrue(should_apply_provider_state("pending", "confirmed"))
        self.assertTrue(should_apply_provider_state("rejected", "confirmed"))

    def test_get_qr_and_demo_use_official_sbp_methods(self) -> None:
        captured = []
        client = TBankClient(TBankSettings("https://example.test/v2", "DEMO", "secret", "n", "s", "f", 3))
        client.call = lambda method, payload: captured.append((method, payload)) or {"Success": True}

        client.get_qr("123")
        client.simulate_sbp("123", "deadline_expired")

        self.assertEqual(captured[0], ("GetQr", {"PaymentId": "123", "DataType": "IMAGE", "PaymentMethod": "SBP"}))
        self.assertEqual(captured[1], ("SbpPayTest", {"PaymentId": "123", "IsDeadlineExpired": True}))

    def test_topup_receipt_is_ffd_12_advance_payment(self) -> None:
        # Проверяет согласованные реквизиты аванса, которые отправятся в кассу.
        with patch.dict(
            "os.environ",
            {
                "TBANK_RECEIPT_EMAIL": "Receipt@Example.com",
                "TBANK_RECEIPT_TAXATION": "usn_income_outcome",
                "TBANK_RECEIPT_TAX": "vat105",
            },
        ):
            receipt = topup_receipt(amount=100_000)

        self.assertEqual(receipt["FfdVersion"], "1.2")
        self.assertEqual(receipt["Email"], "receipt@example.com")
        self.assertEqual(receipt["Taxation"], "usn_income_outcome")
        self.assertEqual(
            receipt["Items"],
            [
                {
                    "Name": "Аванс для оплаты услуг и цифровых товаров HomTech",
                    "Price": 100_000,
                    "Quantity": 1,
                    "Amount": 100_000,
                    "Tax": "vat105",
                    "PaymentMethod": "advance",
                    "PaymentObject": "payment",
                    "MeasurementUnit": "шт",
                }
            ],
        )

    def test_advance_receipt_preserves_gross_amount_in_bank_request(self) -> None:
        # Расчётная ставка не должна прибавлять НДС к платежу или терять копейки.
        captured = []
        client = TBankClient(TBankSettings("https://example.test/v2", "DEMO", "secret", "n", "s", "f", 3))
        client.call = lambda method, payload: captured.append((method, payload)) or {"Success": True}
        with patch.dict("os.environ", {
            "TBANK_RECEIPT_EMAIL": "receipt@example.com",
            "TBANK_RECEIPT_TAXATION": "usn_income_outcome",
            "TBANK_RECEIPT_TAX": "vat105",
        }):
            for amount in (1_000, 105_000, 100_001, 10_000_000):
                with self.subTest(amount=amount):
                    client.init(
                        order_id=f"seller_test_{amount}", amount=amount,
                        expires_at=datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
                        receipt=topup_receipt(amount=amount),
                    )
                    payload = captured[-1][1]
                    self.assertEqual(payload["Amount"], amount)
                    self.assertEqual(sum(item["Amount"] for item in payload["Receipt"]["Items"]), amount)
                    item = payload["Receipt"]["Items"][0]
                    self.assertEqual(item["Price"] * item["Quantity"], amount)
                    self.assertEqual(item["Tax"], "vat105")
                    self.assertEqual(item["PaymentMethod"], "advance")
                    self.assertEqual(item["PaymentObject"], "payment")

    def test_receipt_rejects_missing_or_invalid_tax_configuration(self) -> None:
        # Ошибка окружения должна остановить фискализацию, а не подставить другой налог.
        settings = {
            "TBANK_RECEIPT_EMAIL": "receipt@example.com",
            "TBANK_RECEIPT_TAXATION": "usn_income_outcome",
            "TBANK_RECEIPT_TAX": "vat105",
        }
        for key in ("TBANK_RECEIPT_TAXATION", "TBANK_RECEIPT_TAX"):
            for value in ("", "invalid"):
                with self.subTest(key=key, value=value), patch.dict("os.environ", {**settings, key: value}):
                    with self.assertRaisesRegex(TBankError, key):
                        topup_receipt(amount=1_000)

    def test_init_passes_receipt(self) -> None:
        captured = []
        client = TBankClient(TBankSettings("https://example.test/v2", "DEMO", "secret", "n", "s", "f", 3))
        client.call = lambda method, payload: captured.append((method, payload)) or {"Success": True}
        receipt = {"Email": "receipt@example.com", "Items": [{"Amount": 1_000}]}

        client.init(
            order_id="seller_order_1",
            amount=1_000,
            expires_at=datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc),
            receipt=receipt,
        )

        self.assertEqual(captured[0][0], "Init")
        self.assertIs(captured[0][1]["Receipt"], receipt)

    def test_routes_do_not_accept_workspace_from_client(self) -> None:
        app = FastAPI()
        mount_tbank_payment_routes(
            app,
            database_url=lambda: "",
            psycopg=None,
            current_user=lambda: None,
            user_with_workspace=lambda *_args: None,
        )
        paths = {route.path for route in app.routes}

        self.assertIn("/billing/balance", paths)
        self.assertIn("/billing/topups", paths)
        self.assertIn("/payments/tbank/notifications", paths)
        self.assertIn("/billing/topups/{topup_id}/demo", paths)
        fields = getattr(WorkspaceTopupCreateIn, "model_fields", WorkspaceTopupCreateIn.__fields__)
        self.assertNotIn("workspace_id", fields)

    def test_credit_ledger_has_unique_business_key_guard(self) -> None:
        source = getsource(mount_tbank_payment_routes)
        module_source = getsource(__import__("domains.tbank_payments", fromlist=["_"]))

        self.assertIn("ON CONFLICT (business_key) DO NOTHING", module_source)
        self.assertIn("'payment_id', %s::text", module_source)
        self.assertIn('if state == "confirmed"', module_source)
        self.assertIn("workspace_id=%s", source)


if __name__ == "__main__":
    unittest.main()
