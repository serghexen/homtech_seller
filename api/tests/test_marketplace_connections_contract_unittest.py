"""Контрактные проверки безопасных входных данных подключения магазинов."""

from __future__ import annotations

import unittest
from inspect import getsource

from fastapi import FastAPI
from pydantic import ValidationError

from domains.marketplace_connections_api import (
    MarketplaceConnectionCreateIn,
    MarketplaceConnectionDiscoverIn,
    mount_marketplace_connection_routes,
)


class MarketplaceConnectionsContractTests(unittest.TestCase):
    def test_accepts_supported_ozon_connection_payload(self) -> None:
        # Фиксирует обязательные поля Ozon до обращения к внешнему API и базе данных.
        payload = MarketplaceConnectionCreateIn(
            provider_code="ozon",
            display_name="ASAT",
            client_id="3313715",
            token="test-token-123",
        )
        self.assertEqual(payload.provider_code, "ozon")
        self.assertEqual(payload.client_id, "3313715")

    def test_rejects_unknown_marketplace_provider(self) -> None:
        # Не позволяет создать подключение произвольного сервиса без отдельного адаптера и проверки ключа.
        with self.assertRaises(ValidationError):
            MarketplaceConnectionDiscoverIn(provider_code="other_market", token="test-token-123")

    def test_rejects_too_short_token_before_verification(self) -> None:
        # Отсекает пустой и явно неполный токен без сетевого запроса к маркетплейсу.
        with self.assertRaises(ValidationError):
            MarketplaceConnectionDiscoverIn(provider_code="yandex_market", token="short")

    def test_mounts_reversible_connection_routes(self) -> None:
        # Фиксирует парные операции отключения и повторного включения магазина в HTTP-контракте.
        app = FastAPI()
        mount_marketplace_connection_routes(
            app,
            database_url=lambda: "",
            psycopg=None,
            current_user=lambda: None,
            user_with_workspace=lambda *_args: None,
        )
        paths = {route.path for route in app.routes}
        self.assertIn("/marketplaces/connections/{connection_id}/disable", paths)
        self.assertIn("/marketplaces/connections/{connection_id}/enable", paths)

    def test_connection_creation_claims_external_store_for_one_workspace(self) -> None:
        source = getsource(mount_marketplace_connection_routes)

        self.assertIn("pg_advisory_xact_lock", source)
        self.assertIn("identity_column", source)
        self.assertIn("existing_owner", source)
        self.assertIn("другом аккаунте Seller", source)

    def test_connection_creation_enqueues_both_initial_snapshots(self) -> None:
        source = getsource(mount_marketplace_connection_routes)

        self.assertIn('for sync_kind in ("catalog", "orders")', source)
        self.assertIn("INSERT INTO seller.marketplace_sync_jobs", source)
        self.assertIn("ON CONFLICT DO NOTHING", source)
