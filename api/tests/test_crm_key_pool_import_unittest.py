from __future__ import annotations

import unittest
from datetime import datetime, timezone

from scripts.import_crm_key_pools import INSERT_IMPORTED_KEY_SQL, normalized_source_key, seller_key_hash


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


if __name__ == "__main__":
    unittest.main()
