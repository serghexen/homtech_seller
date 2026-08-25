"""Контрактные тесты безопасного inbox уведомлений Яндекс Маркета."""

from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.yandex_market_webhooks_api import (
    integration_response,
    is_yandex_market_source,
    mount_yandex_market_webhook_routes,
    notification_fingerprint,
    parse_networks,
    source_ip_from_proxy_chain,
)


class FakeCursor:
    def __init__(self, statements: list[tuple[str, tuple | None]], connection_row=None) -> None:
        self.statements = statements
        self.connection_row = connection_row
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, query: str, params: tuple | None = None) -> None:
        self.statements.append((query, params))
        if "SELECT id, workspace_id, status, webhook_processing_enabled" in query:
            self.row = self.connection_row
        else:
            self.row = (17,) if "INSERT INTO seller.yandex_webhook_events" in query else None

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, statements: list[tuple[str, tuple | None]], connection_row=None) -> None:
        self.statements = statements
        self.connection_row = connection_row
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.statements, self.connection_row)

    def commit(self) -> None:
        self.committed = True


class FakePsycopg:
    def __init__(self, connection_row=None) -> None:
        self.statements: list[tuple[str, tuple | None]] = []
        self.connection_row = connection_row

    def connect(self, _database_url: str) -> FakeConnection:
        return FakeConnection(self.statements, self.connection_row)


class YandexMarketWebhookTests(unittest.TestCase):
    def test_accepts_all_official_yandex_networks(self) -> None:
        # Фиксирует опубликованные сети, чтобы инфраструктурный рефакторинг не закрыл Маркету доступ.
        self.assertTrue(is_yandex_market_source("5.45.207.1"))
        self.assertTrue(is_yandex_market_source("141.8.142.127"))
        self.assertTrue(is_yandex_market_source("5.255.253.64"))
        self.assertFalse(is_yandex_market_source("203.0.113.10"))

    def test_proxy_chain_ignores_spoofed_leftmost_address(self) -> None:
        # Берёт ближайший внешний hop справа, поэтому клиент не может подставить сеть Яндекса слева.
        trusted = parse_networks("127.0.0.0/8,172.16.0.0/12")
        source_ip = source_ip_from_proxy_chain(
            "172.18.0.4",
            "5.45.207.1, 203.0.113.20, 127.0.0.1",
            trusted_networks=trusted,
        )
        self.assertEqual(source_ip, "203.0.113.20")
        self.assertFalse(is_yandex_market_source(source_ip))

    def test_proxy_chain_recovers_real_yandex_address(self) -> None:
        trusted = parse_networks("127.0.0.0/8,172.16.0.0/12")
        source_ip = source_ip_from_proxy_chain(
            "172.18.0.4",
            "5.45.207.10, 127.0.0.1",
            trusted_networks=trusted,
        )
        self.assertEqual(source_ip, "5.45.207.10")

    def test_fingerprint_is_stable_for_reordered_json(self) -> None:
        first = {"notificationType": "ORDER_CREATED", "campaignId": 1, "items": [{"id": 2}]}
        second = {"items": [{"id": 2}], "campaignId": 1, "notificationType": "ORDER_CREATED"}
        self.assertEqual(notification_fingerprint(first), notification_fingerprint(second))

    def test_ping_echoes_yandex_time_and_is_saved_as_ignored(self) -> None:
        # Проверяет публичный контракт без авторизации и без запуска бизнес-обработки.
        fake_psycopg = FakePsycopg()
        app = FastAPI()
        mount_yandex_market_webhook_routes(
            app,
            database_url=lambda: "postgresql://test",
            psycopg=fake_psycopg,
            source_ip_resolver=lambda _request: "5.45.207.10",
            processing_enabled=lambda: False,
        )
        ping_time = "2026-08-25T10:09:49.759084017Z"

        response = TestClient(app).post(
            "/marketplaces/yandex/notifications/notification",
            json={"notificationType": "PING", "time": ping_time},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"version": "1.0.0", "name": "HomTech Seller", "time": ping_time})
        insert = next(statement for statement in fake_psycopg.statements if "INSERT INTO seller.yandex_webhook_events" in statement[0])
        self.assertEqual(insert[1][-1], "ignored")
        self.assertFalse(insert[1][-2])
        self.assertIn("ON CONFLICT (event_fingerprint)", insert[0])
        self.assertIn("processing_state='paused'", insert[0])
        self.assertIn("THEN 'received'", insert[0])

    def test_order_event_is_paused_before_cutover(self) -> None:
        fake_psycopg = FakePsycopg()
        app = FastAPI()
        mount_yandex_market_webhook_routes(
            app,
            database_url=lambda: "postgresql://test",
            psycopg=fake_psycopg,
            source_ip_resolver=lambda _request: "141.8.142.25",
            processing_enabled=lambda: False,
        )

        response = TestClient(app).post(
            "/marketplaces/yandex/notifications",
            json={"notificationType": "ORDER_CREATED", "campaignId": 149196813, "orderId": 123},
        )

        self.assertEqual(response.status_code, 200)
        insert = next(statement for statement in fake_psycopg.statements if "INSERT INTO seller.yandex_webhook_events" in statement[0])
        self.assertEqual(insert[1][-1], "paused")

    def test_order_event_requires_global_and_store_switches(self) -> None:
        # Даже глобальное включение не запускает другой магазин без отдельного флага подключения.
        for store_enabled, expected_state in ((False, "paused"), (True, "received")):
            with self.subTest(store_enabled=store_enabled):
                fake_psycopg = FakePsycopg(connection_row=(7, 3, "active", store_enabled))
                app = FastAPI()
                mount_yandex_market_webhook_routes(
                    app,
                    database_url=lambda: "postgresql://test",
                    psycopg=fake_psycopg,
                    source_ip_resolver=lambda _request: "141.8.142.25",
                    processing_enabled=lambda: True,
                )

                response = TestClient(app).post(
                    "/marketplaces/yandex/notifications",
                    json={"notificationType": "ORDER_CREATED", "campaignId": 149196813, "orderId": 123},
                )

                self.assertEqual(response.status_code, 200)
                insert = next(statement for statement in fake_psycopg.statements if "INSERT INTO seller.yandex_webhook_events" in statement[0])
                self.assertEqual(insert[1][-1], expected_state)

    def test_unknown_source_is_rejected_before_database_access(self) -> None:
        fake_psycopg = FakePsycopg()
        app = FastAPI()
        mount_yandex_market_webhook_routes(
            app,
            database_url=lambda: "postgresql://test",
            psycopg=fake_psycopg,
            source_ip_resolver=lambda _request: "203.0.113.10",
        )

        response = TestClient(app).post(
            "/marketplaces/yandex/notifications",
            json={"notificationType": "PING", "time": "2026-08-25T10:00:00Z"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(fake_psycopg.statements, [])

    def test_integration_response_has_current_fallback_time(self) -> None:
        self.assertTrue(integration_response()["time"].endswith("Z"))


if __name__ == "__main__":
    unittest.main()
