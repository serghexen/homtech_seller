"""Контракт ручных ответов на отзывы и защита внешней отправки."""

from __future__ import annotations

import inspect
import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch
from uuid import uuid4

from domains.marketplace_dashboard_service import save_pending_reviews
from domains.marketplace_reviews_api import mount_marketplace_review_routes
from domains.yandex_review_replies import (
    ReviewReplyPayload,
    YandexReviewReplyError,
    YandexReviewReplyProcessor,
    send_yandex_review_reply,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class YandexReviewReplyTests(unittest.TestCase):
    def payload(self) -> ReviewReplyPayload:
        return ReviewReplyPayload(
            job_id=7,
            lock_token=uuid4(),
            review_id=9,
            business_id=77,
            feedback_id=123,
            response_text="Спасибо за отзыв!",
            token="secret",
        )

    @patch("domains.yandex_review_replies.urllib.request.urlopen")
    def test_sender_posts_one_manual_comment(self, urlopen) -> None:
        urlopen.return_value = _Response({"status": "OK", "result": {"id": 55, "status": "UNMODERATED"}})

        result = send_yandex_review_reply(self.payload())

        request = urlopen.call_args.args[0]
        self.assertIn("/v2/businesses/77/goods-feedback/comments/update", request.full_url)
        self.assertEqual(request.method, "POST")
        self.assertEqual(json.loads(request.data), {
            "feedbackId": 123,
            "comment": {"text": "Спасибо за отзыв!"},
        })
        self.assertEqual(result["id"], 55)

    @patch("domains.yandex_review_replies.urllib.request.urlopen")
    def test_http_400_is_definite_but_network_failure_is_unknown(self, urlopen) -> None:
        urlopen.side_effect = urllib.error.HTTPError(
            "https://example", 400, "bad", {}, io.BytesIO(b"invalid text"),
        )
        with self.assertRaises(YandexReviewReplyError) as definite:
            send_yandex_review_reply(self.payload())
        self.assertTrue(definite.exception.definite)

        urlopen.side_effect = urllib.error.URLError("timeout")
        with self.assertRaises(YandexReviewReplyError) as unknown:
            send_yandex_review_reply(self.payload())
        self.assertFalse(unknown.exception.definite)

    def test_api_is_workspace_scoped_and_all_members_may_enqueue(self) -> None:
        source = inspect.getsource(mount_marketplace_review_routes)
        self.assertIn("review.workspace_id=%s", source)
        self.assertIn("market.workspace_id=review.workspace_id", source)
        self.assertIn("seller_user.id", source)
        self.assertNotIn("role_code", source)

    def test_processor_never_requeues_sending_jobs(self) -> None:
        source = inspect.getsource(YandexReviewReplyProcessor)
        self.assertIn("state='unknown'", source)
        self.assertIn("job.state='sending' AND job.locked_until < now()", source)
        sending_recovery = source.split("job.state='sending'", 1)[0][-400:]
        self.assertNotIn("SET state='queued'", sending_recovery)

    def test_pending_review_snapshot_keeps_full_review_content(self) -> None:
        cursor = MagicMock()
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        review = {
            "feedbackId": 123,
            "createdAt": "2026-08-30T10:00:00Z",
            "author": "Покупатель",
            "identifiers": {"orderId": 42, "offerId": "SKU-1"},
            "description": {"advantages": "Быстро", "disadvantages": "", "comment": "Спасибо"},
            "statistics": {"rating": 5, "commentsCount": 0, "recommended": True, "paidAmount": 0},
            "media": {"photos": ["https://example/photo.jpg"]},
        }

        count = save_pending_reviews(
            connection,
            workspace_id=2,
            connection_id=7,
            business_id="77",
            reviews=[review],
        )

        self.assertEqual(count, 1)
        insert = next(call for call in cursor.execute.call_args_list if "INSERT INTO seller.marketplace_reviews" in call.args[0])
        self.assertIn(
            "ON CONFLICT (workspace_id, connection_id, provider_code, external_review_id)",
            insert.args[0],
        )
        self.assertIn("SKU-1", insert.args[1])


if __name__ == "__main__":
    unittest.main()
