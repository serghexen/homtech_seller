from __future__ import annotations

import io
import json
import unittest

from scripts.import_crm_yandex_settings import db_values, prepare_catalog_rows, read_source_rows


class CrmYandexSettingsImportTests(unittest.TestCase):
    def source_payload(self, **overrides):
        payload = {
            "source_store_code": "joycards",
            "offer_id": "MRKT-TEST",
            "manual_stock_limit": 5,
            "published_stock": 4,
            "activation_instruction": "Активируйте код в магазине.",
            "support_message": "Напишите в поддержку.",
            "support_message_delivery_enabled": False,
            "sales_limit": None,
            "sales_limit_daily_extra": 0,
            "sales_limit_day": "2026-08-24",
            "sales_limit_revision": 0,
            "sales_limit_used": 0,
            "sales_limit_reserved": 0,
            "sales_limit_remaining": None,
            "sales_limit_exhausted_at": None,
            "archived_by_sales_limit": False,
            "last_stock_sync_at": "2026-08-24T12:00:00+00:00",
            "source_updated_at": "2026-08-24T12:01:00+00:00",
        }
        payload.update(overrides)
        return payload

    def test_reads_jsonl_without_losing_multiline_instruction(self):
        payload = self.source_payload(activation_instruction="Первая строка\nВторая строка")
        rows = read_source_rows(io.StringIO(json.dumps(payload, ensure_ascii=False) + "\n"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].activation_instruction, "Первая строка\nВторая строка")
        self.assertIsNone(rows[0].sales_limit)
        self.assertIsNone(rows[0].sales_limit_remaining)
        self.assertEqual(rows[0].support_message, "Напишите в поддержку.")
        self.assertFalse(rows[0].support_message_delivery_enabled)

    def test_missing_catalog_offers_are_strict_by_default(self):
        row = read_source_rows(io.StringIO(json.dumps(self.source_payload()) + "\n"))[0]
        with self.assertRaisesRegex(RuntimeError, "1 source offers are missing"):
            prepare_catalog_rows([row], {}, skip_missing=False)

    def test_missing_catalog_offers_can_be_reported_and_skipped(self):
        matched = read_source_rows(io.StringIO(json.dumps(self.source_payload(offer_id="MATCH")) + "\n"))[0]
        missing = read_source_rows(io.StringIO(json.dumps(self.source_payload(offer_id="MISSING")) + "\n"))[0]
        prepared, missing_ids = prepare_catalog_rows(
            [matched, missing],
            {"MATCH": "external-1"},
            skip_missing=True,
        )
        self.assertEqual(prepared, [(matched, "external-1")])
        self.assertEqual(missing_ids, ["MISSING"])

    def test_rejects_duplicate_offer_id_before_database_write(self):
        line = json.dumps(self.source_payload(), ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, "duplicate offer_id"):
            read_source_rows(io.StringIO(f"{line}\n{line}\n"))

    def test_rejects_negative_stock(self):
        payload = self.source_payload(manual_stock_limit=-1)
        with self.assertRaisesRegex(ValueError, "manual_stock_limit must be nonnegative"):
            read_source_rows(io.StringIO(json.dumps(payload) + "\n"))

    def test_reads_boolean_string_without_enabling_false(self):
        payload = self.source_payload(archived_by_sales_limit="false")
        row = read_source_rows(io.StringIO(json.dumps(payload) + "\n"))[0]
        self.assertFalse(row.archived_by_sales_limit)

    def test_db_signature_keeps_external_product_key_separate_from_source_offer(self):
        row = read_source_rows(io.StringIO(json.dumps(self.source_payload()) + "\n"))[0]
        signature = db_values(row, "external-product")
        self.assertEqual(signature[0], "joycards")
        self.assertEqual(signature[1], "external-product")
        self.assertEqual(signature[2], 5)


if __name__ == "__main__":
    unittest.main()
