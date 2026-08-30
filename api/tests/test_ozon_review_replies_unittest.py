"""Контракт ручных ответов Ozon и защита от повторной внешней отправки."""

from __future__ import annotations

import inspect
import io
import json
import unittest
import urllib.error
from unittest.mock import patch
from uuid import uuid4

from domains.ozon_review_replies import (
    OzonReviewReplyError,
    OzonReviewReplyPayload,
    OzonReviewReplyProcessor,
    send_ozon_review_reply,
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


class OzonReviewReplyTests(unittest.TestCase):
    def payload(self) -> OzonReviewReplyPayload:
        return OzonReviewReplyPayload(
            job_id=7,
            lock_token=uuid4(),
            review_id=9,
            external_review_id="017c0d1c-66d3-b838-3d29-cf9b95a6ac48",
            response_text="Спасибо за отзыв!",
            client_id="client",
            token="secret",
        )

    @patch("domains.ozon_review_replies.urllib.request.urlopen")
    def test_sender_posts_one_manual_comment_and_marks_review_processed(self, urlopen) -> None:
        urlopen.return_value = _Response({"comment_id": "comment-55"})

        result = send_ozon_review_reply(self.payload())

        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/v1/review/comment/create"))
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["Client-id"], "client")
        self.assertEqual(json.loads(request.data), {
            "review_id": "017c0d1c-66d3-b838-3d29-cf9b95a6ac48",
            "text": "Спасибо за отзыв!",
            "mark_review_as_processed": True,
        })
        self.assertEqual(result["comment_id"], "comment-55")

    @patch("domains.ozon_review_replies.urllib.request.urlopen")
    def test_http_400_is_definite_but_network_failure_is_unknown(self, urlopen) -> None:
        urlopen.side_effect = urllib.error.HTTPError(
            "https://example", 400, "bad", {}, io.BytesIO(b"invalid text"),
        )
        with self.assertRaises(OzonReviewReplyError) as definite:
            send_ozon_review_reply(self.payload())
        self.assertTrue(definite.exception.definite)

        urlopen.side_effect = urllib.error.URLError("timeout")
        with self.assertRaises(OzonReviewReplyError) as unknown:
            send_ozon_review_reply(self.payload())
        self.assertFalse(unknown.exception.definite)

        urlopen.side_effect = None
        urlopen.return_value = _Response({})
        with self.assertRaises(OzonReviewReplyError) as unconfirmed_success:
            send_ozon_review_reply(self.payload())
        self.assertFalse(unconfirmed_success.exception.definite)

    def test_processor_never_requeues_sending_jobs(self) -> None:
        source = inspect.getsource(OzonReviewReplyProcessor)
        self.assertIn("job.state='sending' AND job.locked_until < now()", source)
        self.assertIn("state='unknown'", source)
        sending_recovery = source.split("job.state='sending'", 1)[0][-400:]
        self.assertNotIn("SET state='queued'", sending_recovery)
        self.assertIn("SELLER_OZON_REVIEW_REPLY_ENABLED", inspect.getsource(__import__(
            "domains.ozon_review_replies", fromlist=["ozon_review_reply_enabled"],
        ).ozon_review_reply_enabled))


if __name__ == "__main__":
    unittest.main()
