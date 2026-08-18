"""Контрактные проверки безопасных входных данных подключения магазинов."""

from __future__ import annotations

import unittest

from pydantic import ValidationError

from domains.marketplace_connections_api import MarketplaceConnectionCreateIn, MarketplaceConnectionDiscoverIn


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
