"""Проверки приоритета источников публикуемого остатка."""

from __future__ import annotations

import unittest

from domains.stock_target_policy import stock_target_base, stock_target_source


class StockTargetPolicyTests(unittest.TestCase):
    def test_pool_free_count_controls_stock_when_supplier_is_disabled(self) -> None:
        self.assertEqual(
            stock_target_base(
                manual_stock=9,
                supplier_issue_enabled=False,
                pool_issue_enabled=True,
                pool_free_count=3,
            ),
            3,
        )
        self.assertEqual(
            stock_target_source(supplier_issue_enabled=False, pool_issue_enabled=True),
            "pool",
        )

    def test_supplier_priority_keeps_manual_stock_when_both_methods_are_enabled(self) -> None:
        self.assertEqual(
            stock_target_base(
                manual_stock=9,
                supplier_issue_enabled=True,
                pool_issue_enabled=True,
                pool_free_count=3,
            ),
            9,
        )

    def test_manual_stock_is_fallback_when_pool_is_disabled(self) -> None:
        self.assertEqual(
            stock_target_base(
                manual_stock=4,
                supplier_issue_enabled=False,
                pool_issue_enabled=False,
                pool_free_count=12,
            ),
            4,
        )

    def test_empty_pool_publishes_zero(self) -> None:
        self.assertEqual(
            stock_target_base(
                manual_stock=None,
                supplier_issue_enabled=False,
                pool_issue_enabled=True,
                pool_free_count=0,
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
