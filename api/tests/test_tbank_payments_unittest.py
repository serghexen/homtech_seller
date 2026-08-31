"""Контрактные проверки пополнения общего баланса через СБП Т-Банка."""

from __future__ import annotations

import base64
import hashlib
import unittest
from inspect import getsource
from unittest.mock import patch

from fastapi import FastAPI

from domains.tbank_payments import (
    TBankClient,
    TBankSettings,
    WorkspaceTopupCreateIn,
    _ssl_context,
    make_token,
    mount_tbank_payment_routes,
    notification_token_is_valid,
    provider_state,
    qr_data_url,
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

    def test_get_qr_and_demo_use_official_sbp_methods(self) -> None:
        captured = []
        client = TBankClient(TBankSettings("https://example.test/v2", "DEMO", "secret", "n", "s", "f", 3))
        client.call = lambda method, payload: captured.append((method, payload)) or {"Success": True}

        client.get_qr("123")
        client.simulate_sbp("123", "deadline_expired")

        self.assertEqual(captured[0], ("GetQr", {"PaymentId": "123", "DataType": "IMAGE", "PaymentMethod": "SBP"}))
        self.assertEqual(captured[1], ("SbpPayTest", {"PaymentId": "123", "IsDeadlineExpired": True}))

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
