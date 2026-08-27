from __future__ import annotations

import unittest
from contextlib import nullcontext
from decimal import Decimal
from unittest.mock import MagicMock

from scripts.import_crm_ozon_delivery_history import normalized_codes, target_context as delivery_target_context
from scripts.import_crm_ozon_snapshot import guarded_max_amount, target_context as snapshot_target_context


class CrmOzonImportTests(unittest.TestCase):
    def test_supplier_price_guard_rounds_up(self):
        self.assertEqual(guarded_max_amount(Decimal("100.01"), Decimal("5")), Decimal("105.02"))

    def test_delivery_codes_are_normalized_without_disclosure(self):
        self.assertEqual(normalized_codes([" AAAA-1111 ", "BBBB-2222"]), ("AAAA-1111", "BBBB-2222"))

    def test_delivery_rejects_duplicate_codes(self):
        with self.assertRaisesRegex(ValueError, "repeats"):
            normalized_codes(["AAAA-1111", "AAAA-1111"])

    def test_snapshot_target_requires_all_ozon_execution_flags_off(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = ("ASAT", False, False, False, True, False)
        target = MagicMock()
        target.cursor.return_value = nullcontext(cursor)

        with self.assertRaisesRegex(RuntimeError, "execution flags"):
            snapshot_target_context(target, 3)

    def test_delivery_target_accepts_disabled_ozon_store(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = ("ASAT", False, False, False, False, False)
        target = MagicMock()
        target.cursor.return_value = nullcontext(cursor)

        self.assertEqual(delivery_target_context(target, 3), "ASAT")


if __name__ == "__main__":
    unittest.main()
