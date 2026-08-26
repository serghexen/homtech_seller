"""Проверки идемпотентной основы выдачи и атомарного резерва ключей."""

from __future__ import annotations

import unittest
from contextlib import nullcontext
from unittest.mock import patch

from domains.fulfillment_service import (
    automatic_pool_reservation_enabled,
    manual_fulfillment_enabled,
    observe_order_fulfillments,
    prepare_manual_keys,
    prepare_support_message,
    release_pool_keys,
    reserve_pool_keys,
)


class ScriptedCursor:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.current: object = None
        self.executions: list[tuple[str, object]] = []
        self.executemany_calls: list[tuple[str, list[object]]] = []

    def execute(self, sql: str, params=None) -> None:
        self.executions.append((sql, params))
        self.current = self.responses.pop(0) if self.responses else None

    def executemany(self, sql: str, params) -> None:
        self.executemany_calls.append((sql, list(params)))

    def fetchone(self):
        if isinstance(self.current, list):
            return self.current[0] if self.current else None
        return self.current

    def fetchall(self):
        if self.current is None:
            return []
        return self.current if isinstance(self.current, list) else [self.current]


class ScriptedConnection:
    def __init__(self, responses: list[object]) -> None:
        self.scripted_cursor = ScriptedCursor(responses)

    def cursor(self):
        return nullcontext(self.scripted_cursor)


class FulfillmentServiceTests(unittest.TestCase):
    @patch.dict("os.environ", {"SELLER_POOL_RESERVATION_ENABLED": "false"})
    def test_global_reservation_switch_is_off_by_default(self) -> None:
        self.assertFalse(automatic_pool_reservation_enabled())

    @patch.dict("os.environ", {"SELLER_MANUAL_FULFILLMENT_ENABLED": "false"})
    def test_manual_fulfillment_switch_is_independent_and_off(self) -> None:
        self.assertFalse(manual_fulfillment_enabled())

    def test_observes_processing_order_item_idempotently(self) -> None:
        connection = ScriptedConnection([
            [("9", "SKU-1", 2, "processing", "DIGITAL")],
            (71,),
        ])

        fulfillment_ids = observe_order_fulfillments(connection, connection_id=7, external_order_id="123")

        self.assertEqual(fulfillment_ids, [71])
        insert_sql, insert_params = connection.scripted_cursor.executions[1]
        self.assertIn("ON CONFLICT (connection_id, external_order_id, external_item_id)", insert_sql)
        self.assertEqual(insert_params, (7, "123", "9", "SKU-1", 2, "seller:yandex_market:7:123:9"))

    def test_does_not_create_fulfillment_for_physical_order(self) -> None:
        connection = ScriptedConnection([
            [("9", "SKU-1", 1, "processing", "DELIVERY")],
            None,
        ])

        fulfillment_ids = observe_order_fulfillments(connection, connection_id=7, external_order_id="123")

        self.assertEqual(fulfillment_ids, [])
        all_sql = "\n".join(sql for sql, _params in connection.scripted_cursor.executions)
        self.assertNotIn("INSERT INTO seller.order_fulfillments(", all_sql)

    def test_closes_old_physical_fulfillment_and_releases_reservation(self) -> None:
        connection = ScriptedConnection([
            [("9", "SKU-1", 1, "processing", "DELIVERY")],
            (71, "reserved", "order-ref"),
            None,
            None,
            None,
        ])

        fulfillment_ids = observe_order_fulfillments(connection, connection_id=7, external_order_id="123")

        self.assertEqual(fulfillment_ids, [71])
        all_sql = "\n".join(sql for sql, _params in connection.scripted_cursor.executions)
        self.assertIn("state='released'", all_sql)
        self.assertIn("status='closed_external'", all_sql)
        self.assertIn("'closed_external'", all_sql)

    def test_reservation_requires_store_and_product_switches(self) -> None:
        connection = ScriptedConnection([
            (71, 7, "SKU-1", 1, "pending", "order-ref", False, True, 51),
        ])

        result = reserve_pool_keys(connection, fulfillment_id=71)

        self.assertEqual(result.state, "skipped")
        self.assertIn("магазина", result.reason)
        self.assertEqual(len(connection.scripted_cursor.executions), 1)

    def test_reserves_only_a_complete_set_without_reading_codes(self) -> None:
        connection = ScriptedConnection([
            (71, 7, "SKU-1", 2, "pending", "order-ref", True, True, 51),
            [(11,), (12,)],
            [(11,), (12,)],
            None,
            None,
        ])

        result = reserve_pool_keys(connection, fulfillment_id=71)

        self.assertEqual(result.state, "reserved")
        self.assertEqual(result.reserved_key_ids, (11, 12))
        all_sql = "\n".join(sql for sql, _params in connection.scripted_cursor.executions)
        self.assertIn("FOR UPDATE SKIP LOCKED", all_sql)
        self.assertIn("expires_at ASC NULLS LAST", all_sql)
        self.assertIn("key_origin='pool'", all_sql)
        self.assertNotIn("code_ciphertext", all_sql)
        self.assertNotIn("pgp_sym_decrypt", all_sql)
        self.assertEqual(
            connection.scripted_cursor.executemany_calls[0][1],
            [(71, 11, "order-ref"), (71, 12, "order-ref")],
        )

    def test_explicit_operator_action_bypasses_only_automatic_switches(self) -> None:
        connection = ScriptedConnection([
            (71, 7, "SKU-1", 1, "pending", "order-ref", False, False, 51),
            [(11,)],
            [(11,)],
            None,
            None,
        ])

        result = reserve_pool_keys(connection, fulfillment_id=71, require_automatic_gates=False)

        self.assertEqual(result.state, "reserved")
        self.assertEqual(result.reserved_key_ids, (11,))

    def test_incomplete_pool_does_not_reserve_partial_set(self) -> None:
        connection = ScriptedConnection([
            (71, 7, "SKU-1", 2, "pending", "order-ref", True, True, 51),
            [(11,)],
            None,
            None,
        ])

        result = reserve_pool_keys(connection, fulfillment_id=71)

        self.assertEqual(result.state, "manual_required")
        self.assertIn("требуется 2, доступно 1", result.reason)
        all_sql = "\n".join(sql for sql, _params in connection.scripted_cursor.executions)
        self.assertNotIn("UPDATE seller.marketplace_keys", all_sql)
        self.assertEqual(connection.scripted_cursor.executemany_calls, [])

    def test_existing_complete_reservation_is_idempotent(self) -> None:
        connection = ScriptedConnection([
            (71, 7, "SKU-1", 2, "reserved", "order-ref", True, True, 51),
            [(11,), (12,)],
        ])

        result = reserve_pool_keys(connection, fulfillment_id=71)

        self.assertEqual(result.reserved_key_ids, (11, 12))
        self.assertEqual(len(connection.scripted_cursor.executions), 2)
        self.assertEqual(connection.scripted_cursor.executemany_calls, [])

    def test_cancelled_order_releases_only_local_reserved_keys(self) -> None:
        connection = ScriptedConnection([
            [("9", "SKU-1", 1, "cancelled", "DIGITAL")],
            (71, "reserved", "order-ref"),
            None,
            None,
            None,
        ])

        observe_order_fulfillments(connection, connection_id=7, external_order_id="123")

        all_sql = "\n".join(sql for sql, _params in connection.scripted_cursor.executions)
        self.assertIn("state='released'", all_sql)
        self.assertIn("key.status='reserved' AND key.issued_order_ref=%s", all_sql)
        self.assertIn("SET status=%s, cancelled_at=now()", all_sql)

    def test_operator_can_release_only_reserved_local_set(self) -> None:
        connection = ScriptedConnection([
            (71, "reserved", "order-ref"),
            None,
            None,
            None,
        ])

        result = release_pool_keys(connection, fulfillment_id=71)

        self.assertEqual(result.state, "pending")
        all_sql = "\n".join(sql for sql, _params in connection.scripted_cursor.executions)
        self.assertIn("state='released'", all_sql)
        self.assertIn("delivery_source='unassigned'", all_sql)
        self.assertNotIn("pgp_sym_decrypt", all_sql)

    def test_operator_can_encrypt_and_attach_exact_manual_set(self) -> None:
        connection = ScriptedConnection([
            (71, 7, "SKU-1", 1, "manual_required", "order-ref", "123", "9"),
            (51,),
            (11,),
            None,
            None,
        ])

        result = prepare_manual_keys(
            connection, fulfillment_id=71, codes=["AAAA-BBBB"], encryption_secret="secret", user_id=5,
        )

        self.assertEqual(result.state, "reserved")
        self.assertEqual(result.reserved_key_ids, (11,))
        all_sql = "\n".join(sql for sql, _params in connection.scripted_cursor.executions)
        self.assertIn("pgp_sym_encrypt", all_sql)
        self.assertNotIn("pgp_sym_decrypt", all_sql)
        self.assertIn("delivery_source='manual'", all_sql)
        self.assertIn("'order'", all_sql)
        self.assertEqual(connection.scripted_cursor.executemany_calls[0][1], [(71, 11, "order-ref")])

    def test_release_returns_only_pool_keys_to_available_stock(self) -> None:
        connection = ScriptedConnection([
            (71, "reserved", "order-ref"),
            None,
            None,
            None,
        ])

        release_pool_keys(connection, fulfillment_id=71)

        release_sql = next(
            sql for sql, _params in connection.scripted_cursor.executions
            if "UPDATE seller.marketplace_keys AS key" in sql
        )
        self.assertIn("key.key_origin='pool' THEN 'free' ELSE 'disabled'", release_sql)
        self.assertIn("key.key_origin='pool' THEN '' ELSE key.issued_order_ref", release_sql)

    def test_manual_set_must_match_order_quantity(self) -> None:
        connection = ScriptedConnection([
            (71, 7, "SKU-1", 2, "pending", "order-ref", "123", "9"),
        ])

        result = prepare_manual_keys(
            connection, fulfillment_id=71, codes=["ONE"], encryption_secret="secret", user_id=5,
        )

        self.assertEqual(result.state, "skipped")
        self.assertIn("требуется ключей: 2", result.reason)
        self.assertEqual(len(connection.scripted_cursor.executions), 1)

    def test_operator_can_snapshot_support_message_without_key_rows(self) -> None:
        connection = ScriptedConnection([
            (71, "pending"),
            None,
            None,
        ])

        result = prepare_support_message(
            connection, fulfillment_id=71, message="Напишите в поддержку", user_id=5,
        )

        self.assertEqual(result.state, "reserved")
        all_sql = "\n".join(sql for sql, _params in connection.scripted_cursor.executions)
        self.assertIn("delivery_source='support_message'", all_sql)
        self.assertIn("support_message_snapshot", all_sql)
        self.assertNotIn("marketplace_keys", all_sql)

    def test_market_confirmation_consumes_sending_keys(self) -> None:
        connection = ScriptedConnection([
            [("9", "SKU-1", 1, "delivered", "DIGITAL")],
            (71, "submitted", "order-ref"),
            None,
            None,
            None,
        ])

        observe_order_fulfillments(connection, connection_id=7, external_order_id="123")

        all_sql = "\n".join(sql for sql, _params in connection.scripted_cursor.executions)
        self.assertIn("state='consumed'", all_sql)
        self.assertIn("status='delivered'", all_sql)
        self.assertIn("SET status=%s, delivered_at=now()", all_sql)
        self.assertIn("INSERT INTO seller.yandex_stock_outbound_jobs", all_sql)

    def test_external_delivery_releases_unused_local_reservation(self) -> None:
        connection = ScriptedConnection([
            [("9", "SKU-1", 1, "delivered", "DIGITAL")],
            (71, "reserved", "order-ref"),
            None,
            None,
            None,
        ])

        observe_order_fulfillments(connection, connection_id=7, external_order_id="123")

        all_sql = "\n".join(sql for sql, _params in connection.scripted_cursor.executions)
        self.assertIn("state='released'", all_sql)
        self.assertIn("delivery_source=%s", all_sql)


if __name__ == "__main__":
    unittest.main()
