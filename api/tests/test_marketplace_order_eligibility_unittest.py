"""Регрессия допуска заказов к необратимой выдаче."""

from __future__ import annotations

import unittest

from domains.marketplace_order_eligibility import marketplace_order_allows_fulfillment


class MarketplaceOrderEligibilityTests(unittest.TestCase):
    def yandex(self, status: str, *, subtype: str = "EMAIL") -> bool:
        return marketplace_order_allows_fulfillment(
            provider_code="yandex_market",
            normalized_status="processing",
            provider_status=status,
            delivery_type="DIGITAL",
            digital_goods_type=subtype,
        )

    def test_yandex_allows_only_exact_processing(self) -> None:
        self.assertTrue(self.yandex("PROCESSING"))
        for status in ("PLACING", "RESERVED", "UNPAID", "PENDING", "DELIVERY", "DELIVERED", "UNKNOWN", ""):
            with self.subTest(status=status):
                self.assertFalse(self.yandex(status))

    def test_yandex_allows_only_code_delivery_scenarios(self) -> None:
        self.assertTrue(self.yandex("PROCESSING", subtype="ACTIVATION_CODE"))
        for subtype in ("CHAT", "STEAM_GIFT", "UNKNOWN", ""):
            with self.subTest(subtype=subtype):
                self.assertFalse(self.yandex("PROCESSING", subtype=subtype))

    def test_ozon_keeps_existing_normalized_status_contract(self) -> None:
        self.assertTrue(marketplace_order_allows_fulfillment(
            provider_code="ozon", normalized_status="processing", provider_status="awaiting_code",
            delivery_type="DIGITAL",
        ))

    def test_non_digital_and_unknown_provider_fail_closed(self) -> None:
        self.assertFalse(marketplace_order_allows_fulfillment(
            provider_code="yandex_market", normalized_status="processing", provider_status="PROCESSING",
            delivery_type="DELIVERY", digital_goods_type="EMAIL",
        ))
        self.assertFalse(marketplace_order_allows_fulfillment(
            provider_code="future_market", normalized_status="processing", provider_status="PROCESSING",
            delivery_type="DIGITAL", digital_goods_type="EMAIL",
        ))


if __name__ == "__main__":
    unittest.main()
