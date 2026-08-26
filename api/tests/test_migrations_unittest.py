"""Минимальные проверки новых runtime-миграций без подключения к рабочей базе."""

from __future__ import annotations

import unittest
from pathlib import Path

from scripts.run_migrations import split_sql_statements


class MigrationFilesTests(unittest.TestCase):
    def test_catalog_presence_migration_contains_schema_and_index_steps(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260824_04_catalog_snapshot_presence.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))

        self.assertEqual(len(statements), 2)
        self.assertIn("ADD COLUMN IF NOT EXISTS is_present", statements[0])
        self.assertIn("WHERE is_present = true", statements[1])

    def test_product_settings_migration_keeps_local_edits_separate(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260824_05_product_card_settings.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))

        self.assertEqual(len(statements), 2)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.product_card_settings", statements[0])
        self.assertIn("sales_limit_day date NOT NULL DEFAULT CURRENT_DATE", statements[0])
        self.assertIn("activation_instruction", statements[0])
        self.assertIn("idx_product_card_settings_updated", statements[1])

    def test_key_pool_migration_encrypts_values_and_has_no_delivery_tables(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260824_06_marketplace_key_pools.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))

        joined = "\n".join(statements)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.marketplace_key_pools", joined)
        self.assertIn("code_ciphertext bytea NOT NULL", joined)
        self.assertIn("code_hash text NOT NULL UNIQUE", joined)
        self.assertNotIn("fulfillment", joined.lower())

    def test_catalog_archive_migration_keeps_marketplace_state_separate(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260825_01_catalog_archive.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))

        self.assertEqual(len(statements), 2)
        self.assertIn("ADD COLUMN IF NOT EXISTS is_archived", statements[0])
        self.assertIn("is_present, is_archived", statements[1])

    def test_yandex_webhook_inbox_starts_paused_and_is_idempotent(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260825_02_yandex_webhook_inbox.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))
        joined = "\n".join(statements)

        self.assertEqual(len(statements), 5)
        self.assertIn("ADD COLUMN IF NOT EXISTS webhook_processing_enabled", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.yandex_webhook_events", joined)
        self.assertIn("processing_state text NOT NULL DEFAULT 'paused'", joined)
        self.assertIn("CREATE UNIQUE INDEX CONCURRENTLY", joined)
        self.assertIn("event_fingerprint", joined)

    def test_fulfillment_foundation_starts_with_all_reservation_switches_off(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260825_03_fulfillment_foundation.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))
        joined = "\n".join(statements)

        self.assertEqual(len(statements), 10)
        self.assertIn("fulfillment_reservation_enabled boolean NOT NULL DEFAULT false", joined)
        self.assertIn("pool_issue_enabled boolean NOT NULL DEFAULT false", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.order_fulfillments", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.fulfillment_key_reservations", joined)
        self.assertIn("WHERE state='reserved'", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.fulfillment_events", joined)

    def test_yandex_outbound_migration_starts_disabled_and_has_uncertain_state(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260825_04_yandex_fulfillment_outbox.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))
        joined = "\n".join(statements)

        self.assertEqual(len(statements), 4)
        self.assertIn("fulfillment_outbound_enabled boolean NOT NULL DEFAULT false", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.fulfillment_outbound_jobs", joined)
        self.assertIn("'submitted', 'unknown', 'failed'", joined)
        self.assertIn("fulfillment_id bigint NOT NULL UNIQUE", joined)
        self.assertNotIn("code_ciphertext", joined)

    def test_manual_and_support_migration_keeps_support_snapshot_separate(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260825_05_manual_and_support_fulfillment.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))
        joined = "\n".join(statements)

        self.assertEqual(len(statements), 6)
        self.assertIn("ADD COLUMN IF NOT EXISTS support_message", joined)
        self.assertIn("ADD COLUMN IF NOT EXISTS support_message_snapshot", joined)
        self.assertIn("'manual', 'support_message'", joined)
        self.assertIn("char_length(support_message_snapshot) <= 2000", joined)

    def test_support_import_migration_preserves_source_enablement_and_local_override(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260825_06_support_message_import.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))
        joined = "\n".join(statements)

        self.assertEqual(len(statements), 6)
        self.assertIn("yandex_product_settings_snapshot", joined)
        self.assertIn("support_message_delivery_enabled", joined)
        self.assertIn("support_message_overridden", joined)

    def test_supplier_fulfillment_migration_is_off_by_default_and_durable(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260826_01_supplier_fulfillment.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("supplier_fulfillment_enabled boolean NOT NULL DEFAULT false", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.product_fulfillment_policies", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.product_supplier_mappings", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.supplier_purchase_attempts", joined)
        self.assertIn("idempotency_key text NOT NULL UNIQUE", joined)
        self.assertIn("'requires_attention'", joined)

    def test_yandex_stock_outbox_is_disabled_and_durable(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260826_02_yandex_stock_outbox.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("stock_outbound_enabled boolean NOT NULL DEFAULT false", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.yandex_stock_outbound_jobs", joined)
        self.assertIn("fulfillment_id bigint NOT NULL UNIQUE", joined)
        self.assertIn("'queued','preparing','sending','succeeded','failed'", joined)
        self.assertIn("SELECT DISTINCT ON (connection_id, offer_id)", joined)
        self.assertIn("status IN ('submitted','delivered')", joined)

    def test_legacy_buyer_text_migration_repairs_both_setting_sources(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260826_03_normalize_buyer_text.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("UPDATE seller.yandex_product_settings_snapshot", joined)
        self.assertIn("UPDATE seller.product_card_settings", joined)
        self.assertIn("activation_instruction", joined)
        self.assertIn(r"E'\\n'", joined)
        self.assertNotIn(r"E'\\\\n'", joined)


if __name__ == "__main__":
    unittest.main()
