from __future__ import annotations

import unittest
from contextlib import nullcontext
from decimal import Decimal
from unittest.mock import MagicMock

from scripts.import_crm_ozon_delivery_history import (
    SourceDelivery,
    normalized_codes,
    seller_order_matches_delivery,
    target_context as delivery_target_context,
)
from scripts.import_crm_ozon_snapshot import (
    existing_order_keys,
    guarded_max_amount,
    target_context as snapshot_target_context,
)


class CrmOzonImportTests(unittest.TestCase):
    def test_supplier_price_guard_rounds_up(self):
        self.assertEqual(guarded_max_amount(Decimal("100.01"), Decimal("5")), Decimal("105.02"))

    def test_delivery_codes_are_normalized_without_disclosure(self):
        self.assertEqual(normalized_codes([" AAAA-1111 ", "BBBB-2222"]), ("AAAA-1111", "BBBB-2222"))

    def test_delivery_rejects_duplicate_codes(self):
        with self.assertRaisesRegex(ValueError, "repeats"):
            normalized_codes(["AAAA-1111", "AAAA-1111"])

    def test_delivery_accepts_fbo_product_id_as_seller_offer(self):
        delivery = SourceDelivery(
            source_id=1,
            external_product_id="5649289862",
            posting_number="posting-1",
            item_id="5204478280",
            offer_id="28634",
            required_qty=1,
            codes=("CODE-1",),
            delivery_source="supplier",
            created_at=None,
            delivered_at=None,
            updated_at=None,
        )

        self.assertTrue(seller_order_matches_delivery(("5649289862", 1), delivery))
        self.assertFalse(seller_order_matches_delivery(("another-product", 1), delivery))

    def test_snapshot_target_requires_all_ozon_execution_flags_off(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = ("ASAT", False, False, False, True, False)
        target = MagicMock()
        target.cursor.return_value = nullcontext(cursor)

        with self.assertRaisesRegex(RuntimeError, "execution flags"):
            snapshot_target_context(target, 3)

    def test_snapshot_matches_existing_order_by_posting_and_sku(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [("posting-1", "9911", "offer-current", 2)]
        target = MagicMock()
        target.cursor.return_value = nullcontext(cursor)

        self.assertEqual(
            existing_order_keys(target, 3),
            {("posting-1", "9911"): ("offer-current", 2)},
        )

    def test_delivery_target_accepts_disabled_ozon_store(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = ("ASAT", False, False, False, False, False)
        target = MagicMock()
        target.cursor.return_value = nullcontext(cursor)

        self.assertEqual(delivery_target_context(target, 3), "ASAT")


if __name__ == "__main__":
    unittest.main()
