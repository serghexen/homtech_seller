"""Проверки формулы продаж, атрибуции и read-only контракта главной."""

from __future__ import annotations

import inspect
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from domains.marketplace_dashboard_api import (
    mount_marketplace_dashboard_routes,
    sales_period_starts,
    subscription_days,
)
from domains.marketplace_dashboard_service import (
    fetch_ozon_pending_reviews,
    fetch_ozon_unread_buyer_messages,
    fetch_yandex_pending_chats,
    fetch_yandex_pending_reviews,
    save_ozon_pending_reviews,
    sync_dashboard_connection,
)
from domains.marketplace_read_api import normalize_marketplace_order_summary
from worker import is_transient_sync_error, stable_dashboard_jitter_seconds
from fastapi import HTTPException


class MarketplaceDashboardTests(unittest.TestCase):
    def test_yandex_sales_include_subsidy_but_not_delivery(self) -> None:
        summary = normalize_marketplace_order_summary(
            "yandex_market",
            {
                "orderId": 101,
                "status": "DELIVERED",
                "prices": {
                    "payment": {"value": "900.50", "currencyId": "RUR"},
                    "cashback": {"value": "40", "currencyId": "RUR"},
                    "subsidy": {"value": "59.50", "currencyId": "RUR"},
                    "delivery": {"value": "300", "currencyId": "RUR"},
                },
            },
        )

        self.assertIsNotNone(summary)
        self.assertEqual(summary["sales_amount"], Decimal("1000.00"))
        self.assertEqual(summary["currency_code"], "RUR")

    def test_ozon_amount_uses_product_prices_without_financial_data(self) -> None:
        summary = normalize_marketplace_order_summary(
            "ozon",
            {
                "posting_number": "1",
                "status": "delivered",
                "created_at": "2026-08-29T12:00:00Z",
                "in_process_at": "2026-08-30T12:00:00Z",
                "products": [
                    {"price": "900.50", "currency": "RUB", "quantity": 2},
                    {"price": {"amount": "99.00", "currency": "RUB"}, "quantity": 1},
                ],
            },
        )
        self.assertEqual(summary["sales_amount"], Decimal("1900.00"))
        self.assertEqual(summary["currency_code"], "RUB")
        self.assertEqual(summary["created_at"], datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc))

    @patch("domains.marketplace_dashboard_service._request_ozon_json")
    def test_ozon_review_reader_uses_unprocessed_status_and_cursor(self, request_json) -> None:
        request_json.side_effect = [
            {"reviews": [{"id": "r-1"}], "has_next": True, "last_id": "next"},
            {"reviews": [{"id": "r-2"}], "has_next": False, "last_id": "done"},
        ]

        rows = fetch_ozon_pending_reviews(client_id="client", token="secret")

        self.assertEqual([row["id"] for row in rows], ["r-1", "r-2"])
        self.assertEqual(request_json.call_args_list[0].kwargs["payload"]["filters"], {"status": "UNPROCESSED"})
        self.assertEqual(request_json.call_args_list[1].kwargs["payload"]["last_id"], "next")

    @patch("domains.marketplace_dashboard_service._request_ozon_json")
    def test_ozon_unread_count_excludes_support_and_system_chats(self, request_json) -> None:
        request_json.return_value = {
            "chats": [
                {"chat": {"chat_type": "Buyer_Seller"}, "unread_count": 3},
                {"chat": {"chat_type": "Buyer_Seller_Select"}, "unread_count": 2},
                {"chat": {"chat_type": "SELLER_SUPPORT"}, "unread_count": 50},
            ],
            "total_unread_count": 55,
            "has_next": False,
        }

        unread = fetch_ozon_unread_buyer_messages(client_id="client", token="secret")

        self.assertEqual(unread, 5)
        self.assertEqual(
            request_json.call_args.kwargs["payload"]["filter"],
            {"chat_status": "OPENED", "unread_only": True},
        )

    def test_ozon_review_snapshot_keeps_uuid_and_blocks_empty_review_reply(self) -> None:
        cursor = MagicMock()
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor

        count = save_ozon_pending_reviews(
            connection,
            workspace_id=2,
            connection_id=7,
            reviews=[{
                "id": "017c0d1c-66d3-b838-3d29-cf9b95a6ac48",
                "sku": 148591503,
                "text": "",
                "published_at": "2026-08-30T10:00:00Z",
                "rating": 5,
                "comments_amount": 0,
                "photos_amount": 0,
                "videos_amount": 0,
            }],
        )

        self.assertEqual(count, 1)
        insert = next(call for call in cursor.execute.call_args_list if "INSERT INTO seller.marketplace_reviews" in call.args[0])
        self.assertIn("provider_code, external_review_id", insert.args[0])
        self.assertIn("017c0d1c-66d3-b838-3d29-cf9b95a6ac48", insert.args[1])
        self.assertIn(False, insert.args[1])

    @patch("domains.marketplace_dashboard_service._request_json")
    def test_review_reader_uses_need_reaction_and_page_token(self, request_json) -> None:
        request_json.side_effect = [
            {"result": {"feedbacks": [{"id": 1}], "paging": {"nextPageToken": "next"}}},
            {"result": {"feedbacks": [{"id": 2}], "paging": {}}},
        ]

        rows = fetch_yandex_pending_reviews(business_id=77, token="secret")

        self.assertEqual([row["id"] for row in rows], [1, 2])
        self.assertEqual(request_json.call_args_list[0].kwargs["payload"], {"reactionStatus": "NEED_REACTION"})
        self.assertIn("goods-feedback?limit=50", request_json.call_args_list[0].args[0])
        self.assertIn("pageToken=next", request_json.call_args_list[1].args[0])

    @patch("domains.marketplace_dashboard_service._request_json")
    def test_review_reader_does_not_truncate_more_than_one_hundred_pages(self, request_json) -> None:
        request_json.side_effect = [
            {
                "result": {
                    "feedbacks": [{"feedbackId": page}],
                    "paging": {"nextPageToken": f"page-{page + 1}"} if page < 100 else {},
                }
            }
            for page in range(101)
        ]

        rows = fetch_yandex_pending_reviews(business_id=77, token="secret")

        self.assertEqual(len(rows), 101)
        self.assertEqual(request_json.call_count, 101)

    @patch("domains.marketplace_dashboard_service._request_json")
    def test_review_reader_rejects_repeated_page_token(self, request_json) -> None:
        request_json.side_effect = [
            {"result": {"feedbacks": [], "paging": {"nextPageToken": "same"}}},
            {"result": {"feedbacks": [], "paging": {"nextPageToken": "same"}}},
        ]

        with self.assertRaisesRegex(HTTPException, "не продвинул"):
            fetch_yandex_pending_reviews(business_id=77, token="secret")

    @patch("domains.marketplace_dashboard_service._request_json")
    def test_chat_reader_counts_only_dialogs_waiting_for_partner(self, request_json) -> None:
        request_json.return_value = {"result": {"chats": [{"id": 1}], "paging": {}}}

        rows = fetch_yandex_pending_chats(business_id=77, token="secret")

        self.assertEqual(len(rows), 1)
        self.assertEqual(
            request_json.call_args.kwargs["payload"],
            {"types": ["CHAT"], "statuses": ["NEW", "WAITING_FOR_PARTNER"]},
        )

    @patch("domains.marketplace_dashboard_service.fetch_yandex_pending_chats")
    @patch("domains.marketplace_dashboard_service.fetch_yandex_pending_reviews")
    def test_multiple_campaigns_are_attributed_by_order_and_campaign(
        self, fetch_reviews, fetch_chats,
    ) -> None:
        fetch_reviews.return_value = [
            {"identifiers": {"orderId": "mine"}},
            {"identifiers": {"orderId": "another"}},
            {"identifiers": {}},
        ]
        fetch_chats.return_value = [
            {"context": {"campaignId": 149}},
            {"context": {"campaignId": 150}},
        ]
        cursor = MagicMock()
        cursor.fetchone.side_effect = [(9,), (2,)]
        cursor.fetchall.return_value = [("mine",)]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor

        count = sync_dashboard_connection(
            connection,
            (7, "yandex_market", "Store", "", "216", "149", "token", None),
        )

        self.assertEqual(count, 2)
        snapshot_call = next(
            call for call in cursor.execute.call_args_list
            if "INSERT INTO seller.marketplace_dashboard_snapshots" in call.args[0]
        )
        self.assertEqual(snapshot_call.args[1], (7, 9, 1, 1, 2))

    def test_dashboard_api_is_workspace_scoped_and_does_not_call_yandex(self) -> None:
        source = inspect.getsource(mount_marketplace_dashboard_routes)
        self.assertIn("WHERE marketplace.workspace_id=%s", source)
        self.assertIn("snapshot.workspace_id=marketplace.workspace_id", source)
        self.assertIn("order_row.created_at >= %s", source)
        self.assertNotIn("fetch_yandex", source)

    def test_sales_periods_and_subscription_use_moscow_calendar_days(self) -> None:
        now = datetime(2026, 8, 30, 22, 30, tzinfo=timezone.utc)  # Уже 31 августа в Москве.
        day_start, month_start = sales_period_starts(now)
        self.assertEqual(day_start, datetime(2026, 8, 30, 21, 0, tzinfo=timezone.utc))
        self.assertEqual(month_start, datetime(2026, 7, 31, 21, 0, tzinfo=timezone.utc))
        self.assertEqual(subscription_days(datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc), now=now), 2)

    def test_dashboard_jitter_is_stable_and_yandex_420_is_retryable(self) -> None:
        self.assertEqual(stable_dashboard_jitter_seconds(17), stable_dashboard_jitter_seconds(17))
        self.assertTrue(
            is_transient_sync_error(HTTPException(status_code=502, detail="Яндекс Маркет: HTTP 420"))
        )


if __name__ == "__main__":
    unittest.main()
