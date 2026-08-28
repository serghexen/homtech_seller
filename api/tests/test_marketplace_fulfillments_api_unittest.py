"""Контракт локальной ручной подготовки выдачи."""

from __future__ import annotations

import inspect
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException

from domains.marketplace_fulfillments_api import (
    FulfillmentIdentityIn,
    FulfillmentUnknownResolutionIn,
    automation_controls_fulfillment,
    manual_preparation_stage_ready,
    mount_marketplace_fulfillment_routes,
)


class MarketplaceFulfillmentsApiTests(unittest.TestCase):
    def test_automatic_chain_owns_pending_and_active_supplier_states(self) -> None:
        self.assertTrue(automation_controls_fulfillment(
            fulfillment_status="pending", handling_mode="automatic", outbound_state="",
            resolver_enabled=True, resolver_active=False, supplier_attempt_active=False,
        ))
        self.assertTrue(automation_controls_fulfillment(
            fulfillment_status="supplier_required", handling_mode="manual", outbound_state="",
            resolver_enabled=False, resolver_active=False, supplier_attempt_active=True,
        ))
        self.assertFalse(manual_preparation_stage_ready(
            fulfillment_status="supplier_required", handling_mode="automatic",
            resolver_enabled=True, automation_in_progress=True,
        ))

    def test_only_explicit_handoff_or_disabled_resolver_allows_manual_preparation(self) -> None:
        self.assertTrue(manual_preparation_stage_ready(
            fulfillment_status="manual_required", handling_mode="manual",
            resolver_enabled=True, automation_in_progress=False,
        ))
        self.assertTrue(manual_preparation_stage_ready(
            fulfillment_status="pending", handling_mode="unassigned",
            resolver_enabled=False, automation_in_progress=False,
        ))
        self.assertFalse(manual_preparation_stage_ready(
            fulfillment_status="pending", handling_mode="unassigned",
            resolver_enabled=True, automation_in_progress=True,
        ))

    def test_failed_automatic_outbound_is_no_longer_presented_as_running(self) -> None:
        self.assertTrue(automation_controls_fulfillment(
            fulfillment_status="reserved", handling_mode="automatic", outbound_state="queued",
            resolver_enabled=True, resolver_active=False, supplier_attempt_active=False,
        ))
        self.assertFalse(automation_controls_fulfillment(
            fulfillment_status="reserved", handling_mode="automatic", outbound_state="failed",
            resolver_enabled=True, resolver_active=False, supplier_attempt_active=False,
        ))

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

    def test_read_exposes_reveal_capability_for_reserved_and_consumed_keys(self) -> None:
        source = inspect.getsource(mount_marketplace_fulfillment_routes)
        self.assertIn("reservation.state IN ('reserved','consumed')", source)
        self.assertIn("can_reveal_keys=bool", source)

    def test_read_exposes_support_message_only_as_explicit_reveal_capability(self) -> None:
        source = inspect.getsource(mount_marketplace_fulfillment_routes)
        self.assertIn("can_reveal_support_message=bool", source)
        self.assertIn('str(row[13] or "") == "support_message"', source)
        self.assertIn("support_message_snapshot", source)

    def test_manual_preparation_requires_digital_delivery(self) -> None:
        source = inspect.getsource(mount_marketplace_fulfillment_routes)
        self.assertIn("item.delivery_type", source)
        self.assertIn("marketplace_order_allows_fulfillment", source)
        self.assertIn("Маркетплейс ещё не разрешил выдачу этого цифрового заказа", source)

    def test_manual_preparation_waits_for_explicit_automatic_handoff(self) -> None:
        route_source = inspect.getsource(mount_marketplace_fulfillment_routes)
        state_source = inspect.getsource(manual_preparation_stage_ready)

        self.assertIn('fulfillment_status == "manual_required"', state_source)
        self.assertIn('fulfillment_status in {"not_prepared", "pending", "supplier_required"}', state_source)
        self.assertIn("automation_in_progress=automation_in_progress", route_source)
        self.assertIn("FOR UPDATE", route_source)
        self.assertIn("Автовыдача уже обрабатывает этот заказ", route_source)

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
