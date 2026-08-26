"""Контракт локальной ручной подготовки выдачи."""

from __future__ import annotations

import inspect
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException

from domains.marketplace_fulfillments_api import (
    FulfillmentIdentityIn,
    FulfillmentUnknownResolutionIn,
    mount_marketplace_fulfillment_routes,
)


class MarketplaceFulfillmentsApiTests(unittest.TestCase):
    def test_mounts_read_local_actions_and_durable_outbound_actions(self) -> None:
        app = FastAPI()
        mount_marketplace_fulfillment_routes(
            app,
            database_url=lambda: "",
            psycopg=None,
            current_user=lambda: None,
            user_with_workspace=lambda *_args: None,
        )

        methods = {
            route.path: route.methods
            for route in app.routes
            if route.path.startswith("/marketplaces/orders/fulfillment")
        }

        self.assertEqual(methods["/marketplaces/orders/fulfillment"], {"GET"})
        self.assertEqual(methods["/marketplaces/orders/fulfillment/prepare"], {"POST"})
        self.assertEqual(methods["/marketplaces/orders/fulfillment/prepare-manual"], {"POST"})
        self.assertEqual(methods["/marketplaces/orders/fulfillment/prepare-support"], {"POST"})
        self.assertEqual(methods["/marketplaces/orders/fulfillment/release"], {"POST"})
        self.assertEqual(methods["/marketplaces/orders/fulfillment/send"], {"POST"})
        self.assertEqual(methods["/marketplaces/orders/fulfillment/cancel-send"], {"POST"})
        self.assertEqual(methods["/marketplaces/orders/fulfillment/resolve-unknown"], {"POST"})
        self.assertEqual(len(methods), 8)

    def test_unknown_resolution_contract_is_explicit(self) -> None:
        accepted = FulfillmentUnknownResolutionIn(
            connection_id=7, external_order_id="123", external_item_id="9", resolution="accepted",
        )
        rejected = FulfillmentUnknownResolutionIn(
            connection_id=7, external_order_id="123", external_item_id="9", resolution="not_accepted",
        )

        self.assertEqual(accepted.resolution, "accepted")
        self.assertEqual(rejected.resolution, "not_accepted")
        with self.assertRaises(ValueError):
            FulfillmentUnknownResolutionIn(
                connection_id=7, external_order_id="123", external_item_id="9", resolution="retry",
            )

    def test_unknown_resolution_is_audited_and_does_not_call_yandex(self) -> None:
        source = inspect.getsource(mount_marketplace_fulfillment_routes)
        self.assertIn("outbound_unknown_resolved_accepted", source)
        self.assertIn("outbound_unknown_resolved_not_accepted", source)
        self.assertIn("Повтор можно разрешить только пока заказ остаётся в обработке", source)
        self.assertNotIn("deliver_yandex", source)

    def test_api_never_decrypts_or_reads_codes(self) -> None:
        source = inspect.getsource(mount_marketplace_fulfillment_routes)
        self.assertNotIn("pgp_sym_decrypt", source)
        self.assertNotIn("urllib", source)
        self.assertNotIn("delivered_codes", source)
        self.assertIn("fulfillment_outbound_jobs", source)

    def test_manual_preparation_requires_digital_delivery(self) -> None:
        source = inspect.getsource(mount_marketplace_fulfillment_routes)
        self.assertIn("item.delivery_type", source)
        self.assertIn("upper() != \"DIGITAL\"", source)
        self.assertIn("только для цифрового DBS-заказа", source)

    @patch.dict("os.environ", {"SELLER_MANUAL_FULFILLMENT_ENABLED": "false"})
    def test_kill_switch_rejects_direct_prepare_and_release_without_database_access(self) -> None:
        app = FastAPI()
        fake_psycopg = Mock()
        mount_marketplace_fulfillment_routes(
            app,
            database_url=lambda: "postgresql://test",
            psycopg=fake_psycopg,
            current_user=lambda: None,
            user_with_workspace=lambda *_args: None,
        )
        payload = FulfillmentIdentityIn(connection_id=7, external_order_id="123", external_item_id="9")

        for path in (
            "/marketplaces/orders/fulfillment/prepare",
            "/marketplaces/orders/fulfillment/prepare-manual",
            "/marketplaces/orders/fulfillment/prepare-support",
            "/marketplaces/orders/fulfillment/release",
            "/marketplaces/orders/fulfillment/send",
            "/marketplaces/orders/fulfillment/cancel-send",
            "/marketplaces/orders/fulfillment/resolve-unknown",
        ):
            endpoint = next(route.endpoint for route in app.routes if route.path == path)
            with self.assertRaises(HTTPException) as raised:
                endpoint(payload=payload, user=Mock())
            self.assertEqual(raised.exception.status_code, 503)

        fake_psycopg.connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
