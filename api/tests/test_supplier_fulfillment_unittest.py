"""Проверки приоритета выдачи и защиты от двойной покупки."""

from __future__ import annotations

import os
import unittest
from decimal import Decimal
from unittest.mock import Mock, patch

from domains.supplier_fulfillment import FulfillmentContext, SupplierFulfillmentProcessor
from domains.supplier_hub_client import SupplierHubSettings


def context(**overrides) -> FulfillmentContext:
    values = dict(
        fulfillment_id=81, connection_id=7, external_order_id="123", external_item_id="9",
        offer_id="SKU-1", quantity=1, status="pending", reservation_ref="7:123:9",
        order_status="processing", delivery_type="DIGITAL", provider_code="yandex_market",
        activation_instruction="Активируйте код", store_local_enabled=True,
        store_supplier_enabled=True, supplier_issue_enabled=True, pool_issue_enabled=True,
        support_issue_enabled=True, support_message="Напишите в поддержку", mapping_id=11,
        service_id=11125, nominal_id="250", params={}, max_amount=Decimal("500"),
        workspace_id=4, supplier_access_enabled=True,
    )
    values.update(overrides)
    return FulfillmentContext(**values)


class Processor(SupplierFulfillmentProcessor):
    def __init__(self, result="blocked", **context_overrides):
        self._database_url = lambda: "test"
        self._psycopg = Mock()
        self.result = result
        self.queued = []
        self.manual = []
        self.reset = []
        self.context_overrides = context_overrides

    def _context(self, _fulfillment_id):
        return context(**self.context_overrides)

    def _resolve_supplier(self, _context):
        return self.result

    def _attempt_rows(self, _fulfillment_id):
        return []

    def _queue_outbound(self, fulfillment_id):
        self.queued.append(fulfillment_id)

    def _mark_manual(self, fulfillment_id, reason):
        self.manual.append((fulfillment_id, reason))

    def _reset_for_fallback(self, fulfillment_id):
        self.reset.append(fulfillment_id)


@patch.dict(
    os.environ,
    {"SELLER_YANDEX_OUTBOUND_ENABLED": "true", "SELLER_OZON_OUTBOUND_ENABLED": "true"},
    clear=False,
)
class SupplierFulfillmentTests(unittest.TestCase):
    @patch("domains.supplier_fulfillment.load_supplier_hub_settings")
    def test_uncertain_supplier_state_never_falls_through_to_pool(self, load_settings) -> None:
        load_settings.return_value = SupplierHubSettings("http://127.0.0.1", "seller", "s" * 48, 5, True)
        processor = Processor(result="blocked")

        processor._resolve(81)

        self.assertEqual(processor.reset, [])
        self.assertEqual(processor.queued, [])
        self.assertEqual(processor.manual, [])

    def test_all_saved_supplier_results_are_reserved_even_if_attempt_was_blocking(self) -> None:
        processor = Processor()
        processor._ensure_attempts = Mock()
        processor._client_factory = Mock()
        processor._attempt_rows = Mock(return_value=[
            (1, 1, "seller:test", "request", "purchase", "succeeded", Decimal("500"), True, True, 91, 11125, "250", {}),
        ])
        processor._reserve_supplier_results = Mock(return_value=True)

        result = SupplierFulfillmentProcessor._resolve_supplier(processor, context())

        self.assertEqual(result, "reserved")
        processor._reserve_supplier_results.assert_called_once_with(context(), [91])

    def test_plan_is_rechecked_immediately_before_new_supplier_purchase(self) -> None:
        processor = Processor()
        client = Mock()
        processor._ensure_attempts = Mock()
        processor._client_factory = Mock(return_value=client)
        processor._supplier_access_enabled = Mock(return_value=False)
        processor._mark_attempt_failed = Mock()
        processor._attempt_rows = Mock(side_effect=[
            [(1, 1, "seller:test", "", "", "created", Decimal("500"), False, False, None, 11125, "250", {})],
            [(1, 1, "seller:test", "", "", "failed", Decimal("500"), False, False, None, 11125, "250", {})],
        ])

        result = SupplierFulfillmentProcessor._resolve_supplier(processor, context())

        self.assertEqual(result, "failed")
        client.create_purchase.assert_not_called()
        processor._mark_attempt_failed.assert_called_once()

    def test_yandex_instruction_is_required_before_supplier_purchase(self) -> None:
        processor = Processor(result="reserved", activation_instruction="")
        processor._resolve_supplier = Mock(return_value="reserved")

        processor._resolve(81)

        processor._resolve_supplier.assert_not_called()
        self.assertEqual(processor.queued, [])
        self.assertEqual(processor.manual, [(81, "Не заполнена инструкция покупателю для Яндекс Маркета")])

    def test_basic_skips_supplier_and_keeps_non_supplier_fallbacks_available(self) -> None:
        processor = Processor(
            supplier_access_enabled=False,
            pool_issue_enabled=False,
            support_issue_enabled=False,
        )
        processor._resolve_supplier = Mock(return_value="blocked")

        processor._resolve(81)

        processor._resolve_supplier.assert_not_called()
        self.assertEqual(processor.manual, [(81, "Автоматические способы не подготовили полный комплект")])

    @patch("domains.supplier_fulfillment.load_supplier_hub_settings")
    def test_ozon_does_not_require_activation_instruction(self, load_settings) -> None:
        load_settings.return_value = SupplierHubSettings("http://127.0.0.1", "seller", "s" * 48, 5, True)
        processor = Processor(result="reserved", provider_code="ozon", activation_instruction="")

        processor._resolve(81)

        self.assertEqual(processor.queued, [81])
        self.assertEqual(processor.manual, [])

    @patch("domains.supplier_fulfillment.ozon_outbound_enabled", return_value=False)
    def test_ozon_never_purchases_when_outbound_is_disabled(self, _outbound_enabled) -> None:
        processor = Processor(result="reserved", provider_code="ozon", activation_instruction="")
        processor._resolve_supplier = Mock(return_value="reserved")

        processor._resolve(81)

        processor._resolve_supplier.assert_not_called()
        self.assertEqual(processor.queued, [])
        self.assertEqual(processor.manual, [(81, "Внешняя отправка магазина выключена")])

    @patch.dict(os.environ, {"SELLER_FULFILLMENT_RESOLVER_ENABLED": "false"}, clear=False)
    def test_global_resolver_switch_prevents_database_claim(self) -> None:
        processor = Processor()
        self.assertEqual(processor.process_pending(), 0)
        processor._psycopg.connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
