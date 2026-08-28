"""Контрактные проверки self-service запуска магазина."""

from __future__ import annotations

import inspect
import unittest

from fastapi import FastAPI

from domains.marketplace_launch_api import MarketplaceLaunchIn, mount_marketplace_launch_routes


class MarketplaceLaunchApiTests(unittest.TestCase):
    def test_mounts_readiness_and_launch_routes(self) -> None:
        app = FastAPI()
        mount_marketplace_launch_routes(
            app, database_url=lambda: "", psycopg=None,
            current_user=lambda: None, user_with_workspace=lambda *_args: None,
        )
        paths = {route.path for route in app.routes}
        self.assertIn("/marketplaces/connections/{connection_id}/launch-readiness", paths)
        self.assertIn("/marketplaces/connections/{connection_id}/launch", paths)

    def test_launch_requires_explicit_exclusive_control_confirmation(self) -> None:
        payload = MarketplaceLaunchIn()
        self.assertFalse(payload.confirm_exclusive_control)
        self.assertFalse(payload.automatic_stock_enabled)

    def test_launch_is_workspace_scoped_atomic_and_plan_aware(self) -> None:
        source = inspect.getsource(mount_marketplace_launch_routes)
        self.assertIn("pg_advisory_xact_lock", source)
        self.assertIn("workspace_id=%s", source)
        self.assertIn("FULFILLMENT_SUPPLIER", source)
        self.assertIn("fulfillment_reservation_enabled=true", source)
        self.assertIn("fulfillment_outbound_enabled=true", source)
        self.assertIn("orders_polling_enabled=true", source)
        self.assertIn("marketplace_connection_launch_events", source)


if __name__ == "__main__":
    unittest.main()
