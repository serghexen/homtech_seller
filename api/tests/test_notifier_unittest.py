"""Контракт долговечных multi-tenant Telegram-уведомлений Seller."""

from __future__ import annotations

from io import BytesIO
import inspect
import json
import unittest
import urllib.error
from unittest.mock import patch

import notifier


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class NotifierTests(unittest.TestCase):
    def settings(self, attempts: int = 3) -> notifier.Settings:
        return notifier.Settings(
            database_url="postgresql://seller",
            enabled=True,
            bot_token="secret",
            api_base="https://api.telegram.org",
            poll_interval_seconds=10,
            lease_seconds=90,
            batch_size=20,
            request_attempts=attempts,
        )

    def test_disabled_notifier_does_not_require_bot_token(self) -> None:
        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://seller"}, clear=True):
            settings = notifier.load_settings()
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.bot_token, "")

    def test_enabled_notifier_requires_bot_token(self) -> None:
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": "postgresql://seller", "SELLER_TELEGRAM_NOTIFICATIONS_ENABLED": "true"},
            clear=True,
        ):
            with self.assertRaises(RuntimeError):
                notifier.load_settings()

    def test_manual_alert_identifies_exact_store_and_order(self) -> None:
        text = notifier.notification_text("manual_required", {
            "provider_code": "yandex_market",
            "store_name": "ASAT GAMES",
            "external_order_id": "60940029440",
            "title": "PSN CHF",
            "quantity": 2,
            "status": "manual_required",
        })
        self.assertIn("Требуется оператор", text)
        self.assertIn("Магазин: ASAT GAMES", text)
        self.assertIn("Заказ: 60940029440", text)
        self.assertIn("Количество: 2", text)
        self.assertIn("откройте заказ в Seller", text)

    def test_retry_backoff_is_bounded(self) -> None:
        self.assertEqual(notifier.retry_delay_seconds(1), 15)
        self.assertEqual(notifier.retry_delay_seconds(2), 30)
        self.assertEqual(notifier.retry_delay_seconds(100), 1800)

    def test_telegram_request_retries_temporary_network_failure(self) -> None:
        with patch.object(
            notifier.urllib.request,
            "urlopen",
            side_effect=[urllib.error.URLError("temporary"), FakeResponse({"ok": True, "result": {}})],
        ) as urlopen_mock, patch.object(notifier.time, "sleep") as sleep_mock:
            result = notifier.telegram_request(self.settings(attempts=2), "getMe", {})

        self.assertTrue(result["ok"])
        self.assertEqual(urlopen_mock.call_count, 2)
        sleep_mock.assert_called_once()

    def test_telegram_request_does_not_retry_permanent_http_error(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.telegram.org", 400, "Bad Request", {}, BytesIO(b'{"description":"chat not found"}'),
        )
        with patch.object(notifier.urllib.request, "urlopen", side_effect=error) as urlopen_mock:
            with self.assertRaises(notifier.TelegramPermanentError):
                notifier.telegram_request(self.settings(), "sendMessage", {"chat_id": 1, "text": "test"})
        self.assertEqual(urlopen_mock.call_count, 1)

    def test_commands_accept_group_bot_suffix(self) -> None:
        self.assertEqual(notifier.command_kind("/start@homtech_bot payload"), "subscribe")
        self.assertEqual(notifier.command_kind("/stop@homtech_bot"), "unsubscribe")

    def test_queue_queries_are_workspace_scoped_and_concurrency_safe(self) -> None:
        materialize_source = inspect.getsource(notifier.materialize_deliveries)
        claim_source = inspect.getsource(notifier.claim_delivery)
        failure_source = inspect.getsource(notifier.fail_delivery)

        self.assertIn("recipient.workspace_id=event.workspace_id", materialize_source)
        self.assertIn("ON CONFLICT (event_id, recipient_id) DO NOTHING", materialize_source)
        self.assertIn("FOR UPDATE OF delivery SKIP LOCKED", claim_source)
        self.assertIn("recipient.workspace_id=event.workspace_id", claim_source)
        self.assertIn("state=%s", failure_source)
        self.assertIn("available_at", failure_source)


if __name__ == "__main__":
    unittest.main()
