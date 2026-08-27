"""Проверки эффективных возможностей тарифов Seller."""

from __future__ import annotations

import unittest

from domains.workspace_entitlements import (
    FULFILLMENT_POOL,
    FULFILLMENT_SUPPLIER,
    SUPPLIER_MAPPING_MANAGE,
    read_workspace_access,
)


class Cursor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.current = []

    def execute(self, _query, _params):
        self.current = self.responses.pop(0)

    def fetchone(self):
        return self.current[0] if self.current else None

    def fetchall(self):
        return list(self.current)


class WorkspaceEntitlementsTests(unittest.TestCase):
    def test_basic_keeps_pool_but_has_no_supplier_access(self) -> None:
        cursor = Cursor([
            [(1, "basic", "Basic", "active", None, None, 3)],
            [("fulfillment.manual",), ("key_pool.manage",), ("fulfillment.pool",)],
            [],
        ])

        access = read_workspace_access(cursor, 4)

        self.assertTrue(access.allows(FULFILLMENT_POOL))
        self.assertFalse(access.allows(SUPPLIER_MAPPING_MANAGE))
        self.assertFalse(access.allows(FULFILLMENT_SUPPLIER))

    def test_active_override_can_grant_supplier_mapping_temporarily(self) -> None:
        cursor = Cursor([
            [(1, "basic", "Basic", "active", None, None, 4)],
            [("fulfillment.pool",)],
            [("supplier_mapping.manage", True)],
        ])

        access = read_workspace_access(cursor, 4)

        self.assertTrue(access.allows(SUPPLIER_MAPPING_MANAGE))

    def test_suspended_subscription_does_not_grant_plan_entitlements(self) -> None:
        cursor = Cursor([
            [(2, "pro", "Pro", "suspended", None, None, 5)],
            [],
        ])

        access = read_workspace_access(cursor, 4)

        self.assertEqual(access.capabilities, frozenset())


if __name__ == "__main__":
    unittest.main()
