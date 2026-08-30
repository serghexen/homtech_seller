"""Контракт фонового курсора заказов без подключения к рабочей базе."""

from __future__ import annotations

import unittest
from pathlib import Path


class OrderActivityApiContractTests(unittest.TestCase):
    def test_activity_cursor_is_authenticated_and_workspace_scoped(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        source = (project_root / "api" / "domains" / "marketplace_read_api.py").read_text(encoding="utf-8")

        self.assertIn('@app.get("/marketplaces/orders/activity"', source)
        self.assertIn("user: AuthenticatedUser = Depends(current_user)", source)
        self.assertIn("seller_user = workspace_for_user(connection, user)", source)
        self.assertIn("WHERE event.workspace_id=%s AND event.id>%s", source)
        self.assertIn("connection.workspace_id=event.workspace_id", source)

    def test_yandex_activity_starts_with_fulfillment_and_advances_past_hidden_events(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        source = (project_root / "api" / "domains" / "marketplace_read_api.py").read_text(encoding="utf-8")

        self.assertIn("event.event_type='fulfillment_started'", source)
        self.assertIn("FROM seller.order_fulfillments AS visible_fulfillment", source)
        self.assertIn("activity_high_watermark", source)
        self.assertIn("if len(items) >= limit else activity_high_watermark", source)

    def test_first_activity_request_only_establishes_a_cursor(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        source = (project_root / "api" / "domains" / "marketplace_read_api.py").read_text(encoding="utf-8")

        self.assertIn("if after_id is None:", source)
        self.assertIn("SELECT COALESCE(MAX(id), 0)", source)
        self.assertIn("MarketplaceOrderActivityOut(items=[], next_cursor=", source)


if __name__ == "__main__":
    unittest.main()
