from __future__ import annotations

import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from unittest.mock import MagicMock

from scripts.import_crm_key_pools import (
    INSERT_IMPORTED_KEY_SQL,
    ensure_target_import_writable,
    normalized_source_key,
    seller_key_hash,
    source_inflight_count,
)


class CrmKeyPoolImportTests(unittest.TestCase):
    def source_row(self, **overrides):
        values = {
            "product_key": "MRKT-TEST",
            "source_key_id": 17,
            "code": "AAAA-BBBB-CCCC",
            "status": "free",
            "expires_at": None,
            "issued_order_ref": "",
            "reserved_at": None,
            "issued_at": None,
            "created_at": datetime(2026, 8, 24, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 8, 24, tzinfo=timezone.utc),
        }
        values.update(overrides)
        return tuple(values.values())

    def test_normalizes_supported_crm_key_without_logging_or_mutating_it(self):
        row = normalized_source_key(self.source_row())
        self.assertEqual(row.product_key, "MRKT-TEST")
        self.assertEqual(row.code, "AAAA-BBBB-CCCC")
        self.assertEqual(row.status, "free")

    def test_rejects_unknown_status_before_target_write(self):
        with self.assertRaisesRegex(ValueError, "unsupported status"):
            normalized_source_key(self.source_row(status="mystery"))

    def test_target_hash_is_stable_and_does_not_contain_plain_key(self):
        first = seller_key_hash("AAAA-BBBB-CCCC")
        self.assertEqual(first, seller_key_hash("AAAA-BBBB-CCCC"))
        self.assertNotIn("AAAA", first)

    def test_import_insert_has_placeholder_for_every_parameter(self):
        self.assertEqual(INSERT_IMPORTED_KEY_SQL.count("%s"), 13)

    def test_apply_guard_rejects_store_after_seller_owns_fulfillment(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (False, False, True, True)
        target = MagicMock()
        target.cursor.return_value = nullcontext(cursor)

        with self.assertRaisesRegex(RuntimeError, "ownership has already started"):
            ensure_target_import_writable(target, connection_id=7)

        self.assertEqual(cursor.execute.call_args.args[1], (7,))

    def test_apply_guard_allows_final_import_before_cutover(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (False, False, False, False)
        target = MagicMock()
        target.cursor.return_value = nullcontext(cursor)

        ensure_target_import_writable(target, connection_id=7)

    def test_inflight_preflight_counts_without_decrypting_keys(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (3,)
        source = MagicMock()
        source.cursor.return_value = nullcontext(cursor)

        count = source_inflight_count(source, marketplace="yandex_market", store_code="joycards")

        self.assertEqual(count, 3)
        sql = cursor.execute.call_args.args[0]
        self.assertNotIn("pgp_sym_decrypt", sql)
        self.assertIn("status IN ('reserved', 'sending')", sql)
        source.commit.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
