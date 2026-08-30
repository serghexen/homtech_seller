"""Проверки эффективных возможностей тарифа отдельного магазина."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from domains.connection_entitlements import (
    FULFILLMENT_POOL,
    FULFILLMENT_SUPPLIER,
    SUPPLIER_MAPPING_MANAGE,
    read_connection_access,
    read_connection_accesses,
)


class Cursor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.current = []
        self.calls = []

    def execute(self, query, params):
        self.calls.append((query, params))
        self.current = self.responses.pop(0)

    def fetchone(self):
        return self.current[0] if self.current else None

    def fetchall(self):
        return list(self.current)


class ConnectionEntitlementsTests(unittest.TestCase):
    def test_basic_keeps_pool_but_has_no_supplier_access(self) -> None:
        cursor = Cursor([
            [(7, 1, "basic", "Basic", "active", None, None, 3)],
            [(1, "fulfillment.manual"), (1, "key_pool.manage"), (1, "fulfillment.pool")],
            [],
        ])

        access = read_connection_access(cursor, workspace_id=4, connection_id=7)

        self.assertEqual(access.connection_id, 7)
        self.assertTrue(access.allows(FULFILLMENT_POOL))
        self.assertFalse(access.allows(SUPPLIER_MAPPING_MANAGE))
        self.assertFalse(access.allows(FULFILLMENT_SUPPLIER))
        self.assertEqual(cursor.calls[0][1], (4, [7]))

    def test_active_override_can_grant_supplier_mapping_to_one_store(self) -> None:
        cursor = Cursor([
            [(8, 1, "basic", "Basic", "active", None, None, 4)],
            [(1, "fulfillment.pool")],
            [(8, "supplier_mapping.manage", True)],
        ])

        access = read_connection_access(cursor, workspace_id=4, connection_id=8)

        self.assertTrue(access.allows(SUPPLIER_MAPPING_MANAGE))
        self.assertEqual(cursor.calls[-1][1], (4, [8]))

    def test_suspended_subscription_does_not_grant_plan_entitlements(self) -> None:
        cursor = Cursor([
            [(9, 2, "pro", "Pro", "suspended", None, None, 5)],
            [],
        ])

        access = read_connection_access(cursor, workspace_id=4, connection_id=9)

        self.assertEqual(access.capabilities, frozenset())

    def test_missing_or_cross_workspace_store_fails_closed(self) -> None:
        cursor = Cursor([[], []])

        access = read_connection_access(cursor, workspace_id=99, connection_id=7)

        self.assertEqual(access.subscription_status, "inactive")
        self.assertEqual(access.capabilities, frozenset())

    def test_two_stores_in_one_workspace_keep_independent_plans(self) -> None:
        cursor = Cursor([
            [
                (7, 1, "basic", "Basic", "active", None, None, 2),
                (8, 2, "pro", "Pro", "active", None, None, 5),
            ],
            [
                (1, "fulfillment.pool"),
                (2, "fulfillment.pool"),
                (2, "fulfillment.supplier"),
            ],
            [],
        ])

        access = read_connection_accesses(cursor, workspace_id=4, connection_ids=[7, 8])

        self.assertFalse(access[7].allows(FULFILLMENT_SUPPLIER))
        self.assertTrue(access[8].allows(FULFILLMENT_SUPPLIER))
        self.assertEqual(access[7].plan_code, "basic")
        self.assertEqual(access[8].plan_code, "pro")
        self.assertEqual(len(cursor.calls), 3)

    def test_expired_active_subscription_fails_closed(self) -> None:
        cursor = Cursor([
            [(7, 2, "pro", "Pro", "active", datetime(2000, 1, 1, tzinfo=timezone.utc), None, 6)],
            [],
        ])

        access = read_connection_access(cursor, workspace_id=4, connection_id=7)

        self.assertEqual(access.capabilities, frozenset())

    def test_past_due_subscription_keeps_access_during_grace(self) -> None:
        cursor = Cursor([
            [(7, 2, "pro", "Pro", "past_due", None, datetime(2999, 1, 1, tzinfo=timezone.utc), 6)],
            [(2, "fulfillment.supplier")],
            [],
        ])

        access = read_connection_access(cursor, workspace_id=4, connection_id=7)

        self.assertTrue(access.allows(FULFILLMENT_SUPPLIER))


if __name__ == "__main__":
    unittest.main()
